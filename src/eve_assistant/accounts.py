"""Grouping characters into EVE accounts.

ESI does not say which account a character belongs to, so the person tells
the tool once. Account rules (one character online at a time, one skill
queue, Alpha accounts cannot multibox) depend on this grouping.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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


# --- characters that can't share an account -----------------------------------
#
# An account logs in one character at a time, so two characters seen online
# at the same time are on different accounts. Switching characters gives
# back-to-back sessions, so only an overlap longer than OVERLAP counts.

OVERLAP = timedelta(minutes=2)
LOOK_BACK = timedelta(days=14)


@dataclass(frozen=True)
class Clash:
    account: str
    characters: tuple[str, str]
    at: str  # when both were online (UTC, ISO 8601)


def _parse(stamp) -> datetime:
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))


def _sessions(db: sqlite3.Connection, character_id: int) -> list[tuple[datetime, datetime]]:
    """When the character was online: each login with its logout, from the
    online snapshots, and a session still going until its last successful check."""
    payloads = [
        json.loads(r[0])
        for r in db.execute(
            "SELECT payload FROM snapshots WHERE character_id = ? AND kind = 'online' "
            "ORDER BY snapshot_id",
            (character_id,),
        )
    ]
    found: dict[datetime, datetime] = {}
    for payload in payloads:
        login, logout = payload.get("last_login"), payload.get("last_logout")
        if login and logout and not payload.get("online") and _parse(logout) >= _parse(login):
            found[_parse(login)] = _parse(logout)
    latest = payloads[-1] if payloads else {}
    if latest.get("online") and latest.get("last_login"):
        checked = db.execute(
            "SELECT MAX(checked_at) FROM snapshot_checks WHERE character_id = ? "
            "AND kind = 'online' AND outcome != 'failed'",
            (character_id,),
        ).fetchone()[0]
        if checked:
            found.setdefault(_parse(latest["last_login"]), _parse(checked))
    return list(found.items())


def _together(first, second, since: datetime) -> datetime | None:
    """The latest moment both were online, if they overlapped for long enough."""
    latest = None
    for start_a, end_a in first:
        for start_b, end_b in second:
            start, end = max(start_a, start_b), min(end_a, end_b)
            if end - start > OVERLAP and end >= since and (latest is None or start > latest):
                latest = start
    return latest


def clashes(db: sqlite3.Connection, now: datetime | None = None) -> list[Clash]:
    """Characters put on the same account that were online at the same time
    in the last LOOK_BACK: one of them must be on another account."""
    since = (now or datetime.now(UTC)) - LOOK_BACK
    by_account: dict[str, list[tuple[int, str]]] = {}
    for row in db.execute(
        "SELECT a.label, c.character_id, c.name FROM characters c "
        "JOIN accounts a USING (account_id) ORDER BY a.label, c.name"
    ):
        by_account.setdefault(row[0], []).append((row[1], row[2]))
    found = []
    for label, members in by_account.items():
        sessions = {cid: _sessions(db, cid) for cid, _name in members}
        for i, (first_id, first) in enumerate(members):
            for second_id, second in members[i + 1 :]:
                at = _together(sessions[first_id], sessions[second_id], since)
                if at is not None:
                    found.append(Clash(label, (first, second), at.isoformat(timespec="seconds")))
    return found
