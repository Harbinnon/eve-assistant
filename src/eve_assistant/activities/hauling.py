"""Hauling: which owned ship can carry a load, and how many trips it takes."""

import math

from eve_assistant import (
    fleet,
    ore,
)


def fits_in_ship(option: ore.Option, holds: tuple[float, float]) -> bool:
    if option.jumps == 0 or option.hauled_m3 <= 0:
        return True
    cargo, ore_hold = holds
    capacity = cargo + (ore_hold if option.hauled_is_ore else 0.0)
    return option.hauled_m3 <= capacity


def pick_hauler(
    ships: list[fleet.FleetShip], station_id: int, here_location: int | None, option: ore.Option
) -> tuple[fleet.FleetShip, float] | None:
    """The cheapest flyable ship that can carry this option's load, and the minutes to
    switch into it: the active ship, one waiting at the ore's station, or one where the
    character is now."""
    best = None
    for ship in ships:
        if not ship.usable or ship.capacity_for(option.hauled_is_ore) < option.hauled_m3:
            continue
        if ship.is_active:
            switch = 0.0
        elif ship.location_id in (station_id, here_location):
            switch = fleet.MINUTES_TO_SWITCH_SHIP
        else:
            continue
        if best is None or (ship.value, switch) < (best[0].value, best[1]):
            best = (ship, switch)
    return best


def multi_trip_haul(
    ships: list[fleet.FleetShip], station_id: int, here_location: int | None, option: ore.Option
) -> tuple[fleet.FleetShip, float, int] | None:
    """The reachable flyable ship needing the fewest trips for a load too big for one."""
    best = None
    for ship in ships:
        capacity = ship.capacity_for(option.hauled_is_ore)
        if not ship.usable or capacity <= 0:
            continue
        if ship.is_active:
            switch = 0.0
        elif ship.location_id in (station_id, here_location):
            switch = fleet.MINUTES_TO_SWITCH_SHIP
        else:
            continue
        trips = math.ceil(option.hauled_m3 / capacity)
        if best is None or (trips, ship.value) < (best[2], best[0].value):
            best = (ship, switch, trips)
    return best
