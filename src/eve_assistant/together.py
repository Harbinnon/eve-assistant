"""Playing several characters at once (roadmap 8.4, design 6.3).

Omega accounts can be logged in together, so one plan can cover characters
played at the same time. An account logs in one character at a time, and
Alpha accounts can't multibox (while one is logged in, no other account may
be), so a group needs each character on its own account, every one Omega.
A character not grouped into an account can't be checked, so it's refused
until it is.

Two shapes are compared, and the one worth more ISK after expected losses is
picked:

- side by side: each character does its own best plan for the same minutes.
  What only one can take (a courier contract, a trade) goes to the character
  whose plan is worth most, and the others plan without it.
- one mines, one hauls (two characters): the miner jettisons each full hold
  into a can instead of docking to unload; the hauler collects the cans,
  carries the ore to the station and sells it. The miner mines for longer,
  and the hauler's time earns nothing else. Offered only when the miner's best
  plan is mining and the hauler has a ship that gets there and keeps up.

The chosen plans are recorded like any other (one per character, so sessions,
outcomes and learning work unchanged) and linked as a team plan, together
with what the other shape would have made.
"""

import json
import math
import sqlite3
from dataclasses import dataclass, replace

from eve_assistant import (
    corrections,
    fleet,
    ore,
    planner,
    preferences,
    risk,
    routes,
    signals,
    skills,
)
from eve_assistant.activities import base
from eve_assistant.activities.base import HAUL_TRIP_MINUTES, PlanItem
from eve_assistant.esi.client import EsiClient
from eve_assistant.skills import Reason
from eve_assistant.store import utc_now

SHAPES = ("side_by_side", "mine_and_haul")
SHARED_KINDS = ("courier", "trade")  # one character takes it, not each


class TogetherError(Exception):
    """The characters can't play together; reasons say why."""

    def __init__(self, reasons: list[Reason]):
        super().__init__(", ".join(r.code for r in reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class Member:
    character_id: int
    name: str
    account: str | None
    clone_state: str


@dataclass(frozen=True)
class Shape:
    kind: str  # side_by_side or mine_and_haul
    plans: tuple[planner.Plan, ...]  # one per member, in the members' order
    roles: tuple[str, ...]  # solo, miner or hauler

    @property
    def value(self) -> float:
        return sum(_value(plan) for plan in self.plans)


@dataclass(frozen=True)
class TeamPlan:
    minutes: float
    members: tuple[Member, ...]
    chosen: Shape
    other: Shape | None  # the shape that lost, when there was one
    notes: tuple[Reason, ...]
    team_id: int | None = None


def _value(plan: planner.Plan) -> float:
    return sum(item.adjusted_isk for item in plan.items)


def members(db: sqlite3.Connection, character_ids: list[int]) -> list[Member]:
    """The characters asked for, each once, in the order given."""
    found = []
    for character_id in dict.fromkeys(character_ids):
        row = db.execute(
            "SELECT c.character_id, c.name, a.label, a.clone_state FROM characters c "
            "LEFT JOIN accounts a USING (account_id) WHERE c.character_id = ?",
            (character_id,),
        ).fetchone()
        if row is not None:
            found.append(Member(row[0], row[1], row[2], row[3] or "unknown"))
    return found


def check(found: list[Member]) -> list[Reason]:
    """Why these characters can't play together; empty when they can."""
    problems = []
    if len(found) < 2:
        problems.append(Reason("together_too_few"))
    by_account: dict[str, list[Member]] = {}
    for member in found:
        if member.account is None:
            problems.append(Reason("together_no_account", {"character": member.name}))
        else:
            by_account.setdefault(member.account, []).append(member)
    for label, group in by_account.items():
        names = [m.name for m in group]
        if len(group) > 1:
            problems.append(
                Reason("together_same_account", {"account": label, "characters": names})
            )
        if group[0].clone_state != "omega":
            problems.append(
                Reason(
                    "together_not_omega",
                    {"account": label, "clone_state": group[0].clone_state, "characters": names},
                )
            )
    return problems


def _shared(item: PlanItem) -> tuple[str, str] | None:
    return (item.kind, item.key) if item.kind in SHARED_KINDS else None


def _side_by_side(found: list[Member], plan) -> tuple[Shape, dict[int, planner.Plan]]:
    """Each character's own plan; the one worth most picks first, and the
    others plan again without what it took, if they wanted it too."""
    solo = {m.character_id: plan(m) for m in found}
    taken: set[tuple[str, str]] = set()
    chosen: dict[int, planner.Plan] = {}
    for member in sorted(found, key=lambda m: -_value(solo[m.character_id])):
        own = solo[member.character_id]
        if any(_shared(i) in taken for i in own.items):
            own = plan(member, taken=frozenset(taken))
        chosen[member.character_id] = own
        taken |= {key for i in own.items if (key := _shared(i))}
    plans = tuple(chosen[m.character_id] for m in found)
    return Shape("side_by_side", plans, ("solo",) * len(found)), solo


@dataclass(frozen=True)
class _Haul:
    ship: fleet.FleetShip
    board: fleet.Boarding
    path: list[int]  # from the ship to where the miner mines
    trips: int
    capacity: float
    minutes_needed: float


def _hauler_ship(db, hauler: Member, system_id: int, m3: float, minutes: float, highsec_only):
    """The ship at least risk that gets the hauler to the miner and carries the
    ore in time: (ship, boarding, path, trips, capacity, minutes needed)."""
    per_jump = corrections.minutes_per_jump(db, hauler.character_id)[0]
    try:
        ships = fleet.owned_ships(db, hauler.character_id)
    except skills.SkillsError:
        return None
    best = None
    for ship in ships:
        capacity = ship.capacity_for(True)
        board = fleet.boarding(db, hauler.character_id, ship, highsec_only, per_jump)
        start = board.path[-1] if board and capacity > 0 else None
        path = routes.jumps_between(db, start, system_id, highsec_only) if start else None
        if path is None:
            continue
        trips = math.ceil(m3 / capacity)
        needed = (
            board.minutes
            + (len(path) - 1) * per_jump
            + trips * HAUL_TRIP_MINUTES
            + ore.MINUTES_TO_SELL
        )
        if needed <= minutes and (best is None or ship.value < best.ship.value):
            best = _Haul(ship, board, path, trips, capacity, needed)
    return best


def _haul_step(db, hauler: Member, miner: Member, mining: PlanItem, minutes, highsec_only):
    """The hauler's step: collect the miner's cans, carry and sell the ore."""
    inputs = mining.inputs
    system_id, m3 = inputs.get("mining_system_id"), inputs.get("m3") or 0.0
    if system_id is None or m3 <= 0:
        return None
    haul = _hauler_ship(db, hauler, system_id, m3, minutes, highsec_only)
    if haul is None:
        return None
    here = base.system_info(db, system_id)
    jumps = len(haul.path) - 1
    assessed = risk.assess(db, [*haul.board.path, *haul.path[1:]], here["region"] if here else None)
    a_trip = min(haul.capacity, m3) * mining.isk / m3  # the most carried at once
    rating = risk.rate(
        assessed.band,
        haul.ship.value + a_trip,
        risk.net_worth(db, hauler.character_id),
        signals.hull_payout(db, haul.ship.type_id),
        risk.model(db),
    )
    where = here["name"] if here else "the miner's system"
    board = (
        f"board your {haul.ship.name} ({haul.board.minutes:.0f} min), then "
        if haul.board.needs_switch
        else ""
    )
    travel = f"fly to {where} ({jumps} jumps) and " if jumps else ""
    title = (
        f"{board}{travel}collect the cans {miner.name} jettisons: about {haul.trips} trips "
        f"to the station in {where}, then sell the ore there"
    )
    return PlanItem(
        kind="team_haul",
        key=f"{miner.character_id}:{haul.ship.item_id}",
        title=title[0].upper() + title[1:],
        isk=0.0,  # the ore's value is on the miner's step, where outcomes count it
        minutes=minutes,
        risk=rating,
        reasons=(
            Reason(
                "team_haul",
                {
                    "miner": miner.name,
                    "ship": haul.ship.name,
                    "m3": m3,
                    "trips": haul.trips,
                    "capacity": haul.capacity,
                    "minutes_needed": haul.minutes_needed,
                },
            ),  # fmt: skip
            *assessed.reasons,
        ),
        inputs={
            "miner_id": miner.character_id,
            "ship_item_id": haul.ship.item_id,
            "mining_system_id": system_id,
            "sell_station_id": inputs.get("sell_station_id"),
            "type_id": inputs.get("type_id"),
            "quantity": inputs.get("quantity"),
            "m3": m3,
            "trips": haul.trips,
            "jumps": jumps,
            "minutes_needed": haul.minutes_needed,
            "unit_prices": inputs.get("unit_prices", {}),
        },
    )


def _hauler_plan(solo: planner.Plan, step: PlanItem, minutes: float) -> planner.Plan:
    """Quick jobs from the hauler's own plan that fit beside the hauling, then
    the hauling; its own main activity becomes an alternative."""
    spare = minutes - step.inputs["minutes_needed"]
    quick = []
    for item in solo.items:
        if planner.is_quick_job(item) and item.minutes <= spare:
            quick.append(item)
            spare -= item.minutes
    step = replace(step, minutes=minutes - sum(i.minutes for i in quick))
    given_up = [planner.Alternative(i, "hauling") for i in solo.items if i not in quick]
    return planner.Plan(
        character_id=solo.character_id,
        minutes_available=minutes,
        items=(*quick, step),
        alternatives=(*given_up, *solo.alternatives),
        notes=solo.notes,
        net_worth=solo.net_worth,
    )


@dataclass(frozen=True)
class _Rules:
    minutes: float
    highsec_only: bool
    allow_risky: bool


def _one_way(db, miner: Member, hauler: Member, solo, plan, rules: _Rules):
    """The miner mining while the hauler hauls: (shape, None), or (None, why not)."""
    names = {"miner": miner.name, "hauler": hauler.name}
    mined = plan(miner, hauled_by=hauler.name)
    mining = next((i for i in mined.items if i.kind == "mining"), None)
    if mining is None:
        return None, Reason("no_team_mining", names)
    step = _haul_step(db, hauler, miner, mining, rules.minutes, rules.highsec_only)
    if step is None:
        return None, Reason("no_team_hauler", names)
    if step.risk.rating == "red" and not rules.allow_risky:  # as the planner does
        return None, Reason("team_haul_risky", names)
    hauled = _hauler_plan(solo[hauler.character_id], step, rules.minutes)
    return Shape("mine_and_haul", (mined, hauled), ("miner", "hauler")), None  # miner first


def _mine_and_haul(db, found, solo, plan, rules: _Rules):
    """The better of each character mining while the other hauls, and a note
    for each way round that doesn't work."""
    best, notes = None, []
    for miner, hauler in ((found[0], found[1]), (found[1], found[0])):
        shape, why = _one_way(db, miner, hauler, solo, plan, rules)
        if shape is None:
            notes.append(why)
            continue
        if found[0] is not miner:  # plans and roles in the members' order
            shape = replace(shape, plans=shape.plans[::-1], roles=shape.roles[::-1])
        if best is None or shape.value > best.value:
            best = shape
    return best, notes


def build(
    db: sqlite3.Connection,
    esi: EsiClient,
    character_ids: list[int],
    minutes: float,
    allow_lowsec: bool = False,
    allow_risky: bool = False,
    ore_names: list[str] | None = None,
) -> TeamPlan:
    """One plan for characters played at the same time. Raises TogetherError
    if they can't be (same account, Alpha, accounts not set)."""
    found = members(db, character_ids)
    problems = check(found)
    if problems:
        raise TogetherError(problems)
    highsec_only = not allow_lowsec or "route:lowsec" in preferences.active_mutes(db)

    def plan(member: Member, taken=frozenset(), hauled_by=None) -> planner.Plan:
        return planner.build_plan(
            db, esi, member.character_id, minutes, allow_lowsec=allow_lowsec,
            allow_risky=allow_risky, ore_names=ore_names, taken=taken, hauled_by=hauled_by,
        )  # fmt: skip

    side, solo = _side_by_side(found, plan)
    notes: list[Reason] = []
    haul = None
    if len(found) == 2:
        rules = _Rules(minutes, highsec_only, allow_risky)
        haul, why = _mine_and_haul(db, found, solo, plan, rules)
        notes += why
    else:
        notes.append(Reason("together_side_by_side_only"))
    chosen, other = (haul, side) if haul and haul.value > side.value else (side, haul)
    notes.insert(
        0,
        Reason(
            "together_chosen",
            {
                "shape": chosen.kind,
                "value": chosen.value,
                "other_shape": other.kind if other else None,
                "other_value": other.value if other else None,
            },
        ),  # fmt: skip
    )
    return TeamPlan(minutes, tuple(found), chosen, other, tuple(notes))


def _shape_summary(shape: Shape | None, found: tuple[Member, ...]) -> dict | None:
    if shape is None:
        return None
    return {
        "shape": shape.kind,
        "value": shape.value,
        "members": [
            {"character_id": m.character_id, "role": role, "value": _value(plan),
             "steps": [item.title for item in plan.items]}
            for m, plan, role in zip(found, shape.plans, shape.roles, strict=True)
        ],
    }  # fmt: skip


def record(db: sqlite3.Connection, team: TeamPlan, inputs: dict) -> TeamPlan:
    """Each character's plan as an ordinary plan, then the team plan linking them."""
    names = [m.name for m in team.members]
    stored = []
    for member, plan, role in zip(team.members, team.chosen.plans, team.chosen.roles, strict=True):
        others = [n for n in names if n != member.name]
        together = {"shape": team.chosen.kind, "role": role, "with": others}
        stored.append(planner.record(db, plan, {**inputs, "together": together}))
    details = {
        "value": team.chosen.value,
        "other": _shape_summary(team.other, team.members),
        "notes": [{"code": n.code, **n.detail} for n in team.notes],
        "inputs": inputs,
    }
    with db:
        team_id = db.execute(
            "INSERT INTO team_plans (created_at, minutes_available, shape, details) "
            "VALUES (?, ?, ?, ?)",
            (utc_now(), team.minutes, team.chosen.kind, json.dumps(details)),
        ).lastrowid
        db.executemany(
            "INSERT INTO team_plan_members (team_id, plan_id, role) VALUES (?, ?, ?)",
            [(team_id, p.plan_id, role) for p, role in zip(stored, team.chosen.roles, strict=True)],
        )
    chosen = replace(team.chosen, plans=tuple(stored))
    return replace(team, chosen=chosen, team_id=team_id)


@dataclass(frozen=True)
class StoredTeam:
    team_id: int
    created_at: str
    minutes_available: float
    shape: str
    members: tuple[tuple[str, planner.StoredPlan], ...]  # (role, plan)
    other: dict | None
    notes: tuple[dict, ...]

    @property
    def value(self) -> float:
        return sum(planner.plan_value(plan) for _, plan in self.members)


def load(db: sqlite3.Connection, team_id: int) -> StoredTeam:
    row = db.execute("SELECT * FROM team_plans WHERE team_id = ?", (team_id,)).fetchone()
    if row is None:
        raise planner.PlanError(f"No plan together {team_id}.")
    details = json.loads(row["details"])
    linked = db.execute(
        "SELECT plan_id, role FROM team_plan_members WHERE team_id = ? ORDER BY rowid",
        (team_id,),
    ).fetchall()
    return StoredTeam(
        team_id=team_id,
        created_at=row["created_at"],
        minutes_available=row["minutes_available"],
        shape=row["shape"],
        members=tuple((r["role"], planner.load_plan(db, r["plan_id"])) for r in linked),
        other=details.get("other"),
        notes=tuple(details.get("notes", [])),
    )


def latest_team_id(db: sqlite3.Connection) -> int | None:
    return db.execute("SELECT MAX(team_id) FROM team_plans").fetchone()[0]


def team_of(db: sqlite3.Connection, plan_id: int) -> int | None:
    """The team plan a plan is part of, if any."""
    row = db.execute("SELECT team_id FROM team_plan_members WHERE plan_id = ?", (plan_id,))
    found = row.fetchone()
    return found[0] if found else None
