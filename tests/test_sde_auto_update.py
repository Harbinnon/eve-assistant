"""Static data that updates itself (when no play session is open), and the
banner's Update now button, which asks the next background tick to do it."""

import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from eve_assistant import refresh, sde, sessions, snapshots, store, web
from eve_assistant.config import Config
from eve_assistant.web import create_app

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
NEW = 3662168
LATEST = json.dumps({"_key": "sde", "buildNumber": NEW, "releaseDate": "2026-10-30T11:00:00Z"})
CONFIG = Config("abc", "http://localhost", None, "local")


def _db(path=":memory:", loaded=3654997, latest=NEW):
    db = store.connect(path)
    db.execute(
        "INSERT INTO sde_meta (build_number, release_date, loaded_at, loader_version) "
        "VALUES (?, '2026-10-27T11:00:00Z', 'x', ?)",
        (loaded, sde.LOADER_VERSION),
    )
    seen = {"build_number": latest, "release_date": "2026-10-30T11:00:00Z",
            "checked_at": NOW.isoformat()}  # fmt: skip
    db.execute("INSERT INTO settings VALUES (?, ?)", (sde.LATEST_SETTING, json.dumps(seen)))
    db.commit()
    return db


def _state(db, state, at):
    db.execute(
        "INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (sde.UPDATE_SETTING, json.dumps({"state": state, "at": at.isoformat()})),
    )
    db.commit()


# --- when it's due -----------------------------------------------------------


@pytest.mark.parametrize(
    "state, ago, playing, automatic, due",
    [
        (None, None, False, True, True),  # newer build, not playing: load it
        (None, None, True, True, False),  # playing: wait
        (None, None, False, False, False),  # switched off
        ("requested", 0, True, False, True),  # Update now: even while playing, even if off
        ("running", 5, False, True, False),  # another tick is at it
        ("running", 45, False, True, True),  # a run that never finished
        ("failed", 60, False, True, False),  # failed an hour ago: wait
        ("failed", 7 * 60, False, True, True),  # failed 7 hours ago: try again
        ("done", 1, False, True, True),  # done, but CCP has published again
    ],
)
def test_when_an_update_is_due(state, ago, playing, automatic, due):
    db = _db()
    if state:
        _state(db, state, NOW - timedelta(minutes=ago))
    assert sde.update_due(db, NOW, playing, automatic) is due


def test_nothing_is_due_when_the_data_is_current():
    db = _db(loaded=NEW)
    assert not sde.update_due(db, NOW, playing=False, automatic=True)


# --- doing it ----------------------------------------------------------------


def _fake_update(db, _http, force=False):
    db.execute("DELETE FROM sde_meta")
    db.execute(
        "INSERT INTO sde_meta (build_number, release_date, loaded_at, loader_version) "
        "VALUES (?, '2026-10-30T11:00:00Z', 'now', ?)",
        (NEW, sde.LOADER_VERSION),
    )
    db.commit()
    return sde.SdeBuild(NEW, "2026-10-30T11:00:00Z"), True


def test_a_finished_update_is_noted_and_the_banner_goes(monkeypatch):
    db = _db()
    monkeypatch.setattr(sde, "update", _fake_update)
    assert sde.update_now(db, None, NOW).build_number == NEW
    status = sde.update_status(db)
    assert status.update["state"] == "done"
    assert not status.needs_update
    assert not status.updating


def test_a_failed_update_is_noted_with_its_reason(monkeypatch):
    db = _db()

    def broken(*_a, **_k):
        raise sde.SdeError("Static data download failed: ConnectError")

    monkeypatch.setattr(sde, "update", broken)
    with pytest.raises(sde.SdeError, match="tries again in 6 hours, or press Update now"):
        sde.update_now(db, None, NOW)
    assert sde.update_state(db)["state"] == "failed"
    assert "ConnectError" in sde.update_state(db)["error"]
    assert not sde.update_due(db, NOW + timedelta(hours=1), playing=False, automatic=True)


def test_an_unexpected_file_counts_as_a_failure_too(monkeypatch):
    db = _db()
    monkeypatch.setattr(sde, "update", lambda *_a, **_k: (_ for _ in ()).throw(KeyError("x")))
    with pytest.raises(sde.SdeError, match="Static data update failed: KeyError"):
        sde.update_now(db, None, NOW)


@pytest.mark.real_static_data_check
@respx.mock
def test_the_background_refresh_loads_a_new_build_when_nobody_is_playing(tmp_path, monkeypatch):
    path = tmp_path / "s.sqlite3"
    db = _db(path)
    respx.get(sde.LATEST_URL).mock(return_value=httpx.Response(200, text=LATEST))
    monkeypatch.setattr(refresh.auth, "http_client", lambda _c: httpx.Client())
    monkeypatch.setattr(sde, "update", _fake_update)
    snapshots.ensure_character(db, 7, "Miner")
    db.commit()
    session = sessions.start(db, 7)
    assert refresh._check_static_data(CONFIG, path, NOW) == []
    assert sde.loaded_build(db).build_number != NEW  # playing: it waited
    sessions.finish(db, session.session_id)
    assert refresh._check_static_data(CONFIG, path, NOW) == []
    assert sde.loaded_build(db).build_number == NEW


@pytest.mark.real_static_data_check
def test_switched_off_it_only_loads_when_asked(tmp_path, monkeypatch):
    path = tmp_path / "s.sqlite3"
    db = _db(path)
    monkeypatch.setattr(refresh.auth, "http_client", lambda _c: httpx.Client())
    monkeypatch.setattr(sde, "update", _fake_update)
    off = Config("abc", "http://localhost", None, "local", auto_update_static_data=False)
    refresh._check_static_data(off, path, NOW)
    assert sde.loaded_build(db).build_number != NEW
    sde.request_update(db, NOW)
    refresh._check_static_data(off, path, NOW)
    assert sde.loaded_build(db).build_number == NEW


# --- the banner --------------------------------------------------------------


def _token(html):
    import re

    return re.search(r'name="token" value="([^"]+)"', html).group(1)


def test_the_banner_offers_update_now_and_shows_it_running(tmp_path, monkeypatch):
    path = tmp_path / "s.sqlite3"
    _db(path).close()
    monkeypatch.setattr(web, "AUTO_TICK_SECONDS", 3600)  # one tick, then quiet
    tick = mock.Mock(return_value=SimpleNamespace(problems=[], retry_after=None))
    with TestClient(create_app(path, auto_refresh=tick), base_url="http://127.0.0.1") as client:
        deadline = time.monotonic() + 5
        while not tick.called and time.monotonic() < deadline:
            time.sleep(0.02)
        page = " ".join(client.get("/today").text.split())
        assert "CCP published new static data" in page
        assert "it loads by itself when you're not playing" in page
        assert 'action="/static-data/update"' in page
        asked = client.post(
            "/static-data/update", data={"token": _token(page)}, follow_redirects=False
        )
        assert asked.status_code == 303
        page = " ".join(client.get(asked.headers["location"]).text.split())
    assert "Static data update asked for" in page
    assert "Updating the static data from CCP" in page
    assert 'action="/static-data/update"' not in page


def test_a_failed_update_shows_on_the_banner(tmp_path, monkeypatch):
    path = tmp_path / "s.sqlite3"
    db = _db(path)
    db.execute(
        "INSERT INTO settings VALUES (?, ?)",
        (sde.UPDATE_SETTING, json.dumps({"state": "failed", "at": NOW.isoformat(),
                                         "error": "Static data download failed: ConnectError"})),
    )  # fmt: skip
    db.commit()
    monkeypatch.setattr(web, "AUTO_TICK_SECONDS", 3600)
    tick = mock.Mock(return_value=SimpleNamespace(problems=[], retry_after=None))
    with TestClient(create_app(path, auto_refresh=tick), base_url="http://127.0.0.1") as client:
        page = " ".join(client.get("/today").text.split())
    assert "The last try failed" in page
    assert "ConnectError" in page
    assert 'action="/static-data/update"' in page  # try again


def test_without_the_background_refresh_the_banner_gives_the_command(tmp_path):
    path = tmp_path / "s.sqlite3"
    _db(path).close()
    with TestClient(create_app(path), base_url="http://127.0.0.1") as client:
        page = " ".join(client.get("/today").text.split())
        assert "eve-assistant sde-update</span>" in page
        assert 'action="/static-data/update"' not in page
        refused = client.post("/static-data/update", data={"token": _token(page)})
    assert refused.status_code == 404
