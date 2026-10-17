"""Starting the dashboard from a desktop shortcut.

`eve-assistant serve --open` opens the dashboard in the default browser once
it is listening, or only opens it if it is already running. On Windows,
`eve-assistant shortcut` puts a shortcut to that on the desktop.

The shortcut runs this package with Python rather than eve-assistant.exe,
so a running dashboard never locks the .exe that pip replaces on reinstall.
Its window starts minimised; closing it stops the dashboard.
"""

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path

import httpx

from eve_assistant.config import PROJECT_ROOT

DEFAULT_PORT = 8700
SHORTCUT_NAME = "EVE Assistant.lnk"
READY_TIMEOUT = 120.0  # seconds to wait for the dashboard to start listening
MARKER = "EVE Assistant"  # in every dashboard page's title

# Values reach PowerShell as environment variables, never inside the
# command text, so no path or argument is ever parsed as code.
MAKE_SHORTCUT = (
    "$folder = if ($env:EA_FOLDER) { $env:EA_FOLDER } "
    "else { [Environment]::GetFolderPath('Desktop') }; "
    "$path = Join-Path $folder $env:EA_NAME; "
    "$link = (New-Object -ComObject WScript.Shell).CreateShortcut($path); "
    "$link.TargetPath = $env:EA_TARGET; "
    "$link.Arguments = $env:EA_ARGUMENTS; "
    "$link.WorkingDirectory = $env:EA_WORKDIR; "
    "$link.Description = 'Start the EVE Assistant dashboard and open it'; "
    "$link.WindowStyle = 7; "  # minimised
    "$link.Save(); "
    "Write-Output $path"
)


class LauncherError(Exception):
    pass


def url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def listening(port: int) -> bool:
    """True if anything accepts connections on 127.0.0.1 at the port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def dashboard_running(port: int) -> bool:
    """True if this tool's dashboard already answers on the port."""
    if not listening(port):
        return False
    try:
        response = httpx.get(url(port), timeout=10.0, follow_redirects=True)
    except httpx.HTTPError:
        return False
    return response.status_code == 200 and MARKER in response.text


def open_in_browser(port: int) -> None:
    webbrowser.open(url(port))


def open_when_listening(
    port: int,
    timeout: float = READY_TIMEOUT,
    opener: Callable[[str], object] = webbrowser.open,
) -> threading.Thread:
    """Open the dashboard in the browser as soon as it accepts connections.
    Runs in a background thread, since the server runs in the foreground."""

    def wait_then_open() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if listening(port):
                opener(url(port))
                return
            time.sleep(0.25)

    thread = threading.Thread(target=wait_then_open, daemon=True)
    thread.start()
    return thread


def _powershell() -> str:
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return str(Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def create_shortcut(
    folder: Path | None = None, port: int = DEFAULT_PORT, run: Callable = subprocess.run
) -> Path:
    """Make a shortcut that starts the dashboard and opens it, on the desktop
    unless another folder is given. Returns where it was saved."""
    if sys.platform != "win32":
        raise LauncherError(
            "Shortcuts are only made on Windows. Start the dashboard with "
            "`eve-assistant serve --open` instead."
        )
    if folder is not None and not folder.is_dir():
        raise LauncherError(f"Folder not found: {folder}")
    arguments = "-m eve_assistant.cli serve --open"
    if port != DEFAULT_PORT:
        arguments += f" --port {port}"
    env = {
        **os.environ,
        "EA_FOLDER": str(folder.resolve()) if folder else "",
        "EA_NAME": SHORTCUT_NAME,
        "EA_TARGET": sys.executable,
        "EA_ARGUMENTS": arguments,
        "EA_WORKDIR": str(PROJECT_ROOT),
    }
    result = run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", MAKE_SHORTCUT],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise LauncherError(f"Couldn't make the shortcut: {detail}")
    return Path(result.stdout.strip().splitlines()[-1])
