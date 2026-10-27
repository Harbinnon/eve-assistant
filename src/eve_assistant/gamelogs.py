"""Reading mining cycles from EVE's local game logs (local mode, optional).

The mining ledger only totals ore per day; the game log has a line per
mining cycle, with a timestamp. That gives real yield per minute for a
session. EVE writes the logs to Documents/EVE/logs/Gamelogs; the folder is
set in config/local.toml ([logs] gamelogs_dir) and nothing is read unless it
is set.

Privacy: a log is read only if its listener is one of the owner's
characters, and only "(mining)" lines are parsed. Nothing else in the log
(combat, other ships, notices) is kept.
"""

import codecs
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from eve_assistant.store import utc_now

LINE = re.compile(r"^\[ (\d{4})\.(\d\d)\.(\d\d) (\d\d):(\d\d):(\d\d) \] \(mining\) (.*)$")
TAG = re.compile(r"<[^<>]*>")  # a tag never holds "<", so each match stays short
MAX_LINE = 2000  # characters; a real mining line is a few hundred at most
MINED = re.compile(
    r"^You mined ([\d,.]+) units of (.+?)(?: with a lost residue of ([\d,.]+) units)?$"
)
CRITICAL = re.compile(r"^Critical mining success! You mined an additional ([\d,.]+) units of (.+)$")
RESIDUE = re.compile(r"^Additional ([\d,.]+) units depleted from asteroid as residue$")
LISTENER = re.compile(r"^\s*Listener:\s*(.+?)\s*$")
UTF16_MARKS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)


@dataclass(frozen=True)
class Cycle:
    at: str
    kind: str  # mined, critical, residue
    ore: str | None
    units: int
    line_number: int


@dataclass
class ImportReport:
    files_read: int = 0
    cycles_added: int = 0
    skipped_files: int = 0  # not one of the owner's characters, or no listener


def _units(text: str) -> int:
    return int(text.replace(",", "").replace(".", ""))


def _read_lines(path: Path) -> list[str]:
    """Complete lines only: EVE may be halfway through writing the last one."""
    raw = path.read_bytes()
    encoding = "utf-16" if raw[:2] in UTF16_MARKS else "utf-8"
    text = raw.decode(encoding, errors="replace")
    lines = text.splitlines()
    if lines and not text.endswith(("\n", "\r")):
        lines.pop()
    return lines


def listener(lines: list[str]) -> str | None:
    """The character a log belongs to, from its header."""
    for line in lines[:6]:
        match = LISTENER.match(line)
        if match:
            return match.group(1)
    return None


def parse_line(line: str, line_number: int) -> list[Cycle]:
    """Mining facts in one log line; empty for any other line (and for an
    over-long one: lines can arrive from the log sender)."""
    if len(line) > MAX_LINE:
        return []
    match = LINE.match(line.rstrip("\r\n"))
    if not match:
        return []
    year, month, day, hour, minute, second, body = match.groups()
    at = datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
    stamp = at.isoformat() + "+00:00"
    text = TAG.sub("", body).strip()
    if found := MINED.match(text):
        ore, units = found.group(2).strip(), _units(found.group(1))
        cycles = [Cycle(stamp, "mined", ore, units, line_number)]
        if found.group(3):
            cycles.append(Cycle(stamp, "residue", None, _units(found.group(3)), line_number))
        return cycles
    if found := CRITICAL.match(text):
        ore, units = found.group(2).strip(), _units(found.group(1))
        return [Cycle(stamp, "critical", ore, units, line_number)]
    if found := RESIDUE.match(text):
        return [Cycle(stamp, "residue", None, _units(found.group(1)), line_number)]
    return []


def _owner_ids(db: sqlite3.Connection) -> dict[str, int]:
    rows = db.execute("SELECT character_id, name FROM characters")
    return {r["name"]: r["character_id"] for r in rows}


def _type_ids(db: sqlite3.Connection, names: set[str]) -> dict[str, int]:
    if not names:
        return {}
    marks = ",".join("?" * len(names))
    rows = db.execute(
        f"SELECT name, type_id FROM sde_types WHERE name IN ({marks})",  # noqa: S608  # placeholders only
        sorted(names),
    )
    return {r["name"]: r["type_id"] for r in rows}


def import_folder(db: sqlite3.Connection, folder: Path | str) -> ImportReport:
    """Read new mining lines from every log in the folder."""
    report = ImportReport()
    folder = Path(folder)
    if not folder.is_dir():
        return report
    owners = _owner_ids(db)
    for path in sorted(folder.glob("*.txt")):
        state = db.execute(
            "SELECT listener, lines_read FROM gamelog_files WHERE file_name = ?", (path.name,)
        ).fetchone()
        if state is not None and state["listener"] not in owners:
            report.skipped_files += 1  # not the owner's (yet): don't read it
            continue
        lines = _read_lines(path)
        name = state["listener"] if state is not None else listener(lines)
        start = state["lines_read"] if state is not None else 0
        character_id = owners.get(name or "")
        read = start
        if character_id is None:
            report.skipped_files += 1
        elif len(lines) > start:
            report.files_read += 1
            report.cycles_added += _store(db, character_id, path.name, lines, start)
            read = len(lines)
        db.execute(
            "INSERT INTO gamelog_files (file_name, listener, lines_read, read_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(file_name) DO UPDATE SET "
            "lines_read = excluded.lines_read, read_at = excluded.read_at",
            (path.name, name, read, utc_now()),
        )
    db.commit()
    return report


def _store(db, character_id: int, file_name: str, lines: list[str], start: int) -> int:
    return _store_numbered(db, character_id, file_name, list(enumerate(lines[start:], start + 1)))


SENT_FILE = re.compile(r"^[\w.-]{1,100}\.txt$")


def store_sent(db: sqlite3.Connection, file_name: str, listener_name: str, lines: list) -> int:
    """Mining lines the gaming PC's log sender sent to the hosted copy, as
    [[line number, text], ...]. Kept only for a listener who is one of the
    owner's characters, and only lines that parse as mining cycles; anything
    else is dropped, as when reading the folder. Returns cycles added."""
    if not isinstance(file_name, str) or not SENT_FILE.match(file_name):
        raise ValueError("file must be a game log's file name")
    character_id = _owner_ids(db).get(listener_name)
    if character_id is None:
        return 0
    numbered = [(int(n), str(text)) for n, text in lines]
    added = _store_numbered(db, character_id, file_name, numbered)
    last = max((n for n, _text in numbered), default=0)
    db.execute(
        "INSERT INTO gamelog_files (file_name, listener, lines_read, read_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(file_name) DO UPDATE SET "
        "lines_read = MAX(lines_read, excluded.lines_read), read_at = excluded.read_at",
        (file_name, listener_name, last, utc_now()),
    )
    db.commit()
    return added


def _store_numbered(db, character_id: int, file_name: str, numbered: list) -> int:
    cycles = [c for n, line in numbered for c in parse_line(line, n)]
    types = _type_ids(db, {c.ore for c in cycles if c.ore})
    added = 0
    for c in cycles:
        cursor = db.execute(
            "INSERT OR IGNORE INTO mining_cycles "
            "(character_id, at, kind, ore, type_id, units, file_name, line_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                character_id,
                c.at,
                c.kind,
                c.ore,
                types.get(c.ore),
                c.units,
                file_name,
                c.line_number,
            ),  # fmt: skip
        )
        added += cursor.rowcount
    return added


def session_mining(db: sqlite3.Connection, character_id: int, start: str, end: str) -> dict | None:
    """Mining in a time window from the logs, or None when there are no lines.
    Yield per minute is given over the whole window and over the time actually
    mining (first to last cycle), since a session also has travel and docking."""
    rows = db.execute(
        "SELECT c.at, c.kind, c.ore, c.type_id, c.units, t.volume FROM mining_cycles c "
        "LEFT JOIN sde_types t USING (type_id) "
        "WHERE c.character_id = ? AND c.at >= ? AND c.at <= ? ORDER BY c.at",
        (character_id, start, end),
    ).fetchall()
    if not rows:
        return None
    by_ore: dict[str, dict] = {}
    residue = 0
    cycles = 0
    for r in rows:
        if r["kind"] == "residue":
            residue += r["units"]
            continue
        cycles += r["kind"] == "mined"
        ore = by_ore.setdefault(
            r["ore"], {"ore": r["ore"], "type_id": r["type_id"], "units": 0, "m3": 0.0}
        )
        ore["units"] += r["units"]
        if r["volume"] is not None:
            ore["m3"] += r["units"] * r["volume"]
    mined_at = [datetime.fromisoformat(r["at"]) for r in rows if r["kind"] != "residue"]
    window = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60
    active = (max(mined_at) - min(mined_at)).total_seconds() / 60 if mined_at else 0.0
    m3 = sum(o["m3"] for o in by_ore.values())
    return {
        "ores": sorted(by_ore.values(), key=lambda o: -o["units"]),
        "cycles": cycles,
        "residue_units": residue,
        "m3": m3,
        "first_cycle": min(mined_at).isoformat() if mined_at else None,
        "last_cycle": max(mined_at).isoformat() if mined_at else None,
        "active_minutes": active,
        "m3_per_minute": m3 / window if window > 0 else None,
        "m3_per_active_minute": m3 / active if active > 0 else None,
    }
