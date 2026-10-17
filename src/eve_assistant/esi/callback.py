"""A one-shot local web server that catches EVE's login redirect.

It listens on the callback address from config (localhost only), waits for a
single request, checks the state value, and hands back the one-time code.
"""

import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from eve_assistant.esi.sso import SSOError

_PAGE = """<!doctype html><meta charset="utf-8"><title>EVE Assistant</title>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem">
<h1 style="font-size:1.4rem">{heading}</h1><p>{text}</p></body>"""


@dataclass
class _Result:
    code: str | None = None
    error: str | None = None


def _handler_for(path: str, state: str, result: _Result, done: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802  # name required by BaseHTTPRequestHandler
            url = urlsplit(self.path)
            if url.path != path:
                self.send_error(404)
                return
            params = parse_qs(url.query)
            if params.get("state", [None])[0] != state:
                result.error = "The login response did not match this login attempt."
            elif "code" not in params:
                result.error = "EVE's login page did not return a code (login cancelled?)."
            else:
                result.code = params["code"][0]
            ok = result.code is not None
            body = _PAGE.format(
                heading="Logged in" if ok else "Login failed",
                text="You can close this tab and go back to the terminal."
                if ok
                else (result.error or "Unknown error."),
            ).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, format, *args):  # noqa: A002  # silence default request logging
            return

    return Handler


def wait_for_code(callback_url: str, state: str, timeout: float = 300) -> str:
    url = urlsplit(callback_url)
    if url.hostname not in ("localhost", "127.0.0.1"):
        raise SSOError("Local login only works with a localhost callback URL.")
    result = _Result()
    done = threading.Event()
    server = HTTPServer(("127.0.0.1", url.port or 80), _handler_for(url.path, state, result, done))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if not done.wait(timeout):
            raise SSOError("Timed out waiting for the EVE login page.")
    finally:
        server.shutdown()
        server.server_close()
    if result.error:
        raise SSOError(result.error)
    assert result.code is not None  # noqa: S101  # set whenever error is not
    return result.code
