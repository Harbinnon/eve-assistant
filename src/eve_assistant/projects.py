"""Multi-session projects and the player's weekly play time.

A project is work too long for one session: an ore haul needing several
trips, or a courier contract with days to deliver. It is started from a
plan suggestion, shows up first in later plans until it is finished or
dropped, and records progress in an append-only log.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eve_assistant.store import utc_now

DEFAULT_WEEKLY_MINUTES = 360.0  # 6 hours a week until the real figure is learned
MAX_OPEN = 2
# Chance a started project is not finished: a starting guess, flagged as such.
NOT_FINISHING_CHANCE = 0.10


class ProjectError(Exception):
    pass


@dataclass(frozen=True)
class Project:
    project_id: int
    character_id: int
    kind: str
    key: str
    title: str
    total_minutes: float
    isk: float
    deadline: str | None
    status: str
    created_at: str
    minutes_done: float
    details: dict

    @property
    def minutes_left(self) -> float:
        return max(0.0, self.total_minutes - self.minutes_done)

    @property
    def share_done(self) -> float:
        return min(1.0, self.minutes_done / self.total_minutes) if self.total_minutes else 1.0


# --- play time -------------------------------------------------------------


def weekly_minutes(db: sqlite3.Connection) -> float:
    row = db.execute("SELECT value FROM settings WHERE key = 'weekly_minutes'").fetchone()
    return float(json.loads(row["value"])) if row else DEFAULT_WEEKLY_MINUTES


def set_weekly_minutes(db: sqlite3.Connection, minutes: float) -> None:
    if not 0 < minutes <= 7 * 24 * 60:
        raise ProjectError("Play time per week must be between a minute and a whole week.")
    db.execute(
        "INSERT INTO settings (key, value) VALUES ('weekly_minutes', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(minutes),),
    )
    db.commit()


def finishes_in_time(total_minutes: float, days: float, weekly: float) -> bool:
    """Whether the player's usual pace gets through the work before the deadline."""
    return total_minutes <= days * weekly / 7


# --- projects --------------------------------------------------------------


def _load(db: sqlite3.Connection, row) -> Project:
    done = db.execute(
        "SELECT COALESCE(SUM(minutes), 0) FROM project_progress WHERE project_id = ?",
        (row["project_id"],),
    ).fetchone()[0]
    return Project(
        project_id=row["project_id"],
        character_id=row["character_id"],
        kind=row["kind"],
        key=row["key"],
        title=row["title"],
        total_minutes=row["total_minutes"],
        isk=row["isk"],
        deadline=row["deadline"],
        status=row["status"],
        created_at=row["created_at"],
        minutes_done=float(done),
        details=json.loads(row["details"]),
    )


def open_projects(db: sqlite3.Connection, character_id: int | None = None) -> list[Project]:
    if character_id is None:
        rows = db.execute("SELECT * FROM projects WHERE status = 'open' ORDER BY project_id")
    else:
        rows = db.execute(
            "SELECT * FROM projects WHERE status = 'open' AND character_id = ? ORDER BY project_id",
            (character_id,),
        )
    return [_load(db, r) for r in rows.fetchall()]


def get(db: sqlite3.Connection, project_id: int) -> Project:
    row = db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
    if row is None:
        raise ProjectError(f"No project {project_id}.")
    return _load(db, row)


def start(
    db: sqlite3.Connection,
    character_id: int,
    kind: str,
    key: str,
    title: str,
    total_minutes: float,
    isk: float,
    details: dict,
    days_to_deadline: float | None = None,
    origin_item_id: int | None = None,
) -> int:
    if len(open_projects(db, character_id)) >= MAX_OPEN:
        raise ProjectError(
            f"This character already has {MAX_OPEN} open projects. Finish or drop one first."
        )
    existing = db.execute(
        "SELECT project_id FROM projects WHERE character_id = ? AND kind = ? AND key = ? "
        "AND status = 'open'",
        (character_id, kind, key),
    ).fetchone()
    if existing:
        return existing["project_id"]
    deadline = None
    if days_to_deadline is not None:
        deadline = (datetime.now(UTC) + timedelta(days=days_to_deadline)).isoformat(
            timespec="seconds"
        )
    cursor = db.execute(
        "INSERT INTO projects (character_id, kind, key, title, total_minutes, isk, deadline, "
        "created_at, origin_item_id, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (character_id, kind, key, title, total_minutes, isk, deadline, utc_now(),
         origin_item_id, json.dumps(details)),
    )  # fmt: skip
    db.commit()
    return cursor.lastrowid


def record_progress(
    db: sqlite3.Connection, project_id: int, minutes: float, item_id: int | None = None
) -> None:
    project = get(db, project_id)
    if project.status != "open":
        raise ProjectError(f"Project {project_id} is {project.status}.")
    db.execute(
        "INSERT INTO project_progress (project_id, recorded_at, minutes, item_id) "
        "VALUES (?, ?, ?, ?)",
        (project_id, utc_now(), minutes, item_id),
    )
    db.commit()


def close(db: sqlite3.Connection, project_id: int, status: str) -> None:
    if status not in ("done", "dropped"):
        raise ProjectError("A project is closed as done or dropped.")
    updated = db.execute(
        "UPDATE projects SET status = ?, closed_at = ? WHERE project_id = ? AND status = 'open'",
        (status, utc_now(), project_id),
    ).rowcount
    if not updated:
        raise ProjectError(f"No open project {project_id}.")
    db.commit()
