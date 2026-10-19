"""Nullsec basics (roadmap 6.2): who holds each system, and jump bridges.

- **Sovereignty** comes from ESI's public /sovereignty/systems: the alliance
  (or NPC faction) holding each system, as IDs only, read twice a day. A
  nullsec system held by the alliance of one of the owner's characters is
  *friendly*: still dangerous, but less so than anyone else's space
  (risk.py).
- **Jump bridges** (Ansiblex gates) belong to an alliance, and no API lists
  them for a character, so the owner pastes their alliance's list in
  Settings: one bridge a line, two system names on it, in any of the usual
  formats ("1DQ1-A » 8QT-H4", "1DQ1-A --> 8QT-H4", "1DQ1-A 8QT-H4"). Routes
  then use them both ways, a bridge counting as one jump.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta

from eve_assistant.esi.client import EsiClient

SOV_EVERY = timedelta(hours=12)
BRIDGES_SETTING = "jump_bridges"
NULLSEC = 0.0  # security status rounds to 0.0 or below


class BridgeError(Exception):
    pass


def due(db: sqlite3.Connection, now: datetime) -> bool:
    row = db.execute("SELECT MAX(fetched_at) FROM sov_systems").fetchone()
    return row[0] is None or now - datetime.fromisoformat(row[0]) >= SOV_EVERY


def _holders(data) -> list[tuple]:
    """(system, alliance, corporation, faction) from either shape ESI has used:
    {"solar_systems": [{"solar_system_id", "claim": {"alliance": {...}} or
    {"faction": {...}} or {"unclaimed": true}}]} (compatibility date 2026-08-18),
    or a flat list of {"system_id", "alliance_id", "corporation_id", "faction_id"}."""
    if isinstance(data, dict):
        found = []
        for s in data.get("solar_systems", []):
            if not isinstance(s, dict) or "solar_system_id" not in s:
                continue
            claim = s.get("claim") or {}
            alliance, faction = claim.get("alliance") or {}, claim.get("faction") or {}
            found.append((s["solar_system_id"], alliance.get("alliance_id"),
                          alliance.get("corporation_id"), faction.get("faction_id")))  # fmt: skip
        return found
    return [
        (s["system_id"], s.get("alliance_id"), s.get("corporation_id"), s.get("faction_id"))
        for s in data or []
        if isinstance(s, dict) and "system_id" in s
    ]


def refresh(db: sqlite3.Connection, esi: EsiClient, now: datetime) -> None:
    """Read who holds each system. Raises EsiError."""
    stamp = now.isoformat(timespec="seconds")
    rows = [(*held, stamp) for held in _holders(esi.get("/sovereignty/systems").data)]
    with db:
        db.execute("DELETE FROM sov_systems")
        db.executemany("INSERT OR REPLACE INTO sov_systems VALUES (?, ?, ?, ?, ?)", rows)


def own_alliances(db: sqlite3.Connection) -> set[int]:
    """The alliances the owner's characters are in."""
    found = set()
    for (payload,) in db.execute(
        "SELECT s.payload FROM snapshots s WHERE s.kind = 'public' AND s.snapshot_id = "
        "(SELECT MAX(snapshot_id) FROM snapshots WHERE character_id = s.character_id "
        " AND kind = 'public')"
    ):
        alliance = json.loads(payload).get("alliance_id")
        if alliance:
            found.add(alliance)
    return found


def friendly_systems(db: sqlite3.Connection) -> set[int]:
    """Systems held by an alliance one of the owner's characters is in."""
    alliances = own_alliances(db)
    if not alliances:
        return set()
    marks = ",".join("?" * len(alliances))
    return {
        r[0]
        for r in db.execute(
            f"SELECT system_id FROM sov_systems WHERE alliance_id IN ({marks})",  # noqa: S608
            sorted(alliances),
        )
    }


def holder(db: sqlite3.Connection, system_id: int) -> str:
    """'yours', 'npc' (an NPC faction), 'other' (another alliance) or 'none'."""
    row = db.execute(
        "SELECT alliance_id, faction_id FROM sov_systems WHERE system_id = ?", (system_id,)
    ).fetchone()
    if row is None or (row["alliance_id"] is None and row["faction_id"] is None):
        return "none"
    if row["alliance_id"] is None:
        return "npc"
    return "yours" if row["alliance_id"] in own_alliances(db) else "other"


def bridges(db: sqlite3.Connection) -> list[tuple[int, int]]:
    """The saved jump bridges, as pairs of system IDs."""
    row = db.execute("SELECT value FROM settings WHERE key = ?", (BRIDGES_SETTING,)).fetchone()
    return [tuple(pair) for pair in json.loads(row["value"])] if row else []


def bridge_links(db: sqlite3.Connection) -> dict[int, set[int]]:
    """Each bridged system and where its bridges go, both ways."""
    links: dict[int, set[int]] = {}
    for a, b in bridges(db):
        links.setdefault(a, set()).add(b)
        links.setdefault(b, set()).add(a)
    return links


def _system(db: sqlite3.Connection, word: str) -> int | None:
    row = db.execute(
        "SELECT system_id FROM sde_solar_systems WHERE name = ? COLLATE NOCASE", (word,)
    ).fetchone()
    return row[0] if row else None


def _systems_on(db: sqlite3.Connection, line: str) -> list[int]:
    """The systems named on a line, in order, each once. Arrows split words at
    their ">" or "<", and the dashes left over are trimmed off."""
    systems = []
    for word in re.split(r"[\s»<>,|@]+", line):
        name = word.strip("-")
        system = _system(db, name) if name else None
        if system is not None and system not in systems:
            systems.append(system)
    return systems


def parse_bridges(db: sqlite3.Connection, text: str) -> list[tuple[int, int]]:
    """Bridges from pasted text: the first two system names on each line.
    Raises BridgeError naming the lines it couldn't read."""
    found, unread = [], []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        systems = _systems_on(db, line)
        if len(systems) < 2:
            unread.append(str(number))
        else:
            found.append((systems[0], systems[1]))
    if unread:
        raise BridgeError(
            f"Couldn't find two system names on line{'s' if len(unread) > 1 else ''} "
            f"{', '.join(unread)}. Put one bridge on each line, e.g. 1DQ1-A » 8QT-H4."
        )
    return found


def save_bridges(db: sqlite3.Connection, text: str) -> int:
    """Replace the saved bridges with those in the pasted text. Returns how many."""
    found = sorted({tuple(sorted(pair)) for pair in parse_bridges(db, text)})
    if found:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (BRIDGES_SETTING, json.dumps(found)),
        )
    else:
        db.execute("DELETE FROM settings WHERE key = ?", (BRIDGES_SETTING,))
    db.commit()
    return len(found)


def bridges_text(db: sqlite3.Connection) -> str:
    """The saved bridges as text, one a line, for the Settings box."""
    names = dict(db.execute("SELECT system_id, name FROM sde_solar_systems"))
    return "\n".join(f"{names.get(a, a)} » {names.get(b, b)}" for a, b in bridges(db))
