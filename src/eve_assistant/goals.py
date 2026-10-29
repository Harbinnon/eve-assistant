"""Goals and how far each character is from them.

Answers "what would it take": skills (with training time and Alpha limits)
and ISK (price against wallet). Once a character has a few settled
sessions, ISK goals also get a rough date: ISK banked per hour played times
the learned play time per week (see pace.py).

A ship goal can be for a fit (roadmap 8.1): then its skills are the hull's
and every item's, and its ISK is the hull and items still to buy, at CCP's
average prices, less anything the character already owns.
"""

import json
import sqlite3
from dataclasses import dataclass, field

from eve_assistant import fittings, numbers, pace, skills, snapshots
from eve_assistant.esi.client import EsiClient
from eve_assistant.skills import Reason, Requirement
from eve_assistant.store import utc_now


class GoalError(Exception):
    pass


@dataclass(frozen=True)
class Goal:
    goal_id: int
    kind: str
    character_id: int | None
    type_id: int | None
    level: int | None
    isk: float | None
    label: str
    status: str
    fit: "fittings.Fit | None" = None  # a ship goal's fit, if it names one


@dataclass(frozen=True)
class FitCost:
    """What a fitted ship comes to: the parts still to buy, at CCP's average prices."""

    fit: "fittings.Fit"
    hull: float | None  # None when it's owned already or has no price
    items: float  # items still to buy
    owned: tuple[str, ...]  # what the character already has, not counted
    unpriced: tuple[str, ...]  # items with no market price, not counted

    @property
    def total(self) -> float:
        return (self.hull or 0.0) + self.items


@dataclass(frozen=True)
class GoalStatus:
    goal: Goal
    character_name: str | None
    requirement: Requirement | None
    price: float | None  # rough market price of the ship, if any
    wallet: float | None
    isk_needed: float | None
    is_alpha: bool | None = None
    reasons: tuple[Reason, ...] = field(default_factory=tuple)
    weeks_to_isk: float | None = None  # at the character's real pace, if known
    earning: "pace.EarningRate | None" = None
    target_isk: float | None = None  # the ISK the goal comes to (ISK and Omega goals)
    fit_cost: FitCost | None = None  # for a ship goal with a fit


def update_prices(db: sqlite3.Connection, esi: EsiClient) -> int:
    """Refresh CCP's average prices. Returns the number of items priced."""
    result = esi.get("/markets/prices")
    now = utc_now()
    with db:
        db.execute("DELETE FROM market_prices")
        db.executemany(
            "INSERT INTO market_prices VALUES (?, ?, ?, ?)",
            (
                (p["type_id"], p.get("average_price"), p.get("adjusted_price"), now)
                for p in result.data
            ),
        )
    return len(result.data)


PLEX_TYPE_ID = 44992
OMEGA_PLEX_PER_MONTH = 500  # one month of Omega


def price_of(db: sqlite3.Connection, type_id: int) -> float | None:
    row = db.execute(
        "SELECT average_price, adjusted_price FROM market_prices WHERE type_id = ?", (type_id,)
    ).fetchone()
    if row is None:
        return None
    return row["average_price"] or row["adjusted_price"]


def resolve_character(db: sqlite3.Connection, name_or_id: str) -> int:
    row = db.execute(
        "SELECT character_id FROM characters WHERE CAST(character_id AS TEXT) = ? "
        "OR name = ? COLLATE NOCASE",
        (name_or_id, name_or_id),
    ).fetchone()
    if row is None:
        known = ", ".join(r["name"] for r in db.execute("SELECT name FROM characters"))
        raise GoalError(f"No character '{name_or_id}'. Known: {known or 'none yet'}.")
    return row["character_id"]


def add_goal(
    db: sqlite3.Connection,
    kind: str,
    character_id: int | None,
    *,
    type_id: int | None = None,
    level: int | None = None,
    isk: float | None = None,
    fit: "fittings.Fit | None" = None,
) -> int:
    if kind in ("fly_ship", "skill") and character_id is None:
        raise GoalError("Ship and skill goals are for a particular character.")
    if fit is not None and kind != "fly_ship":
        raise GoalError("Only a ship goal can have a fit.")
    if kind == "fly_ship":
        type_id = type_id or (fit.ship_type_id if fit else None)
        if fit is not None and fit.ship_type_id != type_id:
            hull = skills.type_name(db, fit.ship_type_id)
            raise GoalError(f"That fit is for a {hull}, not a {skills.type_name(db, type_id)}.")
        verb = "Fly" if skills.is_ship(db, type_id) else "Use"
        label = f"{verb} a {skills.type_name(db, type_id)}" + (", fitted" if fit else "")
    elif kind == "skill":
        label = f"Train {skills.type_name(db, type_id)} {level}"
    elif kind == "isk":
        if not isk or isk <= 0:
            raise GoalError("An ISK goal needs a positive amount.")
        label = f"Have {numbers.isk(isk)} ISK"
    elif kind == "omega":
        level = level or 1
        if not 1 <= level <= 12:
            raise GoalError("An Omega goal is for 1 to 12 months.")
        plex = level * OMEGA_PLEX_PER_MONTH
        label = f"Omega for {level} month{'s' if level != 1 else ''} ({plex:,} PLEX)"
    else:
        raise GoalError(f"Unknown goal kind '{kind}'.")
    cursor = db.execute(
        "INSERT INTO goals (kind, character_id, type_id, level, isk, label, created_at, fit) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, character_id, type_id, level, isk, label, utc_now(),
         json.dumps(fit.as_json()) if fit else None),
    )  # fmt: skip
    db.commit()
    return cursor.lastrowid


def fit_from_text(db: sqlite3.Connection, text: str, character_id: int | None):
    """A fit from what was typed: a pasted EFT fit, or the name of one of the
    character's saved fits. None for nothing. Raises fittings.FitError."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("[") or "\n" in text:
        return fittings.parse_eft(db, text)
    if character_id is None:
        raise GoalError("Choose the character whose saved fit it is.")
    return fittings.saved_by_name(db, character_id, text)


def set_status(db: sqlite3.Connection, goal_id: int, status: str) -> None:
    updated = db.execute(
        "UPDATE goals SET status = ?, status_at = ? WHERE goal_id = ?",
        (status, utc_now(), goal_id),
    ).rowcount
    if not updated:
        raise GoalError(f"No goal {goal_id}.")
    db.commit()


def active_goals(db: sqlite3.Connection) -> list[Goal]:
    return [
        Goal(
            r["goal_id"],
            r["kind"],
            r["character_id"],
            r["type_id"],
            r["level"],
            r["isk"],
            r["label"],
            r["status"],
            fittings.Fit.from_json(json.loads(r["fit"])) if r["fit"] else None,
        )  # fmt: skip
        for r in db.execute("SELECT * FROM goals WHERE status = 'active' ORDER BY goal_id")
    ]


def _wallet(db: sqlite3.Connection, character_id: int | None) -> float | None:
    if character_id is not None:
        value = snapshots.latest(db, character_id, "wallet")
        return float(value) if value is not None else None
    total, seen = 0.0, False
    for row in db.execute("SELECT character_id FROM characters"):
        value = snapshots.latest(db, row["character_id"], "wallet")
        if value is not None:
            total, seen = total + float(value), True
    return total if seen else None


def _owned(db: sqlite3.Connection, character_id: int) -> dict[int, int]:
    """How many of each item the character has, anywhere."""
    counts: dict[int, int] = {}
    for item in snapshots.latest(db, character_id, "assets") or []:
        if isinstance(item, dict) and "type_id" in item:
            counts[item["type_id"]] = counts.get(item["type_id"], 0) + int(item.get("quantity", 1))
    return counts


def fit_cost(db: sqlite3.Connection, character_id: int, fit: "fittings.Fit") -> FitCost:
    """The hull and items still to buy for a fit, less what the character owns."""
    have = _owned(db, character_id)
    owned, unpriced = [], []
    hull = None
    if have.get(fit.ship_type_id, 0) > 0:
        owned.append(skills.type_name(db, fit.ship_type_id))
    else:
        hull = price_of(db, fit.ship_type_id)
        if hull is None:
            unpriced.append(skills.type_name(db, fit.ship_type_id))
    items = 0.0
    for type_id, count in fit.items:
        already = min(have.get(type_id, 0), count)
        if already:
            owned.append(f"{already} × {skills.type_name(db, type_id)}")
        to_buy = count - already
        price = price_of(db, type_id)
        if to_buy and price is None:
            unpriced.append(skills.type_name(db, type_id))
        items += to_buy * (price or 0.0)
    return FitCost(fit, hull, items, tuple(owned), tuple(unpriced))


def _skills_part(db: sqlite3.Connection, goal: Goal, reasons: list[Reason]):
    """For ship and skill goals: (requirement, price, is_alpha, target ISK, fit cost)."""
    character = skills.load_character_skills(db, goal.character_id)
    if character.is_alpha and character.clone_state == "unknown":
        reasons.append(Reason("clone_state_unknown"))
    if goal.kind == "skill":
        requirement = skills.plan_skill(db, character, goal.type_id, goal.level)
        return requirement, None, character.is_alpha, None, None
    if goal.fit is not None:
        cost = fit_cost(db, goal.character_id, goal.fit)
        if cost.unpriced:
            reasons.append(Reason("fit_unpriced", {"items": list(cost.unpriced)}))
        item_ids = [type_id for type_id, _count in goal.fit.items]
        requirement = skills.plan_fit(db, character, goal.type_id, item_ids)
        return requirement, cost.hull, character.is_alpha, cost.total, cost
    price = price_of(db, goal.type_id)
    if price is None:
        reasons.append(Reason("no_price"))
    return skills.plan(db, character, goal.type_id), price, character.is_alpha, price, None


def _goal_date(db: sqlite3.Connection, goal: Goal, isk_needed, reasons: list[Reason]):
    """Weeks to bank the ISK at the character's real pace: (weeks, earning rate)."""
    if not isk_needed or goal.character_id is None:
        return None, None
    earning = pace.banked_per_hour(db, goal.character_id)
    if earning is None:
        reasons.append(Reason("goal_date_needs_sessions"))
    return pace.weeks_to(isk_needed, earning, pace.weekly_play(db).minutes), earning


def evaluate(db: sqlite3.Connection, goal: Goal) -> GoalStatus:
    name = None
    if goal.character_id is not None:
        row = db.execute(
            "SELECT name FROM characters WHERE character_id = ?", (goal.character_id,)
        ).fetchone()
        name = row["name"] if row else None
    wallet = _wallet(db, goal.character_id)

    reasons: list[Reason] = []
    requirement, price, is_alpha, target_isk, cost = None, None, None, goal.isk, None
    if goal.kind in ("fly_ship", "skill"):
        requirement, price, is_alpha, target_isk, cost = _skills_part(db, goal, reasons)
    elif goal.kind == "omega":
        # Follows the PLEX price: the ISK a month of Omega costs today.
        plex_price = price_of(db, PLEX_TYPE_ID)
        if plex_price is None:
            reasons.append(Reason("no_price"))
        else:
            target_isk = (goal.level or 1) * OMEGA_PLEX_PER_MONTH * plex_price

    isk_needed = None
    if target_isk is not None and wallet is not None:
        isk_needed = max(0.0, target_isk - wallet)
    weeks, earning = _goal_date(db, goal, isk_needed, reasons)
    return GoalStatus(
        goal,
        name,
        requirement,
        price,
        wallet,
        isk_needed,
        is_alpha,
        tuple(reasons),
        weeks,
        earning,
        target_isk,
        cost,
    )
