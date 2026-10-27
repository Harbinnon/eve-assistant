"""Sending mining lines from the gaming PC to the hosted copy (roadmap 7.2).

A hosted copy can't read EVE's game logs: they're on the gaming PC. This
reads them there and sends on only what the hosted copy would keep itself:
the "(mining)" lines of logs whose listener is one of the owner's characters,
with their line numbers. Nothing else in a log (combat, chat, other ships)
leaves the PC. The hosted copy checks it all again.

It sends straight to the hosted copy's address on the home network (the
proxy's sign-in can't sit in front of a command), with a token entered at a
hidden prompt (`eve-assistant send-logs --setup`) and kept in the secret
store. What has been sent is remembered per log file, so each run sends only
new lines.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from eve_assistant import gamelogs
from eve_assistant.config import DATA_DIR
from eve_assistant.secrets_store import SecretStore

STORE_ENTRY = "send_logs"
SENT_FILE = DATA_DIR / "sent-gamelogs.json"
BATCH = 2000  # lines a request, far under the hosted copy's 1 MB limit
ROUTE = "/ingest/gamelogs"


class SendError(Exception):
    pass


@dataclass
class SendReport:
    files: int = 0
    lines: int = 0
    stored: int = 0
    problems: list[str] = field(default_factory=list)


def token(store: SecretStore) -> str | None:
    return store.load().get(STORE_ENTRY)


def save_token(store: SecretStore, value: str | None) -> None:
    data = store.load()
    if value:
        data[STORE_ENTRY] = value.strip()
    else:
        data.pop(STORE_ENTRY, None)
    store.save(data)


def _sent(path: Path) -> dict[str, int]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def new_lines(folder: Path, owners: set[str], sent: dict[str, int]) -> list[dict]:
    """Per log file of one of the owners' characters: its new mining lines,
    numbered, and how far it has been read."""
    found = []
    for path in sorted(Path(folder).glob("*.txt")):
        lines = gamelogs._read_lines(path)
        name = gamelogs.listener(lines)
        if name not in owners or len(lines) <= sent.get(path.name, 0):
            continue
        start = sent.get(path.name, 0)
        mining = [
            [n, line]
            for n, line in enumerate(lines[start:], start + 1)
            if gamelogs.LINE.match(line.strip())
        ]
        found.append({"file": path.name, "listener": name, "lines": mining, "read": len(lines)})
    return found


def send(
    http: httpx.Client,
    url: str,
    secret: str,
    folder: Path,
    owners: set[str],
    sent_path: Path = SENT_FILE,
) -> SendReport:
    """Send every log's new mining lines. A file counts as sent only once the
    hosted copy has taken all of it, so a failure is simply sent again later."""
    report = SendReport()
    sent = _sent(sent_path)
    for found in new_lines(folder, owners, sent):
        lines = found["lines"]
        try:
            for i in range(0, max(len(lines), 1), BATCH):
                part = lines[i : i + BATCH]
                if not part:
                    break
                response = http.post(
                    f"{url.rstrip('/')}{ROUTE}",
                    json={"file": found["file"], "listener": found["listener"], "lines": part},
                    headers={"Authorization": f"Bearer {secret}"},
                )
                response.raise_for_status()
                report.stored += response.json().get("stored", 0)
        except httpx.HTTPError as exc:
            report.problems.append(f"{found['file']}: {exc.__class__.__name__}")
            continue
        sent[found["file"]] = found["read"]
        report.files += 1
        report.lines += len(lines)
    sent_path.parent.mkdir(parents=True, exist_ok=True)
    temp = sent_path.with_name(sent_path.name + ".tmp")
    temp.write_text(json.dumps(sent, indent=1), encoding="utf-8")
    temp.replace(sent_path)
    return report
