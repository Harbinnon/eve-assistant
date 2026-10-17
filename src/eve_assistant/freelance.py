"""Freelance Jobs: corporations paying ISK per unit of ore mined or items delivered.

The public listing (ESI /freelance-jobs, no scope) is walked every half hour
and the jobs the planner can use are kept: mining (MineOre) and delivery
(DeliverItem). Only what planning needs is stored: the creator and the
description never are, and job names are player-written, so they are shown
labelled. Raw responses stay out of the ESI cache.

The character's own jobs (esi-characters.read_freelance_jobs.v1) come as a
snapshot kind, so a plan can say whether a job still needs committing to.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from eve_assistant import snapshots
from eve_assistant.esi.client import EsiClient, EsiError
from eve_assistant.store import utc_now

USED = ("MineOre", "DeliverItem")
MAX_PAGES = 5  # listing pages of 100 per refresh
MAX_DETAILS = 150  # job details fetched per refresh; the rest wait for the next
# Location filters the planner understands; a job naming anything else is skipped.
PLACES = ("region", "solarsystem", "station", "structure")


@dataclass(frozen=True)
class Job:
    job_id: str
    name: str  # player-written
    method: str
    type_ids: tuple[int, ...]
    locations: dict[str, list[int]]
    reward_per_unit: float
    unit_limit: int | None
    remaining_units: int

    def units_for(self, quantity: int) -> int:
        """How many of `quantity` units the job would still pay for."""
        cap = (
            self.remaining_units
            if self.unit_limit is None
            else min(self.remaining_units, self.unit_limit)
        )
        return max(0, min(quantity, cap))


def _values(parameter: dict) -> dict[str, list[int]]:
    """{value_type: [ids]} from a job parameter's matcher or values."""
    found: dict[str, list[int]] = {}
    inner = next(iter(parameter.values()), {}) if parameter else {}
    for entry in (inner or {}).get("values", []):
        ids = [int(v) for v in entry.get("values", []) if str(v).isdigit()]
        found.setdefault(entry.get("value_type", "?"), []).extend(ids)
    return found


def parse(detail: dict) -> dict:
    """The planner's view of one job's details."""
    config = detail.get("configuration", {})
    method = config.get("method", "")
    params = config.get("parameters", {})
    type_ids: list[int] = []
    locations: dict[str, list[int]] = {}
    if method == "MineOre":
        type_ids = _values(params.get("ore", {})).get("ore_type", [])
        locations = _values(params.get("location", {}))
    elif method == "DeliverItem":
        delivery = next(iter(params.values()), {}) if params else {}
        delivery = next(iter(delivery.values()), {}) if delivery else {}
        type_ids = _values({"m": delivery.get("item_type", {})}).get("item_type", [])
        locations = _values({"m": delivery.get("corporation_office_location", {})})
    contribution = detail.get("contribution", {})
    progress = detail.get("progress", {})
    access = detail.get("access_and_visibility", {})
    return {
        "job_id": str(detail["id"]),
        "name": str(detail.get("name", ""))[:200],
        "method": method,
        "state": detail.get("state", "Unspecified"),
        "last_modified": detail.get("last_modified"),
        "type_ids": type_ids,
        "locations": locations,
        "reward_per_unit": float(contribution.get("reward_per_contribution", 0) or 0)
        * float(contribution.get("submission_multiplier", 1) or 1),
        "unit_limit": contribution.get("contribution_per_participant_limit"),
        "progress_current": int(progress.get("current", 0) or 0),
        "progress_desired": int(progress.get("desired", 0) or 0),
        "reward_remaining": float(detail.get("reward", {}).get("remaining", 0) or 0),
        "expires": detail.get("details", {}).get("expires"),
        "restricted": bool(access.get("acl_protected")),
    }


def _store(db: sqlite3.Connection, job: dict, now: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO freelance_jobs (job_id, name, method, state, last_modified, "
        "type_ids, locations, reward_per_unit, unit_limit, progress_current, progress_desired, "
        "reward_remaining, expires, restricted, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job["job_id"], job["name"], job["method"], job["state"], job["last_modified"],
         json.dumps(job["type_ids"]), json.dumps(job["locations"]), job["reward_per_unit"],
         job["unit_limit"], job["progress_current"], job["progress_desired"],
         job["reward_remaining"], job["expires"], int(job["restricted"]), now),
    )  # fmt: skip


def refresh(db: sqlite3.Connection, esi: EsiClient, now: str | None = None) -> int:
    """Walk the public listing and update the jobs that changed. Returns how
    many job details were fetched."""
    now = now or utc_now()
    known = dict(db.execute("SELECT job_id, last_modified FROM freelance_jobs").fetchall())
    seen: set[str] = set()
    fetched, cursor = 0, None
    for _page in range(MAX_PAGES):
        listing = esi.get(
            "/freelance-jobs",
            params={"limit": 100, **({"after": cursor} if cursor else {})},
            remember=False,
        ).data
        for summary in listing.get("freelance_jobs", []):
            job_id = str(summary["id"])
            seen.add(job_id)
            if summary.get("state") != "Active":
                db.execute(
                    "UPDATE freelance_jobs SET state = ? WHERE job_id = ?",
                    (summary.get("state", "Closed"), job_id),
                )
                continue
            if known.get(job_id) == summary.get("last_modified") or fetched >= MAX_DETAILS:
                continue
            try:
                detail = esi.get(f"/freelance-jobs/{job_id}", remember=False).data
            except EsiError:
                continue
            fetched += 1
            _store(db, parse(detail), now)
        cursor = (listing.get("cursor") or {}).get("after")
        if not cursor or not listing.get("freelance_jobs"):
            break
    db.commit()
    return fetched


def _place_matches(db: sqlite3.Connection, locations: dict, system_id: int | None) -> bool:
    if not locations:
        return True  # anywhere
    if set(locations) - set(PLACES) or system_id is None:
        return False
    if system_id in locations.get("solarsystem", []):
        return True
    row = db.execute(
        "SELECT region_id FROM sde_solar_systems WHERE system_id = ?", (system_id,)
    ).fetchone()
    return bool(row) and row["region_id"] in locations.get("region", [])


def _active(db: sqlite3.Connection, method: str, now: str) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM freelance_jobs WHERE method = ? AND state = 'Active' AND restricted = 0 "
        "AND progress_current < progress_desired AND reward_remaining > 0 "
        "AND (expires IS NULL OR expires > ?) ORDER BY reward_per_unit DESC",
        (method, now),
    ).fetchall()


def _job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=row["job_id"],
        name=row["name"],
        method=row["method"],
        type_ids=tuple(json.loads(row["type_ids"])),
        locations=json.loads(row["locations"]),
        reward_per_unit=row["reward_per_unit"],
        unit_limit=row["unit_limit"],
        remaining_units=row["progress_desired"] - row["progress_current"],
    )


def _now_eve() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def mining_job(
    db: sqlite3.Connection, type_id: int, system_id: int | None, now: str | None = None
) -> Job | None:
    """The best-paying open mining job for this ore where the character mines."""
    for row in _active(db, "MineOre", now or _now_eve()):
        job = _job(row)
        if type_id in job.type_ids and _place_matches(db, job.locations, system_id):
            return job
    return None


def delivery_jobs(db: sqlite3.Connection, type_id: int, now: str | None = None) -> list[Job]:
    """Open delivery jobs wanting this item, with a station or structure to deliver to."""
    found = []
    for row in _active(db, "DeliverItem", now or _now_eve()):
        job = _job(row)
        if type_id in job.type_ids and (
            job.locations.get("station") or job.locations.get("structure")
        ):
            found.append(job)
    return found


def committed(db: sqlite3.Connection, character_id: int) -> set[str]:
    """Jobs the character has taken on (from its own jobs snapshot)."""
    mine = snapshots.latest(db, character_id, "freelance_jobs") or {}
    return {str(j["id"]) for j in mine.get("freelance_jobs", []) if j.get("state") == "Active"}
