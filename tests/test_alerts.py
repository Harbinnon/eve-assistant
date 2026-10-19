"""Alerts (roadmap 7.1): in-game actions to Discord, health to ntfy, each once."""

import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from eve_assistant import alerts, cli, snapshots, store
from eve_assistant.secrets_store import MemoryStore

NOW = datetime.fromisoformat("2026-10-19T12:00:00+00:00")
WEBHOOK = "https://discord.com/api/webhooks/1/abc"
TOPIC = "https://ntfy.example/eve-health"


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, 7, "Miner")
    yield conn
    conn.close()


@pytest.fixture
def secrets():
    found = MemoryStore()
    alerts.save_address(found, "discord", WEBHOOK)
    alerts.save_address(found, "ntfy", TOPIC)
    return found


def _stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_addresses_are_checked_saved_and_removed():
    found = MemoryStore()
    with pytest.raises(alerts.AlertError, match="Discord webhook"):
        alerts.save_address(found, "discord", "https://example.com/hook")
    with pytest.raises(alerts.AlertError, match="https://"):
        alerts.save_address(found, "ntfy", "http://ntfy.example/x")
    alerts.save_address(found, "discord", f" {WEBHOOK} ")
    assert alerts.addresses(found) == {"discord": WEBHOOK}
    alerts.save_address(found, "discord", None)
    assert alerts.addresses(found) == {}


def test_game_alerts_cover_jobs_queues_and_couriers(db):
    snapshots.record(db, 7, "industry_jobs", [
        {"job_id": 1, "status": "active", "end_date": _stamp(NOW - timedelta(minutes=5))},
        {"job_id": 2, "status": "active", "end_date": _stamp(NOW + timedelta(hours=1))},
    ])  # fmt: skip
    snapshots.record(db, 7, "skillqueue", [])
    snapshots.record(db, 7, "contracts", [
        {"contract_id": 10, "type": "courier", "status": "in_progress", "acceptor_id": 7,
         "date_accepted": _stamp(NOW - timedelta(days=1, hours=-2)), "days_to_complete": 1},
        {"contract_id": 11, "type": "courier", "status": "in_progress", "acceptor_id": 7,
         "date_accepted": _stamp(NOW), "days_to_complete": 3},
    ])  # fmt: skip
    found = {a.key: a for a in alerts.game_alerts(db, NOW)}
    assert set(found) == {"job:1", f"queue:7:{_queue_snapshot(db)}", "courier:10"}
    assert found["courier:10"].text == "Miner: a courier contract is due in 2 hours."
    assert found["job:1"].channel == "discord"


def _queue_snapshot(db):
    return db.execute(
        "SELECT MAX(snapshot_id) FROM snapshots WHERE kind = 'skillqueue'"
    ).fetchone()[0]


@respx.mock
def test_each_alert_is_sent_once_with_a_link(db, secrets):
    discord = respx.post(WEBHOOK).mock(return_value=httpx.Response(204))
    ntfy = respx.post(TOPIC).mock(return_value=httpx.Response(200))
    found = [alerts.Alert("job:1", "discord", "Miner: an industry job has finished."),
             *alerts.health_alerts(["Miner: token refresh failed"] * 2, NOW)]  # fmt: skip
    with httpx.Client() as http:
        assert alerts.send(db, secrets, http, found) == (2, [])
        assert alerts.send(db, secrets, http, found) == (0, [])  # already sent
    body = json.loads(discord.calls[0].request.content)
    assert body["content"] == "Miner: an industry job has finished.\n<http://127.0.0.1:8700/today>"
    assert ntfy.calls[0].request.content == b"Miner: token refresh failed"
    assert ntfy.calls[0].request.headers["Title"] == "EVE Assistant"
    with pytest.raises(Exception, match="append-only"):
        db.execute("DELETE FROM sent_alerts")


@respx.mock
def test_a_failed_send_is_tried_again_next_time(db, secrets):
    respx.post(WEBHOOK).mock(side_effect=[httpx.Response(500), httpx.Response(204)])
    found = [alerts.Alert("job:1", "discord", "done")]
    with httpx.Client() as http:
        sent, problems = alerts.send(db, secrets, http, found)
        assert (sent, problems) == (0, ["Alert to Discord failed: HTTPStatusError"])
        assert alerts.send(db, secrets, http, found) == (1, [])


def test_a_channel_without_an_address_sends_and_records_nothing(db):
    only_discord = MemoryStore()
    alerts.save_address(only_discord, "discord", WEBHOOK)
    with httpx.Client() as http:
        assert alerts.send(db, only_discord, http, alerts.health_alerts(["x"], NOW)) == (0, [])
    assert db.execute("SELECT COUNT(*) FROM sent_alerts").fetchone()[0] == 0


def test_health_alerts_repeat_at_most_hourly():
    first = alerts.health_alerts(["ESI is down"], NOW)[0]
    same_hour = alerts.health_alerts(["ESI is down"], NOW + timedelta(minutes=30))[0]
    next_hour = alerts.health_alerts(["ESI is down"], NOW + timedelta(hours=1))[0]
    assert first.key == same_hour.key != next_hour.key


def test_setup_asks_at_a_hidden_prompt_and_never_prints_the_address(monkeypatch, capsys):
    found = MemoryStore()
    monkeypatch.setattr(cli, "default_store", lambda: found)
    answers = iter([WEBHOOK, ""])
    assert cli._cmd_alerts_setup(None, ask=lambda _prompt: next(answers)) == 0
    assert alerts.addresses(found) == {"discord": WEBHOOK}
    out = capsys.readouterr().out
    assert "Discord webhook address: saved." in out
    assert WEBHOOK not in out
    answers = iter(["-", "not a url"])
    assert cli._cmd_alerts_setup(None, ask=lambda _prompt: next(answers)) == 1
    assert alerts.addresses(found) == {}
