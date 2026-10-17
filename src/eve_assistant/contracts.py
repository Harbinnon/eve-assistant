"""The player's own contracts: courier projects that track themselves.

ESI lists every contract a character is party to. They are collected as a
snapshot kind (cleaned of other players' IDs and of titles, which are
player-written; see snapshots.sanitise). From them:

- an open courier project closes itself when its contract is delivered
  (done) or fails, is deleted, cancelled, rejected or reversed (dropped);
- the History page can say whether a planned courier was delivered.

A contract the character never accepted (someone else took it first) never
appears in its list, so nothing can be said about it; such a project stays
open until the player closes it.
"""

import sqlite3
from datetime import UTC, datetime

from eve_assistant import projects, snapshots

DELIVERED = frozenset({"finished", "finished_contractor", "finished_issuer"})
GAVE_UP = frozenset({"failed", "deleted", "cancelled", "rejected", "reversed"})


def mine(db: sqlite3.Connection, character_id: int) -> dict[int, dict]:
    """The character's contracts from the latest snapshot, by contract ID."""
    payload = snapshots.latest(db, character_id, "contracts") or []
    return {c["contract_id"]: c for c in payload if isinstance(c, dict) and "contract_id" in c}


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def state(contract: dict, character_id: int) -> str:
    """delivered, gave_up, underway (accepted by this character, not delivered) or other."""
    status = contract.get("status", "")
    if status in DELIVERED:
        return "delivered"
    if status in GAVE_UP:
        return "gave_up"
    if status == "in_progress" and contract.get("acceptor_id") == character_id:
        return "underway"
    return "other"


def delivered_between(contract: dict, start: str, end: str) -> bool:
    done = _parse(contract.get("date_completed"))
    return done is not None and _parse(start) <= done <= _parse(end)


def sync_projects(db: sqlite3.Connection, character_id: int) -> list[tuple[int, str]]:
    """Close open courier projects whose contract is settled. Returns
    (project_id, done|dropped) for each one closed."""
    known = mine(db, character_id)
    closed = []
    for project in projects.open_projects(db, character_id):
        if project.kind != "courier" or not project.key.isdigit():
            continue
        contract = known.get(int(project.key))
        if contract is None:
            continue
        outcome = {"delivered": "done", "gave_up": "dropped"}.get(state(contract, character_id))
        if outcome:
            projects.close(db, project.project_id, outcome)
            closed.append((project.project_id, outcome))
    return closed
