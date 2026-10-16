import socket
import threading
import time

import httpx
import pytest

from eve_assistant.esi.callback import wait_for_code
from eve_assistant.esi.sso import SSOError


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(callback_url, state, query):
    outcome = {}

    def target():
        try:
            outcome["code"] = wait_for_code(callback_url, state, timeout=10)
        except SSOError as exc:
            outcome["error"] = str(exc)

    thread = threading.Thread(target=target)
    thread.start()
    for _ in range(50):
        try:
            response = httpx.get(f"{callback_url}?{query}", timeout=2)
            break
        except httpx.ConnectError:
            time.sleep(0.05)
    thread.join(10)
    return outcome, response


def test_returns_the_code_when_state_matches():
    url = f"http://127.0.0.1:{_free_port()}/callback"
    outcome, response = _run(url, "good-state", "code=abc&state=good-state")
    assert outcome == {"code": "abc"}
    assert response.status_code == 200


def test_rejects_a_mismatched_state():
    url = f"http://127.0.0.1:{_free_port()}/callback"
    outcome, response = _run(url, "good-state", "code=abc&state=forged")
    assert "did not match" in outcome["error"]
    assert response.status_code == 400


def test_refuses_non_local_callback():
    with pytest.raises(SSOError, match="localhost"):
        wait_for_code("https://example.com/callback", "s", timeout=1)
