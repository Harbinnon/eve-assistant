"""Routes between solar systems, worked out locally from the stargate map.

No API calls, so a planner can try many routes cheaply. Security filters
match what the in-game autopilot does with "prefer safer": highsec only, or
anything. Lawless highsec (corruption level 5) is NOT known here: no API
reports it, so callers must keep warning the player to check the route.
"""

import sqlite3
from collections import deque

HIGHSEC = 0.45  # security status rounds to 0.5 or above from here


class RouteError(Exception):
    pass


def jumps_between(
    db: sqlite3.Connection, origin: int, destination: int, highsec_only: bool = False
) -> list[int] | None:
    """The shortest list of systems from origin to destination (both included),
    or None if there is no route under the given restriction."""
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
    previous = {origin: None}
    queue = deque([origin])
    while queue:
        here = queue.popleft()
        for (there,) in db.execute(
            "SELECT to_system_id FROM sde_jumps WHERE from_system_id = ?", (here,)
        ):
            if there in previous or (allowed is not None and there not in allowed):
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
    db: sqlite3.Connection, origin: int, destination: int, highsec_only: bool = False
) -> int | None:
    path = jumps_between(db, origin, destination, highsec_only)
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
