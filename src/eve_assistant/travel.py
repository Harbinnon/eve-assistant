"""Travel time: the minutes a jump plans assume, and the travel a session shows.

Plans start from MINUTES_PER_JUMP (align, warp, gate, at a typical pace) and
learn the player's own pace from recorded sessions (corrections.py).

What a session shows comes from the location EVE reports, checked every
couple of minutes while the character is online. A stretch of travel is a
run of checks that each find the character in a different system from the
one before; it ends at the first check that finds it still in the same
system. The jumps are the shortest route through the systems seen. The
character left somewhere between the stretch's first two checks and arrived
somewhere between its last two, so the minutes are taken midpoint to
midpoint. Left out: a stretch seen by only two checks (it can't be timed
that way), one with a long gap between checks (the character may have been
offline), and one through systems with no route between them (a death, a
wormhole). Only the owner's own location is read.
"""

import json
import sqlite3
from datetime import datetime, timedelta

from eve_assistant import routes

MINUTES_PER_JUMP = 1.0  # the starting assumption, until the player's pace is learned
MIN_JUMPS = 3  # sessions with less travel than this are too noisy to learn from
MAX_CHECK_GAP = timedelta(minutes=10)


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _checks(db: sqlite3.Connection, character_id: int, start: str, end: str) -> list:
    """(time, system) for each successful location check in the window."""
    snapshots = db.execute(
        "SELECT taken_at, payload FROM snapshots WHERE character_id = ? AND kind = 'location' "
        "AND taken_at <= ? ORDER BY taken_at, snapshot_id",
        (character_id, end),
    ).fetchall()
    checks = db.execute(
        "SELECT checked_at FROM snapshot_checks WHERE character_id = ? AND kind = 'location' "
        "AND outcome != 'failed' AND checked_at BETWEEN ? AND ? ORDER BY checked_at",
        (character_id, start, end),
    ).fetchall()
    found, index, system = [], 0, None
    for check in checks:
        while index < len(snapshots) and snapshots[index]["taken_at"] <= check["checked_at"]:
            system = json.loads(snapshots[index]["payload"]).get("solar_system_id")
            index += 1
        if system is not None:
            found.append((_parse(check["checked_at"]), system))
    return found


def _stretches(checks: list) -> list[list]:
    """Runs of checks in which every check finds a new system."""
    runs, current = [], checks[:1]
    for check in checks[1:]:
        if check[1] != current[-1][1]:
            current.append(check)
        else:
            if len(current) > 1:
                runs.append(current)
            current = [check]
    if len(current) > 1:
        runs.append(current)
    return runs


def session_travel(db: sqlite3.Connection, character_id: int, start: str, end: str) -> dict | None:
    """Jumps and minutes travelled in a session, or None without a timed stretch."""
    jumps, minutes, stretches = 0, 0.0, 0
    for run in _stretches(_checks(db, character_id, start, end)):
        if len(run) < 3 or any(
            b[0] - a[0] > MAX_CHECK_GAP for a, b in zip(run, run[1:], strict=False)
        ):
            continue
        counts = [
            routes.jump_count(db, a[1], b[1], avoid_camps=False)
            for a, b in zip(run, run[1:], strict=False)
        ]
        if None in counts:
            continue
        left = run[0][0] + (run[1][0] - run[0][0]) / 2
        arrived = run[-2][0] + (run[-1][0] - run[-2][0]) / 2
        jumps += sum(counts)
        minutes += (arrived - left).total_seconds() / 60
        stretches += 1
    if not stretches:
        return None
    return {"jumps": jumps, "minutes": round(minutes, 1), "stretches": stretches}
