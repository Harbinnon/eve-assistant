"""Mining: a session sized to the time left, for each ore worth mining here,
with the learned correction to the mining speed.
"""

import math
import sqlite3

from eve_assistant import (
    corrections,
    fittings,
    fleet,
    freelance,
    mining,
    ore,
    risk,
    routes,
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

# Used only when the mining ledger shows nothing mined in this system yet.
# Common in highsec belts; an assumption, reported as such.
DEFAULT_ORES = ("Veldspar", "Scordite", "Pyroxeres", "Plagioclase")


def _ore_choices(db, character_id, system_id, named: list[str] | None, notes) -> list[int]:
    if named:
        return [skills.find_type(db, name) for name in named]
    ledger = snapshots.latest(db, character_id, "mining_ledger") or []
    mined = list(
        dict.fromkeys(e["type_id"] for e in ledger if e.get("solar_system_id") == system_id)
    )
    if mined:
        notes.append(Reason("ores_from_mining_ledger"))
        return mined
    notes.append(Reason("ores_assumed", {"ores": list(DEFAULT_ORES)}))
    found = []
    for name in DEFAULT_ORES:
        row = db.execute(
            "SELECT type_id FROM sde_types WHERE name = ? AND published = 1", (name,)
        ).fetchone()
        if row:
            found.append(row["type_id"])
    return found


def _first_station(db: sqlite3.Connection, system_id: int | None) -> int | None:
    row = db.execute(
        "SELECT station_id FROM sde_npc_stations WHERE system_id = ? ORDER BY station_id LIMIT 1",
        (system_id,),
    ).fetchone()
    return row["station_id"] if row else None


def mining_candidates(
    db, esi, character_id, ships, minutes, net_worth, ore_names, notes
) -> list[PlanItem]:
    """A mining session in each owned, flyable ship that has mining modules fitted,
    after the time it takes to get into that ship."""
    try:
        character = skills.load_character_skills(db, character_id)
    except skills.SkillsError as exc:
        notes.append(Reason("mining_unavailable", {"why": str(exc)}))
        return []
    implants = snapshots.latest(db, character_id, "implants") or []
    location = snapshots.latest(db, character_id, "location") or {}
    learned = corrections.correction(db, character_id, "mining_rate")
    items = []
    any_miner = False
    for ship in ships:
        board = fleet.boarding(db, character_id, ship)
        if board is None:
            continue
        try:
            estimate = mining.estimate(db, character, ship.type_id, list(ship.fitted), implants)
        except mining.MiningError as exc:
            notes.append(Reason("mining_unavailable", {"why": str(exc)}))
            return []
        if estimate.m3_per_minute <= 0:
            continue
        any_miner = True
        fit_note = _better_saved_fit(db, character_id, character, ship, estimate, implants)
        system_id = location.get("solar_system_id") if board.jumps == 0 else ship.system_id
        docked = location.get("station_id") or base.accessible_structure(
            db, location.get("structure_id")
        )
        if ship.is_active and docked and board.jumps == 0:
            station_id = docked
        else:
            station_id = _first_station(db, system_id)
        if station_id is None:
            notes.append(Reason("no_station_to_sell"))
            continue
        here = base.system_info(db, system_id)
        time_left = minutes - board.minutes
        mining_minutes = max(0.0, time_left - ore.MINUTES_TO_SELL)
        # The estimate is from the static data; the correction scales it to how
        # fast this player really mines. The raw figure is what gets recorded.
        rate = estimate.m3_per_minute * learned.factor
        hold_minutes = estimate.hold_m3 / rate if estimate.hold_m3 else None
        if hold_minutes:
            trips = math.ceil(mining_minutes / (hold_minutes + MINUTES_PER_HOLD_TRIP))
            mining_minutes = max(0.0, mining_minutes - trips * MINUTES_PER_HOLD_TRIP)
        if mining_minutes < MIN_MINING_MINUTES:
            continue
        volume = rate * mining_minutes
        securities = [here["security_status"]] if here else []
        if board.jumps:
            route_low = routes.lowest_security(db, board.path)
            if route_low is not None:
                securities.append(route_low)
        band = risk.band_for_security(
            min(securities) if securities else None, here["region"] if here else None
        )
        prefix = (
            f"Board your {ship.name} ({board.minutes:.0f} min), then mine"
            if board.needs_switch
            else "Mine"
        )
        for type_id in _ore_choices(db, character_id, system_id, ore_names, notes):
            unit = db.execute(
                "SELECT volume FROM sde_types WHERE type_id = ?", (type_id,)
            ).fetchone()
            if not unit or not unit["volume"]:
                continue
            quantity = int(volume / unit["volume"])
            if quantity <= 0:
                continue
            analysis = ore.analyse(db, esi, character_id, type_id, quantity, station_id)
            here_options = [o for o in analysis.options if o.instant and o.jumps == 0]
            if not here_options:
                continue
            best = here_options[0]
            hold_value = (best.isk / volume) * (estimate.hold_m3 or volume) if volume else 0.0
            items.append(
                PlanItem(
                    kind="mining",
                    key=f"{ship.item_id}:{type_id}",
                    title=f"{prefix} {analysis.type_name} for {mining_minutes:.0f} min, then "
                    f"{best.title[0].lower()}{best.title[1:]}",
                    isk=best.isk,
                    minutes=minutes,
                    risk=risk.rate(band, ship.value + hold_value, net_worth),
                    reasons=(
                        Reason(
                            "mining_estimate",
                            {"m3_per_minute": estimate.m3_per_minute, "ship": estimate.ship_name},
                        ),
                        learned.reason(),
                        *([fit_note] if fit_note else []),
                        *estimate.reasons,
                    ),
                    inputs={
                        "type_id": type_id,
                        "quantity": quantity,
                        "mining_minutes": mining_minutes,
                        "sell_option": best.key,
                        "ship_item_id": ship.item_id,
                        "board_minutes": board.minutes,
                    },
                )
            )
            job_item = _for_a_job(db, character_id, items[-1], system_id)
            if job_item is not None:
                items.append(job_item)
    if not any_miner:
        notes.append(Reason("no_mining_ship"))
    return items


def _for_a_job(db, character_id, plain: PlanItem, system_id) -> PlanItem | None:
    """The same session mined for the best open Freelance Job wanting this ore
    here, if it pays more. Valued at the job's pay only for the units it takes
    (whether the ore stays yours isn't certain), market value for the rest."""
    type_id, quantity = plain.inputs["type_id"], plain.inputs["quantity"]
    job = freelance.mining_job(db, type_id, system_id)
    if job is None or quantity <= 0:
        return None
    counted = job.units_for(quantity)
    unit_sale = plain.isk / quantity
    isk = counted * job.reward_per_unit + (quantity - counted) * unit_sale
    if isk <= plain.isk:
        return None
    taken = job.job_id in freelance.committed(db, character_id)
    title = plain.title.split(", then ")[0]
    title += f" for the Freelance Job \u201c{job.name}\u201d" + (
        "" if taken else " (commit to it in game first)"
    )
    return PlanItem(
        kind="mining",
        key=f"{plain.key}:job:{job.job_id}",
        title=title,
        isk=isk,
        minutes=plain.minutes,
        risk=plain.risk,
        reasons=(
            *plain.reasons[:2],
            Reason(
                "freelance_mining",
                {
                    "job": job.name,
                    "reward_per_unit": job.reward_per_unit,
                    "counted": counted,
                    "quantity": quantity,
                    "ore_value": counted * unit_sale,
                    "committed": taken,
                },
            ),  # fmt: skip
            *plain.reasons[2:],
        ),
        inputs={**plain.inputs, "freelance_job_id": job.job_id, "player_job_name": job.name},
    )


def _better_saved_fit(db, character_id, character, ship, current, implants) -> Reason | None:
    """A saved fit for this hull that would mine noticeably more than what is
    fitted now, as a what-if note; None if there isn't one."""
    best = None
    for fit in fittings.for_hull(db, character_id, ship.type_id):
        try:
            with_fit = mining.estimate(db, character, ship.type_id, list(fit.modules), implants)
        except mining.MiningError:
            return None
        if with_fit.m3_per_minute >= current.m3_per_minute * fittings.BETTER_BY and (
            best is None or with_fit.m3_per_minute > best[1].m3_per_minute
        ):
            best = (fit, with_fit)
    if best is None:
        return None
    fit, with_fit = best
    return Reason(
        "better_saved_fit",
        {
            "fit_name": fit.name,  # player-written
            "now": current.m3_per_minute,
            "with_fit": with_fit.m3_per_minute,
            "missing": fittings.missing_modules(
                db, character_id, fit, ship.item_id, ship.location_id
            ),
        },
    )


def candidates(ctx: Context) -> list[PlanItem]:
    return mining_candidates(
        ctx.db, ctx.esi, ctx.character_id, ctx.ships, ctx.minutes, ctx.net_worth,
        ctx.ore_names, ctx.notes,
    )  # fmt: skip


def learned(db: sqlite3.Connection, character_id: int) -> list[corrections.Correction]:
    """What mining has learned from this character's sessions."""
    return [corrections.correction(db, character_id, "mining_rate")]
