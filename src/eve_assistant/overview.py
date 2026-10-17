"""Read-only summaries of each character, for the dashboard and CLI."""

import sqlite3
from dataclasses import dataclass

from eve_assistant import skills, snapshots, structures


@dataclass(frozen=True)
class CharacterSummary:
    character_id: int
    name: str
    account: str | None
    clone_state: str
    total_sp: int
    alpha_locked_skills: int
    alpha_training_blocked: bool
    wallet: float | None
    system_name: str | None
    security: float | None
    region_name: str | None
    docked: bool
    structure_name: str | None  # player-written; only for structures we can see
    ship_type: str | None
    ship_name: str | None  # player-written
    online: bool | None
    queue_length: int
    training: str | None  # e.g. "Mining 4", when the queue is running
    last_checked: str | None


def _system(db: sqlite3.Connection, system_id: int | None):
    if system_id is None:
        return None, None, None
    row = db.execute(
        "SELECT s.name, s.security_status, r.name AS region FROM sde_solar_systems s "
        "LEFT JOIN sde_regions r USING (region_id) WHERE s.system_id = ?",
        (system_id,),
    ).fetchone()
    return (row["name"], row["security_status"], row["region"]) if row else (None, None, None)


def _structure_name(db: sqlite3.Connection, structure_id: int | None) -> str | None:
    known = structures.get(db, structure_id) if structure_id else None
    return known.player_name if known and known.accessible else None


def summarize(db: sqlite3.Connection, character_id: int) -> CharacterSummary:
    row = db.execute(
        "SELECT c.name, a.label, a.clone_state FROM characters c "
        "LEFT JOIN accounts a USING (account_id) WHERE c.character_id = ?",
        (character_id,),
    ).fetchone()
    skills_data = snapshots.latest(db, character_id, "skills") or {"skills": []}
    location = snapshots.latest(db, character_id, "location") or {}
    ship = snapshots.latest(db, character_id, "ship") or {}
    online = snapshots.latest(db, character_id, "online") or {}
    queue = snapshots.latest(db, character_id, "skillqueue") or []
    wallet = snapshots.latest(db, character_id, "wallet")
    system, security, region = _system(db, location.get("solar_system_id"))

    clone_state = (row["clone_state"] if row else None) or "unknown"
    total_sp = skills_data.get("total_sp", 0) + skills_data.get("unallocated_sp", 0)
    locked = sum(
        1 for s in skills_data["skills"] if s["active_skill_level"] < s["trained_skill_level"]
    )
    training = None
    active = [e for e in queue if e.get("start_date") and e.get("finish_date")]
    if active:
        first = min(active, key=lambda e: e["queue_position"])
        training = f"{skills.type_name(db, first['skill_id'])} {first['finished_level']}"
    checked = db.execute(
        "SELECT MAX(checked_at) FROM snapshot_checks WHERE character_id = ?", (character_id,)
    ).fetchone()[0]

    return CharacterSummary(
        character_id=character_id,
        name=row["name"] if row else str(character_id),
        account=row["label"] if row else None,
        clone_state=clone_state,
        total_sp=total_sp,
        alpha_locked_skills=locked if clone_state != "omega" else 0,
        alpha_training_blocked=clone_state != "omega" and total_sp >= skills.ALPHA_QUEUE_SP_LIMIT,
        wallet=float(wallet) if wallet is not None else None,
        system_name=system,
        security=security,
        region_name=region,
        docked=bool(location.get("station_id") or location.get("structure_id")),
        structure_name=_structure_name(db, location.get("structure_id")),
        ship_type=skills.type_name(db, ship["ship_type_id"]) if ship.get("ship_type_id") else None,
        ship_name=ship.get("ship_name"),
        online=online.get("online"),
        queue_length=len(queue),
        training=training,
        last_checked=checked,
    )


def default_character(db: sqlite3.Connection) -> int | None:
    """The character to show when none was chosen: one that is online (by
    name), else the first by name; None without characters."""
    ids = [r[0] for r in db.execute("SELECT character_id FROM characters ORDER BY name")]
    for character_id in ids:
        if (snapshots.latest(db, character_id, "online") or {}).get("online"):
            return character_id
    return ids[0] if ids else None


def all_characters(db: sqlite3.Connection) -> list[CharacterSummary]:
    ids = [r["character_id"] for r in db.execute("SELECT character_id FROM characters")]
    return sorted((summarize(db, cid) for cid in ids), key=lambda c: -c.total_sp)
