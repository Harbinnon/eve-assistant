"""Muted categories: what the person asked the planner to stop suggesting.

Categories are short strings: "kind:courier", "kind:mining", "route:lowsec",
and "ore:<type ID>" for an ore the person said isn't around (only in ore
anomalies, say: no API shows which anomalies are up, so the mining ledger
alone can't tell). Unmuting keeps the row (sets removed_at), so the history
survives.
"""

import sqlite3

from eve_assistant.store import utc_now

KNOWN = {
    "kind:sell_hangar": "selling ore from your hangar",
    "kind:mining": "mining",
    "kind:gas": "gas harvesting",
    "kind:manufacturing": "manufacturing",
    "kind:planet": "restarting extractors on your planets",
    "kind:mission": "agent missions",
    "kind:exploration": "exploration",
    "kind:trade": "trade runs between the hubs",
    "kind:courier": "courier contracts",
    "kind:skill": "skill training reminders",
    "route:lowsec": "anything that goes through lowsec",
}


ORE_PREFIX = "ore:"


class PreferenceError(Exception):
    pass


def ore_category(type_id: int) -> str:
    return f"{ORE_PREFIX}{int(type_id)}"


def is_ore(category: str) -> bool:
    return category.startswith(ORE_PREFIX) and category[len(ORE_PREFIX) :].isdigit()


def ruled_out_ores(db: sqlite3.Connection) -> set[int]:
    """Ores the person said aren't around: never planned unless named."""
    return {int(c[len(ORE_PREFIX) :]) for c in active_mutes(db) if is_ore(c)}


def active_mutes(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT category FROM mutes WHERE removed_at IS NULL")}


def mute(db: sqlite3.Connection, category: str) -> bool:
    """Returns False if it was already muted."""
    if category not in KNOWN and not is_ore(category):
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
