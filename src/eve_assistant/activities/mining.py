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
    signals,
    skills,
    snapshots,
)
from eve_assistant.activities import base
from eve_assistant.activities.base import (
    HAUL_TRIP_MINUTES,
    JETTISON_MINUTES,
    MIN_MINING_MINUTES,
    MINUTES_PER_HOLD_TRIP,
    Context,
    PlanItem,
)
from eve_assistant.skills import Reason

# Used only when the mining ledger shows nothing mined in this system yet.
# Common in highsec belts; an assumption, reported as such.
DEFAULT_ORES = ("Veldspar", "Scordite", "Pyroxeres", "Plagioclase")


def _mined_here(db, character_id: int, system_id) -> list[int]:
    ledger = snapshots.latest(db, character_id, "mining_ledger") or []
    return list(
        dict.fromkeys(e["type_id"] for e in ledger if e.get("solar_system_id") == system_id)
    )


def _ore_choices(
    db, character_id, system_id, named: list[str] | None, notes, partners: tuple[int, ...] = ()
) -> list[int]:
    """Ores to consider: named ones, else what the mining ledger shows was mined in
    this system (playing together, by any of the characters), else common ones."""
    if named:
        return [skills.find_type(db, name) for name in named]
    own = _mined_here(db, character_id, system_id)
    shared = [t for p in partners for t in _mined_here(db, p, system_id) if t not in own]
    mined = list(dict.fromkeys([*own, *shared]))
    if shared:
        notes.append(Reason("ores_from_partners", {"count": len(set(shared))}))
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


def _mining_minutes(time_left: float, rate: float, hold_m3: float, hauled: bool) -> float:
    """Minutes actually mining. Alone: a dock trip for each full hold and the
    sale at the end. With a hauler: a can jettisoned for each full hold, and
    stopping in time for the hauler to carry the last can in and sell it."""
    if hauled:
        mining = max(0.0, time_left - HAUL_TRIP_MINUTES - ore.MINUTES_TO_SELL)
        per_hold = JETTISON_MINUTES
    else:
        mining = max(0.0, time_left - ore.MINUTES_TO_SELL)
        per_hold = MINUTES_PER_HOLD_TRIP
    hold_minutes = hold_m3 / rate if hold_m3 else None
    if hold_minutes:
        trips = math.ceil(mining / (hold_minutes + per_hold))
        mining = max(0.0, mining - trips * per_hold)
    return mining


def _team_reasons(boost, hauled_by) -> list[Reason]:
    """Playing together: who boosts this miner, or who hauls for it."""
    if boost:
        return [Reason("team_boosted", {"booster": boost[0], "cycle_factor": boost[1]})]
    if hauled_by:
        return [Reason("team_mining", {"hauler": hauled_by})]
    return []


def _estimate_reasons(reasons, boosted: bool) -> tuple:
    """The estimate's reasons; with a booster, fleet boosts are no longer "not modelled"."""
    if not boosted:
        return tuple(reasons)
    kept = []
    for reason in reasons:
        if reason.code == "not_modelled":
            items = [i for i in reason.detail.get("items", []) if i != "fleet boosts"]
            reason = Reason("not_modelled", {**reason.detail, "items": items})
        kept.append(reason)
    return tuple(kept)


def mining_candidates(
    db,
    esi,
    character_id,
    ships,
    minutes,
    net_worth,
    ore_names,
    notes,
    hauled_by=None,
    boost=None,
    ore_partners: tuple[int, ...] = (),
) -> list[PlanItem]:
    """A mining session in each owned, flyable ship that has mining modules fitted,
    after the time it takes to get into that ship. With hauled_by, another
    character collects the ore and sells it (playing together)."""
    try:
        character = skills.load_character_skills(db, character_id)
    except skills.SkillsError as exc:
        notes.append(Reason("mining_unavailable", {"why": str(exc)}))
        return []
    implants = snapshots.latest(db, character_id, "implants") or []
    location = snapshots.latest(db, character_id, "location") or {}
    learned = corrections.correction(db, character_id, "mining_rate")
    sale = corrections.correction(db, character_id, "sale_price")
    per_jump = corrections.minutes_per_jump(db, character_id)
    items = []
    any_miner = False
    for ship in ships:
        board = fleet.boarding(db, character_id, ship, minutes_a_jump=per_jump[0])
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
        # The estimate is from the static data; the correction scales it to how
        # fast this player really mines. The raw figure is what gets recorded.
        rate = estimate.m3_per_minute * learned.factor
        if boost:  # a booster's Mining Foreman Burst shortens every cycle
            rate /= boost[1]
        mining_minutes = _mining_minutes(
            minutes - board.minutes, rate, estimate.hold_m3, hauled_by is not None
        )
        if mining_minutes < MIN_MINING_MINUTES:
            continue
        volume = rate * mining_minutes
        assessed = risk.assess(
            db, [system_id, *(board.path if board.jumps else [])], here["region"] if here else None
        )
        payout = signals.hull_payout(db, ship.type_id)
        prefix = (
            f"Board your {ship.name} ({board.minutes:.0f} min), then mine"
            if board.needs_switch
            else "Mine"
        )
        for type_id in _ore_choices(db, character_id, system_id, ore_names, notes, ore_partners):
            unit = db.execute(
                "SELECT volume FROM sde_types WHERE type_id = ?", (type_id,)
            ).fetchone()
            if not unit or not unit["volume"]:
                continue
            quantity = int(volume / unit["volume"])
            if quantity <= 0:
                continue
            analysis = ore.analyse(
                db, esi, character_id, type_id, quantity, station_id, per_jump=per_jump
            )
            here_options = [o for o in analysis.options if o.instant and o.jumps == 0]
            if not here_options:
                continue
            best = here_options[0]
            hold_value = (best.isk / volume) * (estimate.hold_m3 or volume) if volume else 0.0
            if boost:
                then = (
                    f"boosted by {boost[0]} (cycles {(1 - boost[1]) * 100:.0f}% shorter), "
                    f"passing each full hold to {boost[0]}"
                )
                together = {"hauled_by": hauled_by, "boosted_by": boost[0],
                            "cycle_factor": boost[1], "mining_system_id": system_id,
                            "sell_station_id": station_id, "m3": volume}  # fmt: skip
            elif hauled_by:
                then = f"jettisoning each full hold into a can for {hauled_by} to collect"
                together = {"hauled_by": hauled_by, "mining_system_id": system_id,
                            "sell_station_id": station_id, "m3": volume}  # fmt: skip
            else:
                then, together = f"then {best.title[0].lower()}{best.title[1:]}", {}
            items.append(
                PlanItem(
                    kind="mining",
                    key=f"{ship.item_id}:{type_id}",
                    title=f"{prefix} {analysis.type_name} for {mining_minutes:.0f} min, {then}",
                    # What sales really fetch, as learned; unit_prices keep the market's.
                    isk=best.isk * sale.factor,
                    minutes=minutes,
                    risk=risk.rate(assessed.band, ship.value + hold_value, net_worth, payout),
                    reasons=(
                        Reason(
                            "mining_estimate",
                            {"m3_per_minute": estimate.m3_per_minute, "ship": estimate.ship_name},
                        ),
                        learned.reason(),
                        sale.reason(),
                        *assessed.reasons,
                        *([fit_note] if fit_note else []),
                        *_estimate_reasons(estimate.reasons, boosted=bool(boost)),
                        *_team_reasons(boost, hauled_by),
                    ),
                    inputs={
                        "type_id": type_id,
                        "quantity": quantity,
                        "mining_minutes": mining_minutes,
                        "sell_option": best.key,
                        "ship_item_id": ship.item_id,
                        "board_minutes": board.minutes,
                        "unit_prices": best.unit_prices,
                        **together,
                    },
                )
            )
            job_item = None if hauled_by else _for_a_job(db, character_id, items[-1], system_id)
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
        ctx.ore_names, ctx.notes, hauled_by=ctx.hauled_by, boost=ctx.boost,
        ore_partners=ctx.ore_partners,
    )  # fmt: skip


def learned(db: sqlite3.Connection, character_id: int) -> list[corrections.Correction]:
    """What mining has learned from this character's sessions."""
    return [corrections.correction(db, character_id, "mining_rate")]
