"""Sale-price and travel-time corrections (roadmap 3.8): what plans assumed,
what sessions show, and the factors learned from the difference."""

import json
from datetime import datetime, timedelta

import pytest

from eve_assistant import corrections, explain, planner, sessions, snapshots, store, travel

CHAR = 7
START = datetime.fromisoformat("2026-10-17T10:00:00+00:00")
LINE = [30000001 + n for n in range(10)]  # a straight line of systems, one jump apart


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, CHAR, "Miner")
    for a, b in zip(LINE, LINE[1:], strict=False):
        conn.executemany("INSERT INTO sde_jumps VALUES (?, ?)", [(a, b), (b, a)])
    yield conn
    conn.close()


def _seen(db, minute: float, system: int | None, outcome="unchanged"):
    """A location check `minute` minutes after START; a snapshot when it moved."""
    at = (START + timedelta(minutes=minute)).isoformat(timespec="seconds")
    if system is not None:
        db.execute(
            "INSERT INTO snapshots (character_id, kind, taken_at, content_hash, payload) "
            "VALUES (?, 'location', ?, ?, ?)",
            (CHAR, at, f"h{minute}", json.dumps({"solar_system_id": system})),
        )
    db.execute(
        "INSERT INTO snapshot_checks (character_id, kind, checked_at, outcome) "
        "VALUES (?, 'location', ?, ?)",
        (CHAR, at, outcome),
    )


def _travel(db, end_minute=60):
    end = (START + timedelta(minutes=end_minute)).isoformat(timespec="seconds")
    return travel.session_travel(db, CHAR, START.isoformat(timespec="seconds"), end)


def test_a_stretch_is_timed_midpoint_to_midpoint(db):
    _seen(db, 0, LINE[0], "changed")
    _seen(db, 2, None)  # still in the first system
    _seen(db, 4, LINE[2], "changed")  # two jumps on
    _seen(db, 6, LINE[5], "changed")  # three more
    _seen(db, 8, None)  # stopped there
    # left between 2 and 4, arrived between 4 and 6: about 2 minutes for 5 jumps
    assert _travel(db) == {"jumps": 5, "minutes": 2.0, "stretches": 1}


def test_short_gappy_or_routeless_stretches_are_left_out(db):
    _seen(db, 0, LINE[0], "changed")
    _seen(db, 2, LINE[3], "changed")  # seen by two checks only
    _seen(db, 4, None)
    _seen(db, 6, LINE[4], "changed")
    _seen(db, 30, LINE[6], "changed")  # 24 minutes between checks
    _seen(db, 32, 99999999, "changed")  # no route to it
    _seen(db, 34, None)
    _seen(db, 36, None, "failed")
    assert _travel(db) is None


def _session(db, outcome: dict, steps: list[dict], hour: int) -> None:
    plan = planner.Plan(
        character_id=CHAR, minutes_available=60, items=(), alternatives=(), notes=(), net_worth=0.0
    )
    plan_id = planner.record(db, plan, {}).plan_id
    items = []
    for n, details in enumerate(steps):
        item_id = db.execute(
            "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, "
            "risk_rating, expected_loss, details) VALUES (?, ?, 'sell_hangar', ?, 'Sell', 1, 10, "
            "'green', 0, ?)",
            (plan_id, n, f"k{n}", json.dumps(details)),
        ).lastrowid
        items.append({"item_id": item_id, "kind": "sell_hangar", "response": None})
    session = sessions.start(db, CHAR, plan_id, now=f"2026-10-17T{hour:02d}:00:00+00:00")
    sessions.finish(db, session.session_id, now=f"2026-10-17T{hour:02d}:59:00+00:00")
    db.execute(
        "INSERT INTO session_outcomes (session_id, computed_at, settled, content_hash, details) "
        "VALUES (?, 'x', 1, 'h', ?)",
        (session.session_id, json.dumps({"plan": {"items": items}, **outcome})),
    )


def test_sales_are_checked_against_the_prices_the_plan_expected(db):
    priced = {"inputs": {"unit_prices": {"1230": 20.0, "34": 5.0}}}
    sold = [
        {"type_id": 1230, "units": 1000, "isk": 18_000.0},  # 18 a unit against 20
        {"type_id": 34, "units": 2000, "isk": 10_000.0},  # as expected
        {"type_id": 35, "units": 50, "isk": 999.0},  # not priced by the plan: left out
    ]
    _session(db, {"sold": sold}, [priced], hour=1)
    _session(db, {"sold": []}, [priced], hour=2)  # nothing sold: nothing to learn
    (seen,) = corrections.sale_observations(db, CHAR)
    assert (seen.predicted, seen.actual) == (30_000.0, 28_000.0)
    learned = corrections.correction(db, CHAR, "sale_price")
    assert learned.sessions == 1
    assert learned.factor == pytest.approx(1 + (28 / 30 - 1) * 1 / 4)
    assert "Sale prices: adjusted" in explain.describe(
        "learned_correction", learned.reason().detail
    )


def test_travel_is_checked_against_the_minutes_a_jump_assumed(db):
    assumed = {"reasons": [{"code": "assumed_minutes_per_jump", "minutes": 1.0, "base": 1.0}]}
    _session(db, {"travel": {"jumps": 10, "minutes": 15.0, "stretches": 2}}, [assumed], hour=1)
    _session(db, {"travel": {"jumps": 2, "minutes": 9.0, "stretches": 1}}, [assumed], hour=2)
    _session(db, {"travel": {"jumps": 6, "minutes": 9.0, "stretches": 1}}, [], hour=3)
    assert [o.ratio for o in corrections.travel_observations(db, CHAR)] == [1.5, 1.5]
    used, reason = corrections.minutes_per_jump(db, CHAR)
    assert used == pytest.approx(1 + 0.5 * 2 / 5)
    assert reason.detail["sessions"] == 2 and reason.detail["base"] == 1.0
    assert "learned from 2 of your sessions" in explain.describe(reason.code, reason.detail)


def test_before_any_sessions_plans_say_what_they_assume(db):
    used, reason = corrections.minutes_per_jump(db, CHAR)
    assert used == travel.MINUTES_PER_JUMP
    assert "until the tool learns your pace" in explain.describe(reason.code, reason.detail)
    for what, words in (("sale_price", "market sales"), ("minutes_per_jump", "your travel")):
        text = explain.describe("not_learned_yet", {"what": what})
        assert words in text and "published figure" not in text
