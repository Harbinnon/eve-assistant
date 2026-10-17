"""The character's saved fittings, for "what if you used this fit" in plans.

Saved fits come from ESI (fittings scope) as a snapshot kind; only the fit
name, hull and fitted modules are kept (descriptions are dropped). Fit
names are written by the player, so they are shown labelled as their own
text, never as the tool's.

The planner keeps predicting with what is actually fitted; a saved fit
that would do noticeably better is mentioned beside it, with whether its
modules are where the ship is.
"""

import sqlite3
from dataclasses import dataclass

from eve_assistant import snapshots

FITTED_FLAGS = ("HiSlot", "MedSlot", "LoSlot", "RigSlot", "SubSystemSlot")
BETTER_BY = 1.05  # only mention a saved fit that beats the current one by 5% or more


@dataclass(frozen=True)
class SavedFit:
    fitting_id: int
    name: str  # player-written
    ship_type_id: int
    modules: tuple[int, ...]  # one type ID per fitted module


def saved(db: sqlite3.Connection, character_id: int) -> list[SavedFit]:
    fits = []
    for fit in snapshots.latest(db, character_id, "fittings") or []:
        if not isinstance(fit, dict) or "ship_type_id" not in fit:
            continue
        modules = tuple(
            item["type_id"]
            for item in fit.get("items", [])
            if str(item.get("flag", "")).startswith(FITTED_FLAGS)
            for _ in range(max(1, int(item.get("quantity", 1))))
        )
        fits.append(SavedFit(fit["fitting_id"], str(fit.get("name", ""))[:100],
                             fit["ship_type_id"], modules))  # fmt: skip
    return fits


def for_hull(db: sqlite3.Connection, character_id: int, ship_type_id: int) -> list[SavedFit]:
    return [f for f in saved(db, character_id) if f.ship_type_id == ship_type_id]


def missing_modules(
    db: sqlite3.Connection, character_id: int, fit: SavedFit, ship_item_id: int, location_id
) -> list[str]:
    """Modules the fit needs that are neither on the ship nor in the hangar where
    it is. Names from the static data."""
    have: dict[int, int] = {}
    for item in snapshots.latest(db, character_id, "assets") or []:
        on_ship = item.get("location_id") == ship_item_id
        in_hangar = item.get("location_id") == location_id and item.get("location_flag") == "Hangar"
        if on_ship or in_hangar:
            have[item["type_id"]] = have.get(item["type_id"], 0) + int(item.get("quantity", 1))
    needed: dict[int, int] = {}
    for type_id in fit.modules:
        needed[type_id] = needed.get(type_id, 0) + 1
    short = [type_id for type_id, count in needed.items() if have.get(type_id, 0) < count]
    if not short:
        return []
    marks = ",".join("?" * len(short))
    rows = db.execute(
        f"SELECT type_id, name FROM sde_types WHERE type_id IN ({marks})",  # noqa: S608  # placeholders only
        short,
    )
    names = dict(rows.fetchall())
    return sorted(names.get(t, str(t)) for t in short)
