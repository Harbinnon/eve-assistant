"""The ships a character owns, where they are, and what each can do.

Read from the assets snapshot: every assembled ship, its location, the
modules fitted inside it, its cargo and ore hold, and whether the character
has the skills to fly it (Alpha limits included). The planner uses this to
pick the best ship for a job and to add the cost of getting into it.
"""

import sqlite3
from dataclasses import dataclass, field

from eve_assistant import mining, ore, risk, routes, skills, snapshots, structures

MINUTES_TO_SWITCH_SHIP = 2.0  # dock or board a ship in the same station
SLOT_PREFIXES = ("HiSlot", "MedSlot", "LoSlot", "RigSlot")


@dataclass(frozen=True)
class FleetShip:
    item_id: int
    type_id: int
    name: str
    location_id: int | None
    system_id: int | None  # None when the location's system is unknown (other structures)
    is_active: bool
    usable: bool  # the character can fly it now
    cargo_m3: float
    ore_hold_m3: float
    fitted: tuple[int, ...] = field(default_factory=tuple)
    value: float = 0.0  # hull plus everything inside, at average prices

    def capacity_for(self, is_ore: bool) -> float:
        return self.cargo_m3 + (self.ore_hold_m3 if is_ore else 0.0)


@dataclass(frozen=True)
class Boarding:
    """What it takes to start a job in this ship."""

    ship: FleetShip
    minutes: float
    jumps: int
    path: list[int]

    @property
    def needs_switch(self) -> bool:
        return not self.ship.is_active


def _ore_hold(db: sqlite3.Connection, type_id: int) -> float:
    rows = db.execute(
        "SELECT a.value FROM sde_type_attributes a "
        "JOIN sde_dogma_attributes d USING (attribute_id) WHERE a.type_id = ? AND d.name IN (?, ?)",
        (type_id, *mining.HOLD_ATTRIBUTES),
    ).fetchall()
    return max((r["value"] for r in rows), default=0.0)


def _is_ship(db: sqlite3.Connection, type_id: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sde_types t JOIN sde_groups g USING (group_id) "
            "WHERE t.type_id = ? AND g.category_id = 6",
            (type_id,),
        ).fetchone()
        is not None
    )


def _system_of(db: sqlite3.Connection, location_id: int | None, character_location: dict):
    if location_id is None:
        return character_location.get("solar_system_id")
    if location_id in (
        character_location.get("station_id"),
        character_location.get("structure_id"),
    ):
        return character_location.get("solar_system_id")
    return structures.system_of(db, location_id)


def owned_ships(db: sqlite3.Connection, character_id: int) -> list[FleetShip]:
    character = skills.load_character_skills(db, character_id)
    assets = snapshots.latest(db, character_id, "assets") or []
    active = snapshots.latest(db, character_id, "ship") or {}
    location = snapshots.latest(db, character_id, "location") or {}
    here = location.get("station_id") or location.get("structure_id")

    contents: dict[int, list[dict]] = {}
    for item in assets:
        contents.setdefault(item.get("location_id"), []).append(item)

    found: dict[int, tuple[int, int | None]] = {}
    for item in assets:
        if (
            item.get("is_singleton")
            and item.get("location_flag") == "Hangar"
            and _is_ship(db, item["type_id"])
        ):
            found[item["item_id"]] = (item["type_id"], item["location_id"])
    if active.get("ship_item_id"):
        found[active["ship_item_id"]] = (active["ship_type_id"], here)

    ships = []
    usable_cache: dict[int, bool] = {}
    for item_id, (type_id, location_id) in found.items():
        if type_id not in usable_cache:
            usable_cache[type_id] = skills.plan(db, character, type_id).usable_now
        inside = contents.get(item_id, [])
        fitted = tuple(
            i["type_id"]
            for i in inside
            if str(i.get("location_flag", "")).startswith(SLOT_PREFIXES)
            for _ in range(max(1, i.get("quantity", 1)))
        )
        row = db.execute(
            "SELECT name, capacity FROM sde_types WHERE type_id = ?", (type_id,)
        ).fetchone()
        value = risk.item_value(db, type_id) + sum(
            risk.item_value(db, i["type_id"]) * i.get("quantity", 1) for i in inside
        )
        ships.append(
            FleetShip(
                item_id=item_id,
                type_id=type_id,
                name=row["name"] if row else f"ship {type_id}",
                location_id=location_id,
                system_id=_system_of(db, location_id, location),
                is_active=item_id == active.get("ship_item_id"),
                usable=usable_cache[type_id],
                cargo_m3=(row["capacity"] or 0.0) if row else 0.0,
                ore_hold_m3=_ore_hold(db, type_id),
                fitted=fitted,
                value=value,
            )
        )
    return sorted(ships, key=lambda s: (not s.is_active, s.name))


def boarding(
    db: sqlite3.Connection,
    character_id: int,
    ship: FleetShip,
    highsec_only: bool = True,
    minutes_a_jump: float = ore.MINUTES_PER_JUMP,
) -> Boarding | None:
    """Minutes and route to get into this ship from where the character is now,
    or None if it cannot be reached (unknown location, no route, can't fly it)."""
    if not ship.usable:
        return None
    location = snapshots.latest(db, character_id, "location") or {}
    here_system = location.get("solar_system_id")
    if ship.is_active:
        return Boarding(ship, 0.0, 0, [here_system])
    here = location.get("station_id") or location.get("structure_id")
    if ship.location_id == here:
        return Boarding(ship, MINUTES_TO_SWITCH_SHIP, 0, [here_system])
    if ship.system_id is None:
        return None
    path = routes.jumps_between(db, here_system, ship.system_id, highsec_only)
    if path is None:
        return None
    jumps = len(path) - 1
    minutes = jumps * minutes_a_jump + MINUTES_TO_SWITCH_SHIP + 2.0
    return Boarding(ship, minutes, jumps, path)
