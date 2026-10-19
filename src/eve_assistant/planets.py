"""Planetary Interaction (roadmap 6.3): your colonies, their extractor timers
and what they make.

Read with the esi-planets.manage_planets.v1 scope (read-only, despite its
name). It's asked for only when switched on in config/local.toml
([esi] planets = true): a login asking for a scope the ESI application
hasn't ticked fails, for every character.

Alpha clones can't train any PI skill, so an Alpha has one planet with a
basic command centre; Omega allows up to six (Interplanetary Consolidation)
and bigger command centres (Command Center Upgrades). Without Remote Sensing
(also Omega) a planet can only be managed from its own system.

What a colony makes a day is worked out from its layout: each running
extractor at the rate EVE reports (each cycle's yield varies, and falls over
a program), then each factory in turn, basic ones first, takes what its
schematic needs from what's made, up to one batch a cycle. What's left over
is what the colony makes, valued at CCP's average prices less the customs
office's export tax: a guess, since each office's owner sets the rate.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from eve_assistant import goals, snapshots
from eve_assistant.esi.client import EsiClient

LAYOUTS = "planet_layouts"  # snapshot kind: each colony's pins, by planet ID
OMEGA_PLANETS = 6  # Interplanetary Consolidation V
CUSTOMS_TAX = 0.10  # a guess: customs office owners set their own rate
# Customs offices' base export cost a unit, by tier (raw resources are tier 0);
# the tax is this times the office's rate. Approximate.
EXPORT_BASE = {0: 5.0, 1: 500.0, 2: 9_000.0, 3: 70_000.0, 4: 1_350_000.0}
TIER_GROUPS = {1042: 1, 1034: 2, 1040: 3, 1041: 4}  # Planetary Commodities groups
RAW_CATEGORY = 42  # Planetary Resources
ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII",
         "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX")  # fmt: skip
DAY = 86_400


@dataclass(frozen=True)
class Extractor:
    product_type_id: int
    qty_per_cycle: int
    cycle_seconds: int
    installed: datetime | None
    expires: datetime | None

    @property
    def per_day(self) -> float:
        return self.qty_per_cycle * DAY / self.cycle_seconds if self.cycle_seconds else 0.0

    def running(self, now: datetime) -> bool:
        return self.expires is not None and self.expires > now

    @property
    def program_hours(self) -> float | None:
        if self.installed is None or self.expires is None:
            return None
        return (self.expires - self.installed).total_seconds() / 3600


@dataclass(frozen=True)
class Colony:
    character_id: int
    planet_id: int
    name: str
    planet_type: str
    system_id: int
    system_name: str
    upgrade_level: int
    extractors: tuple[Extractor, ...]
    factories: tuple[int, ...]  # each factory's schematic
    stored: dict[int, int]  # type ID -> units waiting in storage and launchpads

    def stopped(self, now: datetime, within_minutes: float = 0.0) -> list[Extractor]:
        """Extractors stopped by now, or by `within_minutes` from now."""
        later = now.timestamp() + within_minutes * 60
        return [e for e in self.extractors if e.expires is None or e.expires.timestamp() <= later]

    def next_stop(self, now: datetime) -> datetime | None:
        running = [e.expires for e in self.extractors if e.running(now)]
        return min(running) if running else None


def _at(text: str | None) -> datetime | None:
    if not text:
        return None
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def refresh(db: sqlite3.Connection, esi: EsiClient, character_id: int) -> None:
    """Read each colony's layout (its pins), after the colony list. Raises EsiError."""
    layouts = {}
    for colony in snapshots.latest(db, character_id, "planets") or []:
        if isinstance(colony, dict) and "planet_id" in colony:
            data = esi.get(
                f"/characters/{character_id}/planets/{colony['planet_id']}",
                character_id=character_id,
            ).data
            layouts[str(colony["planet_id"])] = {"pins": data.get("pins", [])}
    snapshots.record(db, character_id, LAYOUTS, layouts)


def planet_name(db: sqlite3.Connection, planet_id: int, system_name: str) -> str:
    row = db.execute(
        "SELECT celestial_index FROM sde_planets WHERE planet_id = ?", (planet_id,)
    ).fetchone()
    if row is None or not row[0] or row[0] > len(ROMAN):
        return f"a planet in {system_name}"
    return f"{system_name} {ROMAN[row[0] - 1]}"


def _colony(db, character_id: int, entry: dict, pins: list) -> Colony:
    system = db.execute(
        "SELECT name FROM sde_solar_systems WHERE system_id = ?", (entry.get("solar_system_id"),)
    ).fetchone()
    system_name = system["name"] if system else "?"
    extractors, factories, stored = [], [], {}
    for pin in pins:
        details = pin.get("extractor_details")
        if details and details.get("product_type_id"):
            extractors.append(
                Extractor(
                    details["product_type_id"],
                    details.get("qty_per_cycle", 0),
                    details.get("cycle_time", 0),
                    _at(pin.get("install_time")),
                    _at(pin.get("expiry_time")),
                )
            )
        elif pin.get("schematic_id"):
            factories.append(pin["schematic_id"])
        else:
            for content in pin.get("contents", []):
                stored[content["type_id"]] = stored.get(content["type_id"], 0) + content["amount"]
    return Colony(
        character_id,
        entry["planet_id"],
        planet_name(db, entry["planet_id"], system_name),
        entry.get("planet_type", "?"),
        entry.get("solar_system_id"),
        system_name,
        entry.get("upgrade_level", 0),
        tuple(extractors),
        tuple(factories),
        stored,
    )


def colonies(db: sqlite3.Connection, character_id: int) -> list[Colony]:
    layouts = snapshots.latest(db, character_id, LAYOUTS) or {}
    return [
        _colony(db, character_id, entry, layouts.get(str(entry["planet_id"]), {}).get("pins", []))
        for entry in snapshots.latest(db, character_id, "planets") or []
        if isinstance(entry, dict) and "planet_id" in entry
    ]


def _schematic(db: sqlite3.Connection, schematic_id: int):
    """(cycle seconds, inputs {type: quantity}, (output type, quantity)), or None."""
    row = db.execute(
        "SELECT cycle_seconds FROM sde_planet_schematics WHERE schematic_id = ?", (schematic_id,)
    ).fetchone()
    if row is None:
        return None
    inputs, output = {}, None
    for t in db.execute(
        "SELECT type_id, quantity, is_input FROM sde_planet_schematic_types WHERE schematic_id = ?",
        (schematic_id,),
    ):
        if t["is_input"]:
            inputs[t["type_id"]] = t["quantity"]
        else:
            output = (t["type_id"], t["quantity"])
    return (row["cycle_seconds"], inputs, output) if output else None


def tier(db: sqlite3.Connection, type_id: int) -> int:
    row = db.execute(
        "SELECT t.group_id, g.category_id FROM sde_types t JOIN sde_groups g USING (group_id) "
        "WHERE t.type_id = ?",
        (type_id,),
    ).fetchone()
    if row is None or row["category_id"] == RAW_CATEGORY:
        return 0
    return TIER_GROUPS.get(row["group_id"], 0)


def made_a_day(
    db: sqlite3.Connection, colony: Colony, now: datetime, all_running: bool = False
) -> dict[int, float]:
    """What the colony makes a day and doesn't use itself, by type: with the
    extractors running now, or as if all were (all_running)."""
    pool: dict[int, float] = {}
    for extractor in colony.extractors:
        if all_running or extractor.running(now):
            pool[extractor.product_type_id] = pool.get(extractor.product_type_id, 0.0) + (
                extractor.per_day
            )
    schematics = [s for s in (_schematic(db, f) for f in colony.factories) if s is not None]
    for cycle, inputs, (out_type, out_qty) in sorted(schematics, key=lambda s: tier(db, s[2][0])):
        batches = min([DAY / cycle] + [pool.get(t, 0.0) / q for t, q in inputs.items()])
        if batches <= 0:
            continue
        for t, q in inputs.items():
            pool[t] -= batches * q
        pool[out_type] = pool.get(out_type, 0.0) + batches * out_qty
    return {t: units for t, units in pool.items() if units > 1e-6}


def value(db: sqlite3.Connection, made: dict[int, float]) -> tuple[float, float]:
    """(ISK at CCP's average prices, the export tax on it), for units by type."""
    worth = tax = 0.0
    for type_id, units in made.items():
        worth += units * (goals.price_of(db, type_id) or 0.0)
        tax += units * EXPORT_BASE[tier(db, type_id)] * CUSTOMS_TAX
    return worth, tax


def a_day(db: sqlite3.Connection, colony: Colony, now: datetime, all_running=False) -> float:
    """ISK a day after export tax."""
    worth, tax = value(db, made_a_day(db, colony, now, all_running))
    return max(0.0, worth - tax)


def waiting(db: sqlite3.Connection, colony: Colony) -> float:
    """What the goods waiting in storage and launchpads are worth, after export tax."""
    worth, tax = value(db, {t: float(u) for t, u in colony.stored.items()})
    return max(0.0, worth - tax)


@dataclass(frozen=True)
class Timer:
    """A colony at a glance: when its extractors stop and what it makes."""

    planet: str
    stops: datetime | None  # the next running extractor's end
    stopped: int  # extractors not running
    stopped_at: datetime | None  # when the last of those stopped
    a_day: float  # ISK a day after export tax, as it runs now
    waiting: float  # goods waiting to be exported


def timers(db: sqlite3.Connection, character_id: int, now: datetime) -> list[Timer]:
    found = []
    for colony in colonies(db, character_id):
        stopped = colony.stopped(now)
        ends = [e.expires for e in stopped if e.expires is not None]
        found.append(
            Timer(
                colony.name,
                colony.next_stop(now),
                len(stopped),
                max(ends) if ends else None,
                a_day(db, colony, now),
                waiting(db, colony),
            )
        )
    return found
