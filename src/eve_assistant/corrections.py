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
- **sale_price**: what the session's market sales got (before tax, from
  the wallet transactions) against the unit prices its plan expected for
  the same items and units. Only items a step of the plan priced count.
- **minutes_per_jump**: minutes a jump in the session's travel (from the
  location checks, see travel.py) against the minutes a jump its plan
  assumed (MINUTES_PER_JUMP before plans recorded it). Needs at least
  travel.MIN_JUMPS jumps.
"""

import json
import sqlite3
from dataclasses import dataclass

from eve_assistant import travel
from eve_assistant.skills import Reason

PRIOR_SESSIONS = 3.0  # how many sessions' weight the starting guess keeps
LIMITS = (0.5, 1.5)
MIN_ACTIVE_MINUTES = 10.0  # shorter mining stretches are too noisy to learn from
WHATS = ("mining_rate", "sale_price", "minutes_per_jump")


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


def _details(db: sqlite3.Connection, item_id: int) -> dict:
    row = db.execute("SELECT details FROM plan_items WHERE item_id = ?", (item_id,)).fetchone()
    return json.loads(row["details"]) if row else {}


def _steps_done(db: sqlite3.Connection, plan: dict | None) -> list[dict]:
    """The details of the plan's steps the player didn't decline."""
    if not plan:
        return []
    return [_details(db, i["item_id"]) for i in plan["items"] if i.get("response") in (None, "do")]


def _predicted_mining_rate(db: sqlite3.Connection, item_id: int) -> float | None:
    for reason in _details(db, item_id).get("reasons", []):
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


def sale_observations(db: sqlite3.Connection, character_id: int) -> list[Observation]:
    """Sessions whose market sales include items their plan priced: ISK got
    against the plan's unit prices for the units sold."""
    found = []
    for outcome in _latest_outcomes(db, character_id):
        expected_prices: dict[int, float] = {}
        for step in _steps_done(db, outcome.get("plan")):
            for type_id, price in step.get("inputs", {}).get("unit_prices", {}).items():
                expected_prices.setdefault(int(type_id), float(price))
        expected = actual = 0.0
        for line in outcome.get("sold", []):
            price = expected_prices.get(line["type_id"])
            if price and line["units"] > 0:
                expected += line["units"] * price
                actual += line["isk"]
        if expected > 0:
            found.append(Observation(outcome["session_id"], expected, actual))
    return found


def _assumed_minutes_per_jump(steps: list[dict]) -> float:
    for step in steps:
        for reason in step.get("reasons", []):
            if reason.get("code") == "assumed_minutes_per_jump" and reason.get("base"):
                return float(reason["base"])
    return travel.MINUTES_PER_JUMP


def travel_observations(db: sqlite3.Connection, character_id: int) -> list[Observation]:
    """Sessions with at least travel.MIN_JUMPS timed jumps: minutes a jump
    against what the plan assumed."""
    found = []
    for outcome in _latest_outcomes(db, character_id):
        moved = outcome.get("travel")
        if not moved or moved["jumps"] < travel.MIN_JUMPS or moved["minutes"] <= 0:
            continue
        assumed = _assumed_minutes_per_jump(_steps_done(db, outcome.get("plan")))
        found.append(Observation(outcome["session_id"], assumed, moved["minutes"] / moved["jumps"]))
    return found


OBSERVATIONS = {
    "mining_rate": mining_observations,
    "sale_price": sale_observations,
    "minutes_per_jump": travel_observations,
}


def correction(db: sqlite3.Connection, character_id: int, what: str) -> Correction:
    if what not in WHATS:
        raise ValueError(f"Unknown correction {what!r}.")
    observations = OBSERVATIONS[what](db, character_id)
    if not observations:
        return Correction(what, 1.0, 0, None)
    n = len(observations)
    average = sum(o.ratio for o in observations) / n
    factor = 1 + (average - 1) * n / (n + PRIOR_SESSIONS)
    low, high = LIMITS
    return Correction(what, min(high, max(low, factor)), n, average)


def minutes_per_jump(db: sqlite3.Connection, character_id: int) -> tuple[float, Reason]:
    """The minutes a jump plans use for this character, and the reason that
    records it: the starting assumption scaled by the learned correction."""
    learned = correction(db, character_id, "minutes_per_jump")
    used = travel.MINUTES_PER_JUMP * learned.factor
    return used, Reason(
        "assumed_minutes_per_jump",
        {"minutes": round(used, 2), "base": travel.MINUTES_PER_JUMP, "sessions": learned.sessions},
    )
