"""The shared parts of planner activities: the candidate type, the context
every activity gets, and helpers more than one activity uses.
"""

import sqlite3
from dataclasses import dataclass, field

from eve_assistant import (
    risk,
    snapshots,
    structures,
)
from eve_assistant.esi.client import EsiClient
from eve_assistant.risk import RiskRating
from eve_assistant.skills import Reason

MINUTES_PER_HOLD_TRIP = 3.0  # dock, unload, undock between full holds
MINUTES_TO_QUEUE_SKILLS = 1.0
MIN_MINING_MINUTES = 10.0  # shorter than this is not worth undocking for
# Mining with a hauler (playing together): the miner jettisons each full hold
# into a can instead of docking; the hauler collects the cans.
JETTISON_MINUTES = 0.5  # stop and jettison a full hold
COLLECT_MINUTES = 1.0  # a hauler finding a can and taking the ore
HAUL_TRIP_MINUTES = MINUTES_PER_HOLD_TRIP + COLLECT_MINUTES  # collect, dock, unload, undock


@dataclass(frozen=True)
class PlanItem:
    kind: str  # sell_hangar, mining, courier, skill
    key: str
    title: str
    isk: float
    minutes: float
    risk: RiskRating
    reasons: tuple[Reason, ...] = field(default_factory=tuple)
    inputs: dict = field(default_factory=dict)
    new_to_you: bool = False

    @property
    def adjusted_isk(self) -> float:
        return self.isk - self.risk.expected_loss

    def details(self) -> dict:
        return {
            "reasons": [{"code": r.code, **r.detail} for r in self.reasons],
            "inputs": self.inputs,
            "risk": self.risk.as_dict(),
            "new_to_you": self.new_to_you,
        }


@dataclass
class Context:
    """What every activity gets to make its candidates for one plan."""

    db: sqlite3.Connection
    esi: EsiClient
    character_id: int
    minutes: float
    net_worth: float
    highsec_only: bool
    ships: list
    ore_names: list[str] | None
    notes: list[Reason]  # activities add notes about what they assumed
    hauled_by: str | None = None  # playing together: who collects this miner's ore


def accessible_structure(db: sqlite3.Connection, location_id: int | None) -> int | None:
    known = structures.get(db, location_id) if location_id else None
    return location_id if known is not None and known.accessible else None


def place(db: sqlite3.Connection, location_id: int):
    """(system_id, system name, is_structure) for an NPC station or an accessible
    structure; None for anything else."""
    row = db.execute(
        "SELECT st.system_id, s.name FROM sde_npc_stations st JOIN sde_solar_systems s "
        "USING (system_id) WHERE st.station_id = ?",
        (location_id,),
    ).fetchone()
    if row:
        return row["system_id"], row["name"], False
    known = structures.get(db, location_id)
    if known is None or not known.accessible or not known.solar_system_id:
        return None
    name = db.execute(
        "SELECT name FROM sde_solar_systems WHERE system_id = ?", (known.solar_system_id,)
    ).fetchone()
    return known.solar_system_id, name["name"] if name else "?", True


def selling_station(db: sqlite3.Connection, character_id: int, notes: list[Reason]) -> int | None:
    """Where to sell: where the character is docked (an NPC station, or a player
    structure it can access), or else the first NPC station in its system."""
    location = snapshots.latest(db, character_id, "location") or {}
    if accessible_structure(db, location.get("structure_id")):
        notes.append(Reason("selling_at_structure_here"))
        return location["structure_id"]
    station = location.get("station_id")
    if (
        station
        and db.execute("SELECT 1 FROM sde_npc_stations WHERE station_id = ?", (station,)).fetchone()
    ):
        return station
    row = db.execute(
        "SELECT station_id FROM sde_npc_stations WHERE system_id = ? ORDER BY station_id LIMIT 1",
        (location.get("solar_system_id"),),
    ).fetchone()
    if row:
        notes.append(Reason("selling_at_nearby_station", {"station_id": row["station_id"]}))
        return row["station_id"]
    notes.append(Reason("no_npc_station_here"))
    return None


def system_info(db: sqlite3.Connection, system_id: int | None):
    return db.execute(
        "SELECT s.name, s.security_status, r.name AS region FROM sde_solar_systems s "
        "JOIN sde_regions r USING (region_id) WHERE s.system_id = ?",
        (system_id,),
    ).fetchone()


def is_ore(db: sqlite3.Connection, type_id: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sde_types t JOIN sde_groups g USING (group_id) "
            "WHERE t.type_id = ? AND g.category_id = 25 "
            "AND EXISTS (SELECT 1 FROM sde_type_materials m WHERE m.type_id = t.type_id)",
            (type_id,),
        ).fetchone()
        is not None
    )


def work_site(db: sqlite3.Connection, location: dict, ship, board):
    """Where work in this ship happens (here, or where the ship is) and the
    risk of getting there and working: (system_id, risk.Assessment)."""
    system_id = location.get("solar_system_id") if board.jumps == 0 else ship.system_id
    here = system_info(db, system_id)
    assessed = risk.assess(
        db, [system_id, *(board.path if board.jumps else [])], here["region"] if here else None
    )
    return system_id, assessed


def board_first(ship, board, verb: str) -> str:
    """ "Board your Venture (4 min), then find" or just "Find"."""
    if board.needs_switch:
        return f"Board your {ship.name} ({board.minutes:.0f} min), then {verb}"
    return verb.capitalize()
