"""Past plans against what happened, step by step.

This is where the learning can be checked rather than trusted: each played
session shows its plan's steps with what was predicted and what the data
says happened. Matching is by what each step is about:

- mining: ore of that type mined (game logs if read, else the mining
  ledger), and m3 per minute against the plan's estimate;
- selling or hauling ore from the hangar: units of that type sold, and the ISK;
- courier contracts: the contract's own status (delivered in the session,
  or accepted and still underway), else contract ISK in the session;
- skill reminders and project steps aren't measured.
"""

import sqlite3
from dataclasses import dataclass

from eve_assistant import contracts, outcomes, planner
from eve_assistant.outcomes import Outcome
from eve_assistant.planner import StoredItem, StoredPlan
from eve_assistant.sessions import PlaySession

VERDICTS = {
    "seen": "happened",
    "underway": "accepted, not delivered yet",
    "not_seen": "no sign of it",
    "waiting": "waiting for EVE's data",
    "not_measured": "not measured",
    "unknown": "can't tell from the data",
}


@dataclass(frozen=True)
class StepResult:
    item: StoredItem
    verdict: str  # a key of VERDICTS
    actual_isk: float | None = None
    actual_units: int | None = None
    predicted_units: int | None = None
    predicted_rate: float | None = None
    actual_rate: float | None = None
    source: str | None = None  # logs, ledger, trades, journal

    @property
    def verdict_text(self) -> str:
        return VERDICTS[self.verdict]

    @property
    def isk_share(self) -> float | None:
        if self.actual_isk is None or not self.item.isk:
            return None
        return self.actual_isk / self.item.isk

    @property
    def rate_share(self) -> float | None:
        if self.actual_rate is None or not self.predicted_rate:
            return None
        return self.actual_rate / self.predicted_rate


@dataclass(frozen=True)
class SessionHistory:
    session: PlaySession
    outcome: Outcome | None
    plan: StoredPlan | None
    steps: list[StepResult]


def _mining_rate(item: StoredItem) -> float | None:
    for reason in item.details.get("reasons", []):
        if reason.get("code") == "mining_estimate":
            return reason.get("m3_per_minute")
    return None


def _mining_step(item: StoredItem, outcome: Outcome) -> StepResult:
    inputs = item.details.get("inputs", {})
    type_id, predicted = inputs.get("type_id"), inputs.get("quantity")
    logs = outcome.log_mining
    predicted_rate = _mining_rate(item)
    if logs:
        units = sum(o["units"] for o in logs["ores"] if o["type_id"] == type_id)
        return StepResult(
            item,
            "seen" if units else "not_seen",
            actual_units=units,
            predicted_units=predicted,
            predicted_rate=predicted_rate,
            actual_rate=logs.get("m3_per_active_minute"),
            source="logs",
        )
    mined = outcome.details.get("mined")
    if mined is None:
        return StepResult(item, "unknown", predicted_units=predicted, predicted_rate=predicted_rate)
    units = sum(m["units"] for m in mined if m["type_id"] == type_id)
    if not units and not outcome.details["data"].get("ledger_settled"):
        verdict = "waiting"
    else:
        verdict = "seen" if units else "not_seen"
    return StepResult(
        item,
        verdict,
        actual_units=units,
        predicted_units=predicted,
        predicted_rate=predicted_rate,
        source="ledger",
    )


def _selling_step(item: StoredItem, outcome: Outcome) -> StepResult:
    inputs = item.details.get("inputs", {})
    type_id = inputs.get("type_id")
    sold = [s for s in outcome.details.get("sold", []) if s["type_id"] == type_id]
    units = sum(s["units"] for s in sold)
    isk = sum(s["isk"] for s in sold)
    if not units:
        verdict = "not_seen" if outcome.details["data"].get("journal_settled") else "waiting"
    else:
        verdict = "seen"
    return StepResult(
        item,
        verdict,
        actual_isk=isk if units else None,
        actual_units=units,
        predicted_units=inputs.get("quantity"),
        source="trades",
    )


def _courier_step(item: StoredItem, outcome: Outcome, known: dict[int, dict]) -> StepResult:
    contract = known.get(item.details.get("inputs", {}).get("contract_id"))
    if contract is not None:
        window = outcome.details["window"]
        if contracts.delivered_between(contract, window["start"], window["end"]):
            return StepResult(item, "seen", actual_isk=contract.get("reward"), source="contract")
        # The acceptor ID is only kept when it is one of the owner's characters.
        if contract.get("status") == "in_progress" and contract.get("acceptor_id"):
            return StepResult(item, "underway", source="contract")
    income = outcome.details["isk"].get("contracts", 0.0)
    if income > 0:
        return StepResult(item, "seen", actual_isk=income, source="journal")
    return StepResult(item, "unknown")


def compare_step(
    item: StoredItem, outcome: Outcome | None, known: dict[int, dict] | None = None
) -> StepResult:
    if item.kind == "skill" or item.kind.startswith("project"):
        return StepResult(item, "not_measured")
    if outcome is None:
        return StepResult(item, "waiting")
    if item.kind == "mining":
        return _mining_step(item, outcome)
    if item.kind in ("sell_hangar", "haul"):
        return _selling_step(item, outcome)
    if item.kind == "courier":
        return _courier_step(item, outcome, known or {})
    return StepResult(item, "not_measured")


def sessions_history(db: sqlite3.Connection, limit: int = 20) -> list[SessionHistory]:
    """Recent finished sessions, newest first, each with its plan compared."""
    found = []
    for session, outcome in outcomes.recent(db, limit):
        plan = None
        if session.plan_id is not None:
            try:
                plan = planner.load_plan(db, session.plan_id)
            except planner.PlanError:
                plan = None
        known = contracts.mine(db, session.character_id)
        steps = [compare_step(item, outcome, known) for item in plan.items] if plan else []
        found.append(SessionHistory(session, outcome, plan, steps))
    return found
