import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from eve_assistant import cli, detection, planner, session_flow, sessions, snapshots, store
from eve_assistant.web import create_app

NOW = datetime.now(UTC).replace(microsecond=0)


def eve(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


def online(db, character_id, login, logout, is_online=False):
    snapshots.record(
        db,
        character_id,
        "online",
        {"online": is_online, "last_login": eve(login), "last_logout": eve(logout), "logins": 1},
    )


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "detect.sqlite3"
    conn = store.connect(path)
    snapshots.ensure_character(conn, 7, "Miner")
    snapshots.ensure_character(conn, 8, "Alt")
    conn.close()
    return path


@pytest.fixture
def db(db_path):
    conn = store.connect(db_path)
    yield conn
    conn.close()


LOGIN = NOW - timedelta(hours=5)
LOGOUT = NOW - timedelta(hours=3)
LOGIN_AT, LOGOUT_AT = iso(LOGIN), iso(LOGOUT)


def test_finished_sessions_are_offered_once_each(db):
    online(db, 7, LOGIN, LOGIN - timedelta(days=1), is_online=True)  # still playing then
    online(db, 7, LOGIN + timedelta(seconds=2), LOGOUT)  # EVE's login time wobbles
    online(db, 7, LOGIN - timedelta(seconds=1), LOGOUT + timedelta(seconds=1))
    (offered,) = detection.suggestions(db)
    assert offered.character_name == "Miner"
    assert offered.minutes == pytest.approx(120, abs=0.1)
    assert offered.plan_id is None


def test_short_old_and_recorded_sessions_are_not_offered(db):
    online(db, 7, NOW - timedelta(hours=1), NOW - timedelta(minutes=55))  # 5 minutes
    online(db, 8, NOW - timedelta(days=20), NOW - timedelta(days=20) + timedelta(hours=1))
    online(db, 8, LOGIN, LOGOUT)
    sessions.start(db, 8, now=iso(LOGIN + timedelta(minutes=30)))  # overlaps, still running
    assert detection.suggestions(db) == []


def test_the_plan_made_around_the_session_is_linked(db):
    blank = planner.Plan(
        character_id=7, minutes_available=60, items=(), alternatives=(), notes=(), net_worth=0.0
    )
    plan_id = planner.record(db, blank, {}).plan_id
    made = datetime.fromisoformat(
        db.execute("SELECT created_at FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()[0]
    )
    later = iso(made + timedelta(hours=2))
    online(db, 7, made + timedelta(minutes=10), made + timedelta(minutes=70))  # plan just before
    online(db, 8, made - timedelta(hours=5), made - timedelta(hours=4))  # another character
    offered = {d.character_id: d.plan_id for d in detection.suggestions(db, now=later)}
    assert offered == {7: plan_id, 8: None}


def test_recording_creates_a_finished_session_with_its_outcome(db, db_path):
    online(db, 7, LOGIN, LOGOUT)
    session = session_flow.record_detected(db_path, 7, iso(LOGIN))
    assert session.how == "finished"
    assert session.minutes() == pytest.approx(120, abs=0.1)
    assert session.start_problems == [detection.NOTE]
    assert detection.suggestions(db) == []  # recorded, so no longer offered
    assert (
        db.execute(
            "SELECT COUNT(*) FROM session_outcomes WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
        == 1
    )
    with pytest.raises(sessions.SessionNotFound, match="isn't on offer"):
        session_flow.record_detected(db_path, 7, LOGIN_AT)


def test_dismissed_sessions_stay_dismissed(db, db_path):
    online(db, 7, LOGIN, LOGOUT)
    session_flow.dismiss_detected(db_path, 7, iso(LOGIN))
    assert detection.suggestions(db) == []
    with pytest.raises(sessions.SessionNotFound):
        session_flow.dismiss_detected(db_path, 7, LOGIN_AT)


def test_record_past_refuses_overlaps_and_backwards_times(db):
    sessions.record_past(db, 7, iso(LOGIN), iso(LOGOUT), None, "note")
    a_bit_later, now = iso(LOGIN + timedelta(minutes=5)), iso(NOW)
    with pytest.raises(sessions.SessionError, match="overlaps"):
        sessions.record_past(db, 7, a_bit_later, now, None, "note")
    with pytest.raises(sessions.SessionError, match="end after it starts"):
        sessions.record_past(db, 8, LOGOUT_AT, LOGIN_AT, None, "note")
    with pytest.raises(sessions.SessionNotFound):
        sessions.record_past(db, 99, LOGIN_AT, LOGOUT_AT, None, "note")


def test_today_offers_noticed_sessions(db, db_path):
    online(db, 7, LOGIN, LOGOUT)
    online(db, 8, LOGIN, LOGOUT)
    with TestClient(create_app(db_path), base_url="http://127.0.0.1") as client:
        page = client.get("/today").text
        assert "Sessions EVE saw you play" in page
        token = re.search(r'name="token" value="([^"]+)"', page).group(1)
        form = {"token": token, "character_id": "7", "login": iso(LOGIN)}
        recorded = client.post("/detected/record", data=form)
        assert "Session recorded." in recorded.text
        assert "Recent sessions" in recorded.text
        dismissed = client.post("/detected/dismiss", data={**form, "character_id": "8"})
        assert "won&#39;t be offered again" in dismissed.text
        assert "Sessions EVE saw you play" not in dismissed.text
        made_up = client.post("/detected/record", data={**form, "login": iso(NOW)})
        assert "isn&#39;t on offer any more" in made_up.text
        assert client.post("/detected/record", data={**form, "token": "x"}).status_code == 403
        assert client.post("/detected/steal", data=form).status_code == 404


def test_cli_lists_records_and_dismisses(db, db_path, monkeypatch, capsys):
    online(db, 7, LOGIN, LOGOUT)
    online(db, 8, LOGIN, LOGOUT)
    monkeypatch.setattr(store, "DEFAULT_DB_PATH", db_path)
    assert cli.main(["detected"]) == 0
    listed = capsys.readouterr().out
    assert "1: " in listed
    assert "2: " in listed
    assert cli.main(["detected", "record", "1"]) == 0
    assert "Recorded as session" in capsys.readouterr().out
    assert cli.main(["detected", "dismiss", "1"]) == 0
    assert cli.main(["detected"]) == 0
    assert "No sessions to record" in capsys.readouterr().out
    assert cli.main(["detected", "record", "5"]) == 2
