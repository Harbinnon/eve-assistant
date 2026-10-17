import json

import pytest

from eve_assistant import corrections, explain, planner, sessions, snapshots, store

CHAR = 7


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, CHAR, "Miner")
    yield conn
    conn.close()


def _session(db, predicted, actual, active=30.0, response="do", start_hour=10) -> int:
    plan = planner.Plan(
        character_id=CHAR, minutes_available=60, items=(), alternatives=(), notes=(), net_worth=0.0
    )
    plan_id = planner.record(db, plan, {}).plan_id
    details = {"reasons": [{"code": "mining_estimate", "m3_per_minute": predicted}]}
    item_id = db.execute(
        "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, risk_rating, "
        "expected_loss, details) VALUES (?, 0, 'mining', 'k', 'Mine', 1, 50, 'green', 0, ?)",
        (plan_id, json.dumps(details)),
    ).lastrowid
    if response:
        planner.respond(db, item_id, response)
    start = f"2026-10-17T{start_hour:02d}:00:00+00:00"
    session = sessions.start(db, CHAR, plan_id, now=start)
    sessions.finish(db, session.session_id, now=f"2026-10-17T{start_hour:02d}:59:00+00:00")
    outcome = {
        "plan": {"items": [{"item_id": item_id, "kind": "mining", "response": response}]},
        "log_mining": {"m3_per_active_minute": actual, "active_minutes": active},
    }
    db.execute(
        "INSERT INTO session_outcomes (session_id, computed_at, settled, content_hash, details) "
        "VALUES (?, 'x', 1, 'h', ?)",
        (session.session_id, json.dumps(outcome)),
    )
    db.commit()
    return session.session_id


def test_no_sessions_means_the_published_figure(db):
    learned = corrections.correction(db, CHAR, "mining_rate")
    assert learned.factor == 1.0
    assert learned.reason().code == "not_learned_yet"
    text = explain.describe(learned.reason().code, learned.reason().detail)
    assert "game logs" in text


def test_the_factor_moves_towards_the_player_as_sessions_agree(db):
    _session(db, predicted=400, actual=300, start_hour=1)
    one = corrections.correction(db, CHAR, "mining_rate")
    assert one.factor == pytest.approx(1 - 0.25 * 1 / 4)
    for hour in (2, 3, 4):
        _session(db, predicted=400, actual=300, start_hour=hour)
    four = corrections.correction(db, CHAR, "mining_rate")
    assert four.sessions == 4
    assert four.factor == pytest.approx(1 - 0.25 * 4 / 7)
    assert "4 of your sessions" in explain.describe(four.reason().code, four.reason().detail)


def test_declined_steps_and_short_mining_are_ignored(db):
    _session(db, 400, 100, response="not_now", start_hour=1)
    _session(db, 400, 100, active=5, start_hour=2)
    _session(db, 400, 200, response=None, start_hour=3)  # no answer still counts
    assert [o.ratio for o in corrections.mining_observations(db, CHAR)] == [0.5]


def test_only_the_latest_outcome_counts_and_limits_hold(db):
    session_id = _session(db, 400, 4000, start_hour=1)
    newer = {
        "plan": json.loads(db.execute("SELECT details FROM session_outcomes").fetchone()[0])[
            "plan"
        ],
        "log_mining": {"m3_per_active_minute": 40, "active_minutes": 30},
    }
    db.execute(
        "INSERT INTO session_outcomes (session_id, computed_at, settled, content_hash, details) "
        "VALUES (?, 'y', 1, 'h2', ?)",
        (session_id, json.dumps(newer)),
    )
    for hour in range(2, 12):
        _session(db, 400, 40, start_hour=hour)
    learned = corrections.correction(db, CHAR, "mining_rate")
    assert learned.sessions == 11
    assert learned.factor == corrections.LIMITS[0]


def test_unknown_corrections_are_refused(db):
    with pytest.raises(ValueError, match="Unknown correction"):
        corrections.correction(db, CHAR, "luck")
