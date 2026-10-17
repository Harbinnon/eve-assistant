"""Sessions noticed from EVE's own login and logout times.

ESI's online status carries the character's last login and last logout.
Every time that changes, a snapshot keeps it, so each finished session
leaves a (login, logout) pair in the history: enough to offer "Looks like
you played 18:02-19:10. Record it?" without the player pressing anything
while playing. Nothing is recorded without a yes.

Offered: finished sessions of at least MIN_MINUTES from the last
LOOKBACK_DAYS that don't overlap a recorded session and weren't dismissed.
A pair is only seen if the dashboard (or a refresh) ran between that login
and the next one; sessions in between are missed, and the page says so
rather than guessing.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eve_assistant import sessions
from eve_assistant.store import utc_now

MIN_MINUTES = 10
LOOKBACK_DAYS = 14
SAME_LOGIN = timedelta(minutes=2)  # EVE's times wobble by a second or two between reads
PLAN_BEFORE = timedelta(hours=3)  # plans from this long before login until logout count
NOTE = "Noticed from EVE's login and logout times; no snapshot was taken at either end."


@dataclass(frozen=True)
class Detected:
    character_id: int
    character_name: str
    login: str  # ISO UTC
    logout: str
    plan_id: int | None

    @property
    def minutes(self) -> float:
        return (_parse(self.logout) - _parse(self.login)).total_seconds() / 60


def _parse(stamp: str) -> datetime:
    value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _pairs(db: sqlite3.Connection, character_id: int) -> list[tuple[datetime, datetime]]:
    """Finished (login, logout) pairs from the online-status history, merged
    when EVE reported the same session with slightly different times."""
    found: list[tuple[datetime, datetime]] = []
    rows = db.execute(
        "SELECT payload FROM snapshots WHERE character_id = ? AND kind = 'online' "
        "ORDER BY snapshot_id",
        (character_id,),
    )
    for (payload,) in rows:
        data = json.loads(payload)
        if not isinstance(data, dict) or not data.get("last_login") or not data.get("last_logout"):
            continue
        login, logout = _parse(data["last_login"]), _parse(data["last_logout"])
        if logout <= login:
            continue  # still online then; that logout belongs to an earlier session
        if any(abs(login - known) <= SAME_LOGIN for known, _ in found):
            continue
        found.append((login, logout))
    return found


def _plan_for(
    db: sqlite3.Connection, character_id: int, login: datetime, logout: datetime
) -> int | None:
    """The latest plan made from shortly before logging in until logging out."""
    row = db.execute(
        "SELECT plan_id FROM plans WHERE character_id = ? AND created_at >= ? "
        "AND created_at <= ? ORDER BY created_at DESC LIMIT 1",
        (character_id, _stamp(login - PLAN_BEFORE), _stamp(logout)),
    ).fetchone()
    return row["plan_id"] if row else None


def _overlaps(session: sessions.PlaySession, login: datetime, logout: datetime, now) -> bool:
    start = _parse(session.started_at)
    end = _parse(session.ended_at) if session.ended_at else now
    return start < logout and login < end


def suggestions(db: sqlite3.Connection, now: str | None = None) -> list[Detected]:
    """Sessions worth offering, newest first."""
    now_dt = _parse(now or utc_now())
    oldest = now_dt - timedelta(days=LOOKBACK_DAYS)
    recorded = sessions.recent(db, 500)
    dismissed = {
        (r["character_id"], r["login_at"])
        for r in db.execute("SELECT character_id, login_at FROM detected_session_dismissals")
    }
    offered = []
    for row in db.execute("SELECT character_id, name FROM characters").fetchall():
        character_id = row["character_id"]
        mine = [s for s in recorded if s.character_id == character_id]
        for login, logout in _pairs(db, character_id):
            if login < oldest or (logout - login) < timedelta(minutes=MIN_MINUTES):
                continue
            if (character_id, _stamp(login)) in dismissed:
                continue
            if any(_overlaps(s, login, logout, now_dt) for s in mine):
                continue
            plan_id = _plan_for(db, character_id, login, logout)
            offered.append(
                Detected(character_id, row["name"], _stamp(login), _stamp(logout), plan_id)
            )
    return sorted(offered, key=lambda d: d.login, reverse=True)


def find(db: sqlite3.Connection, character_id: int, login: str, now: str | None = None) -> Detected:
    """The offered session with this login, worked out again from the data
    (never taken from a form), or SessionNotFound."""
    for detected in suggestions(db, now):
        if detected.character_id == character_id and detected.login == login:
            return detected
    raise sessions.SessionNotFound("That session isn't on offer any more.")


def dismiss(db: sqlite3.Connection, character_id: int, login: str, now: str | None = None) -> None:
    """Don't offer this session again."""
    detected = find(db, character_id, login, now)
    db.execute(
        "INSERT OR IGNORE INTO detected_session_dismissals "
        "(character_id, login_at, dismissed_at) VALUES (?, ?, ?)",
        (detected.character_id, detected.login, now or utc_now()),
    )
    db.commit()
