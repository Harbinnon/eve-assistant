"""Routes between solar systems, worked out locally from the stargate map.

No API calls, so a planner can try many routes cheaply. Security filters
match what the in-game autopilot does with "prefer safer": highsec only, or
anything. Routes steer around hotspots (signals.py: gank hotspots and likely
camps, from ships lost there in the last hour): each one on the way costs
as much as HOTSPOT_JUMPS extra jumps, so a short detour is taken and a long
one isn't. The journey's ends are never avoided. Lawless highsec (corruption level 5) is NOT
known here: no API reports it, so callers must keep warning the player to
check the route.
"""

import heapq
import sqlite3
from collections import deque

from eve_assistant import signals

HIGHSEC = 0.45  # security status rounds to 0.5 or above from here
HOTSPOT_JUMPS = 5  # a hotspot on the way weighs as much as this many extra jumps


class RouteError(Exception):
    pass


def jumps_between(
    db: sqlite3.Connection,
    origin: int,
    destination: int,
    highsec_only: bool = False,
    avoid_camps: bool = True,
) -> list[int] | None:
    """The shortest list of systems from origin to destination (both included),
    or None if there is no route under the given restriction. With avoid_camps,
    hotspots on the way count as HOTSPOT_JUMPS extra jumps each."""
    if origin == destination:
        return [origin]
    allowed = None
    if highsec_only:
        allowed = {
            row[0]
            for row in db.execute(
                "SELECT system_id FROM sde_solar_systems WHERE security_status >= ?", (HIGHSEC,)
            )
        }
        if origin not in allowed or destination not in allowed:
            return None
    hot = set(signals.hotspots(db)) - {origin, destination} if avoid_camps else set()
    if hot:
        return _weighted(db, origin, destination, allowed, hot)
    return _shortest(db, origin, destination, allowed)


def _neighbours(db, here: int, allowed: set | None):
    for (there,) in db.execute(
        "SELECT to_system_id FROM sde_jumps WHERE from_system_id = ?", (here,)
    ):
        if allowed is None or there in allowed:
            yield there


def _weighted(db, origin: int, destination: int, allowed: set | None, hot: set) -> list | None:
    """Fewest jumps, a hotspot counting as HOTSPOT_JUMPS more."""
    best = {origin: 0}
    previous: dict[int, int | None] = {origin: None}
    queue = [(0, origin)]
    while queue:
        cost, here = heapq.heappop(queue)
        if here == destination:
            path = [here]
            while previous[path[-1]] is not None:
                path.append(previous[path[-1]])
            return path[::-1]
        if cost > best[here]:
            continue
        for there in _neighbours(db, here, allowed):
            step = cost + 1 + (HOTSPOT_JUMPS if there in hot else 0)
            if step < best.get(there, step + 1):
                best[there] = step
                previous[there] = here
                heapq.heappush(queue, (step, there))
    return None


def _shortest(db, origin: int, destination: int, allowed: set | None) -> list | None:
    previous = {origin: None}
    queue = deque([origin])
    while queue:
        here = queue.popleft()
        for there in _neighbours(db, here, allowed):
            if there in previous:
                continue
            previous[there] = here
            if there == destination:
                path = [there]
                while previous[path[-1]] is not None:
                    path.append(previous[path[-1]])
                return path[::-1]
            queue.append(there)
    return None


def jump_count(
    db: sqlite3.Connection,
    origin: int,
    destination: int,
    highsec_only: bool = False,
    avoid_camps: bool = True,
) -> int | None:
    path = jumps_between(db, origin, destination, highsec_only, avoid_camps)
    return None if path is None else len(path) - 1


def lowest_security(db: sqlite3.Connection, path: list[int]) -> float | None:
    if not path:
        return None
    marks = ",".join("?" * len(path))
    row = db.execute(
        f"SELECT MIN(security_status) FROM sde_solar_systems WHERE system_id IN ({marks})",  # noqa: S608
        path,
    ).fetchone()
    return row[0]
