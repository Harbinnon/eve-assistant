"""Goals and how far each character is from them.

Answers "what would it take": skills (with training time and Alpha limits)
and ISK (price against wallet). Once a character has a few settled
sessions, ISK goals also get a rough date: ISK banked per hour played times
the learned play time per week (see pace.py).
"""

import sqlite3
from dataclasses import dataclass, field

from eve_assistant import numbers, pace, skills, snapshots
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
) -> int:
    if kind in ("fly_ship", "skill") and character_id is None:
        raise GoalError("Ship and skill goals are for a particular character.")
    if kind == "fly_ship":
        verb = "Fly" if skills.is_ship(db, type_id) else "Use"
        label = f"{verb} a {skills.type_name(db, type_id)}"
    elif kind == "skill":
        label = f"Train {skills.type_name(db, type_id)} {level}"
    elif kind == "isk":
        if not isk or isk <= 0:
            raise GoalError("An ISK goal needs a positive amount.")
        label = f"Have {numbers.isk(isk)} ISK"
    else:
        raise GoalError(f"Unknown goal kind '{kind}'.")
    cursor = db.execute(
        "INSERT INTO goals (kind, character_id, type_id, level, isk, label, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, character_id, type_id, level, isk, label, utc_now()),
    )
    db.commit()
    return cursor.lastrowid


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


def _skills_part(db: sqlite3.Connection, goal: Goal, reasons: list[Reason]):
    """For ship and skill goals: (requirement, price, is_alpha, target ISK)."""
    character = skills.load_character_skills(db, goal.character_id)
    if character.is_alpha and character.clone_state == "unknown":
        reasons.append(Reason("clone_state_unknown"))
    if goal.kind == "skill":
        requirement = skills.plan_skill(db, character, goal.type_id, goal.level)
        return requirement, None, character.is_alpha, None
    price = price_of(db, goal.type_id)
    if price is None:
        reasons.append(Reason("no_price"))
    return skills.plan(db, character, goal.type_id), price, character.is_alpha, price


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
    requirement, price, is_alpha, target_isk = None, None, None, goal.isk
    if goal.kind in ("fly_ship", "skill"):
        requirement, price, is_alpha, target_isk = _skills_part(db, goal, reasons)

    isk_needed = None
    if target_isk is not None and wallet is not None:
        isk_needed = max(0.0, target_isk - wallet)
    weeks, earning = _goal_date(db, goal, isk_needed, reasons)
    return GoalStatus(
        goal, name, requirement, price, wallet, isk_needed, is_alpha, tuple(reasons), weeks, earning
    )
