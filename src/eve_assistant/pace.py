"""The player's real pace: play time per week, how often projects finish,
and ISK banked per hour. Learned from recorded sessions and projects.

Each figure starts from a setting or a published guess and moves towards
what the player actually does as evidence builds up, the same way as the
planner's corrections:

    learned = (prior * PRIOR + observed * n) / (PRIOR + n)

Play time counts real clock time, so two characters playing at once count
once. Only complete weeks (Monday to Sunday, UTC) from the last
LOOKBACK_WEEKS that have at least one session are counted: a week with no
recorded session more likely means the buttons weren't used than that
nobody played, which would drag the figure down.
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eve_assistant import projects
from eve_assistant.skills import Reason
from eve_assistant.store import utc_now

LOOKBACK_WEEKS = 8
WEEKS_PRIOR = 2.0  # weeks' weight the setting keeps
PROJECTS_PRIOR = 3.0  # closed projects' weight the 10% guess keeps
MIN_SESSIONS_FOR_RATE = 2


def _parse(stamp: str) -> datetime:
    value = datetime.fromisoformat(stamp)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _week_start(moment: datetime) -> datetime:
    day = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


@dataclass(frozen=True)
class PlayTime:
    minutes: float  # what planning uses
    setting: float  # the player's setting, or the default
    weeks: int  # complete weeks with sessions that were counted
    observed: float | None  # average minutes in those weeks

    def reason(self) -> Reason:
        if not self.weeks:
            return Reason("play_time_setting", {"weekly_minutes": self.setting})
        return Reason(
            "play_time_learned",
            {
                "weekly_minutes": round(self.minutes),
                "observed_minutes": round(self.observed),
                "weeks": self.weeks,
                "setting_minutes": self.setting,
            },
        )


def _session_intervals(db: sqlite3.Connection) -> list[tuple[datetime, datetime]]:
    rows = db.execute(
        "SELECT s.started_at, e.ended_at FROM play_sessions s "
        "JOIN play_session_ends e USING (session_id) WHERE e.how = 'finished'"
    )
    return [(_parse(r["started_at"]), _parse(r["ended_at"])) for r in rows]


def _union_minutes_by_week(intervals) -> dict[datetime, float]:
    """Clock minutes played per week, overlapping sessions counted once."""
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    weeks: dict[datetime, float] = {}
    for start, end in merged:
        while start < end:  # split at week boundaries
            week = _week_start(start)
            stop = min(end, week + timedelta(days=7))
            weeks[week] = weeks.get(week, 0.0) + (stop - start).total_seconds() / 60
            start = stop
    return weeks


def weekly_play(db: sqlite3.Connection, now: str | None = None) -> PlayTime:
    setting = projects.weekly_minutes(db)
    this_week = _week_start(_parse(now or utc_now()))
    oldest = this_week - timedelta(weeks=LOOKBACK_WEEKS)
    weeks = {
        week: minutes
        for week, minutes in _union_minutes_by_week(_session_intervals(db)).items()
        if oldest <= week < this_week
    }
    if not weeks:
        return PlayTime(setting, setting, 0, None)
    n = len(weeks)
    observed = sum(weeks.values()) / n
    learned = (setting * WEEKS_PRIOR + observed * n) / (WEEKS_PRIOR + n)
    return PlayTime(learned, setting, n, observed)


@dataclass(frozen=True)
class FinishRate:
    not_finishing: float  # chance a started project isn't finished
    done: int
    dropped: int

    def reason(self) -> Reason:
        if not self.done + self.dropped:
            return Reason("finish_rate_guess", {"not_finishing": self.not_finishing})
        return Reason(
            "finish_rate_learned",
            {
                "not_finishing": round(self.not_finishing, 3),
                "done": self.done,
                "dropped": self.dropped,
            },
        )


def finish_rate(db: sqlite3.Connection) -> FinishRate:
    counts = dict(
        db.execute(
            "SELECT status, COUNT(*) FROM projects WHERE status IN ('done', 'dropped') "
            "GROUP BY status"
        ).fetchall()
    )
    done, dropped = counts.get("done", 0), counts.get("dropped", 0)
    n = done + dropped
    prior = projects.NOT_FINISHING_CHANCE
    if not n:
        return FinishRate(prior, 0, 0)
    learned = (prior * PROJECTS_PRIOR + dropped) / (PROJECTS_PRIOR + n)
    return FinishRate(learned, done, dropped)


@dataclass(frozen=True)
class EarningRate:
    isk_per_hour: float
    sessions: int
    hours: float


def banked_per_hour(db: sqlite3.Connection, character_id: int) -> EarningRate | None:
    """ISK that actually reached the wallet per hour played, from settled
    session outcomes. Ore mined but still in the hangar isn't counted, so
    this is the cautious figure."""
    rows = db.execute(
        "SELECT s.started_at, e.ended_at, json_extract(o.details, '$.isk.net') AS net "
        "FROM session_outcomes o JOIN play_sessions s USING (session_id) "
        "JOIN play_session_ends e USING (session_id) "
        "WHERE s.character_id = ? AND o.settled = 1",
        (character_id,),
    ).fetchall()
    if len(rows) < MIN_SESSIONS_FOR_RATE:
        return None
    hours = sum(
        (_parse(r["ended_at"]) - _parse(r["started_at"])).total_seconds() / 3600 for r in rows
    )
    if hours <= 0:
        return None
    return EarningRate(sum(r["net"] or 0.0 for r in rows) / hours, len(rows), hours)


def weeks_to(isk_needed: float, rate: EarningRate | None, weekly_minutes: float) -> float | None:
    """Weeks to bank `isk_needed` at this rate and play time; None if it can't be said."""
    if rate is None or rate.isk_per_hour <= 0 or weekly_minutes <= 0:
        return None
    return isk_needed / (rate.isk_per_hour * weekly_minutes / 60)
