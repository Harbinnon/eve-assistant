"""Mining boosts (roadmap 8.5): a Mining Foreman Burst shortening the fleet's
mining cycles, and how strong it is.

The rules are EVE's; the numbers come from CCP's static data. A burst charge
carries the base value (Mining Laser Optimization: -15% mining cycle time).
The burst module multiplies it (warfareBuff1Value: 1.0 for a Burst I, 1.25 for
a II), and so does each of these on the booster's side:

- the Mining Director skill (commandStrengthBonus a level),
- an industrial command ship (shipBonusICS2 a level of Industrial Command
  Ships),
- an Industrial Core fitted (assumed running while boosting; the static data
  gives a flat bonus, which may also scale with a skill in game),
- a Mining Foreman Mindlink plugged in (mindlinkBonus).

The strongest burst of a kind applies, and a running burst uses one charge a
cycle. Not modelled yet: the Efficiency charge's critical hits, the
booster's own drones, and range (the booster is assumed beside the miner).
"""

import math
import sqlite3
from dataclasses import dataclass

from eve_assistant import fleet, goals, mining, skills, snapshots

BURST_GROUP = "Command Burst"  # every kind of burst
MINING_BURST = "Mining Foreman Burst"  # the ones that boost mining, by name
CHARGE_GROUP = "Mining Foreman Burst Charges"
CYCLE_BUFF = 24  # Mining Laser Optimization: mining modules' capacitor and duration
DIRECTOR = "Mining Director"
COMMAND_SHIPS = "Industrial Command Ships"
STRONGEST_CYCLE_CHANGE = -90.0  # a guard against odd data: cycles never vanish


@dataclass(frozen=True)
class Boost:
    """What a booster's ship gives the miner it boosts."""

    ship: fleet.FleetShip
    burst: str  # the strongest burst module fitted
    charge_type_id: int
    charge: str
    base: float  # the charge's value before strength, percent (e.g. -15)
    strength: float  # the multiplier on it
    factors: tuple[tuple[str, float], ...]  # (what, multiplier), for "why?"
    cycle_minutes: float  # the burst's own cycle: one charge each
    charges_loaded: int
    charge_price: float | None

    @property
    def cycle_change(self) -> float:
        """Percent change to the miner's cycle time, e.g. -29.3."""
        return max(self.base * self.strength, STRONGEST_CYCLE_CHANGE)

    @property
    def cycle_factor(self) -> float:
        return 1 + self.cycle_change / 100

    def charges_needed(self, minutes: float) -> int:
        return math.ceil(minutes / self.cycle_minutes) if self.cycle_minutes > 0 else 0


def _in_group(db: sqlite3.Connection, group_name: str) -> dict[int, str]:
    return dict(
        db.execute(
            "SELECT t.type_id, t.name FROM sde_types t JOIN sde_groups g USING (group_id) "
            "WHERE g.name = ?",
            (group_name,),
        ).fetchall()
    )


def cycle_charge(db: sqlite3.Connection) -> tuple[int, str, float] | None:
    """The burst charge that shortens mining cycles: (type ID, name, base percent)."""
    for type_id, name in _in_group(db, CHARGE_GROUP).items():
        values = mining.attributes(db, type_id)
        if int(values.get("warfareBuff1ID", 0)) == CYCLE_BUFF:
            return type_id, name, values.get("warfareBuff1Multiplier", 0.0)
    return None


def _type_name(db: sqlite3.Connection, type_id: int) -> str:
    row = db.execute("SELECT name FROM sde_types WHERE type_id = ?", (type_id,)).fetchone()
    return row[0] if row else f"type {type_id}"


def _ship_factors(db, character, ship: fleet.FleetShip, implants) -> list[tuple[str, float]]:
    """Each thing on the booster's side that strengthens its bursts."""
    factors = []
    director = mining.skill_level(db, character, DIRECTOR)
    director_id = db.execute("SELECT type_id FROM sde_types WHERE name = ?", (DIRECTOR,)).fetchone()
    per_level = (
        mining.attributes(db, director_id[0]).get("commandStrengthBonus", 0.0)
        if director_id
        else 0.0
    )
    if director and per_level:
        factors.append((f"{DIRECTOR} {director}", 1 + per_level * director / 100))
    ship_bonus = mining.attributes(db, ship.type_id).get("shipBonusICS2", 0.0)
    command = mining.skill_level(db, character, COMMAND_SHIPS)
    if ship_bonus and command:
        factors.append((f"{ship.name}, {COMMAND_SHIPS} {command}", 1 + ship_bonus * command / 100))
    cores = [
        (mining.attributes(db, t).get("industrialCoreBonusMiningBurstStrength", 0.0), t)
        for t in set(ship.fitted)
    ]
    core_bonus, core = max(cores, default=(0.0, None))
    if core_bonus:
        factors.append((f"{_type_name(db, core)} (running)", 1 + core_bonus / 100))
    for implant in implants:
        bonus = mining.attributes(db, implant).get("mindlinkBonus", 0.0)
        name = _type_name(db, implant)
        if bonus and "Mining" in name:
            factors.append((name, 1 + bonus / 100))
    return factors


def boost_of(
    db, character: skills.CharacterSkills, ship: fleet.FleetShip, implants
) -> Boost | None:
    """The boost a ship gives with its mining bursts fitted, or None without any."""
    bursts = {
        t: name for t, name in _in_group(db, BURST_GROUP).items() if name.startswith(MINING_BURST)
    }
    fitted = [t for t in set(ship.fitted) if t in bursts]
    charge = cycle_charge(db)
    if not fitted or charge is None:
        return None
    values = {t: mining.attributes(db, t) for t in fitted}
    best = max(fitted, key=lambda t: values[t].get("warfareBuff1Value", 1.0))
    module = values[best].get("warfareBuff1Value", 1.0)
    factors = [(bursts[best], module), *_ship_factors(db, character, ship, implants)]
    strength = math.prod(value for _what, value in factors)
    charge_id, charge_name, base = charge
    return Boost(
        ship=ship,
        burst=bursts[best],
        charge_type_id=charge_id,
        charge=charge_name,
        base=base,
        strength=strength,
        factors=tuple(factors),
        cycle_minutes=values[best].get("duration", 60_000.0) / 60_000,
        charges_loaded=ship.fitted.count(charge_id),
        charge_price=goals.price_of(db, charge_id),
    )


def best_boost(db: sqlite3.Connection, character_id: int) -> Boost | None:
    """The strongest boost among the ships a character can fly, or None."""
    try:
        character = skills.load_character_skills(db, character_id)
        ships = fleet.owned_ships(db, character_id)
    except skills.SkillsError:
        return None
    implants = snapshots.latest(db, character_id, "implants") or []
    found = [
        boost
        for ship in ships
        if ship.usable and (boost := boost_of(db, character, ship, implants)) is not None
    ]
    return min(found, key=lambda b: (b.cycle_change, b.ship.value), default=None)
