"""Gas harvesting (roadmap 5.1): a session in a ship with gas scoops fitted,
after finding a gas site (nebula), valued at the average highsec booster gas
price. Learned like mining, from EVE's mining ledger, if it records the gas.
"""

import math
import sqlite3

from eve_assistant import (
    corrections,
    fees,
    fleet,
    gas,
    mining,
    risk,
    signals,
    skills,
    snapshots,
)
from eve_assistant.activities import base
from eve_assistant.activities.base import (
    MIN_MINING_MINUTES,
    MINUTES_PER_HOLD_TRIP,
    Context,
    PlanItem,
)
from eve_assistant.skills import Reason

SEARCH_MINUTES = 20.0  # to find a nebula: scanning it down or flying to a bookmark (a guess)


def _harvest_minutes(minutes: float, rate: float, hold_m3: float | None) -> float:
    """Minutes actually harvesting, after trips to dock and unload a full hold."""
    if hold_m3 and rate:
        trips = math.ceil(minutes / (hold_m3 / rate + MINUTES_PER_HOLD_TRIP))
        minutes -= trips * MINUTES_PER_HOLD_TRIP
    return max(0.0, minutes)


def gas_candidates(db, character_id, ships, minutes, net_worth, notes) -> list[PlanItem]:
    """A gas session in each owned, flyable ship with gas scoops fitted."""
    try:
        character = skills.load_character_skills(db, character_id)
    except skills.SkillsError:
        return []
    fitted_any = [s for s in ships if any(gas.is_gas_module(db, t) for t in s.fitted)]
    if not fitted_any:
        if mining.skill_level(db, character, gas.SKILL) > 0:
            notes.append(Reason("gas_needs_scoops"))
        return []
    isk_per_m3 = gas.isk_per_m3(db)
    if not isk_per_m3:
        notes.append(Reason("gas_unpriced"))
        return []
    tax = fees.sales_tax(mining.skill_level(db, character, "Accounting"))
    learned = corrections.correction(db, character_id, "gas_rate")
    sale = corrections.correction(db, character_id, "sale_price")
    per_jump = corrections.minutes_per_jump(db, character_id)
    location = snapshots.latest(db, character_id, "location") or {}
    items = []
    for ship in fitted_any:
        board = fleet.boarding(db, character_id, ship, minutes_a_jump=per_jump[0])
        if board is None:
            continue
        estimate = gas.estimate(db, character, ship.type_id, list(ship.fitted))
        if estimate.m3_per_minute <= 0:
            continue
        rate = estimate.m3_per_minute * learned.factor  # the raw figure is what's recorded
        harvest = _harvest_minutes(minutes - board.minutes - SEARCH_MINUTES, rate, estimate.hold_m3)
        if harvest < MIN_MINING_MINUTES:
            continue
        volume = rate * harvest
        system_id = location.get("solar_system_id") if board.jumps == 0 else ship.system_id
        here = base.system_info(db, system_id)
        assessed = risk.assess(
            db, [system_id, *(board.path if board.jumps else [])], here["region"] if here else None
        )
        held = min(volume, estimate.hold_m3 or volume) * isk_per_m3
        prefix = (
            f"Board your {ship.name} ({board.minutes:.0f} min), then find"
            if board.needs_switch
            else "Find"
        )
        items.append(
            PlanItem(
                kind="gas",
                key=f"{ship.item_id}:gas",
                title=f"{prefix} a gas site (nebula) and harvest gas for {harvest:.0f} min",
                isk=volume * isk_per_m3 * (1 - tax) * sale.factor,
                minutes=minutes,
                risk=risk.rate(
                    assessed.band,
                    ship.value + held,
                    net_worth,
                    signals.hull_payout(db, ship.type_id),
                ),
                reasons=(
                    Reason(
                        "gas_estimate",
                        {
                            "m3_per_minute": estimate.m3_per_minute,
                            "ship": estimate.ship_name,
                            "harvesters": estimate.harvesters,
                        },
                    ),
                    learned.reason(),
                    Reason("gas_site_search", {"minutes": SEARCH_MINUTES}),
                    Reason("gas_valued_as", {"isk_per_m3": isk_per_m3, "tax": tax}),
                    sale.reason(),
                    *estimate.reasons,
                    *assessed.reasons,
                ),
                inputs={
                    "ship_item_id": ship.item_id,
                    "gas_minutes": harvest,
                    "m3_per_minute": estimate.m3_per_minute,
                    "search_minutes": SEARCH_MINUTES,
                    "isk_per_m3": isk_per_m3,
                    "board_minutes": board.minutes,
                },
            )
        )
    return items


def candidates(ctx: Context) -> list[PlanItem]:
    return gas_candidates(
        ctx.db, ctx.character_id, ctx.ships, ctx.minutes, ctx.net_worth, ctx.notes
    )


def learned(db: sqlite3.Connection, character_id: int) -> list:
    """Gas harvesting speed, from the mining ledger's gas against the plans."""
    return [corrections.correction(db, character_id, "gas_rate")]
