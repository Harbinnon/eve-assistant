"""The hosted copy (roadmap 7.2): its config, the guard in front of the
dashboard, login through /callback, the log route and sender, and serve --hosted."""

import json
import re
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from eve_assistant import (
    auth,
    cli,
    config,
    gamelogs,
    logsender,
    secrets_store,
    snapshots,
    store,
    web,
)
from eve_assistant.web import create_app

# Made-up addresses: the real ones live only in the server's config.
PROXY, HOME, LISTEN, OWNER = "10.9.8.1", "10.9.8.0/24", "10.9.8.21", "123456789"
HOSTED_TOML = f"""[esi]
client_id = "abc"
callback_url = "https://eve.example.com/callback"
[app]
mode = "hosted"
[hosted]
hostname = "eve.example.com"
listen = "{LISTEN}"
proxy = "{PROXY}"
home_network = "{HOME}"
owner_discord_id = "{OWNER}"
"""
MINED = "[ 2026.10.19 03:00:00 ] (mining) You mined 1,000 units of Veldspar"
COMBAT = "[ 2026.10.19 03:00:05 ] (combat) Someone hit you"
TOKEN = "Bearer tok"  # pragma: allowlist secret


def _hosted(tmp_path, text=HOSTED_TOML):
    path = tmp_path / "local.toml"
    path.write_text(text, encoding="utf-8")
    return config.load_config(path)


def test_the_hosted_section_is_checked(tmp_path):
    hosted = _hosted(tmp_path).hosted
    assert (hosted.hostname, hosted.listen, hosted.port) == ("eve.example.com", LISTEN, 8700)
    assert hosted.public_origin == "https://eve.example.com"
    broken = [
        (HOSTED_TOML.replace(f'proxy = "{PROXY}"', 'proxy = "nginx"'), "proxy"),
        (HOSTED_TOML.replace("https://eve.example.com/callback", "http://localhost:8765/c"),
         "callback_url"),
        (HOSTED_TOML.replace(f'owner_discord_id = "{OWNER}"', ""), "owner_discord_id"),
        (HOSTED_TOML + 'dev_identity = "1"\n', "dev_identity"),
    ]  # fmt: skip
    for text, message in broken:
        with pytest.raises(config.ConfigError, match=message):
            _hosted(tmp_path, text)


@pytest.fixture
def hosted_app(tmp_path):
    db_path = tmp_path / "eve.sqlite3"
    conn = store.connect(db_path)
    snapshots.ensure_character(conn, 7, "Miner")
    conn.close()
    started, finished = [], []

    def start():
        pending = auth.PendingLogin("https://login.example/x", f"s{len(started)}", "v", time.time())
        started.append(pending)
        return pending

    def finish(pending, code):
        finished.append((pending, code))
        return "New"

    app = create_app(
        db_path,
        hosted=_hosted(tmp_path).hosted,
        web_login=SimpleNamespace(start=start, finish=finish),
        ingest_token="tok",
    )
    return SimpleNamespace(app=app, started=started, finished=finished, db_path=db_path)


def _client(app, address):
    return TestClient(app, base_url="http://eve.example.com", client=(address, 50000))


def test_a_refused_address_gets_a_quick_403(hosted_app):
    with _client(hosted_app.app, "10.9.8.160") as monitor:  # on the home network
        assert monitor.get("/characters").status_code == 403
    with _client(hosted_app.app, PROXY) as proxy:
        assert proxy.get("/characters").status_code == 403  # no sign-in identity
        assert proxy.get("/characters", headers={"X-Auth-Id": "999"}).status_code == 403
        assert proxy.get("/characters", headers={"X-Auth-Id": OWNER}).status_code == 200
        through_proxy = proxy.post("/ingest/gamelogs", headers={"X-Auth-Id": OWNER})
        assert through_proxy.status_code == 403


def test_forms_check_the_public_origin_and_login_comes_back_to_callback(hosted_app):
    signed_in = {"X-Auth-Id": OWNER}
    with _client(hosted_app.app, PROXY) as proxy:
        page = proxy.get("/characters", headers=signed_in).text
        token = re.search(r'name="token" value="([^"]+)"', page).group(1)
        plain = {**signed_in, "Origin": "http://eve.example.com"}
        assert (
            proxy.post("/characters/add", data={"token": token}, headers=plain).status_code == 403
        )
        public = {**signed_in, "Origin": "https://eve.example.com"}
        sent = proxy.post(
            "/characters/add", data={"token": token}, headers=public, follow_redirects=False
        )
        assert (sent.status_code, sent.headers["location"]) == (303, "https://login.example/x")
        assert proxy.get("/callback?code=c&state=other", headers=signed_in).status_code == 400
        state = hosted_app.started[0].state
        back = proxy.get(f"/callback?code=c1&state={state}", headers=signed_in)
        assert hosted_app.finished == [(hosted_app.started[0], "c1")]
        assert "Added New." in back.text
        again = proxy.get(f"/callback?code=c1&state={state}", headers=signed_in)
        assert again.status_code == 400  # each login is used once


def test_the_log_route_needs_its_token_and_keeps_mining_lines_only(hosted_app):
    body = {
        "file": "20261019_030000.txt",
        "listener": "Miner",
        "lines": [[12, MINED], [13, COMBAT]],
    }
    auth_header = {"Authorization": TOKEN}
    with _client(hosted_app.app, "10.9.8.50") as pc:
        assert pc.post("/ingest/gamelogs", json=body).status_code == 401
        assert pc.post("/ingest/gamelogs", json=body, headers=auth_header).json() == {"stored": 1}
        stranger = {**body, "listener": "Someone Else"}
        assert pc.post("/ingest/gamelogs", json=stranger, headers=auth_header).json() == {
            "stored": 0
        }
        escape = {**body, "file": "../secrets"}
        assert pc.post("/ingest/gamelogs", json=escape, headers=auth_header).status_code == 400
        big = pc.post("/ingest/gamelogs", content=b"x" * 1_000_001, headers=auth_header)
        assert big.status_code == 413
    conn = store.connect(hosted_app.db_path)
    rows = conn.execute("SELECT units, line_number FROM mining_cycles").fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == [(1000, 12)]


def test_crafted_lines_are_quick_and_errors_say_nothing_inside(hosted_app):
    started = time.monotonic()
    crafted = "[ 2026.10.19 03:00:00 ] (mining) " + "<" * 50_000
    assert gamelogs.parse_line(crafted, 1) == []  # too long: refused at once
    assert gamelogs.parse_line("[ 2026.10.19 03:00:00 ] (mining) " + "<" * 1900, 1) == []
    assert time.monotonic() - started < 1
    assert gamelogs.parse_line(MINED.replace("Veldspar", "<b>Veldspar</b>"), 1)[0].ore == "Veldspar"
    with _client(hosted_app.app, "10.9.8.50") as pc:
        bad = pc.post("/ingest/gamelogs", content=b"{not json", headers={"Authorization": TOKEN})
    assert bad.json() == {"error": "unreadable request"}


def test_the_log_route_is_rate_limited(hosted_app, monkeypatch):
    monkeypatch.setattr(web, "INGEST_PER_HOUR", 2)
    body = {"file": "a.txt", "listener": "Miner", "lines": []}
    with _client(hosted_app.app, "10.9.8.50") as pc:
        codes = [
            pc.post("/ingest/gamelogs", json=body, headers={"Authorization": TOKEN}).status_code
            for _ in range(3)
        ]
    assert codes == [200, 200, 429]


def test_the_sender_sends_only_new_mining_lines_of_your_characters(tmp_path):
    logs = tmp_path / "Gamelogs"
    logs.mkdir()
    header = ["-" * 20, "  Gamelog", "  Listener: Miner", "  Session Started: x", "-" * 20]
    (logs / "20261019_030000.txt").write_text("\n".join([*header, MINED, COMBAT]), "utf-8")
    other = [*header[:2], "  Listener: Someone Else", MINED]
    (logs / "20261019_040000.txt").write_text("\n".join(other), "utf-8")
    posted = []

    def handler(request):
        posted.append(json.loads(request.content))
        assert request.headers["Authorization"] == TOKEN
        return httpx.Response(200, json={"stored": 1})

    sent = tmp_path / "sent.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        first = logsender.send(http, "http://10.9.8.21:8700", "tok", logs, {"Miner"}, sent)
        second = logsender.send(http, "http://10.9.8.21:8700", "tok", logs, {"Miner"}, sent)
    assert (first.files, first.lines, first.stored) == (1, 1, 1)
    assert posted == [{"file": "20261019_030000.txt", "listener": "Miner", "lines": [[6, MINED]]}]
    assert (second.files, second.lines) == (0, 0)  # nothing new


def test_serve_hosted_exits_2_for_a_config_problem(tmp_path, monkeypatch, capsys):
    local = _hosted(tmp_path, HOSTED_TOML.replace('mode = "hosted"', 'mode = "local"'))
    monkeypatch.setattr(cli, "load_config", lambda: local)
    assert cli.main(["serve", "--hosted"]) == cli.CONFIG_PROBLEM
    assert "Configuration problem" in capsys.readouterr().err
    good = _hosted(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: good)

    def no_key():
        raise secrets_store.SecretStoreError("EVE_ASSISTANT_SECRET_KEY isn't set")

    monkeypatch.setattr(cli, "default_store", no_key)
    assert cli.main(["serve", "--hosted"]) == cli.CONFIG_PROBLEM
    assert "EVE_ASSISTANT_SECRET_KEY isn't set" in capsys.readouterr().err
