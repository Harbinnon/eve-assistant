"""Collecting character state from ESI into the append-only snapshot history.

Collecting from day one matters even before anything reads it: ESI only
keeps about 30 days of wallet journal and mining ledger, and learning later
needs that history.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from eve_assistant.esi.client import EsiClient, EsiError, EsiRateLimited
from eve_assistant.store import utc_now


@dataclass(frozen=True)
class Kind:
    name: str
    path: str  # {id} is replaced with the character ID
    needs_token: bool = True


KINDS = (
    Kind("public", "/characters/{id}", needs_token=False),
    Kind("skills", "/characters/{id}/skills"),
    Kind("skillqueue", "/characters/{id}/skillqueue"),
    Kind("attributes", "/characters/{id}/attributes"),
    Kind("implants", "/characters/{id}/implants"),
    Kind("clones", "/characters/{id}/clones"),
    Kind("location", "/characters/{id}/location"),
    Kind("ship", "/characters/{id}/ship"),
    Kind("online", "/characters/{id}/online"),
    Kind("wallet", "/characters/{id}/wallet"),
    Kind("wallet_journal", "/characters/{id}/wallet/journal"),
    Kind("wallet_transactions", "/characters/{id}/wallet/transactions"),
    Kind("mining_ledger", "/characters/{id}/mining"),
    Kind("assets", "/characters/{id}/assets"),
    Kind("standings", "/characters/{id}/standings"),
    Kind("orders", "/characters/{id}/orders"),
    Kind("industry_jobs", "/characters/{id}/industry/jobs"),
)


@dataclass(frozen=True)
class CheckResult:
    kind: str
    outcome: str  # changed, unchanged, failed
    detail: str | None = None


def ensure_character(db: sqlite3.Connection, character_id: int, name: str) -> None:
    db.execute(
        "INSERT INTO characters (character_id, name, first_seen) VALUES (?, ?, ?) "
        "ON CONFLICT(character_id) DO UPDATE SET name = excluded.name",
        (character_id, name, utc_now()),
    )
    db.commit()


def _hash(payload) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def latest(db: sqlite3.Connection, character_id: int, kind: str):
    """The most recent payload of a kind for a character, or None."""
    row = db.execute(
        "SELECT payload FROM snapshots WHERE character_id = ? AND kind = ? "
        "ORDER BY snapshot_id DESC LIMIT 1",
        (character_id, kind),
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def record(db: sqlite3.Connection, character_id: int, kind: str, payload) -> bool:
    """Append a snapshot if the content changed. Returns True if a row was written."""
    digest = _hash(payload)
    previous = db.execute(
        "SELECT content_hash FROM snapshots WHERE character_id = ? AND kind = ? "
        "ORDER BY snapshot_id DESC LIMIT 1",
        (character_id, kind),
    ).fetchone()
    changed = previous is None or previous["content_hash"] != digest
    now = utc_now()
    if changed:
        db.execute(
            "INSERT INTO snapshots (character_id, kind, taken_at, content_hash, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (character_id, kind, now, digest, json.dumps(payload)),
        )
    db.execute(
        "INSERT INTO snapshot_checks (character_id, kind, checked_at, outcome) VALUES (?, ?, ?, ?)",
        (character_id, kind, now, "changed" if changed else "unchanged"),
    )
    db.commit()
    return changed


def _record_failure(db: sqlite3.Connection, character_id: int, kind: str, detail: str) -> None:
    db.execute(
        "INSERT INTO snapshot_checks (character_id, kind, checked_at, outcome, detail) "
        "VALUES (?, ?, ?, 'failed', ?)",
        (character_id, kind, utc_now(), detail),
    )
    db.commit()


def collect(db: sqlite3.Connection, esi: EsiClient, character_id: int) -> list[CheckResult]:
    """Fetch every kind for one character. One failing kind does not stop the rest;
    a rate limit stops everything."""
    results = []
    for kind in KINDS:
        try:
            result = esi.get(
                kind.path.format(id=character_id),
                character_id=character_id if kind.needs_token else None,
            )
        except EsiRateLimited:
            raise
        except EsiError as exc:
            _record_failure(db, character_id, kind.name, str(exc))
            results.append(CheckResult(kind.name, "failed", str(exc)))
            continue
        changed = record(db, character_id, kind.name, result.data)
        results.append(CheckResult(kind.name, "changed" if changed else "unchanged"))
    return results
