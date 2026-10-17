"""Per-activity corrections learned from what actually happened.

A correction is a factor applied to one of the planner's estimates, worked
out from the player's own sessions: "the static data says 384 m3 a minute,
you really mine about 307". Each session gives a ratio of actual to
predicted. The factor starts at 1 (the published figures) and moves towards
the average ratio as sessions accumulate, so one odd session can't swing it
far:

    factor = 1 + (average ratio - 1) * n / (n + PRIOR_SESSIONS)

and it is kept within LIMITS. Predictions always store the *uncorrected*
estimate, so later sessions are compared against the static data, not
against an estimate that was already corrected.

Corrections learned so far:

- **mining_rate**: m3 per minute while actually mining (first to last
  cycle, from the game logs) against the plan's estimate for that session.
  Needs the game logs; without them it stays at 1 and says so.
"""

import json
import sqlite3
from dataclasses import dataclass

from eve_assistant.skills import Reason

PRIOR_SESSIONS = 3.0  # how many sessions' weight the starting guess keeps
LIMITS = (0.5, 1.5)
MIN_ACTIVE_MINUTES = 10.0  # shorter mining stretches are too noisy to learn from
WHATS = ("mining_rate",)


@dataclass(frozen=True)
class Observation:
    session_id: int
    predicted: float
    actual: float

    @property
    def ratio(self) -> float:
        return self.actual / self.predicted


@dataclass(frozen=True)
class Correction:
    what: str
    factor: float
    sessions: int
    average_ratio: float | None

    def reason(self) -> Reason:
        if not self.sessions:
            return Reason("not_learned_yet", {"what": self.what})
        return Reason(
            "learned_correction",
            {
                "what": self.what,
                "factor": round(self.factor, 3),
                "sessions": self.sessions,
                "average_ratio": round(self.average_ratio, 3),
            },
        )


def _latest_outcomes(db: sqlite3.Connection, character_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT o.session_id, o.details FROM session_outcomes o "
        "JOIN play_sessions s USING (session_id) "
        "WHERE s.character_id = ? AND o.outcome_id = ("
        " SELECT MAX(outcome_id) FROM session_outcomes WHERE session_id = o.session_id) "
        "ORDER BY s.started_at",
        (character_id,),
    )
    return [{"session_id": r["session_id"], **json.loads(r["details"])} for r in rows]


def _predicted_mining_rate(db: sqlite3.Connection, item_id: int) -> float | None:
    row = db.execute("SELECT details FROM plan_items WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    for reason in json.loads(row["details"]).get("reasons", []):
        if reason.get("code") == "mining_estimate" and reason.get("m3_per_minute"):
            return float(reason["m3_per_minute"])
    return None


def mining_observations(db: sqlite3.Connection, character_id: int) -> list[Observation]:
    """Sessions whose plan had a mining step the player didn't decline, and whose
    game logs show at least MIN_ACTIVE_MINUTES of mining."""
    found = []
    for outcome in _latest_outcomes(db, character_id):
        logs, plan = outcome.get("log_mining"), outcome.get("plan")
        if not logs or not plan or not logs.get("m3_per_active_minute"):
            continue
        if logs.get("active_minutes", 0) < MIN_ACTIVE_MINUTES:
            continue
        step = next(
            (
                i
                for i in plan["items"]
                if i["kind"] == "mining" and i.get("response") in (None, "do")
            ),
            None,
        )
        if step is None:
            continue
        predicted = _predicted_mining_rate(db, step["item_id"])
        if predicted:
            found.append(
                Observation(outcome["session_id"], predicted, logs["m3_per_active_minute"])
            )
    return found


def correction(db: sqlite3.Connection, character_id: int, what: str) -> Correction:
    if what not in WHATS:
        raise ValueError(f"Unknown correction {what!r}.")
    observations = mining_observations(db, character_id)
    if not observations:
        return Correction(what, 1.0, 0, None)
    n = len(observations)
    average = sum(o.ratio for o in observations) / n
    factor = 1 + (average - 1) * n / (n + PRIOR_SESSIONS)
    low, high = LIMITS
    return Correction(what, min(high, max(low, factor)), n, average)
