"""The character's saved fittings, for "what if you used this fit" in plans.

Saved fits come from ESI (fittings scope) as a snapshot kind; only the fit
name, hull and fitted modules are kept (descriptions are dropped). Fit
names are written by the player, so they are shown labelled as their own
text, never as the tool's.

The planner keeps predicting with what is actually fitted; a saved fit
that would do noticeably better is mentioned beside it, with whether its
modules are where the ship is.

A ship goal can be for a fit (roadmap 8.1): one of the character's saved fits,
or one pasted in the text format Pyfa and EVEShip.fit export (EFT):

    [Retriever, Ice run]
    Mining Laser Upgrade II
    [Empty Low slot]

    Survey Scanner II

    Strip Miner I, Veldspar Mining Crystal I
    Hobgoblin II x2

The first line names the hull and the fit; each other line is an item, with
an optional loaded charge after a comma and a count after " x".
"""

import re
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


class FitError(Exception):
    pass


@dataclass(frozen=True)
class Fit:
    """A fit a ship goal is for."""

    name: str  # player-written
    ship_type_id: int
    items: tuple[tuple[int, int], ...]  # (type ID, quantity), hull not included
    source: str  # saved or pasted

    def as_json(self) -> dict:
        return {"name": self.name, "ship_type_id": self.ship_type_id, "source": self.source,
                "items": [list(i) for i in self.items]}  # fmt: skip

    @classmethod
    def from_json(cls, data: dict) -> "Fit":
        items = tuple((int(t), int(q)) for t, q in data.get("items", []))
        return cls(str(data.get("name", ""))[:100], int(data["ship_type_id"]), items,
                   str(data.get("source", "pasted")))  # fmt: skip


HEADER = re.compile(r"^\[(?P<hull>[^,\]]+?)\s*(?:,\s*(?P<name>[^\]]*))?\]$")
COUNT = re.compile(r"^(?P<name>.+?)\s+x(?P<count>\d{1,6})$")
MAX_PASTE = 20_000  # characters
MAX_ITEMS = 300


def _type_id(db: sqlite3.Connection, name: str) -> int | None:
    row = db.execute(
        "SELECT type_id FROM sde_types WHERE name = ? COLLATE NOCASE ORDER BY published DESC",
        (name.strip(),),
    ).fetchone()
    return row[0] if row else None


def _lines(text: str) -> list[str]:
    if len(text) > MAX_PASTE:
        raise FitError("That fit is too long to be a fit.")
    lines = [line.strip() for line in text.strip().splitlines()]
    lines = [line for line in lines if line]
    if not lines or not HEADER.match(lines[0]):
        raise FitError(
            "A pasted fit starts with a line like [Retriever, My fit]: copy it from Pyfa "
            "or EVEShip.fit with Export as EFT, or the game's Copy to clipboard."
        )
    if len(lines) > MAX_ITEMS:
        raise FitError("That fit has more lines than any ship can carry.")
    return lines


def _item_names(lines: list[str]) -> list[tuple[str, int]]:
    """(item name, count) for each item line: empty slots skipped, a loaded charge
    counted as an item, "/OFFLINE" dropped."""
    found = []
    for line in lines:
        if line.startswith("["):  # [Empty High slot] and the like
            continue
        line = line.removesuffix("/OFFLINE").strip()
        counted = COUNT.match(line)
        count = int(counted["count"]) if counted else 1
        name = counted["name"] if counted else line
        module, _, charge = name.partition(",")
        found.append((module.strip(), count))
        if charge.strip():
            found.append((charge.strip(), 1))
    return found


def parse_eft(db: sqlite3.Connection, text: str) -> Fit:
    """A fit from text in the EFT format. Raises FitError naming anything unknown."""
    lines = _lines(text)
    header = HEADER.match(lines[0])
    hull = _type_id(db, header["hull"])
    if hull is None:
        raise FitError(f"No ship is called '{header['hull'].strip()}'.")
    counts: dict[int, int] = {}
    unknown = []
    for name, count in _item_names(lines[1:]):
        type_id = _type_id(db, name)
        if type_id is None:
            unknown.append(name)
        else:
            counts[type_id] = counts.get(type_id, 0) + count
    if unknown:
        raise FitError(f"Not found in CCP's data: {', '.join(sorted(set(unknown)))}.")
    name = (header["name"] or "").strip()[:100] or "pasted fit"
    return Fit(name, hull, tuple(sorted(counts.items())), "pasted")


def saved_by_name(db: sqlite3.Connection, character_id: int, name: str) -> Fit:
    """One of the character's saved fits, by its name. Raises FitError."""
    wanted = name.strip().casefold()
    for fit in saved(db, character_id):
        if fit.name.strip().casefold() == wanted:
            counts: dict[int, int] = {}
            for type_id in fit.modules:
                counts[type_id] = counts.get(type_id, 0) + 1
            return Fit(fit.name, fit.ship_type_id, tuple(sorted(counts.items())), "saved")
    raise FitError(
        f"No saved fit called '{name.strip()}' for this character. Paste the fit instead, "
        "or save it in game and refresh."
    )


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
