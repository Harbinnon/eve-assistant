"""Selling ore from station hangars: where the character is docked (a quick
job) or elsewhere, hauled there first with an owned ship.
"""

import sqlite3

from eve_assistant import (
    corrections,
    freelance,
    ore,
    pace,
    risk,
    routes,
    signals,
    skills,
    snapshots,
)
from eve_assistant.activities import base, hauling
from eve_assistant.activities import projects as project_steps
from eve_assistant.activities.base import (
    Context,
    PlanItem,
)
from eve_assistant.skills import Reason


def hangar_candidates(
    db, esi, character_id, ships, net_worth, highsec_only, minutes, notes
) -> list[PlanItem]:
    """Ore in station hangars: where the character is docked (quick jobs) and in
    other NPC stations it can reach (travel there, then sell). Hauls use whichever
    owned ship can carry the load."""
    location = snapshots.latest(db, character_id, "location") or {}
    here_system = location.get("solar_system_id")
    docked_at = location.get("station_id") or base.accessible_structure(
        db, location.get("structure_id")
    )
    here_location = docked_at or location.get("structure_id")
    active = next((s for s in ships if s.is_active), None)
    active_value = active.value if active else 0.0
    sale = corrections.correction(db, character_id, "sale_price")
    per_jump = corrections.minutes_per_jump(db, character_id)
    a_jump = per_jump[0]
    assets = snapshots.latest(db, character_id, "assets") or []
    stacks: dict[int, dict[int, int]] = {}
    for item in assets:
        if item.get("location_flag") == "Hangar" and base.is_ore(db, item["type_id"]):
            by_type = stacks.setdefault(item["location_id"], {})
            by_type[item["type_id"]] = by_type.get(item["type_id"], 0) + item.get("quantity", 1)
    items = []
    for station_id, by_type in stacks.items():
        place = base.place(db, station_id)
        if place is None:
            notes.append(Reason("ore_in_structure_skipped"))
            continue
        station = {"system_id": place[0], "name": place[1]}
        kind_of_place = "structure" if place[2] else "station"
        if station_id == docked_at:
            travel_path = [here_system]
        else:
            travel_path = routes.jumps_between(db, here_system, station["system_id"], highsec_only)
            if travel_path is None:
                continue
        travel_jumps = len(travel_path) - 1
        travel_minutes = 0.0 if station_id == docked_at else travel_jumps * a_jump + 2.0
        for type_id, quantity in by_type.items():
            analysis = ore.analyse(
                db, esi, character_id, type_id, quantity, station_id, highsec_only, per_jump
            )
            fitting = []
            multi_trip = []
            for option in analysis.options:
                if not option.instant:
                    continue
                hauler, switch = None, 0.0
                if option.jumps and option.hauled_m3 > 0:
                    picked = hauling.pick_hauler(ships, station_id, here_location, option)
                    if picked is None:
                        trips = hauling.multi_trip_haul(ships, station_id, here_location, option)
                        if trips is not None:
                            multi_trip.append((option, *trips))
                        continue
                    hauler, switch = picked
                total = travel_minutes + switch + option.active_minutes
                if total <= minutes:
                    fitting.append((option, hauler, switch, total))
                elif hauler is not None:
                    multi_trip.append((option, hauler, switch, 1))  # too long: a project
            for option, hauler, _switch, total in fitting[:2]:  # best, and the next best
                assessed = risk.Assessment("very_low")
                if travel_jumps == 0 and option.jumps == 0:
                    rating = risk.rate("very_low", 0.0, net_worth)  # docked: nothing exposed
                else:
                    assessed = risk.assess(db, travel_path)
                    carrier = hauler or active
                    stake = hauler.value + option.isk if hauler is not None else active_value
                    payout = signals.hull_payout(db, carrier.type_id if carrier else None)
                    rating = risk.rate(assessed.band, stake, net_worth, payout)
                if station_id == docked_at:
                    where = ""
                elif travel_jumps == 0:
                    where = (
                        f"Dock at the {kind_of_place} in {station['name']} where it is stored, "
                        "then: "
                    )
                else:
                    where = f"Go to {station['name']} ({travel_jumps} jumps), then: "
                ship_note = f" (in your {hauler.name})" if hauler is not None else ""
                reasons = (*option.reasons, sale.reason(), *assessed.reasons)
                if station_id != docked_at:
                    reasons = (
                        Reason(
                            "ore_stored_elsewhere",
                            {"system": station["name"], "jumps": travel_jumps},
                        ),
                        *reasons,
                    )
                if hauler is not None:
                    reasons = (
                        Reason(
                            "hauler_chosen",
                            {
                                "ship": hauler.name,
                                "load_m3": option.hauled_m3,
                                "capacity_m3": hauler.capacity_for(option.hauled_is_ore),
                            },
                        ),
                        *reasons,
                    )
                items.append(
                    PlanItem(
                        kind="sell_hangar",
                        key=f"{station_id}:{type_id}:{option.key}",
                        title=(
                            f"{where}{analysis.type_name} x{quantity:,}: {option.title}{ship_note}"
                        ),
                        isk=option.isk * sale.factor,
                        minutes=total,
                        risk=rating,
                        reasons=reasons,
                        inputs={
                            "type_id": type_id,
                            "quantity": quantity,
                            "station_id": station_id,
                            "travel_jumps": travel_jumps,
                            "jumps": option.jumps,
                            "option": option.key,
                            "ship_item_id": hauler.item_id if hauler else None,
                            "unit_prices": option.unit_prices,
                        },
                    )
                )
            for option, hauler, switch, trips in multi_trip[:1]:
                per_trip = 2 * option.jumps * a_jump + 4.0
                total = (
                    travel_minutes
                    + switch
                    + ore.MINUTES_TO_SELL
                    + trips * per_trip
                    - option.jumps * a_jump  # no trip back after the last load
                )
                assessed = risk.assess(db, travel_path)
                stake = hauler.value + option.isk / trips
                payout = signals.hull_payout(db, hauler.type_id)
                title = (
                    f"Haul {analysis.type_name} x{quantity:,} from {station['name']} to "
                    f"{option.location} in your {hauler.name}"
                    + (f" ({trips} trips)" if trips > 1 else "")
                    + " and sell it there"
                )
                items.append(
                    project_steps.project_item(
                        finish=pace.finish_rate(db),
                        kind="haul",
                        key=f"{station_id}:{type_id}:{option.key}",
                        title=title,
                        total_minutes=total,
                        isk=option.isk * sale.factor,
                        minutes=minutes,
                        rating=risk.rate(assessed.band, stake, net_worth, payout),
                        reasons=(
                            Reason(
                                "multi_trip_haul",
                                {"trips": trips, "ship": hauler.name, "load_m3": option.hauled_m3},
                            ),  # fmt: skip
                            *option.reasons,
                            sale.reason(),
                            *assessed.reasons,
                        ),
                        inputs={
                            "type_id": type_id,
                            "quantity": quantity,
                            "station_id": station_id,
                            "trips": trips,
                            "ship_item_id": hauler.item_id,
                            "unit_prices": option.unit_prices,
                        },  # fmt: skip
                    )
                )
    return items


def _delivery_route(db, location, active, target, units_m3, highsec_only):
    """(path, jumps) to take a stack to a job's location: none if it's already
    there, or hauled by the active ship from where the character is docked."""
    stack_at, here_system, docked_at = location
    if stack_at == target[0]:
        return [target[1]], 0
    if stack_at != docked_at or active is None or active.capacity_for(False) < units_m3:
        return None
    path = routes.jumps_between(db, here_system, target[1], highsec_only)
    return (path, len(path) - 1) if path is not None else None


def delivery_candidates(
    db, character_id, ships, net_worth, highsec_only, minutes
) -> list[PlanItem]:
    """Hangar stacks a Freelance Job wants delivered: where they already are, or
    carried there by the active ship. A stack is either sold or delivered."""
    location = snapshots.latest(db, character_id, "location") or {}
    here_system = location.get("solar_system_id")
    docked_at = location.get("station_id") or base.accessible_structure(
        db, location.get("structure_id")
    )
    active = next((s for s in ships if s.is_active and s.usable), None)
    taken = freelance.committed(db, character_id)
    a_jump, jump_reason = corrections.minutes_per_jump(db, character_id)
    stacks: dict[tuple[int, int], int] = {}
    for item in snapshots.latest(db, character_id, "assets") or []:
        if item.get("location_flag") == "Hangar":
            key = (item["location_id"], item["type_id"])
            stacks[key] = stacks.get(key, 0) + item.get("quantity", 1)
    items = []
    for (stack_at, type_id), quantity in stacks.items():
        for job in freelance.delivery_jobs(db, type_id):
            units = job.units_for(quantity)
            volume = db.execute(
                "SELECT volume FROM sde_types WHERE type_id = ?", (type_id,)
            ).fetchone()
            for target_id in job.locations.get("station", []) + job.locations.get("structure", []):
                place = base.place(db, target_id)
                if place is None or units <= 0:
                    continue
                load = units * ((volume and volume["volume"]) or 0)
                route = _delivery_route(
                    db, (stack_at, here_system, docked_at), active, (target_id, place[0]), load,
                    highsec_only,
                )  # fmt: skip
                if route is None:
                    continue
                path, jumps = route
                needed = jumps * a_jump + ore.MINUTES_TO_SELL + (2.0 if jumps else 0.0)
                if needed > minutes:
                    continue
                isk = units * job.reward_per_unit
                assessed = risk.Assessment("very_low")
                if jumps:
                    assessed = risk.assess(db, path)
                    payout = signals.hull_payout(db, active.type_id if active else None)
                    stake = (active.value if active else 0.0) + isk
                    rating = risk.rate(assessed.band, stake, net_worth, payout)
                else:
                    rating = risk.rate("very_low", 0.0, net_worth)
                name = skills.type_name(db, type_id)
                items.append(
                    PlanItem(
                        kind="sell_hangar",
                        key=f"deliver:{job.job_id}:{stack_at}:{type_id}",
                        title=f"Deliver {units:,} {name} to {place[1]} for the Freelance Job "
                        f"\u201c{job.name}\u201d"
                        + ("" if job.job_id in taken else " (commit to it in game first)"),
                        isk=isk,
                        minutes=needed,
                        risk=rating,
                        reasons=(
                            Reason(
                                "freelance_delivery",
                                {
                                    "job": job.name,
                                    "reward_per_unit": job.reward_per_unit,
                                    "units": units,
                                    "place": place[1],
                                    "jumps": jumps,
                                    "committed": job.job_id in taken,
                                },
                            ),  # fmt: skip
                            *([jump_reason] if jumps else []),
                            *assessed.reasons,
                        ),
                        inputs={
                            "station_id": stack_at,
                            "type_id": type_id,
                            "quantity": units,
                            "jumps": jumps,
                            "travel_jumps": 0,
                            "freelance_job_id": job.job_id,
                            "player_job_name": job.name,
                        },  # fmt: skip
                    )
                )
                break  # one place per job
    return items


def candidates(ctx: Context) -> list[PlanItem]:
    return hangar_candidates(
        ctx.db, ctx.esi, ctx.character_id, ctx.ships, ctx.net_worth, ctx.highsec_only,
        ctx.minutes, ctx.notes,
    ) + delivery_candidates(
        ctx.db, ctx.character_id, ctx.ships, ctx.net_worth, ctx.highsec_only, ctx.minutes
    )  # fmt: skip


def learned(db: sqlite3.Connection, character_id: int) -> list:
    """What market sales really fetch, against the prices plans expected."""
    return [corrections.correction(db, character_id, "sale_price")]
