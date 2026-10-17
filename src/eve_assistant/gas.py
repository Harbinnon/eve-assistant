"""Gas harvesting estimates (roadmap 5.1), from the static data.

A gas cloud scoop or harvester takes `miningAmount` m3 every `duration`. A
ship adds a flat role bonus to the yield (shipRoleBonusGasHarvestingYield,
+100% on the Venture) and per-level bonuses to the cycle time (the Venture's
5% a level of Mining Frigate). Each level of Gas Cloud Harvesting allows one
more active harvester (EVE University); Alpha stops at the level CCP's clone
grades give (3), and the ship's turret slots limit it too.

Gas sites (nebulae) are cosmic signatures: they have to be scanned down, and
the Venture can't fit a probe launcher, so finding one takes a scanning ship
or a bookmark. Which gas a site holds depends on the site, so gas is valued
at the average CCP price of the highsec booster gases (Mykoserocin), per m3.
Mining skills and upgrades don't affect gas.
"""

import sqlite3
from dataclasses import dataclass, field

from eve_assistant import mining, skills
from eve_assistant.skills import Reason

GAS_GROUPS = ("Gas Cloud Scoops", "Gas Cloud Harvesters")
SKILL = "Gas Cloud Harvesting"
ROLE_YIELD = "shipRoleBonusGasHarvestingYield"
ROLE_DURATION = "shipRoleBonusGasHarvesterDuration"
# Ship bonus attributes that shorten the cycle per level of a skill, and the skill.
PER_LEVEL_DURATION = {
    "miningFrigateBonusGasCloudHarvestingDuration": "Mining Frigate",
    "expeditionFrigateBonusGasHarvestingDuration": "Expedition Frigates",
    "miningBargeBonusGasHarvestingDuration": "Mining Barge",
    "exhumersBonusGasHarvestingDuration": "Exhumers",
}
HOLD_ATTRIBUTES = ("specialGasHoldCapacity", "generalMiningHoldCapacity")
HIGHSEC_GAS = "Mykoserocin"  # the booster gases highsec nebulae hold


@dataclass(frozen=True)
class GasEstimate:
    ship_name: str
    harvesters: int  # active, after the skill and slot limits
    m3_per_minute: float
    hold_m3: float | None
    reasons: tuple[Reason, ...] = field(default_factory=tuple)


def is_gas_module(db: sqlite3.Connection, type_id: int) -> bool:
    row = db.execute(
        "SELECT 1 FROM sde_types t JOIN sde_groups g USING (group_id) "
        "WHERE t.type_id = ? AND g.name IN (?, ?)",
        (type_id, *GAS_GROUPS),
    ).fetchone()
    return row is not None


def estimate(
    db: sqlite3.Connection,
    character: skills.CharacterSkills,
    ship_type_id: int,
    fitted_type_ids: list[int],
) -> GasEstimate:
    """Gas harvesting yield for a ship with these fitted items."""
    ship = mining.attributes(db, ship_type_id)
    reasons: list[Reason] = []
    yield_factor = 1 + ship.get(ROLE_YIELD, 0.0) / 100
    duration_factor = 1 + ship.get(ROLE_DURATION, 0.0) / 100
    for attribute, skill_name in PER_LEVEL_DURATION.items():
        if attribute in ship:
            level = mining.skill_level(db, character, skill_name)
            duration_factor *= 1 + ship[attribute] / 100 * level
    scoops = [
        (type_id, mining.attributes(db, type_id))
        for type_id in fitted_type_ids
        if is_gas_module(db, type_id) and skills.plan(db, character, type_id).usable_now
    ]
    allowed = min(mining.skill_level(db, character, SKILL), int(ship.get("turretSlotsLeft", 8)))
    scoops.sort(key=lambda s: -s[1].get("miningAmount", 0) / s[1].get("duration", 1))
    if len(scoops) > allowed:
        reasons.append(Reason("gas_skill_limit", {"fitted": len(scoops), "active": allowed}))
    used = scoops[:allowed]
    per_minute = sum(
        attrs.get("miningAmount", 0.0) * yield_factor * 60
        / (attrs.get("duration", 60000.0) / 1000 * duration_factor)
        for _type_id, attrs in used
    )  # fmt: skip
    if not used:
        reasons.append(Reason("no_gas_modules"))
    hold = next((ship[a] for a in HOLD_ATTRIBUTES if ship.get(a)), None)
    return GasEstimate(
        ship_name=skills.type_name(db, ship_type_id),
        harvesters=len(used),
        m3_per_minute=per_minute,
        hold_m3=hold,
        reasons=tuple(reasons),
    )


def isk_per_m3(db: sqlite3.Connection) -> float | None:
    """The average CCP price per m3 of the highsec booster gases (uncompressed)."""
    rows = db.execute(
        "SELECT t.volume, COALESCE(p.average_price, p.adjusted_price) AS price "
        "FROM sde_types t JOIN market_prices p USING (type_id) "
        "WHERE t.name LIKE ? AND t.name NOT LIKE 'Compressed %' AND t.published = 1 "
        "AND t.volume > 0",
        (f"% {HIGHSEC_GAS}",),
    ).fetchall()
    values = [r["price"] / r["volume"] for r in rows if r["price"]]
    return sum(values) / len(values) if values else None
