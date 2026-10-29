"""Sending mining lines from the gaming PC to the hosted copy (roadmap 7.2).

A hosted copy can't read EVE's game logs: they're on the gaming PC. This
reads them there and sends on only what the hosted copy would keep itself:
the "(mining)" lines of logs whose listener is one of the owner's characters,
with their line numbers. Nothing else in a log (combat, chat, other ships)
leaves the PC. The hosted copy checks it all again.

It sends straight to the hosted copy's address on the home network (the
proxy's sign-in can't sit in front of a command), with a token entered at a
hidden prompt (`eve-assistant send-logs --setup`) and kept in the secret
store. It's meant to run unattended (a scheduled task every 15 minutes), so:

- **Nothing new, nothing done:** a log whose size hasn't changed since the
  last run isn't even read, and with no new lines nothing touches the network.
- **The server proves itself before the token is sent.** On another network
  that happens to use the same address, some other device would answer: so
  the sender first asks for an HMAC of a fresh random challenge, keyed with
  the token. Only a server that knows the token can answer; anything else
  gets nothing, the token included.
- **Away from home it gives up fast:** a 3-second connection timeout, and the
  first failure ends the run. What wasn't sent is sent next time.
- **A character this PC doesn't know** (logged in only on the hosted copy)
  has its logs skipped, and the run says so by name.
- Each run that did or found something adds a line to var/send-logs.log, the
  only place an unattended run can be seen.
"""

import hmac
import json
import secrets
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from eve_assistant import gamelogs, hosted
from eve_assistant.config import DATA_DIR
from eve_assistant.secrets_store import SecretStore

STORE_ENTRY = "send_logs"
SENT_FILE = DATA_DIR / "sent-gamelogs.json"
LOG_FILE = DATA_DIR / "send-logs.log"
LOG_MAX_BYTES = 100_000  # then send-logs.log becomes send-logs.log.1 and a new one starts
BATCH = 2000  # lines a request, far under the hosted copy's 1 MB limit
ROUTE = "/ingest/gamelogs"
HELLO = "/ingest/hello"
TIMEOUT = httpx.Timeout(20.0, connect=3.0)  # away from home, fail in seconds


class SendError(Exception):
    pass


@dataclass
class SendReport:
    files: int = 0
    lines: int = 0
    stored: int = 0
    problems: list[str] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)  # unknown character -> logs
    unreachable: bool = False  # the hosted copy didn't answer (e.g. away from home)

    @property
    def summary(self) -> str:
        return (f"Sent {self.lines:,} mining lines from {self.files} logs "
                f"({self.stored:,} new cycles).")  # fmt: skip

    @property
    def warnings(self) -> list[str]:
        return [
            f"Skipped {n} log{'s' if n != 1 else ''} of {name}, who isn't a character on this "
            "PC: log them in here too (eve-assistant login) to send their mining."
            for name, n in sorted(self.skipped.items())
        ]


def token(store: SecretStore) -> str | None:
    return store.load().get(STORE_ENTRY)


def save_token(store: SecretStore, value: str | None) -> None:
    data = store.load()
    if value:
        data[STORE_ENTRY] = value.strip()
    else:
        data.pop(STORE_ENTRY, None)
    store.save(data)


def _progress(value) -> tuple[int, int | None]:
    """(lines sent, the file's size then) from a saved entry; older entries are a
    bare line count."""
    if isinstance(value, dict):
        lines, size = value.get("lines"), value.get("bytes")
        return (lines if isinstance(lines, int) else 0, size if isinstance(size, int) else None)
    return (value if isinstance(value, int) else 0, None)


def _sent(path: Path) -> dict[str, tuple[int, int | None]]:
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): _progress(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}


def new_lines(folder: Path, owners: set[str], sent: dict, skipped: Counter | None = None) -> list:
    """Per changed log file of one of the owners' characters: its new mining
    lines, numbered, how far it has been read, and its size before reading.
    Logs of characters this PC doesn't know are counted in `skipped`."""
    found = []
    for path in sorted(Path(folder).glob("*.txt")):
        start, size_then = sent.get(path.name, (0, None))
        size = path.stat().st_size  # before reading: lines added meanwhile are read next run
        if size == size_then:
            continue  # unchanged since the last run
        lines = gamelogs._read_lines(path)
        name = gamelogs.listener(lines)
        if name is None:  # no Listener line, so no one's: remembered, so not read again
            found.append({"file": path.name, "listener": None, "lines": [], "read": 0,
                          "bytes": size})  # fmt: skip
            continue
        if name not in owners:  # read again each run, in case they're added here later
            if skipped is not None and any(gamelogs.LINE.match(x.strip()) for x in lines):
                skipped[name] += 1
            continue
        mining = [
            [n, line]
            for n, line in enumerate(lines[start:], start + 1)
            if gamelogs.LINE.match(line.strip())
        ]
        found.append({"file": path.name, "listener": name, "lines": mining,
                      "read": max(len(lines), start), "bytes": size})  # fmt: skip
    return found


def _server_knows(http: httpx.Client, url: str, secret: str) -> bool:
    """Whether the server at `url` can prove it knows the token, without the
    token being sent: it must answer a fresh random challenge with its HMAC."""
    challenge = secrets.token_hex(16)
    response = http.get(f"{url.rstrip('/')}{HELLO}", params={"nonce": challenge}, timeout=TIMEOUT)
    response.raise_for_status()
    try:
        answer = str(response.json().get("proof", ""))
    except (ValueError, AttributeError):  # not our server's JSON: some other device
        return False
    return hmac.compare_digest(answer, hosted.proof(secret, challenge))


def _check_server(http, url: str, secret: str, report: SendReport) -> bool:
    """Whether to send: only to a server that proves it knows the token. Says
    why not in the report otherwise."""
    try:
        if _server_knows(http, url, secret):
            return True
        report.problems.append(
            f"The device at {url} couldn't prove it's your hosted copy, so nothing was sent "
            "(not even the token)."
        )
    except httpx.TransportError as exc:  # no answer: away from home, or the server is down
        report.unreachable = True
        report.problems.append(
            f"Your hosted copy at {url} didn't answer ({exc.__class__.__name__}); nothing was "
            "sent. It catches up next time, at home."
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        report.problems.append(
            f"The hosted copy at {url} doesn't offer the proof check yet (it needs v0.11.4 or "
            "later); nothing was sent."
            if code == 404
            else f"The hosted copy at {url} answered {code} to the proof check; nothing was sent."
        )
    return False


def _post_all(http, url: str, secret: str, found: dict, report: SendReport) -> None:
    """Send one log's new lines, in batches. Raises httpx.HTTPError."""
    lines = found["lines"]
    for i in range(0, len(lines), BATCH):
        response = http.post(
            f"{url.rstrip('/')}{ROUTE}",
            json={
                "file": found["file"],
                "listener": found["listener"],
                "lines": lines[i : i + BATCH],
            },  # fmt: skip
            headers={"Authorization": f"Bearer {secret}"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        report.stored += response.json().get("stored", 0)


def send(
    http: httpx.Client,
    url: str,
    secret: str,
    folder: Path,
    owners: set[str],
    sent_path: Path = SENT_FILE,
) -> SendReport:
    """Send every changed log's new mining lines. A log counts as sent only once
    the hosted copy has taken all of it, so anything not sent goes next time."""
    report = SendReport()
    sent = _sent(sent_path)
    found = new_lines(folder, owners, sent, report.skipped)
    for log in [f for f in found if not f["lines"]]:  # changed, but nothing to send
        sent[log["file"]] = (log["read"], log["bytes"])
    to_send = [f for f in found if f["lines"]]
    if to_send and not _check_server(http, url, secret, report):
        to_send = []
    for log in to_send:
        try:
            _post_all(http, url, secret, log, report)
        except (httpx.HTTPError, ValueError) as exc:
            report.problems.append(f"{log['file']}: {exc.__class__.__name__}; the rest waits.")
            break  # don't keep knocking: the next run tries again
        sent[log["file"]] = (log["read"], log["bytes"])
        report.files += 1
        report.lines += len(log["lines"])
    _save_sent(sent_path, folder, sent)
    return report


def _save_sent(sent_path: Path, folder: Path, sent: dict) -> None:
    """Remember how far each log has been sent and its size then: names taken
    from the folder itself (so logs since deleted drop out) with plain numbers,
    never anything read back from the file as it was."""
    kept = {}
    for path in sorted(Path(folder).glob("*.txt")):
        lines, size = sent.get(path.name, (0, None))
        if lines > 0 or size is not None:
            kept[path.name] = {
                "lines": int(lines),
                "bytes": int(size) if size is not None else None,
            }
    sent_path.parent.mkdir(parents=True, exist_ok=True)
    temp = sent_path.with_name(sent_path.name + ".tmp")
    temp.write_text(json.dumps(kept, indent=1), encoding="utf-8")
    temp.replace(sent_path)


def note(report: SendReport, path: Path = LOG_FILE, now: datetime | None = None) -> None:
    """Add a line to send-logs.log for a run that sent, skipped or hit something;
    quiet runs leave no trace. Only ever appended to (never read back); past
    LOG_MAX_BYTES it's moved to send-logs.log.1, replacing the one before."""
    if not (report.lines or report.problems or report.skipped):
        return
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    entry = " ".join([report.summary, *report.problems, *report.warnings])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_name(path.name + ".1"))
    with path.open("a", encoding="utf-8") as log:
        log.write(f"{stamp} {entry}\n")
