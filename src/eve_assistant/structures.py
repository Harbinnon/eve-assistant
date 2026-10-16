"""Player-owned structures: where they are, looked up once and remembered.

The static data only knows NPC stations. Structures (citadels, refineries)
are looked up through ESI with the character's own token, which works only
if the character has docking access. Lookups happen during a refresh, so the
planner and dashboard never need the network to know where a structure is.

Structure names are chosen by their owners: they are stored and shown only
as labelled player-written text.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eve_assistant import snapshots
from eve_assistant.esi.client import EsiClient, EsiError, EsiRateLimited
from eve_assistant.store import utc_now

RECHECK_AFTER = timedelta(days=7)  # structures move rarely; access can change
STRUCTURE_ID_FLOOR = 1_000_000_000_000  # structure item IDs are above this


@dataclass(frozen=True)
class Structure:
    structure_id: int
    accessible: bool
    player_name: str | None
    solar_system_id: int | None


def is_structure_id(location_id: int | None) -> bool:
    return location_id is not None and location_id >= STRUCTURE_ID_FLOOR


def get(db: sqlite3.Connection, structure_id: int) -> Structure | None:
    row = db.execute("SELECT * FROM structures WHERE structure_id = ?", (structure_id,)).fetchone()
    if row is None:
        return None
    return Structure(
        structure_id=structure_id,
        accessible=bool(row["accessible"]),
        player_name=row["player_name"],
        solar_system_id=row["solar_system_id"],
    )


def system_of(db: sqlite3.Connection, location_id: int | None) -> int | None:
    """The solar system of an NPC station or a known structure, else None."""
    if location_id is None:
        return None
    row = db.execute(
        "SELECT system_id FROM sde_npc_stations WHERE station_id = ?", (location_id,)
    ).fetchone()
    if row:
        return row["system_id"]
    known = get(db, location_id)
    return known.solar_system_id if known and known.accessible else None


def referenced(db: sqlite3.Connection, character_id: int) -> set[int]:
    """Structure IDs in the character's location and assets."""
    found = set()
    location = snapshots.latest(db, character_id, "location") or {}
    if is_structure_id(location.get("structure_id")):
        found.add(location["structure_id"])
    assets = snapshots.latest(db, character_id, "assets") or []
    # Items inside ships and containers also have location_type "item"; their
    # location is another asset, not a structure, so those are excluded.
    item_ids = {a.get("item_id") for a in assets}
    for item in assets:
        location_id = item.get("location_id")
        if (
            item.get("location_type") == "item"
            and item.get("location_flag") == "Hangar"
            and is_structure_id(location_id)
            and location_id not in item_ids
        ):
            found.add(location_id)
    return found


def _needs_check(db: sqlite3.Connection, structure_id: int, now: datetime) -> bool:
    row = db.execute(
        "SELECT checked_at FROM structures WHERE structure_id = ?", (structure_id,)
    ).fetchone()
    if row is None:
        return True
    return datetime.fromisoformat(row["checked_at"]) < now - RECHECK_AFTER


def refresh(db: sqlite3.Connection, esi: EsiClient, character_id: int) -> int:
    """Look up structures this character references that are unknown or stale.
    Returns how many were looked up. Item IDs of containers inside stations can
    look like structure IDs; ESI refuses those, and they are remembered as
    inaccessible so they are not asked about again for a week."""
    now = datetime.now(UTC)
    looked_up = 0
    for structure_id in sorted(referenced(db, character_id)):
        if not _needs_check(db, structure_id, now):
            continue
        try:
            data = esi.get(f"/universe/structures/{structure_id}", character_id=character_id).data
            values = (1, str(data.get("name", ""))[:100], data.get("solar_system_id"),
                      data.get("type_id"), data.get("owner_id"))  # fmt: skip
        except EsiRateLimited:
            raise
        except EsiError:
            values = (0, None, None, None, None)
        db.execute(
            "INSERT INTO structures (structure_id, accessible, player_name, solar_system_id, "
            "type_id, owner_id, checked_at, checked_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(structure_id) DO UPDATE SET accessible = excluded.accessible, "
            "player_name = excluded.player_name, solar_system_id = excluded.solar_system_id, "
            "type_id = excluded.type_id, owner_id = excluded.owner_id, "
            "checked_at = excluded.checked_at, checked_by = excluded.checked_by",
            (structure_id, *values, utc_now(), character_id),
        )
        looked_up += 1
    db.commit()
    return looked_up
