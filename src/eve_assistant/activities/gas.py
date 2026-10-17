"""Gas harvesting (roadmap 5.1): a session in a ship with gas scoops fitted,
after finding a gas site (nebula), valued at the average highsec booster gas
price. Learned like mining, from EVE's mining ledger, if it records the gas.
"""

import math
import sqlite3
from dataclasses import dataclass

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


@dataclass(frozen=True)
class _Setup:
    """What every gas session of one character shares."""

    character: skills.CharacterSkills
    isk_per_m3: float
    tax: float
    learned: corrections.Correction
    sale: corrections.Correction
    per_jump: float
    location: dict


def _session(db, character_id, ship, minutes, net_worth, setup: _Setup) -> PlanItem | None:
    """A gas session in this ship, or None if it can't fit one in."""
    board = fleet.boarding(db, character_id, ship, minutes_a_jump=setup.per_jump)
    estimate = gas.estimate(db, setup.character, ship.type_id, list(ship.fitted))
    if board is None or estimate.m3_per_minute <= 0:
        return None
    rate = estimate.m3_per_minute * setup.learned.factor  # the raw figure is what's recorded
    harvest = _harvest_minutes(minutes - board.minutes - SEARCH_MINUTES, rate, estimate.hold_m3)
    if harvest < MIN_MINING_MINUTES:
        return None
    volume = rate * harvest
    _system_id, assessed = base.work_site(db, setup.location, ship, board)
    held = min(volume, estimate.hold_m3 or volume) * setup.isk_per_m3
    return PlanItem(
        kind="gas",
        key=f"{ship.item_id}:gas",
        title=f"{base.board_first(ship, board, 'find')} a gas site (nebula) and harvest gas "
        f"for {harvest:.0f} min",
        isk=volume * setup.isk_per_m3 * (1 - setup.tax) * setup.sale.factor,
        minutes=minutes,
        risk=risk.rate(
            assessed.band, ship.value + held, net_worth, signals.hull_payout(db, ship.type_id)
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
            setup.learned.reason(),
            Reason("gas_site_search", {"minutes": SEARCH_MINUTES}),
            Reason("gas_valued_as", {"isk_per_m3": setup.isk_per_m3, "tax": setup.tax}),
            setup.sale.reason(),
            *estimate.reasons,
            *assessed.reasons,
        ),
        inputs={
            "ship_item_id": ship.item_id,
            "gas_minutes": harvest,
            "m3_per_minute": estimate.m3_per_minute,
            "search_minutes": SEARCH_MINUTES,
            "isk_per_m3": setup.isk_per_m3,
            "board_minutes": board.minutes,
        },
    )


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
    setup = _Setup(
        character=character,
        isk_per_m3=isk_per_m3,
        tax=fees.sales_tax(mining.skill_level(db, character, "Accounting")),
        learned=corrections.correction(db, character_id, "gas_rate"),
        sale=corrections.correction(db, character_id, "sale_price"),
        per_jump=corrections.minutes_per_jump(db, character_id)[0],
        location=snapshots.latest(db, character_id, "location") or {},
    )
    sessions = (_session(db, character_id, s, minutes, net_worth, setup) for s in fitted_any)
    return [item for item in sessions if item is not None]


def candidates(ctx: Context) -> list[PlanItem]:
    return gas_candidates(
        ctx.db, ctx.character_id, ctx.ships, ctx.minutes, ctx.net_worth, ctx.notes
    )


def learned(db: sqlite3.Connection, character_id: int) -> list:
    """Gas harvesting speed, from the mining ledger's gas against the plans."""
    return [corrections.correction(db, character_id, "gas_rate")]
