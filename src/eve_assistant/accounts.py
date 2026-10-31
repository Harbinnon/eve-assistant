"""Grouping characters into EVE accounts.

ESI does not say which account a character belongs to, so the person tells
the tool once. Account rules (one character online at a time, one skill
queue, Alpha accounts cannot multibox) depend on this grouping.
"""

import sqlite3
from dataclasses import dataclass

CLONE_STATES = ("unknown", "alpha", "omega")
MAX_LABEL = 40


class AccountError(Exception):
    pass


@dataclass(frozen=True)
class AccountView:
    label: str
    clone_state: str
    characters: tuple[str, ...]


def assign(db: sqlite3.Connection, character_id: int, label: str) -> None:
    label = label.strip()
    if not label:
        raise AccountError("Give the account a name, e.g. 'main' or 'alt account'.")
    if (
        db.execute("SELECT 1 FROM characters WHERE character_id = ?", (character_id,)).fetchone()
        is None
    ):
        raise AccountError(
            f"Character {character_id} is not known yet. Run `eve-assistant snapshot` first."
        )
    db.execute("INSERT OR IGNORE INTO accounts (label) VALUES (?)", (label,))
    db.execute(
        "UPDATE characters SET account_id = (SELECT account_id FROM accounts WHERE label = ?) "
        "WHERE character_id = ?",
        (label, character_id),
    )
    db.execute(
        "DELETE FROM accounts WHERE account_id NOT IN "
        "(SELECT account_id FROM characters WHERE account_id IS NOT NULL)"
    )
    db.commit()


def set_clone_state(db: sqlite3.Connection, label: str, state: str) -> None:
    if state not in CLONE_STATES:
        raise AccountError(f"Clone state must be one of: {', '.join(CLONE_STATES)}.")
    updated = db.execute(
        "UPDATE accounts SET clone_state = ? WHERE label = ?", (state, label)
    ).rowcount
    if not updated:
        raise AccountError(f"No account called '{label}'.")
    db.commit()


def set_up(db: sqlite3.Connection, character_id: int, label: str, state: str) -> None:
    """Put a character on an account and mark that account Alpha or Omega (the
    Characters page does both at once). The state applies to the whole account."""
    label = label.strip()
    if state not in CLONE_STATES:
        raise AccountError(f"Clone state must be one of: {', '.join(CLONE_STATES)}.")
    if len(label) > MAX_LABEL:
        raise AccountError(f"Keep the account name under {MAX_LABEL} characters.")
    assign(db, character_id, label)
    set_clone_state(db, label, state)


def labels(db: sqlite3.Connection) -> list[str]:
    return [r[0] for r in db.execute("SELECT label FROM accounts ORDER BY label")]


def overview(db: sqlite3.Connection) -> list[AccountView]:
    rows = db.execute(
        "SELECT a.label, a.clone_state, c.name FROM characters c "
        "LEFT JOIN accounts a ON a.account_id = c.account_id ORDER BY a.label, c.name"
    ).fetchall()
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        key = (row["label"] or "(no account set)", row["clone_state"] or "unknown")
        grouped.setdefault(key, []).append(row["name"])
    return [AccountView(label, state, tuple(names)) for (label, state), names in grouped.items()]
