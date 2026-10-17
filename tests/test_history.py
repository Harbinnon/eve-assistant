import json

import pytest
from fastapi.testclient import TestClient

from eve_assistant import history, planner, sessions, snapshots, store
from eve_assistant.web import create_app

CHAR, ORE = 7, 17470


def _plan(db) -> int:
    plan = planner.Plan(
        character_id=CHAR, minutes_available=60, items=(), alternatives=(), notes=(), net_worth=0.0
    )
    plan_id = planner.record(db, plan, {}).plan_id
    steps = [
        ("sell_hangar", "Sell <Veldspar>", 3_000_000, {
            "inputs": {"type_id": ORE, "quantity": 300_000},
        }),
        ("mining", "Mine Veldspar", 400_000, {
            "inputs": {"type_id": ORE, "quantity": 40_000},
            "reasons": [{"code": "mining_estimate", "m3_per_minute": 400}],
        }),
        ("courier", "Haul a contract", 1_000_000, {}),
        ("skill", "Queue skills", 0, {}),
    ]  # fmt: skip
    for position, (kind, title, isk, details) in enumerate(steps):
        db.execute(
            "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, "
            "risk_rating, expected_loss, details) VALUES (?, ?, ?, 'k', ?, ?, 10, 'green', 0, ?)",
            (plan_id, position, kind, title, isk, json.dumps(details)),
        )
    db.commit()
    return plan_id


def _outcome(db, session_id, **changes):
    details = {
        "isk": {"net": 2_500_000, "contracts": 0.0},
        "sold": [{"type_id": ORE, "name": "Veldspar II-Grade", "units": 290_000, "isk": 2_700_000}],
        "mined": [{"type_id": ORE, "units": 30_000, "m3": 3000, "value": 300_000}],
        "mined_value": 300_000,
        "log_mining": None,
        "plan": None,
        "data": {"journal_settled": True, "ledger_settled": True},
        "notes": [],
    }
    details.update(changes)
    db.execute(
        "INSERT INTO session_outcomes (session_id, computed_at, settled, content_hash, details) "
        "VALUES (?, 'x', 1, 'h', ?)",
        (session_id, json.dumps(details)),
    )
    db.commit()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "h.sqlite3"
    conn = store.connect(path)
    snapshots.ensure_character(conn, CHAR, "Miner")
    conn.close()
    return path


@pytest.fixture
def db(db_path):
    conn = store.connect(db_path)
    yield conn
    conn.close()


def _played(db, plan_id, hour=10):
    session = sessions.start(db, CHAR, plan_id, now=f"2026-10-17T{hour:02d}:00:00+00:00")
    return sessions.finish(db, session.session_id, now=f"2026-10-17T{hour:02d}:55:00+00:00")


def test_each_step_is_compared_with_what_happened(db):
    session = _played(db, _plan(db))
    _outcome(db, session.session_id)
    (past,) = history.sessions_history(db)
    sell, mine, courier, skill = past.steps
    assert (sell.verdict, sell.actual_units, sell.actual_isk) == ("seen", 290_000, 2_700_000)
    assert sell.isk_share == pytest.approx(0.9)
    assert (mine.verdict, mine.source, mine.actual_units) == ("seen", "ledger", 30_000)
    assert mine.predicted_units == 40_000
    assert mine.rate_share is None  # no game logs: no speed
    assert courier.verdict == "unknown"  # rewards may arrive later
    assert skill.verdict == "not_measured"


def test_game_logs_give_the_mining_speed(db):
    session = _played(db, _plan(db))
    logs = {"ores": [{"type_id": ORE, "units": 35_000}], "m3_per_active_minute": 300}
    _outcome(db, session.session_id, log_mining=logs)
    mine = history.sessions_history(db)[0].steps[1]
    assert (mine.source, mine.actual_units) == ("logs", 35_000)
    assert mine.rate_share == pytest.approx(0.75)


def test_missing_and_late_data(db):
    plan_id = _plan(db)
    first = _played(db, plan_id, hour=10)
    _outcome(
        db,
        first.session_id,
        sold=[],
        mined=None,
        isk={"net": 0, "contracts": 1_200_000},
        data={"journal_settled": False, "ledger_settled": False},
    )
    sell, mine, courier, _skill = history.sessions_history(db)[0].steps
    assert sell.verdict == "waiting"
    assert mine.verdict == "unknown"
    assert (courier.verdict, courier.actual_isk) == ("seen", 1_200_000)

    second = _played(db, plan_id, hour=12)  # no outcome worked out yet
    newest = history.sessions_history(db)[0]
    assert newest.session.session_id == second.session_id
    assert {s.verdict for s in newest.steps} == {"waiting", "not_measured"}


def test_history_page(db_path, db):
    client = TestClient(create_app(db_path), base_url="http://127.0.0.1")
    assert "No finished sessions yet" in client.get("/history").text
    session = _played(db, _plan(db))
    _outcome(db, session.session_id)
    page = client.get("/history").text
    assert "Sell &lt;Veldspar&gt;" in page  # escaped
    assert "290,000 units (trades)" in page
    assert "90%" in page
    assert 'aria-current="page">History' in page
