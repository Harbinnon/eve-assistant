"""Lowsec planning with live signals and the moving risk lines (roadmap 4.3)."""

import json
from datetime import UTC, datetime

import pytest

from eve_assistant import planner, risk, routes, sessions, signals, snapshots, store

A, B, C, D, E = (31000001 + n for n in range(5))


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, 7, "Miner")
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, 1, 1, 0.3, 'C')",
        [(s, f"System {n}") for n, s in enumerate((A, B, C, D, E))],
    )
    # A-B-D is the short way (2 jumps); A-C-E-D the long one (3 jumps).
    for x, y in ((A, B), (B, D), (A, C), (C, E), (E, D)):
        conn.executemany("INSERT INTO sde_jumps VALUES (?, ?)", [(x, y), (y, x)])
    yield conn
    conn.close()


def _kills(db, *systems, n=12):  # lowsec: camped from 10
    for system in systems:
        db.execute("INSERT INTO system_kills VALUES (?, ?, 0, 0, 'x')", (system, n))
    signals._mark(db, signals.SETTING, datetime.now(UTC))


def test_routes_go_around_a_camp_when_the_detour_is_short(db):
    assert routes.jumps_between(db, A, D) == [A, B, D]
    _kills(db, B)
    assert routes.jumps_between(db, A, D) == [A, C, E, D]  # one jump more beats a camp
    assert routes.jump_count(db, A, D, avoid_camps=False) == 2  # timing past travel
    _kills(db, C)
    assert routes.jumps_between(db, A, D) == [A, B, D]  # a camp either way: the short one


def test_a_long_detour_isnt_worth_it(db, monkeypatch):
    monkeypatch.setattr(routes, "HOTSPOT_JUMPS", 0.5)
    _kills(db, B)
    assert routes.jumps_between(db, A, D) == [A, B, D]


def test_the_ends_are_never_avoided_and_stale_camps_are_ignored(db):
    _kills(db, A, D)
    assert routes.jumps_between(db, A, D) == [A, B, D]
    _kills(db, B, n=9)  # busy, not camped
    assert routes.jumps_between(db, A, D) == [A, B, D]
    db.execute("UPDATE settings SET value = '2020-01-01T00:00:00+00:00' WHERE key = ?",
               (signals.SETTING,))  # fmt: skip
    db.execute("UPDATE system_kills SET ship_kills = 9")
    assert signals.hotspots(db) == {}


def _step(db, plan_id, n, rating, response=None, exposure=1e6):
    item_id = db.execute(
        "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, risk_rating, "
        "expected_loss, details) VALUES (?, ?, 'courier', ?, 'x', 1, 10, ?, 0, ?)",
        (plan_id, n, f"k{n}", rating, json.dumps({"risk": {"band": "low", "exposure": exposure}})),
    ).lastrowid
    if response:
        planner.respond(db, item_id, response)


def _plan(db, played=False, hour=1):
    plan = planner.Plan(
        character_id=7, minutes_available=60, items=(), alternatives=(), notes=(), net_worth=0.0
    )
    plan_id = planner.record(db, plan, {}).plan_id
    if played:
        session = sessions.start(db, 7, plan_id, now=f"2026-10-17T{hour:02d}:00:00+00:00")
        sessions.finish(db, session.session_id, now=f"2026-10-17T{hour:02d}:30:00+00:00")
    return plan_id


def test_caution_and_risky_steps_taken_or_declined_are_votes(db):
    played = _plan(db, played=True, hour=1)
    _step(db, played, 0, "amber")  # unanswered, but played: taken
    _step(db, played, 1, "red", "do")  # taken
    _step(db, played, 2, "green", "do")  # Safe: not a vote
    _step(db, played, 3, "amber", "do", exposure=0.0)  # nothing at stake: not a vote
    unplayed = _plan(db)
    _step(db, unplayed, 0, "amber")  # never played or answered: no vote
    _step(db, unplayed, 1, "amber", "not_now")  # declined
    assert risk.votes(db) == (2, 1)
    learned = risk.model(db)
    scale = 2 ** ((2 - 1) / (2 + 1 + 5))
    assert learned.columns == pytest.approx(tuple(c * scale for c in risk.SHARE_COLUMNS))
    assert learned.lines_moved == pytest.approx(scale)


def test_the_lines_stay_between_half_and_twice():
    assert risk.lines(0, 0) == risk.SHARE_COLUMNS
    assert risk.lines(1000, 0)[0] == pytest.approx(0.04, rel=0.01)
    assert risk.lines(0, 1000)[0] == pytest.approx(0.01, rel=0.01)
    wider = risk.Model(columns=risk.lines(20, 0))
    assert risk.rate("very_low", 3.0, 100.0).rating == "green"  # 3%: second column
    assert risk.rate("low", 3.0, 100.0).rating == "amber"
    assert risk.rate("low", 3.0, 100.0, model=wider).rating == "green"  # the lines moved past it
