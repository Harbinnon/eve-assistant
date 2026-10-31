import contextlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from eve_assistant import refresh, sde, store
from eve_assistant.config import Config
from eve_assistant.web import create_app

NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)
LATEST = json.dumps({"_key": "sde", "buildNumber": 3610000, "releaseDate": "2026-10-20T11:00:00Z"})


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "sde.sqlite3"


def _loaded(db, build=3607705):
    db.execute(
        "INSERT INTO sde_meta (build_number, release_date, loaded_at, loader_version) "
        "VALUES (?, '2026-10-12T11:08:38Z', 'x', ?)",
        (build, sde.LOADER_VERSION),
    )
    db.commit()


@respx.mock
def test_the_check_remembers_the_latest_build(db_path):
    respx.get(sde.LATEST_URL).mock(return_value=httpx.Response(200, text=LATEST + "\n"))
    db = store.connect(db_path)
    _loaded(db)
    assert sde.check_due(db, NOW)
    with httpx.Client() as http:
        sde.check_for_update(db, http, NOW)
    status = sde.update_status(db)
    assert status.latest.build_number == 3610000
    assert status.newer_available
    assert status.needs_update
    assert not sde.check_due(db, NOW + timedelta(hours=23))
    assert sde.check_due(db, NOW + timedelta(hours=24))
    db.close()


def test_status_without_data_and_when_current(db_path):
    db = store.connect(db_path)
    nothing = sde.update_status(db)
    assert nothing.loaded is None
    assert nothing.needs_update
    _loaded(db)
    current = sde.update_status(db)
    assert not current.needs_update  # no check yet: nothing to say
    db.close()


def test_an_older_loader_asks_for_a_reload(db_path):
    db = store.connect(db_path)
    db.execute(
        "INSERT INTO sde_meta (build_number, release_date, loaded_at, loader_version) "
        "VALUES (1, 'x', 'y', 0)"
    )
    db.commit()
    assert sde.update_status(db).reload_needed
    db.close()


@pytest.mark.real_static_data_check
@respx.mock
def test_the_background_refresh_checks_once_a_day(db_path, monkeypatch):
    route = respx.get(sde.LATEST_URL).mock(return_value=httpx.Response(200, text=LATEST))
    monkeypatch.setattr(refresh.auth, "list_characters", lambda _s: [])
    monkeypatch.setattr(refresh.auth, "http_client", lambda _c: httpx.Client())
    monkeypatch.setattr(refresh.backups, "run_if_due", lambda *_a: [])
    config = Config("abc", "http://localhost", None, "local", auto_update_static_data=False)
    refresh.auto_refresh_once(config, None, db_path, now=NOW)
    refresh.auto_refresh_once(config, None, db_path, now=NOW + timedelta(hours=1))
    assert route.call_count == 1
    route.mock(return_value=httpx.Response(503))
    report = refresh.auto_refresh_once(config, None, db_path, now=NOW + timedelta(days=1))
    assert "Could not check the latest static data" in report.problems[0]


@pytest.mark.real_static_data_check
def test_a_failed_check_never_breaks_the_tick(db_path, monkeypatch):
    def broken(_config):
        return contextlib.nullcontext(None)

    monkeypatch.setattr(refresh.auth, "http_client", broken)
    monkeypatch.setattr(
        refresh.sde, "latest_build", lambda _http: (_ for _ in ()).throw(sde.SdeError("down"))
    )
    config = Config("abc", "http://localhost", None, "local")
    assert refresh._check_static_data(config, db_path, NOW) == ["down"]


def test_pages_show_the_banner(db_path):
    db = store.connect(db_path)
    with TestClient(create_app(db_path), base_url="http://127.0.0.1") as client:
        assert "No static data loaded yet" in client.get("/today").text
        _loaded(db)
        assert "static data" not in client.get("/today").text.split("<main")[1][:400]
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            (sde.LATEST_SETTING, json.dumps({
                "build_number": 3610000, "release_date": "2026-10-20T11:00:00Z",
                "checked_at": NOW.isoformat(),
            })),
        )  # fmt: skip
        db.commit()
        page = client.get("/characters").text
    db.close()
    text = " ".join(page.split())
    assert "CCP published new static data</strong> on 2026-10-20" in text
    assert "(build 3610000; you have 3607705)" in text
