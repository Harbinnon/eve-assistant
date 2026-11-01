"""The PC's side of asking an AI assistant (design 6.6, stage 1).

Claude Code and the Claude desktop app start a local MCP server as a command
and talk to it over its input and output, one JSON message a line.
`eve-assistant mcp` is that command: it passes each message on to the hosted
copy's /mcp over the home network, with the token from this PC's secret
store, and passes the answers back. It reads nothing in the messages beyond
what it needs to pass them on, so the tools are always whatever the server
offers.

Like the log sender, it first asks the server to prove it knows the token
(an HMAC of a fresh random challenge, /mcp/hello), so on another network that
happens to use the same address the token is never sent to whatever answers.
"""

import hmac
import json
import secrets
from collections.abc import Callable, Iterable

import httpx

from eve_assistant import hosted
from eve_assistant.secrets_store import SecretStore

STORE_ENTRY = "mcp"
TIMEOUT = httpx.Timeout(60.0, connect=3.0)  # a tool can take a while; the network can't
SERVER_ERROR = -32000  # JSON-RPC: an error on the server's side
PARSE_ERROR = -32700
ACCEPT = "application/json, text/event-stream"


class RelayError(Exception):
    pass


def token(store: SecretStore) -> str | None:
    return store.load().get(STORE_ENTRY)


def save_token(store: SecretStore, value: str | None) -> None:
    data = store.load()
    if value is None:
        data.pop(STORE_ENTRY, None)
    else:
        data[STORE_ENTRY] = value
    store.save(data)


def check_server(http: httpx.Client, url: str, secret: str) -> None:
    """Raise RelayError unless the server at `url` proves it knows the token."""
    challenge = secrets.token_hex(16)
    try:
        response = http.get(f"{url}{hosted.MCP}/hello", params={"nonce": challenge},
                            timeout=TIMEOUT)  # fmt: skip
    except httpx.HTTPError as exc:
        raise RelayError(
            f"Can't reach the hosted copy at {url} ({exc.__class__.__name__}). "
            "Is this PC on the home network?"
        ) from None
    proof = response.json().get("proof", "") if response.status_code == 200 else ""
    if not hmac.compare_digest(str(proof), hosted.proof(secret, challenge)):
        raise RelayError(
            f"The server at {url} couldn't prove it knows the assistant's token, so it "
            "wasn't sent. Check [mcp] url in config/local.toml and the token "
            "(eve-assistant mcp --setup)."
        )


def _ids(message) -> list:
    """The IDs of the requests in a message (notifications have none)."""
    items = message if isinstance(message, list) else [message]
    return [m["id"] for m in items if isinstance(m, dict) and "id" in m]


def _errors(message, text: str, code: int = SERVER_ERROR) -> list[str]:
    return [
        json.dumps({"jsonrpc": "2.0", "id": i, "error": {"code": code, "message": text}})
        for i in _ids(message)
    ]


def _bodies(response: httpx.Response) -> list[str]:
    """The JSON messages in an answer: a JSON body, or an event stream's data."""
    if "text/event-stream" not in response.headers.get("content-type", ""):
        return [response.text]
    found, data = [], []
    for line in [*response.text.splitlines(), ""]:
        if line.startswith("data:"):
            data.append(line[5:].strip())
        elif not line and data:
            found.append("\n".join(data))
            data = []
    return found


def forward(http: httpx.Client, url: str, secret: str, line: str, state: dict) -> list[str]:
    """Pass one message on; return the lines to answer with (none for a notification)."""
    try:
        message = json.loads(line)
    except ValueError:
        return [json.dumps({"jsonrpc": "2.0", "id": None,
                            "error": {"code": PARSE_ERROR, "message": "Parse error"}})]  # fmt: skip
    headers = {"Authorization": f"Bearer {secret}", "Accept": ACCEPT,
               "Content-Type": "application/json"}  # fmt: skip
    if state.get("protocol"):
        headers["MCP-Protocol-Version"] = state["protocol"]
    if state.get("session"):
        headers["Mcp-Session-Id"] = state["session"]
    try:
        response = http.post(f"{url}{hosted.MCP}", content=line.encode(), headers=headers,
                             timeout=TIMEOUT)  # fmt: skip
    except httpx.HTTPError as exc:
        return _errors(message, f"The hosted copy didn't answer ({exc.__class__.__name__}).")
    state["session"] = response.headers.get("mcp-session-id") or state.get("session")
    if response.status_code == 202 or not response.content:
        return []
    try:
        answers = [json.loads(body) for body in _bodies(response)]
    except ValueError:
        return _errors(message, f"The hosted copy answered {response.status_code}, not JSON.")
    for answer in answers:
        version = (
            answer.get("result", {}).get("protocolVersion") if isinstance(answer, dict) else None
        )
        if isinstance(version, str):
            state["protocol"] = version
    return [json.dumps(a, separators=(",", ":")) for a in answers]


def run(
    http: httpx.Client, url: str, secret: str, lines: Iterable[str], write: Callable[[str], None]
) -> None:
    """Check the server, then pass every message on until the input ends."""
    check_server(http, url, secret)
    state: dict = {}
    for line in lines:
        if line.strip():
            for answer in forward(http, url, secret, line, state):
                write(answer)
