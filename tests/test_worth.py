"""Net worth over time (roadmap 3.7): wallet plus valued assets, a point a day."""

import json

import pytest

from eve_assistant import risk, snapshots, store, worth
from eve_assistant.web import charts

ORE, HULL = 1230, 32880


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, 7, "Miner")
    snapshots.ensure_character(conn, 8, "Hauler")
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, NULL, 'x')", [(ORE, 10.0), (HULL, 1_000_000.0)]
    )
    conn.commit()
    yield conn
    conn.close()


def snap(db, character_id, kind, payload, at):
    """A snapshot taken at a given time (record() uses the clock)."""
    db.execute(
        "INSERT INTO snapshots (character_id, kind, taken_at, content_hash, payload) "
        "VALUES (?, ?, ?, 'h', json(?))",
        (character_id, kind, at, json.dumps(payload)),
    )


def test_a_point_a_day_from_wallet_and_assets(db):
    snap(db, 7, "wallet", 5_000_000.0, "2026-10-15T08:00:00+00:00")
    snap(db, 7, "assets", [{"type_id": HULL, "quantity": 1}], "2026-10-15T08:00:01+00:00")
    snap(db, 7, "wallet", 6_000_000.0, "2026-10-15T20:00:00+00:00")  # the day's last counts
    snap(db, 7, "assets", [{"type_id": HULL, "quantity": 1}, {"type_id": ORE, "quantity": 50_000}],
         "2026-10-16T09:00:00+00:00")  # fmt: skip
    points = worth.history(db, [7], now="2026-10-17T12:00:00+00:00")
    assert [(p.at[:10], p.wallet, p.assets) for p in points] == [
        ("2026-10-15", 6_000_000.0, 1_000_000.0),
        ("2026-10-16", 6_000_000.0, 1_500_000.0),
    ]
    assert points[-1].total == 7_500_000.0


def test_older_snapshots_carry_into_the_window(db):
    snap(db, 7, "wallet", 1_000_000.0, "2026-01-01T00:00:00+00:00")
    snap(db, 7, "wallet", 2_000_000.0, "2026-10-16T00:00:00+00:00")
    points = worth.history(db, [7], days=30, now="2026-10-17T00:00:00+00:00")
    assert [(p.at, p.total) for p in points] == [
        ("2026-09-17T00:00:00+00:00", 1_000_000.0),  # the state as the window opens
        ("2026-10-16T00:00:00+00:00", 2_000_000.0),
    ]


def test_an_unchanged_worth_is_carried_to_the_latest_check(db):
    snap(db, 7, "wallet", 5_000_000.0, "2026-10-16T08:00:00+00:00")
    for at, outcome in (("2026-10-17T09:00:00+00:00", "unchanged"),
                        ("2026-10-17T10:00:00+00:00", "failed")):  # fmt: skip
        db.execute(
            "INSERT INTO snapshot_checks (character_id, kind, checked_at, outcome) "
            "VALUES (7, 'wallet', ?, ?)",
            (at, outcome),
        )
    points = worth.history(db, iter([7]), now="2026-10-17T12:00:00+00:00")
    assert [(p.at, p.total) for p in points] == [
        ("2026-10-16T08:00:00+00:00", 5_000_000.0),
        ("2026-10-17T09:00:00+00:00", 5_000_000.0),  # the same worth, checked a day later
    ]


def test_several_characters_add_up_and_copies_count_as_nothing(db):
    snap(db, 7, "wallet", 1_000_000.0, "2026-10-16T00:00:00+00:00")
    snap(db, 8, "wallet", 3_000_000.0, "2026-10-16T01:00:00+00:00")
    snap(db, 8, "assets", [{"type_id": HULL, "quantity": 1, "is_blueprint_copy": True}],
         "2026-10-16T02:00:00+00:00")  # fmt: skip
    (point,) = worth.history(db, worth.all_characters(db), now="2026-10-17T00:00:00+00:00")
    assert (point.wallet, point.assets) == (4_000_000.0, 0.0)
    assert worth.history(db, [], now="2026-10-17T00:00:00+00:00") == []


def test_risk_uses_the_same_valuation(db):
    snapshots.record(db, 7, "wallet", 1_000_000.0)
    copy = {"type_id": HULL, "quantity": 1, "is_blueprint_copy": True}
    snapshots.record(db, 7, "assets", [{"type_id": HULL, "quantity": 2}, copy])
    assert risk.net_worth(db, 7) == 3_000_000.0


def test_the_line_is_spaced_by_time_and_scaled_to_the_box():
    assert charts.line([("2026-10-16T00:00:00+00:00", 1.0)]) is None
    line = charts.line(
        [
            ("2026-10-15T00:00:00+00:00", 100.0),
            ("2026-10-16T00:00:00+00:00", 300.0),
            ("2026-10-19T00:00:00+00:00", 200.0),
        ],
        width=104,
        height=24,
    )
    assert line.points == "4.0,20.0 28.0,4.0 100.0,12.0"  # a day is 1/4 of the way across
    assert (line.change, line.low, line.high, line.since) == (100.0, 100.0, 300.0, "2026-10-15")
    flat = charts.line([("2026-10-15T00:00:00+00:00", 5.0), ("2026-10-16T00:00:00+00:00", 5.0)])
    assert flat.points == "4.0,60.0 596.0,60.0"
