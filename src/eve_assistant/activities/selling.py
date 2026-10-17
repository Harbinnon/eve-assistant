"""Selling ore from station hangars: where the character is docked (a quick
job) or elsewhere, hauled there first with an owned ship.
"""

import sqlite3

from eve_assistant import (
    ore,
    pace,
    risk,
    routes,
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
    active_value = next((s.value for s in ships if s.is_active), 0.0)
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
        travel_minutes = (
            0.0 if station_id == docked_at else travel_jumps * ore.MINUTES_PER_JUMP + 2.0
        )
        for type_id, quantity in by_type.items():
            analysis = ore.analyse(
                db, esi, character_id, type_id, quantity, station_id, highsec_only
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
                if travel_jumps == 0 and option.jumps == 0:
                    rating = risk.rate("very_low", 0.0, net_worth)  # docked: nothing exposed
                else:
                    band = risk.band_for_security(routes.lowest_security(db, travel_path))
                    stake = hauler.value + option.isk if hauler is not None else active_value
                    rating = risk.rate(band, stake, net_worth)
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
                reasons = option.reasons
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
                        isk=option.isk,
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
                        },
                    )
                )
            for option, hauler, switch, trips in multi_trip[:1]:
                per_trip = 2 * option.jumps * ore.MINUTES_PER_JUMP + 4.0
                total = (
                    travel_minutes
                    + switch
                    + ore.MINUTES_TO_SELL
                    + trips * per_trip
                    - option.jumps * ore.MINUTES_PER_JUMP  # no trip back after the last load
                )
                band = risk.band_for_security(routes.lowest_security(db, travel_path))
                stake = hauler.value + option.isk / trips
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
                        isk=option.isk,
                        minutes=minutes,
                        rating=risk.rate(band, stake, net_worth),
                        reasons=(
                            Reason(
                                "multi_trip_haul",
                                {"trips": trips, "ship": hauler.name, "load_m3": option.hauled_m3},
                            ),  # fmt: skip
                            *option.reasons,
                        ),
                        inputs={
                            "type_id": type_id,
                            "quantity": quantity,
                            "station_id": station_id,
                            "trips": trips,
                            "ship_item_id": hauler.item_id,
                        },  # fmt: skip
                    )
                )
    return items


def candidates(ctx: Context) -> list[PlanItem]:
    return hangar_candidates(
        ctx.db, ctx.esi, ctx.character_id, ctx.ships, ctx.net_worth, ctx.highsec_only,
        ctx.minutes, ctx.notes,
    )  # fmt: skip


def learned(db: sqlite3.Connection, character_id: int) -> list:
    return []
