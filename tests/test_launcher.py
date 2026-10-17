import http.server
import os
import socket
import subprocess
import sys
import threading
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from eve_assistant import cli, launcher
from eve_assistant.config import PROJECT_ROOT, Config
from eve_assistant.web import create_app

LOCAL = Config(client_id="x", callback_url="http://localhost:1/c", contact=None, mode="local")


@pytest.fixture
def listener():
    """Something listening on a free local port: yields the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    yield sock.getsockname()[1]
    sock.close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _web_server(body: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802  # the name http.server calls
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_listening(listener):
    assert launcher.listening(listener)
    assert not launcher.listening(_free_port())


@pytest.mark.parametrize(("body", "ours"), [("<title>Today · EVE Assistant</title>", True),
                                            ("<title>Something else</title>", False)])  # fmt: skip
def test_dashboard_running_only_for_this_dashboard(body, ours):
    server = _web_server(body)
    try:
        assert launcher.dashboard_running(server.server_address[1]) is ours
    finally:
        server.shutdown()
        server.server_close()
    assert not launcher.dashboard_running(_free_port())


def test_dashboard_pages_carry_the_marker(tmp_path):
    with TestClient(create_app(tmp_path / "t.sqlite3"), base_url="http://127.0.0.1") as client:
        assert launcher.MARKER in client.get("/").text


def test_browser_opens_once_listening(listener):
    opened = []
    launcher.open_when_listening(listener, timeout=5, opener=opened.append).join()
    assert opened == [f"http://127.0.0.1:{listener}/"]


def test_browser_not_opened_if_it_never_starts():
    opened = []
    launcher.open_when_listening(_free_port(), timeout=0.3, opener=opened.append).join()
    assert opened == []


def _fake_run(stdout="C:\\Desktop\\EVE Assistant.lnk\n", returncode=0, stderr=""):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)

    run.calls = calls
    return run


def test_shortcut_values_go_to_powershell_as_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    run = _fake_run()
    path = launcher.create_shortcut(run=run)
    assert path.name == "EVE Assistant.lnk"
    command, env = run.calls[0]
    assert command[0].lower().endswith("powershell.exe")
    assert sys.executable not in command[-1]  # never inside the command text
    assert env["EA_TARGET"] == sys.executable
    assert env["EA_ARGUMENTS"] == "-m eve_assistant.cli serve --open"
    assert env["EA_WORKDIR"] == str(PROJECT_ROOT)
    assert env["EA_FOLDER"] == ""  # PowerShell finds the desktop

    launcher.create_shortcut(tmp_path, port=8800, run=run)
    _command, env = run.calls[1]
    assert env["EA_ARGUMENTS"].endswith("--open --port 8800")
    assert env["EA_FOLDER"] == str(tmp_path.resolve())


def test_shortcut_problems_are_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    with pytest.raises(launcher.LauncherError, match="only made on Windows"):
        launcher.create_shortcut(run=_fake_run())
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    missing = tmp_path / "missing"
    with pytest.raises(launcher.LauncherError, match="Folder not found"):
        launcher.create_shortcut(missing, run=_fake_run())
    failing = _fake_run(stdout="", returncode=1, stderr="Access denied")
    with pytest.raises(launcher.LauncherError, match="Access denied"):
        launcher.create_shortcut(tmp_path, run=failing)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_real_shortcut_on_windows(tmp_path):
    path = launcher.create_shortcut(tmp_path)
    assert path == tmp_path.resolve() / "EVE Assistant.lnk"
    read_back = subprocess.run(  # noqa: S603  # fixed command; the path comes as a variable
        [launcher._powershell(), "-NoProfile", "-NonInteractive", "-Command",
         "$l = (New-Object -ComObject WScript.Shell).CreateShortcut($env:EA_PATH); "
         "Write-Output $l.TargetPath $l.Arguments $l.WorkingDirectory $l.WindowStyle"],
        env={**os.environ, "EA_PATH": str(path)},
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout.split("\n")  # fmt: skip
    target, arguments, workdir, style = (line.strip() for line in read_back[:4])
    assert target.lower() == sys.executable.lower()
    assert arguments == "-m eve_assistant.cli serve --open"
    assert workdir.lower() == str(PROJECT_ROOT).lower()
    assert style == "7"


def test_serve_open_when_already_running_only_opens_it(capsys):
    with (
        mock.patch.object(cli, "load_config", return_value=LOCAL),
        mock.patch.object(launcher, "dashboard_running", return_value=True),
        mock.patch.object(launcher, "open_in_browser") as opened,
        mock.patch("uvicorn.run") as run,
    ):
        assert cli.main(["serve", "--open"]) == 0
    opened.assert_called_once_with(8700)
    run.assert_not_called()
    assert "already running" in capsys.readouterr().out


def test_serve_open_refuses_a_port_in_use(capsys):
    with (
        mock.patch.object(cli, "load_config", return_value=LOCAL),
        mock.patch.object(launcher, "dashboard_running", return_value=False),
        mock.patch.object(launcher, "listening", return_value=True),
        mock.patch("uvicorn.run") as run,
    ):
        assert cli.main(["serve", "--open", "--port", "8701"]) == 2
    run.assert_not_called()
    assert "Something else is using port 8701" in capsys.readouterr().out


def test_serve_open_starts_it_and_opens_the_browser():
    with (
        mock.patch.object(cli, "load_config", return_value=LOCAL),
        mock.patch.object(cli, "default_store"),
        mock.patch.object(launcher, "dashboard_running", return_value=False),
        mock.patch.object(launcher, "listening", return_value=False),
        mock.patch.object(launcher, "open_when_listening") as waiting,
        mock.patch("eve_assistant.web.create_app"),
        mock.patch("uvicorn.run") as run,
    ):
        assert cli.main(["serve", "--open"]) == 0
    waiting.assert_called_once_with(8700)
    assert run.call_args.kwargs["host"] == "127.0.0.1"


def test_cli_shortcut(capsys, tmp_path):
    made = tmp_path / "EVE Assistant.lnk"
    with mock.patch.object(launcher, "create_shortcut", return_value=made) as create:
        assert cli.main(["shortcut", "--folder", str(tmp_path)]) == 0
    create.assert_called_once_with(tmp_path, 8700)
    assert f"Made {made}." in capsys.readouterr().out


def test_cli_shortcut_error_is_shown(capsys):
    problem = launcher.LauncherError("Shortcuts are only made on Windows.")
    with mock.patch.object(launcher, "create_shortcut", side_effect=problem):
        assert cli.main(["shortcut"]) == 2
    assert "only made on Windows" in capsys.readouterr().err
