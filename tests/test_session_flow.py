from unittest import mock

import pytest

from eve_assistant import planner, session_flow, sessions, snapshots, store


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "flow.sqlite3"
    conn = store.connect(path)
    snapshots.ensure_character(conn, 7, "Miner")
    snapshots.ensure_character(conn, 8, "Alt")
    conn.close()
    return path


def _plan(db_path, character_id=7) -> int:
    conn = store.connect(db_path)
    plan = planner.Plan(
        character_id=character_id,
        minutes_available=60,
        items=(),
        alternatives=(),
        notes=(),
        net_worth=0.0,
    )
    plan_id = planner.record(conn, plan, {}).plan_id
    conn.close()
    return plan_id


def test_begin_from_a_plan_takes_a_snapshot_and_records(db_path):
    plan_id = _plan(db_path)
    snapshot = mock.Mock(return_value=["Miner, wallet: timed out"])
    result = session_flow.begin(db_path, snapshot, plan_id=plan_id)
    snapshot.assert_called_once_with(7)
    assert result.session.character_id == 7
    assert result.session.plan_id == plan_id
    assert result.session.start_problems == ["Miner, wallet: timed out"]
    assert result.problems == ["Miner, wallet: timed out"]


def test_mistakes_are_caught_before_the_snapshot(db_path):
    snapshot = mock.Mock(return_value=[])
    with pytest.raises(sessions.SessionNotFound, match="No plan"):
        session_flow.begin(db_path, snapshot, plan_id=999)
    alts_plan = _plan(db_path, 8)
    with pytest.raises(sessions.SessionError, match="another character"):
        session_flow.begin(db_path, snapshot, character_id=7, plan_id=alts_plan)
    with pytest.raises(sessions.SessionError, match="which character"):
        session_flow.begin(db_path, snapshot)
    session_flow.begin(db_path, snapshot, character_id=7)
    snapshot.reset_mock()
    with pytest.raises(sessions.SessionError, match="already has a session"):
        session_flow.begin(db_path, snapshot, character_id=7)
    with pytest.raises(sessions.SessionNotFound):
        session_flow.end(db_path, snapshot, 999)
    snapshot.assert_not_called()


def test_snapshot_trouble_never_stops_a_session(db_path):
    broken = mock.Mock(side_effect=RuntimeError("ESI is down"))
    result = session_flow.begin(db_path, broken, character_id=7)
    assert result.problems == ["Snapshot failed: ESI is down"]
    without = session_flow.begin(db_path, None, character_id=8)
    assert "not available here" in without.problems[0]


def test_end_finishes_with_a_snapshot_and_works_out_the_outcome(db_path, monkeypatch):
    snapshot = mock.Mock(return_value=[])
    started = session_flow.begin(db_path, snapshot, character_id=7).session
    update = mock.Mock(return_value=1)
    monkeypatch.setattr(session_flow.outcomes, "update", update)
    result = session_flow.end(db_path, snapshot, started.session_id)
    assert result.session.how == "finished"
    assert snapshot.call_count == 2
    update.assert_called_once()
    assert update.call_args.args[1] == 7


def test_cancel_takes_no_snapshot_and_bad_minutes_take_none_either(db_path):
    snapshot = mock.Mock(return_value=[])
    first = session_flow.begin(db_path, snapshot, character_id=7).session
    snapshot.reset_mock()
    with pytest.raises(sessions.SessionError, match="can't have lasted"):
        session_flow.end(db_path, snapshot, first.session_id, minutes_played=500)
    cancelled = session_flow.end(db_path, snapshot, first.session_id, how="cancelled")
    assert cancelled.session.how == "cancelled"
    snapshot.assert_not_called()


def test_running_session_id(db_path):
    with pytest.raises(sessions.SessionError, match="No session is running"):
        session_flow.running_session_id(db_path, 7)
    started = session_flow.begin(db_path, None, character_id=7).session
    assert session_flow.running_session_id(db_path, 7) == started.session_id
