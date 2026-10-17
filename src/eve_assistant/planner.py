"""The session planner: "I have N minutes, what should this character do?"

It gathers candidates from the activity models (ore in the hangar, a mining
session sized to the time left, courier contracts), rates each with the risk
matrix, and builds a plan:

1. quick jobs where the character is docked (selling ore from the hangar,
   queueing skills),
2. one main activity for the remaining time, chosen by ISK after expected
   losses,
3. everything else as alternatives, each with the reason it was not chosen.

Every plan is recorded (append-only) with its predictions, so phase 3 can
compare them with what actually happened.
"""

import json
import sqlite3
from dataclasses import dataclass

from eve_assistant import (
    activities,
    fleet,
    preferences,
    projects,
    risk,
    skills,
    snapshots,
)
from eve_assistant.activities.base import (
    MIN_MINING_MINUTES,
    Context,
    PlanItem,
    selling_station,
)
from eve_assistant.esi.client import EsiClient
from eve_assistant.skills import Reason
from eve_assistant.store import utc_now

# What can be "new to you"; reminders never are.
ACTIVITY_KINDS = ("mining", "courier", "project_start")


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Alternative:
    item: PlanItem
    why_not: str  # code: worse, too_long, red_risk, muted, new_limit


@dataclass(frozen=True)
class Plan:
    character_id: int
    minutes_available: float
    items: tuple[PlanItem, ...]
    alternatives: tuple[Alternative, ...]
    notes: tuple[Reason, ...]
    net_worth: float
    plan_id: int | None = None

    @property
    def total_isk(self) -> float:
        return sum(i.isk for i in self.items)

    @property
    def total_minutes(self) -> float:
        return sum(i.minutes for i in self.items)


def _has_done(db: sqlite3.Connection, character_id: int, kind: str) -> bool:
    if kind == "mining" and snapshots.latest(db, character_id, "mining_ledger"):
        return True
    return (
        db.execute(
            "SELECT 1 FROM plan_responses r JOIN plan_items i USING (item_id) "
            "JOIN plans p USING (plan_id) WHERE p.character_id = ? AND i.kind = ? "
            "AND r.response = 'do' LIMIT 1",
            (character_id, kind),
        ).fetchone()
        is not None
    )


def build_plan(
    db: sqlite3.Connection,
    esi: EsiClient,
    character_id: int,
    minutes: float,
    allow_lowsec: bool = False,
    allow_risky: bool = False,
    ore_names: list[str] | None = None,
) -> Plan:
    if minutes <= 0:
        raise PlanError("Give the number of minutes you have, e.g. 60.")
    notes: list[Reason] = []
    mutes = preferences.active_mutes(db)
    highsec_only = not allow_lowsec or "route:lowsec" in mutes
    worth = risk.net_worth(db, character_id)
    selling_station(db, character_id, notes)  # notes where selling happens
    try:
        ships = fleet.owned_ships(db, character_id)
    except skills.SkillsError as exc:
        notes.append(Reason("mining_unavailable", {"why": str(exc)}))
        ships = []

    context = Context(db, esi, character_id, minutes, worth, highsec_only, ships, ore_names, notes)
    candidates = [item for activity in activities.ALL for item in activity.candidates(context)]
    candidates = [
        PlanItem(
            **{
                **c.__dict__,
                "new_to_you": c.kind in ACTIVITY_KINDS and not _has_done(db, character_id, c.kind),
            }
        )
        for c in candidates
    ]

    alternatives: list[Alternative] = []
    usable: list[PlanItem] = []
    for item in candidates:
        if f"kind:{item.kind}" in mutes:
            alternatives.append(Alternative(item, "muted"))
        elif item.risk.rating == "red" and not allow_risky:
            alternatives.append(Alternative(item, "red_risk"))
        else:
            usable.append(item)

    chosen: list[PlanItem] = []
    time_left = minutes
    new_used = False

    def take(item: PlanItem) -> None:
        nonlocal time_left, new_used
        chosen.append(item)
        time_left -= item.minutes
        new_used = new_used or item.new_to_you

    # 1. quick jobs where the character is docked
    for item in [i for i in usable if i.kind == "skill"]:
        take(item)
    sells = sorted(
        (
            i
            for i in usable
            if i.kind == "sell_hangar"
            and i.inputs.get("jumps") == 0
            and i.inputs.get("travel_jumps", 0) == 0
        ),
        key=lambda i: -i.adjusted_isk,
    )

    def stack(item: PlanItem) -> tuple:
        return (item.inputs.get("station_id"), item.inputs["type_id"])

    sold_stacks: set[tuple] = set()
    # stacks already promised to an open haul project are not sold again
    for project in projects.open_projects(db, character_id):
        if project.kind == "haul":
            station_text, type_text = project.key.split(":")[:2]
            sold_stacks.add((int(station_text), int(type_text)))
    for item in sells:
        if stack(item) in sold_stacks:
            continue
        if item.minutes <= time_left:
            take(item)
            sold_stacks.add(stack(item))

    # 2. open projects come before anything new
    for item in sorted(
        (i for i in usable if i.kind == "project_continue"), key=lambda i: -i.adjusted_isk
    ):
        if time_left >= MIN_MINING_MINUTES:
            session = min(item.minutes, time_left)
            share = session / item.minutes if item.minutes else 0
            take(PlanItem(**{**item.__dict__, "minutes": session, "isk": item.isk * share}))

    # 3. one main activity for the remaining time
    open_count = len(projects.open_projects(db, character_id))
    main_pool = [
        i
        for i in usable
        if i.kind in ("mining", "courier")
        or (
            i.kind == "project_start"
            and open_count < projects.MAX_OPEN
            and not (i.inputs.get("station_id") and stack(i) in sold_stacks)
        )
        or (
            i.kind == "sell_hangar"
            and (i.inputs.get("jumps", 0) > 0 or i.inputs.get("travel_jumps", 0) > 0)
            and stack(i) not in sold_stacks
        )
    ]
    main_pool.sort(key=lambda i: -i.adjusted_isk)
    picked_main = None
    for item in main_pool:
        if item.kind not in ("mining", "project_start") and item.minutes > time_left + 1e-9:
            continue
        if item.kind == "mining" and time_left < MIN_MINING_MINUTES:
            continue
        if item.new_to_you and new_used:
            continue
        if item.kind == "project_start":
            if time_left < MIN_MINING_MINUTES:
                continue
            share = time_left / item.minutes if item.minutes else 0
            item = PlanItem(**{**item.__dict__, "isk": item.isk * share, "minutes": time_left})
        elif item.kind == "mining":
            # the mining estimate was sized for the whole session; rescale to what is left
            share = time_left / item.minutes if item.minutes else 0
            item = PlanItem(**{**item.__dict__, "isk": item.isk * share, "minutes": time_left,
                               "risk": item.risk})  # fmt: skip
        take(item)
        picked_main = item
        break

    for item in usable:
        if item in chosen or (picked_main and item.key == picked_main.key
                              and item.kind == picked_main.kind):  # fmt: skip
            continue
        if item.kind == "project_continue":
            why = "too_long"
        elif item.kind == "project_start" and open_count >= projects.MAX_OPEN:
            why = "too_many_projects"
        elif item.kind == "sell_hangar" and stack(item) in sold_stacks:
            why = "worse"
        elif item.minutes > minutes:
            why = "too_long"
        elif item.new_to_you and new_used:
            why = "new_limit"
        else:
            why = "worse"
        alternatives.append(Alternative(item, why))

    if not chosen:
        notes.append(Reason("nothing_fits"))
    notes.append(Reason("loss_chances_are_guesses"))
    return Plan(
        character_id=character_id,
        minutes_available=minutes,
        items=tuple(chosen),
        alternatives=tuple(sorted(alternatives, key=lambda a: -a.item.adjusted_isk)),
        notes=tuple({n.code: n for n in notes}.values()),
        net_worth=worth,
    )


def record(db: sqlite3.Connection, plan: Plan, inputs: dict) -> Plan:
    """Store the plan, its chosen items (with positions) and the alternatives."""
    with db:
        plan_id = db.execute(
            "INSERT INTO plans (character_id, created_at, minutes_available, inputs) "
            "VALUES (?, ?, ?, ?)",
            (
                plan.character_id,
                utc_now(),
                plan.minutes_available,
                json.dumps(
                    {
                        **inputs,
                        "net_worth": plan.net_worth,
                        "notes": [{"code": n.code, **n.detail} for n in plan.notes],
                    }
                ),
            ),
        ).lastrowid
        rows = [(position, item, None) for position, item in enumerate(plan.items)]
        rows += [(None, alt.item, alt.why_not) for alt in plan.alternatives]
        for position, item, why_not in rows:
            details = item.details()
            if why_not:
                details["why_not"] = why_not
            db.execute(
                "INSERT INTO plan_items (plan_id, position, kind, key, title, isk, minutes, "
                "risk_rating, expected_loss, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_id, position, item.kind, item.key, item.title, item.isk, item.minutes,
                 item.risk.rating, item.risk.expected_loss, json.dumps(details)),
            )  # fmt: skip
    return Plan(**{**plan.__dict__, "plan_id": plan_id})


def respond(db: sqlite3.Connection, item_id: int, response: str) -> None:
    if response not in ("do", "not_now", "mute"):
        raise PlanError("Response must be do, not_now or mute.")
    row = db.execute(
        "SELECT p.character_id, i.kind, i.minutes, i.details FROM plan_items i "
        "JOIN plans p USING (plan_id) WHERE i.item_id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise PlanError(f"No plan item {item_id}.")
    with db:
        db.execute(
            "INSERT INTO plan_responses (item_id, response, responded_at) VALUES (?, ?, ?)",
            (item_id, response, utc_now()),
        )
    if response == "mute":
        category = "kind:courier" if row["kind"] == "project_start" else f"kind:{row['kind']}"
        if category in preferences.KNOWN:
            preferences.mute(db, category)
        return
    if response != "do":
        return
    inputs = json.loads(row["details"]).get("inputs", {})
    try:
        if row["kind"] == "project_start":
            spec = inputs["project"]
            project_id = projects.start(
                db,
                row["character_id"],
                spec["kind"],
                spec["key"],
                spec["title"],
                spec["total_minutes"],
                spec["isk"],
                {"risk": spec.get("risk", {}), "inputs": inputs},
                days_to_deadline=spec.get("deadline_days"),
                origin_item_id=item_id,
            )
            projects.record_progress(db, project_id, row["minutes"], item_id)
        elif row["kind"] == "project_continue":
            projects.record_progress(db, inputs["project_id"], row["minutes"], item_id)
    except projects.ProjectError as exc:
        raise PlanError(str(exc)) from None


@dataclass(frozen=True)
class StoredItem:
    item_id: int
    position: int | None
    kind: str
    title: str
    isk: float
    minutes: float
    risk_rating: str
    expected_loss: float
    details: dict
    response: str | None  # the latest response, if any


@dataclass(frozen=True)
class StoredPlan:
    plan_id: int
    character_id: int
    character_name: str
    created_at: str
    minutes_available: float
    net_worth: float | None
    notes: list[dict]
    items: list[StoredItem]
    alternatives: list[StoredItem]
    allow_lowsec: bool = False  # the options it was made with, to plan again the same way
    allow_risky: bool = False

    @property
    def total_isk(self) -> float:
        return sum(i.isk for i in self.items)

    @property
    def total_minutes(self) -> float:
        return sum(i.minutes for i in self.items)


def load_plan(db: sqlite3.Connection, plan_id: int) -> StoredPlan:
    row = db.execute(
        "SELECT p.*, c.name FROM plans p JOIN characters c USING (character_id) "
        "WHERE p.plan_id = ?",
        (plan_id,),
    ).fetchone()
    if row is None:
        raise PlanError(f"No plan {plan_id}.")
    inputs = json.loads(row["inputs"])
    latest = {
        r["item_id"]: r["response"]
        for r in db.execute(
            "SELECT item_id, response FROM plan_responses WHERE item_id IN "
            "(SELECT item_id FROM plan_items WHERE plan_id = ?) ORDER BY response_id",
            (plan_id,),
        )
    }
    items, alternatives = [], []
    for r in db.execute(
        "SELECT * FROM plan_items WHERE plan_id = ? ORDER BY position IS NULL, position, isk DESC",
        (plan_id,),
    ):
        stored = StoredItem(
            item_id=r["item_id"],
            position=r["position"],
            kind=r["kind"],
            title=r["title"],
            isk=r["isk"],
            minutes=r["minutes"],
            risk_rating=r["risk_rating"],
            expected_loss=r["expected_loss"],
            details=json.loads(r["details"]),
            response=latest.get(r["item_id"]),
        )
        (items if r["position"] is not None else alternatives).append(stored)
    return StoredPlan(
        plan_id=plan_id,
        character_id=row["character_id"],
        character_name=row["name"],
        created_at=row["created_at"],
        minutes_available=row["minutes_available"],
        net_worth=inputs.get("net_worth"),
        notes=inputs.get("notes", []),
        items=items,
        alternatives=alternatives,
        allow_lowsec=bool(inputs.get("allow_lowsec")),
        allow_risky=bool(inputs.get("risky")),
    )


def latest_plan_id(db: sqlite3.Connection, character_id: int | None = None) -> int | None:
    if character_id is None:
        row = db.execute("SELECT MAX(plan_id) FROM plans").fetchone()
    else:
        row = db.execute(
            "SELECT MAX(plan_id) FROM plans WHERE character_id = ?", (character_id,)
        ).fetchone()
    return row[0]


# Steps that are the plan's one main activity (what other activities compete with).
MAIN_KINDS = ("mining", "courier", "project_start")


def _stack(item: StoredItem) -> tuple | None:
    inputs = item.details.get("inputs", {})
    if "type_id" not in inputs or inputs.get("station_id") is None:
        return None
    return (inputs["station_id"], inputs["type_id"])


def _rival(plan: StoredPlan, alt: StoredItem) -> tuple[StoredItem, bool] | None:
    """The chosen step an alternative lost to, and whether it sells the same
    stack of goods (the quick sale where you're docked comes first)."""
    stack = _stack(alt)
    if stack is not None:
        for item in plan.items:
            if item.kind == "sell_hangar" and _stack(item) == stack:
                return item, True
    main = [i for i in plan.items if i.kind in MAIN_KINDS or (
        i.kind == "sell_hangar" and i.details.get("inputs", {}).get("jumps", 0) > 0)]  # fmt: skip
    return (main[-1], False) if main else None


def why_not(plan: StoredPlan, alt: StoredItem) -> Reason:
    """Why an alternative wasn't picked, in numbers specific to it."""
    code = alt.details.get("why_not", "worse")
    if code == "too_long":
        return Reason("too_long", {"minutes": alt.minutes, "available": plan.minutes_available})
    if code != "worse":
        return Reason(code)
    found = _rival(plan, alt)
    if found is None:
        return Reason("worse")
    rival, same_stack = found
    return Reason(
        "same_stack" if same_stack else "less_after_losses",
        {
            "step": plan.items.index(rival) + 1,
            "isk": alt.isk,
            "minutes": alt.minutes,
            "loss": alt.expected_loss,
            "rival_isk": rival.isk,
            "rival_minutes": rival.minutes,
            "rival_loss": rival.expected_loss,
        },
    )
