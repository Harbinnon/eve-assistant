"""Estimating ore mining yield from ship, modules, implants and skills.

Values come from the static data; this module only holds the rules that
connect them (which skill a ship bonus scales with). Not modelled yet, and
reported as such: mining crystals, drones, fleet boosts, rigs and stacking
penalties. The estimate is rough by design; the mining ledger corrects it
once real sessions are recorded (phase 3).
"""

import math
import sqlite3
from dataclasses import dataclass, field

from eve_assistant import skills, snapshots
from eve_assistant.skills import Reason

MINING_SKILLS = ("Mining", "Astrogeology")  # each carries miningAmountBonus per level

# Ship bonus attributes that scale with a skill level, and the skill.
PER_LEVEL_YIELD = {
    "miningFrigatesBonusOreMiningYield": "Mining Frigate",
    "miningBargeBonusOreMiningYield": "Mining Barge",
    "exhumersBonusOreMiningYield": "Exhumers",
    "expeditionFrigateBonusOreMiningYield": "Expedition Frigates",
}
PER_LEVEL_DURATION = {"exhumersBonusOreMiningDuration": "Exhumers"}
ROLE_YIELD = "shipRoleBonusOreMiningYield"  # flat percent
ROLE_DURATION = "shipRoleBonusOreMiningDuration"  # flat percent (negative = faster)
SHIP_MULTIPLIER = "miningAmountMultiplier"
HOLD_ATTRIBUTES = ("generalMiningHoldCapacity", "specialOreHoldCapacity")
MINING_GROUPS = ("Mining Laser", "Strip Miner", "Frequency Mining Laser")


class MiningError(Exception):
    pass


@dataclass(frozen=True)
class ModuleYield:
    type_id: int
    name: str
    m3_per_cycle: float
    cycle_seconds: float

    @property
    def m3_per_minute(self) -> float:
        return self.m3_per_cycle * 60 / self.cycle_seconds


@dataclass(frozen=True)
class MiningEstimate:
    ship_type_id: int
    ship_name: str
    modules: tuple[ModuleYield, ...]
    hold_m3: float | None
    multipliers: dict[str, float]  # what scaled the base yield, for "why?"
    reasons: tuple[Reason, ...] = field(default_factory=tuple)

    @property
    def m3_per_minute(self) -> float:
        return sum(m.m3_per_minute for m in self.modules)

    @property
    def minutes_to_fill_hold(self) -> float | None:
        if not self.hold_m3 or not self.m3_per_minute:
            return None
        return self.hold_m3 / self.m3_per_minute


def attributes(db: sqlite3.Connection, type_id: int) -> dict[str, float]:
    return {
        row["name"]: row["value"]
        for row in db.execute(
            "SELECT d.name, a.value FROM sde_type_attributes a "
            "JOIN sde_dogma_attributes d USING (attribute_id) WHERE a.type_id = ?",
            (type_id,),
        )
    }


def skill_level(db: sqlite3.Connection, character: skills.CharacterSkills, name: str) -> int:
    row = db.execute(
        "SELECT type_id FROM sde_types WHERE name = ? AND published = 1", (name,)
    ).fetchone()
    trained = character.skills.get(row["type_id"]) if row else None
    if trained is None:
        return 0
    return trained.active_level if character.is_alpha else trained.trained_level


def _is_mining_module(db: sqlite3.Connection, type_id: int) -> bool:
    marks = ",".join("?" * len(MINING_GROUPS))
    row = db.execute(
        f"SELECT 1 FROM sde_types t JOIN sde_groups g USING (group_id) "  # noqa: S608
        f"WHERE t.type_id = ? AND g.name IN ({marks})",
        (type_id, *MINING_GROUPS),
    ).fetchone()
    return row is not None


def estimate(
    db: sqlite3.Connection,
    character: skills.CharacterSkills,
    ship_type_id: int,
    fitted_type_ids: list[int],
    implant_type_ids: list[int] = (),
) -> MiningEstimate:
    """Ore mining yield for a ship with these fitted items (modules, upgrades)."""
    if db.execute("SELECT 1 FROM sde_dogma_attributes LIMIT 1").fetchone() is None:
        raise MiningError("Static data needs reloading. Run `eve-assistant sde-update --force`.")
    ship = attributes(db, ship_type_id)
    reasons: list[Reason] = []
    yield_factor = ship.get(SHIP_MULTIPLIER, 1.0)
    multipliers: dict[str, float] = {}
    if not math.isclose(yield_factor, 1.0):
        multipliers["ship"] = yield_factor

    for name in MINING_SKILLS:
        level = skill_level(db, character, name)
        row = db.execute(
            "SELECT type_id FROM sde_types WHERE name = ? AND published = 1", (name,)
        ).fetchone()
        bonus = attributes(db, row["type_id"]).get("miningAmountBonus", 0.0) if row else 0.0
        factor = 1 + bonus / 100 * level
        multipliers[f"{name} {level}"] = factor
        yield_factor *= factor

    duration_factor = 1.0
    for attribute, skill_name in PER_LEVEL_YIELD.items():
        if attribute in ship:
            level = skill_level(db, character, skill_name)
            factor = 1 + ship[attribute] / 100 * level
            multipliers[f"{skill_name} {level}"] = factor
            yield_factor *= factor
    for attribute, skill_name in PER_LEVEL_DURATION.items():
        if attribute in ship:
            level = skill_level(db, character, skill_name)
            duration_factor *= 1 + ship[attribute] / 100 * level
    if ROLE_YIELD in ship:
        factor = 1 + ship[ROLE_YIELD] / 100
        multipliers["role bonus"] = factor
        yield_factor *= factor
    if ROLE_DURATION in ship:
        duration_factor *= 1 + ship[ROLE_DURATION] / 100

    if not skills.plan(db, character, ship_type_id).usable_now:
        reasons.append(Reason("ship_not_usable", {"ship": skills.type_name(db, ship_type_id)}))
    usable: dict[int, bool] = {}
    unusable: list[str] = []

    lasers: list[tuple[int, dict[str, float]]] = []
    for type_id in fitted_type_ids:
        if type_id not in usable:
            usable[type_id] = skills.plan(db, character, type_id).usable_now
        if not usable[type_id]:
            unusable.append(skills.type_name(db, type_id))
            continue
        attrs = attributes(db, type_id)
        if _is_mining_module(db, type_id) and "miningAmount" in attrs:
            lasers.append((type_id, attrs))
        elif "miningAmountBonus" in attrs:  # mining upgrades
            factor = 1 + attrs["miningAmountBonus"] / 100
            multipliers[skills.type_name(db, type_id)] = factor
            yield_factor *= factor
    for type_id in implant_type_ids:
        bonus = attributes(db, type_id).get("miningAmountBonus")
        if bonus:
            multipliers[skills.type_name(db, type_id)] = 1 + bonus / 100
            yield_factor *= 1 + bonus / 100

    modules = []
    for type_id, attrs in lasers:
        crit = attrs.get("miningCritChance", 0.0) * attrs.get("miningCritBonusYield", 0.0)
        modules.append(
            ModuleYield(
                type_id=type_id,
                name=skills.type_name(db, type_id),
                m3_per_cycle=attrs["miningAmount"] * yield_factor * (1 + crit),
                cycle_seconds=attrs.get("duration", 60000.0) / 1000 * duration_factor,
            )
        )
        if any(word in skills.type_name(db, type_id) for word in ("Modulated", "Deep Core")):
            reasons.append(Reason("crystals_not_modelled"))
    if unusable:
        reasons.append(Reason("modules_not_usable", {"modules": sorted(set(unusable))}))
    if not modules:
        reasons.append(Reason("no_mining_modules"))
    reasons.append(Reason("not_modelled", {"items": ["drones", "fleet boosts", "rigs"]}))

    hold = next((ship[a] for a in HOLD_ATTRIBUTES if ship.get(a)), None)
    if hold is None:
        row = db.execute(
            "SELECT capacity FROM sde_types WHERE type_id = ?", (ship_type_id,)
        ).fetchone()
        hold = row["capacity"] if row else None
    return MiningEstimate(
        ship_type_id=ship_type_id,
        ship_name=skills.type_name(db, ship_type_id),
        modules=tuple(modules),
        hold_m3=hold,
        multipliers=multipliers,
        reasons=tuple({r.code: r for r in reasons}.values()),
    )


def current_fit(db: sqlite3.Connection, character_id: int) -> tuple[int, list[int], list[int]]:
    """The character's active ship, its fitted items and its implants, from snapshots."""
    ship = snapshots.latest(db, character_id, "ship")
    assets = snapshots.latest(db, character_id, "assets") or []
    implants = snapshots.latest(db, character_id, "implants") or []
    if not ship:
        raise MiningError("No ship data yet. Run `eve-assistant snapshot`.")
    fitted = []
    for item in assets:
        flag = item.get("location_flag", "")
        if item.get("location_id") == ship.get("ship_item_id") and flag.startswith(
            ("HiSlot", "MedSlot", "LoSlot")
        ):
            fitted.extend([item["type_id"]] * max(1, item.get("quantity", 1)))
    return ship["ship_type_id"], fitted, list(implants)


def estimate_current(db: sqlite3.Connection, character_id: int) -> MiningEstimate:
    character = skills.load_character_skills(db, character_id)
    ship_type_id, fitted, implants = current_fit(db, character_id)
    return estimate(db, character, ship_type_id, fitted, implants)
