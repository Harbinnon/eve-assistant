"""Your own losses (roadmap 4.2), from your killmails.

ESI lists the killmails a character was involved in, as IDs and hashes
(snapshot kind "killmails", scope esi-killmails.read_killmails.v1). Each new
one is fetched once (a public route; kept out of the ESI cache, as it names
the attackers) and kept only if one of your characters was the victim:
when, where, the ship, and the types and quantities aboard. Attackers are
never kept, and nor is anything from a killmail where your character was
one of the attackers.

risk.loss_chances learns from these against the trips plans rated.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from eve_assistant import goals, snapshots
from eve_assistant.esi.client import EsiClient
from eve_assistant.store import utc_now

MAX_FETCH = 50  # killmails fetched per refresh; the rest wait for the next


@dataclass(frozen=True)
class Loss:
    killmail_id: int
    character_id: int
    killed_at: str
    system_id: int
    ship_type_id: int
    items: tuple[tuple[int, int], ...]  # (type_id, quantity) aboard


def _aboard(items) -> dict[int, int]:
    """Types and quantities aboard, containers' contents included."""
    found: dict[int, int] = {}
    for item in items or []:
        if not isinstance(item, dict) or "item_type_id" not in item:
            continue
        quantity = item.get("quantity_destroyed", 0) + item.get("quantity_dropped", 0)
        found[item["item_type_id"]] = found.get(item["item_type_id"], 0) + quantity
        for type_id, inner in _aboard(item.get("items")).items():
            found[type_id] = found.get(type_id, 0) + inner
    return found


def _stamp(killmail_time: str) -> str:
    """EVE's "2026-10-10T12:00:00Z" as the "+00:00" form sessions use."""
    return datetime.fromisoformat(killmail_time.replace("Z", "+00:00")).isoformat(
        timespec="seconds"
    )


def _ours(db: sqlite3.Connection) -> set[int]:
    return {r[0] for r in db.execute("SELECT character_id FROM characters")}


def update(db: sqlite3.Connection, esi: EsiClient, character_id: int) -> int:
    """Fetch this character's new killmails and keep its losses. Returns how
    many losses were recorded. Raises EsiError."""
    listed = snapshots.latest(db, character_id, "killmails") or []
    ours = _ours(db)
    recorded = fetched = 0
    for entry in listed:
        if fetched >= MAX_FETCH:
            break
        if not isinstance(entry, dict) or "killmail_id" not in entry:
            continue
        killmail_id = int(entry["killmail_id"])
        if db.execute(
            "SELECT 1 FROM killmails_seen WHERE killmail_id = ?", (killmail_id,)
        ).fetchone():
            continue
        fetched += 1
        mail = esi.get(f"/killmails/{killmail_id}/{entry['killmail_hash']}", remember=False).data
        victim = mail.get("victim", {}) if isinstance(mail, dict) else {}
        now = utc_now()
        with db:
            if victim.get("character_id") in ours and victim.get("ship_type_id"):
                db.execute(
                    "INSERT OR IGNORE INTO own_losses (killmail_id, character_id, killed_at, "
                    "solar_system_id, ship_type_id, items, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (killmail_id, victim["character_id"], _stamp(mail["killmail_time"]),
                     mail["solar_system_id"], victim["ship_type_id"],
                     json.dumps(sorted(_aboard(victim.get("items")).items())), now),
                )  # fmt: skip
                recorded += 1
            db.execute("INSERT OR IGNORE INTO killmails_seen VALUES (?, ?)", (killmail_id, now))
    return recorded


def value(db: sqlite3.Connection, loss: Loss) -> float:
    """The ship and what it carried, at CCP's average prices."""
    total = goals.price_of(db, loss.ship_type_id) or 0.0
    for type_id, quantity in loss.items:
        total += (goals.price_of(db, type_id) or 0.0) * quantity
    return total


def all_losses(db: sqlite3.Connection) -> list[Loss]:
    """Every recorded loss, newest first."""
    return [
        Loss(
            r["killmail_id"],
            r["character_id"],
            r["killed_at"],
            r["solar_system_id"],
            r["ship_type_id"],
            tuple(tuple(i) for i in json.loads(r["items"])),
        )
        for r in db.execute("SELECT * FROM own_losses ORDER BY killed_at DESC")
    ]
