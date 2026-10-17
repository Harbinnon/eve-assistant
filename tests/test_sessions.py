import sqlite3

import pytest

from eve_assistant import cli, planner, refresh, sessions, snapshots, store

START = "2026-10-17T18:00:00+00:00"


def _at(minutes: float) -> str:
    hours, mins = divmod(int(minutes), 60)
    return f"2026-10-{17 + (18 + hours) // 24}T{(18 + hours) % 24:02d}:{mins:02d}:00+00:00"


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, 7, "Miner")
    snapshots.ensure_character(conn, 8, "Alt")
    yield conn
    conn.close()


def _plan(db, character_id=7) -> int:
    plan = planner.Plan(
        character_id=character_id,
        minutes_available=60,
        items=(),
        alternatives=(),
        notes=(),
        net_worth=0.0,
    )
    return planner.record(db, plan, {}).plan_id


def test_start_and_finish_record_times_and_problems(db):
    plan_id = _plan(db)
    started = sessions.start(db, 7, plan_id, ["wallet: timed out"], now=START)
    assert started.is_open
    assert started.plan_id == plan_id
    assert started.start_problems == ["wallet: timed out"]
    assert sessions.open_session(db, 7) == started

    ended = sessions.finish(db, started.session_id, now=_at(75))
    assert not ended.is_open
    assert ended.how == "finished"
    assert ended.minutes() == 75
    assert sessions.open_session(db, 7) is None


def test_minutes_played_moves_the_end_back(db):
    started = sessions.start(db, 7, now=START)
    ended = sessions.finish(db, started.session_id, minutes_played=40, now=_at(90))
    assert ended.ended_at == _at(40)
    assert ended.minutes() == 40


def test_one_running_session_per_character(db):
    sessions.start(db, 7, now=START)
    later = _at(5)
    with pytest.raises(sessions.SessionError, match="already has a session"):
        sessions.start(db, 7, now=later)
    other = sessions.start(db, 8, now=later)  # another character can play meanwhile
    assert other.is_open


def test_a_plan_must_belong_to_the_character(db):
    plan_id = _plan(db, character_id=8)
    with pytest.raises(sessions.SessionError, match="another character"):
        sessions.start(db, 7, plan_id)
    with pytest.raises(sessions.SessionError, match="No plan"):
        sessions.start(db, 7, 999)
    with pytest.raises(sessions.SessionError, match="Unknown character"):
        sessions.start(db, 99)


def test_bad_endings_are_refused(db):
    session_id = sessions.start(db, 7, now=START).session_id
    half_hour, after = _at(30), _at(31)
    with pytest.raises(sessions.SessionError, match="can't have lasted"):
        sessions.finish(db, session_id, minutes_played=90, now=half_hour)
    with pytest.raises(sessions.SessionError, match="more than zero"):
        sessions.finish(db, session_id, minutes_played=0, now=half_hour)
    with pytest.raises(sessions.SessionError, match="finished or cancelled"):
        sessions.finish(db, session_id, how="paused", now=half_hour)
    sessions.finish(db, session_id, now=half_hour)
    with pytest.raises(sessions.SessionError, match="already ended"):
        sessions.finish(db, session_id, now=after)


def test_a_stale_session_needs_the_real_length(db):
    session = sessions.start(db, 7, now=START)
    next_day = _at(13 * 60)
    assert session.is_stale(next_day)
    with pytest.raises(sessions.SessionError, match="how many minutes"):
        sessions.finish(db, session.session_id, now=next_day)
    ended = sessions.finish(db, session.session_id, minutes_played=90, now=next_day)
    assert ended.minutes() == 90


def test_cancelling_ignores_minutes_and_staleness(db):
    session = sessions.start(db, 7, now=START)
    ended = sessions.finish(db, session.session_id, 5000, how="cancelled", now=_at(13 * 60))
    assert ended.how == "cancelled"


def test_sessions_are_append_only(db):
    session = sessions.start(db, 7, now=START)
    sessions.finish(db, session.session_id, now=_at(10))
    for statement in (
        "UPDATE play_sessions SET started_at = 'x'",
        "DELETE FROM play_sessions",
        "UPDATE play_session_ends SET how = 'cancelled'",
        "DELETE FROM play_session_ends",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(statement)


def test_recent_lists_newest_first(db):
    first = sessions.start(db, 7, now=START)
    sessions.finish(db, first.session_id, now=_at(10))
    second = sessions.start(db, 7, now=_at(20))
    assert [s.session_id for s in sessions.recent(db)] == [second.session_id, first.session_id]


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    path = tmp_path / "cli.sqlite3"
    conn = store.connect(path)
    snapshots.ensure_character(conn, 7, "Miner")
    plan_id = _plan(conn)
    conn.close()
    real_connect = store.connect
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(store, "connect", lambda _db_path=None: real_connect(path))
    taken = []
    monkeypatch.setattr(cli, "_snapshot_for_session", lambda cid: taken.append(cid) or [])
    return path, plan_id, taken


def test_cli_start_follows_the_latest_plan_and_finish_snapshots(cli_db, capsys):
    path, plan_id, taken = cli_db
    assert cli.main(["session", "start", "Miner"]) == 0
    assert f"following plan {plan_id}" in capsys.readouterr().out
    assert cli.main(["session", "start", "Miner"]) == 2  # already running
    assert cli.main(["session", "finish", "Miner"]) == 0
    assert taken == [7, 7]
    assert cli.main(["sessions"]) == 0
    assert "finished" in capsys.readouterr().out


def test_cli_cancel_takes_no_snapshot(cli_db):
    _path, _plan_id, taken = cli_db
    assert cli.main(["session", "start", "Miner", "--no-plan"]) == 0
    assert cli.main(["session", "cancel", "Miner"]) == 0
    assert taken == [7]
    assert cli.main(["session", "finish", "Miner"]) == 2  # nothing running


def test_snapshot_character_skips_characters_not_logged_in(monkeypatch, tmp_path):
    monkeypatch.setattr(refresh.auth, "list_characters", lambda _secrets: [])
    problems = refresh.snapshot_character(None, None, 7, tmp_path / "x.sqlite3")
    assert problems == ["This character is not logged in, so no snapshot was taken."]
