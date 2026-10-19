"""Muted categories: what the person asked the planner to stop suggesting.

Categories are short strings: "kind:courier", "kind:mining", "route:lowsec".
Unmuting keeps the row (sets removed_at), so the history survives.
"""

import sqlite3

from eve_assistant.store import utc_now

KNOWN = {
    "kind:sell_hangar": "selling ore from your hangar",
    "kind:mining": "mining",
    "kind:gas": "gas harvesting",
    "kind:manufacturing": "manufacturing",
    "kind:mission": "agent missions",
    "kind:exploration": "exploration",
    "kind:trade": "trade runs between the hubs",
    "kind:courier": "courier contracts",
    "kind:skill": "skill training reminders",
    "route:lowsec": "anything that goes through lowsec",
}


class PreferenceError(Exception):
    pass


def active_mutes(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT category FROM mutes WHERE removed_at IS NULL")}


def mute(db: sqlite3.Connection, category: str) -> bool:
    """Returns False if it was already muted."""
    if category not in KNOWN:
        raise PreferenceError(f"Unknown category '{category}'. Known: {', '.join(KNOWN)}.")
    if category in active_mutes(db):
        return False
    db.execute("INSERT INTO mutes (category, created_at) VALUES (?, ?)", (category, utc_now()))
    db.commit()
    return True


def unmute(db: sqlite3.Connection, category: str) -> bool:
    changed = db.execute(
        "UPDATE mutes SET removed_at = ? WHERE category = ? AND removed_at IS NULL",
        (utc_now(), category),
    ).rowcount
    db.commit()
    return bool(changed)
