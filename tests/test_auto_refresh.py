import contextlib
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from eve_assistant import campaigns, industry, refresh, signals, snapshots, store
from eve_assistant.config import Config, ConfigError, load_config
from eve_assistant.esi import PLANETS_SCOPE, SCOPES
from eve_assistant.esi.client import EsiError, EsiRateLimited
from eve_assistant.web import create_app

NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)
CONFIG = Config("abc", "http://localhost", None, "local")


def check(db, kind, minutes_ago, character_id=7):
    when = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO snapshot_checks (character_id, kind, checked_at, outcome) "
        "VALUES (?, ?, ?, 'unchanged')",
        (character_id, kind, when),
    )
    db.commit()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "auto.sqlite3"
    conn = store.connect(path)
    snapshots.ensure_character(conn, 7, "Miner")
    conn.close()
    return path


def test_intervals_depend_on_being_online():
    assert refresh.interval("wallet", online=True) == timedelta(minutes=2)
    assert refresh.interval("wallet", online=False) == timedelta(hours=1)
    assert refresh.interval("mining_ledger", online=True) == timedelta(minutes=15)
    assert refresh.interval("assets", online=True) == timedelta(hours=1)
    assert refresh.interval("online", online=False) == timedelta(minutes=5)


def test_due_kinds_follow_the_last_check(db_path):
    db = store.connect(db_path)
    for kind in snapshots.KINDS:
        check(db, kind.name, minutes_ago=3)
    assert refresh.due_kinds(db, 7, NOW) == set()  # offline: only hourly (and online every 5)
    # record() notes its check at the clock's time: pin it to 3 minutes before NOW.
    three_ago = (NOW - timedelta(minutes=3)).isoformat(timespec="seconds")
    with mock.patch.object(snapshots, "utc_now", return_value=three_ago):
        snapshots.record(db, 7, "online", {"online": True})
    assert refresh.due_kinds(db, 7, NOW) == {"wallet", "location", "ship", "online"}
    db.close()


@pytest.fixture
def fake_esi(monkeypatch):
    calls = []
    monkeypatch.setattr(
        refresh.auth,
        "list_characters",
        lambda _s: [SimpleNamespace(character_id=7, name="Miner", scopes=SCOPES)],
    )
    monkeypatch.setattr(refresh.auth, "http_client", lambda _c: contextlib.nullcontext(None))
    monkeypatch.setattr(
        refresh.auth, "AccessTokens", lambda *_a: SimpleNamespace(get=lambda _cid: "t")
    )

    def snapshot_one(db, esi, character_id, name, kinds=None, granted=None):
        calls.append(set(kinds))
        for kind in kinds:
            check(db, kind, minutes_ago=0, character_id=character_id)
        return "line", []

    monkeypatch.setattr(refresh, "_snapshot_one", snapshot_one)
    prices = mock.Mock(return_value=1)
    monkeypatch.setattr(refresh.goals, "update_prices", prices)
    monkeypatch.setattr(refresh.freelance, "refresh", mock.Mock(return_value=0))
    monkeypatch.setattr(
        refresh.signals, "refresh", lambda db, _esi, now: signals._mark(db, signals.SETTING, now)
    )
    monkeypatch.setattr(
        refresh.industry, "refresh", lambda db, _esi, now: signals._mark(db, industry.SETTING, now)
    )
    monkeypatch.setattr(
        refresh.campaigns,
        "refresh",
        lambda db, _esi, now: signals._mark(db, campaigns.SETTING, now),
    )
    monkeypatch.setattr(
        refresh.sovereignty,
        "refresh",
        lambda db, _esi, now: db.execute(
            "INSERT OR REPLACE INTO sov_systems VALUES (1, NULL, NULL, NULL, ?)",
            (now.isoformat(timespec="seconds"),),
        ),
    )
    return calls, prices


def test_a_tick_fetches_only_what_is_due(db_path, fake_esi):
    calls, prices = fake_esi
    first = refresh.auto_refresh_once(CONFIG, None, db_path, now=NOW)
    assert calls == [{k.name for k in snapshots.KINDS}]
    assert first.checked == {"Miner": len(snapshots.KINDS)}
    assert prices.call_count == 1

    db = store.connect(db_path)
    db.execute("INSERT INTO market_prices VALUES (1, 1.0, 1.0, ?)", (NOW.isoformat(),))
    db.commit()
    db.close()
    second = refresh.auto_refresh_once(CONFIG, None, db_path, now=NOW + timedelta(minutes=1))
    assert len(calls) == 1  # nothing due a minute later
    assert second.checked == {}

    later = refresh.auto_refresh_once(CONFIG, None, db_path, now=NOW + timedelta(minutes=6))
    assert calls[-1] == {"online"}
    assert later.checked == {"Miner": 1}


def test_corporation_and_colony_layouts_follow_their_kinds(monkeypatch):
    seen = []
    monkeypatch.setattr(refresh.corporation, "refresh", lambda *_a: seen.append("corporation"))
    monkeypatch.setattr(refresh.planets, "refresh", lambda *_a: seen.append("planets"))
    refresh._extras(None, None, 7, "Miner", {"wallet"}, SCOPES)
    assert seen == []
    refresh._extras(None, None, 7, "Miner", {"public", "planets"}, SCOPES)
    assert seen == ["corporation"]  # the login hasn't the planets scope
    refresh._extras(None, None, 7, "Miner", {"planets"}, (*SCOPES, PLANETS_SCOPE))
    assert seen == ["corporation", "planets"]

    def failing(*_args):
        raise EsiError("timed out")

    monkeypatch.setattr(refresh.corporation, "refresh", failing)
    assert refresh._extras(None, None, 7, "Miner", {"public"}, SCOPES) == [
        "Miner, corporation: timed out"
    ]


def test_a_rate_limit_stops_the_tick_and_says_when_to_retry(db_path, fake_esi, monkeypatch):
    def limited(*_args, **_kwargs):
        raise EsiRateLimited("Slow down.", retry_after=30)

    monkeypatch.setattr(refresh, "_snapshot_one", limited)
    report = refresh.auto_refresh_once(CONFIG, None, db_path, now=NOW)
    assert report.retry_after == 30
    assert "Try again in about 30 seconds" in report.problems[0]


def test_nobody_logged_in_means_nothing_to_do(db_path, monkeypatch):
    monkeypatch.setattr(refresh.auth, "list_characters", lambda _s: [])
    assert refresh.auto_refresh_once(CONFIG, None, db_path, now=NOW).checked == {}


def test_the_dashboard_runs_ticks_and_shows_their_status(db_path):
    tick = mock.Mock(
        return_value=SimpleNamespace(problems=["Miner, wallet: timed out"], retry_after=None)
    )
    app = create_app(db_path, auto_refresh=tick, auto_tick_seconds=0.05)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        deadline = time.monotonic() + 5
        while tick.call_count < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        page = client.get("/today").text
    assert tick.call_count >= 2
    assert "Updates from EVE automatically" in page
    assert "last check" in page
    assert "1 problem: Miner, wallet: timed out" in page


def test_a_failing_tick_keeps_the_loop_alive(db_path):
    tick = mock.Mock(side_effect=RuntimeError("boom"))
    app = create_app(db_path, auto_refresh=tick, auto_tick_seconds=0.05)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        deadline = time.monotonic() + 5
        while tick.call_count < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        page = client.get("/today").text
    assert tick.call_count >= 2
    assert "Background refresh failed: boom" in page


def test_without_auto_refresh_the_page_says_so(db_path):
    with TestClient(create_app(db_path), base_url="http://127.0.0.1") as client:
        assert "Automatic updates are off" in client.get("/today").text


def test_auto_refresh_setting(tmp_path):
    path = tmp_path / "local.toml"
    path.write_text('[esi]\nclient_id = "abc"\n', encoding="utf-8")
    assert load_config(path).auto_refresh is True
    path.write_text('[esi]\nclient_id = "abc"\n[app]\nauto_refresh = false\n', encoding="utf-8")
    assert load_config(path).auto_refresh is False
    path.write_text('[esi]\nclient_id = "abc"\n[app]\nauto_refresh = "yes"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="true or false"):
        load_config(path)
