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
    # Kinds that name other players are cleaned before storing (see
    # `sanitise`), and their raw responses are kept out of the ESI cache.
    names_others: bool = False


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
    Kind("wallet_journal", "/characters/{id}/wallet/journal", names_others=True),
    Kind("wallet_transactions", "/characters/{id}/wallet/transactions", names_others=True),
    Kind("mining_ledger", "/characters/{id}/mining"),
    Kind("assets", "/characters/{id}/assets"),
    Kind("standings", "/characters/{id}/standings"),
    Kind("orders", "/characters/{id}/orders"),
    Kind("industry_jobs", "/characters/{id}/industry/jobs"),
    Kind("contracts", "/characters/{id}/contracts", names_others=True),
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


# Wallet journal fields kept as they are. Everything else is dropped, in
# particular `description` (CCP writes other players' names into it, e.g.
# "Market: <buyer> bought stuff from <you>") and `reason` (player-written
# for donations).
JOURNAL_FIELDS = ("id", "date", "ref_type", "amount", "balance", "tax")
# Journal fields holding a character or corporation ID: kept only when it is
# one of the owner's characters or an NPC corporation.
JOURNAL_PARTIES = ("first_party_id", "second_party_id", "tax_receiver_id")
# context_id is kept unless it identifies a character, corporation or alliance.
PERSON_CONTEXTS = ("character_id", "corporation_id", "alliance_id")
# Wallet transactions: everything except client_id (the other party).
TRANSACTION_DROP = ("client_id",)
# Contracts: what courier tracking and learning need. Titles are dropped
# (player-written); party IDs are kept only for the owner or NPC corps.
CONTRACT_FIELDS = (
    "contract_id", "type", "status", "availability", "for_corporation",
    "date_issued", "date_accepted", "date_completed", "date_expired", "days_to_complete",
    "reward", "collateral", "price", "buyout", "volume",
    "start_location_id", "end_location_id",
)  # fmt: skip
CONTRACT_PARTIES = ("issuer_id", "issuer_corporation_id", "acceptor_id", "assignee_id")


def _known_parties(db: sqlite3.Connection) -> set[int]:
    owners = {r[0] for r in db.execute("SELECT character_id FROM characters")}
    npcs = {r[0] for r in db.execute("SELECT corporation_id FROM sde_npc_corporations")}
    return owners | npcs


def sanitise(db: sqlite3.Connection, kind: str, payload):
    """Remove other players' IDs, names and player-written text from a payload.
    CLAUDE.md: never collect other players' data."""
    if kind in ("wallet_journal", "wallet_transactions", "contracts"):
        # An unexpected shape is not stored as it is: it might name people.
        payload = [e for e in payload if isinstance(e, dict)] if isinstance(payload, list) else []
    if kind == "wallet_transactions":
        return [{k: v for k, v in t.items() if k not in TRANSACTION_DROP} for t in payload]
    if kind == "contracts":
        known = _known_parties(db)
        cleaned = []
        for contract in payload:
            kept = {k: contract[k] for k in CONTRACT_FIELDS if k in contract}
            for field in CONTRACT_PARTIES:
                if contract.get(field) in known:
                    kept[field] = contract[field]
            cleaned.append(kept)
        return cleaned
    if kind != "wallet_journal":
        return payload
    known = _known_parties(db)
    cleaned = []
    for entry in payload:
        kept = {k: entry[k] for k in JOURNAL_FIELDS if k in entry}
        for field in JOURNAL_PARTIES:
            if entry.get(field) in known:
                kept[field] = entry[field]
        if "context_id" in entry:
            context_type = entry.get("context_id_type")
            if context_type not in PERSON_CONTEXTS or entry["context_id"] in known:
                kept["context_id"] = entry["context_id"]
                kept["context_id_type"] = context_type
        cleaned.append(kept)
    return cleaned


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


def last_checked(db: sqlite3.Connection, character_id: int) -> dict[str, str]:
    """When each kind was last tried for a character, successful or not."""
    rows = db.execute(
        "SELECT kind, MAX(checked_at) FROM snapshot_checks WHERE character_id = ? GROUP BY kind",
        (character_id,),
    )
    return {kind: when for kind, when in rows}


def _record_failure(db: sqlite3.Connection, character_id: int, kind: str, detail: str) -> None:
    db.execute(
        "INSERT INTO snapshot_checks (character_id, kind, checked_at, outcome, detail) "
        "VALUES (?, ?, ?, 'failed', ?)",
        (character_id, kind, utc_now(), detail),
    )
    db.commit()


def collect(
    db: sqlite3.Connection, esi: EsiClient, character_id: int, kinds: set[str] | None = None
) -> list[CheckResult]:
    """Fetch every kind (or only `kinds`) for one character. One failing kind does
    not stop the rest; a rate limit stops everything."""
    results = []
    for kind in KINDS:
        if kinds is not None and kind.name not in kinds:
            continue
        try:
            result = esi.get(
                kind.path.format(id=character_id),
                character_id=character_id if kind.needs_token else None,
                remember=not kind.names_others,
            )
        except EsiRateLimited:
            raise
        except EsiError as exc:
            _record_failure(db, character_id, kind.name, str(exc))
            results.append(CheckResult(kind.name, "failed", str(exc)))
            continue
        changed = record(db, character_id, kind.name, sanitise(db, kind.name, result.data))
        results.append(CheckResult(kind.name, "changed" if changed else "unchanged"))
    return results
