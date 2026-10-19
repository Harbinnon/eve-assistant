"""The session planner: "I have N minutes, what should this character do?"

It gathers candidates from the activities (activities/: skills, projects,
selling, mining, couriers; each a module with one interface), each rated
with the risk matrix, and builds a plan:

1. quick jobs where the character is docked (selling ore from the hangar,
   queueing skills),
2. one main activity for the remaining time, chosen by ISK after expected
   losses,
3. everything else as alternatives, each with the reason it was not chosen.

Every plan is recorded (append-only) with its predictions, so phase 3 can
compare them with what actually happened.
"""

import dataclasses
import json
import sqlite3
from dataclasses import dataclass, field

from eve_assistant import (
    activities,
    campaigns,
    fleet,
    preferences,
    projects,
    risk,
    signals,
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
ACTIVITY_KINDS = (
    "mining",
    "courier",
    "project_start",
    "gas",
    "manufacturing",
    "mission",
    "exploration",
    "trade",
)
# Main activities sized to the time left (the rest take the minutes they need).
SIZED_KINDS = ("mining", "project_start", "gas", "mission", "exploration")


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


@dataclass
class _Fitting:
    """The plan as it fills up: what is chosen, the time left, which stacks of
    goods are already sold, and whether the one new activity is used."""

    minutes: float
    time_left: float
    sold_stacks: set[tuple]
    chosen: list[PlanItem] = field(default_factory=list)
    new_used: bool = False

    def take(self, item: PlanItem) -> None:
        self.chosen.append(item)
        self.time_left -= item.minutes
        self.new_used = self.new_used or item.new_to_you


def _stack_of(item: PlanItem) -> tuple:
    return (item.inputs.get("station_id"), item.inputs["type_id"])


def _resized(item: PlanItem, minutes: float) -> PlanItem:
    """An item cut (or stretched) to the minutes it gets, ISK in proportion."""
    share = minutes / item.minutes if item.minutes else 0
    return PlanItem(**{**item.__dict__, "isk": item.isk * share, "minutes": minutes})


def _with_new_flags(db, character_id, candidates: list[PlanItem]) -> list[PlanItem]:
    return [
        PlanItem(
            **{
                **c.__dict__,
                "new_to_you": c.kind in ACTIVITY_KINDS and not _has_done(db, character_id, c.kind),
            }
        )
        for c in candidates
    ]


def _rated(candidates: list[PlanItem], learned: risk.Model, worth: float) -> list[PlanItem]:
    """Each candidate rated with the chances and lines learned so far: activities
    say where it goes (the band) and what's at stake; the planner rates it."""
    return [
        dataclasses.replace(
            item,
            risk=risk.rate(item.risk.band, item.risk.exposure, worth, item.risk.payout, learned),
        )
        for item in candidates
    ]


def _set_aside(candidates, mutes, allow_risky) -> tuple[list[PlanItem], list[Alternative]]:
    """Muted kinds, and Risky items unless allowed, become alternatives."""
    usable, alternatives = [], []
    for item in candidates:
        if f"kind:{item.kind}" in mutes:
            alternatives.append(Alternative(item, "muted"))
        elif item.risk.rating == "red" and not allow_risky:
            alternatives.append(Alternative(item, "red_risk"))
        else:
            usable.append(item)
    return usable, alternatives


def _promised_stacks(db, character_id) -> set[tuple]:
    """Stacks already promised to an open haul project: not sold again."""
    promised = set()
    for project in projects.open_projects(db, character_id):
        if project.kind == "haul":
            station_text, type_text = project.key.split(":")[:2]
            promised.add((int(station_text), int(type_text)))
    return promised


def _quick_jobs(fit: _Fitting, usable: list[PlanItem]) -> None:
    """1. Quick jobs where the character is docked: skills, the best build job,
    then selling here."""
    for item in [i for i in usable if i.kind == "skill"]:
        fit.take(item)
    builds = sorted((i for i in usable if i.kind == "manufacturing"), key=lambda i: -i.adjusted_isk)
    for item in builds[:1]:
        if item.minutes <= fit.time_left and not (item.new_to_you and fit.new_used):
            fit.take(item)
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
    for item in sells:
        if _stack_of(item) in fit.sold_stacks:
            continue
        if item.minutes <= fit.time_left:
            fit.take(item)
            fit.sold_stacks.add(_stack_of(item))


def _open_projects(fit: _Fitting, usable: list[PlanItem]) -> None:
    """2. Open projects come before anything new."""
    for item in sorted(
        (i for i in usable if i.kind == "project_continue"), key=lambda i: -i.adjusted_isk
    ):
        if fit.time_left >= MIN_MINING_MINUTES:
            fit.take(_resized(item, min(item.minutes, fit.time_left)))


def _main_pool(fit: _Fitting, usable: list[PlanItem], open_count: int) -> list[PlanItem]:
    def eligible(i: PlanItem) -> bool:
        if i.kind in ("courier", "trade") or (i.kind in SIZED_KINDS and i.kind != "project_start"):
            return True
        if i.kind == "project_start":
            return open_count < projects.MAX_OPEN and not (
                i.inputs.get("station_id") and _stack_of(i) in fit.sold_stacks
            )
        return (
            i.kind == "sell_hangar"
            and (i.inputs.get("jumps", 0) > 0 or i.inputs.get("travel_jumps", 0) > 0)
            and _stack_of(i) not in fit.sold_stacks
        )

    return sorted((i for i in usable if eligible(i)), key=lambda i: -i.adjusted_isk)


def _fits_main(fit: _Fitting, item: PlanItem) -> bool:
    if item.kind not in SIZED_KINDS and item.minutes > fit.time_left + 1e-9:
        return False
    if item.kind in SIZED_KINDS and fit.time_left < MIN_MINING_MINUTES:
        return False
    return not (item.new_to_you and fit.new_used)


def _main_activity(fit: _Fitting, pool: list[PlanItem]) -> PlanItem | None:
    """3. One main activity for the remaining time, by ISK after expected losses.
    Mining, gas and project starts are sized to the time left."""
    for item in pool:
        if not _fits_main(fit, item):
            continue
        if item.kind in SIZED_KINDS:
            item = _resized(item, fit.time_left)
        fit.take(item)
        return item
    return None


def _why_not(item: PlanItem, fit: _Fitting, open_count: int) -> str:
    if item.kind == "project_continue":
        return "too_long"
    if item.kind == "project_start" and open_count >= projects.MAX_OPEN:
        return "too_many_projects"
    if item.kind == "sell_hangar" and _stack_of(item) in fit.sold_stacks:
        return "worse"
    if item.minutes > fit.minutes:
        return "too_long"
    if item.new_to_you and fit.new_used:
        return "new_limit"
    return "worse"


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
    learned = risk.model(db)
    candidates = _rated(
        [item for activity in activities.ALL for item in activity.candidates(context)],
        learned,
        worth,
    )
    usable, alternatives = _set_aside(
        _with_new_flags(db, character_id, candidates), mutes, allow_risky
    )

    fit = _Fitting(minutes, minutes, _promised_stacks(db, character_id))
    _quick_jobs(fit, usable)
    _open_projects(fit, usable)
    open_count = len(projects.open_projects(db, character_id))
    picked = _main_activity(fit, _main_pool(fit, usable, open_count))

    for item in usable:
        if item in fit.chosen or (picked and item.key == picked.key and item.kind == picked.kind):
            continue
        alternatives.append(Alternative(item, _why_not(item, fit, open_count)))

    if not fit.chosen:
        notes.append(Reason("nothing_fits"))
    if not signals.fresh(db):
        notes.append(Reason("no_live_signals"))
    for objective in campaigns.open_objectives(db)[:3]:
        notes.append(
            Reason(
                "campaign_objective",
                {
                    "campaign": objective.campaign,
                    "title": objective.title,
                    "activity": objective.activity,
                    "isk": objective.isk,
                    "lp": objective.lp,
                    "systems": len(objective.systems),
                },
            )
        )
    trips = sum(c.trips for c in learned.chances.values())
    if trips:
        losses = sum(c.losses for c in learned.chances.values())
        notes.append(Reason("loss_chances_learned", {"trips": trips, "losses": losses}))
    else:
        notes.append(Reason("loss_chances_are_guesses"))
    return Plan(
        character_id=character_id,
        minutes_available=minutes,
        items=tuple(fit.chosen),
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


def plan_value(plan: StoredPlan) -> float:
    """What a plan is worth: its ISK after expected losses."""
    return sum(item.isk - item.expected_loss for item in plan.items)


def who_should_play(plans: list[StoredPlan]) -> list[StoredPlan]:
    """Plans for the same time, one per character, best first. Characters on
    one Alpha account can't play at once, so the first is the one to pick."""
    return sorted(plans, key=lambda plan: -plan_value(plan))
