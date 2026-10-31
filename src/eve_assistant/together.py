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
- one mines, one boosts (two characters, roadmap 8.5): the booster runs a
  Mining Foreman Burst beside the miner (boosts.py: cycles shorter by the
  burst's strength), takes the ore into its own hold, compressing it if a
  compressor is fitted, and sells it at the end. The burst charges it uses
  are a cost on its step.

The chosen plans are recorded like any other (one per character, so sessions,
outcomes and learning work unchanged) and linked as a team plan, together
with what the other shape would have made.
"""

import json
import math
import sqlite3
from dataclasses import dataclass, replace

from eve_assistant import (
    boosts,
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

SHAPES = ("side_by_side", "mine_and_haul", "mine_and_boost")
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
    others: tuple[Shape, ...]  # the shapes that lost, best first
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
    carried: float  # m3 in the hold: compressed when the ship can compress
    compresses: bool


def _reach(db, character_id: int, ship: fleet.FleetShip, system_id: int, highsec_only: bool):
    """Getting into this ship and flying it to where the miner mines:
    (boarding, path, minutes a jump), or None if it can't be done."""
    per_jump = corrections.minutes_per_jump(db, character_id)[0]
    board = fleet.boarding(db, character_id, ship, highsec_only, per_jump)
    start = board.path[-1] if board else None
    path = routes.jumps_between(db, start, system_id, highsec_only) if start else None
    return None if path is None else (board, path, per_jump)


def _carry_minutes(board, path, per_jump, trips: int) -> float:
    return (
        board.minutes + (len(path) - 1) * per_jump + trips * HAUL_TRIP_MINUTES + ore.MINUTES_TO_SELL
    )


def _jumps(count: int) -> str:
    return f"{count} jump{'' if count == 1 else 's'}"


def _hauler_ship(db, hauler: Member, mining: PlanItem, minutes: float, highsec_only):
    """The ship that gets the hauler to the miner and carries the ore in time:
    fewest trips first (a ship that compresses often needs none until the end),
    then the least at risk."""
    inputs = mining.inputs
    system_id, m3 = inputs["mining_system_id"], inputs["m3"]
    try:
        ships = fleet.owned_ships(db, hauler.character_id)
    except skills.SkillsError:
        return None
    best = None
    for ship in ships:
        capacity = ship.capacity_for(True)
        reach = _reach(db, hauler.character_id, ship, system_id, highsec_only) if capacity else None
        if reach is None:
            continue
        squeezed = (
            _compressed_m3(db, inputs.get("type_id"), inputs.get("quantity") or 0)
            if _compresses(db, ship)
            else None
        )
        carried = squeezed if squeezed is not None else m3
        trips = max(1, math.ceil(carried / capacity))
        needed = _carry_minutes(*reach, trips)
        if needed > minutes:
            continue
        haul = _Haul(
            ship, reach[0], reach[1], trips, capacity, needed, carried, squeezed is not None
        )
        if best is None or (haul.trips, ship.value) < (best.trips, best.ship.value):
            best = haul
    return best


def _haul_step(db, hauler: Member, miner: Member, mining: PlanItem, minutes, highsec_only):
    """The hauler's step: collect the miner's cans, carry and sell the ore."""
    inputs = mining.inputs
    system_id, m3 = inputs.get("mining_system_id"), inputs.get("m3") or 0.0
    if system_id is None or m3 <= 0:
        return None
    haul = _hauler_ship(db, hauler, mining, minutes, highsec_only)
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
    travel = f"fly to {where} ({_jumps(jumps)}) and " if jumps else ""
    if haul.trips == 1:
        keep = (
            f"compress them in your {haul.ship.name} (it all fits compressed, so no trips "
            "until the end)"
            if haul.compresses
            else "keep them in your hold (it all fits, so no trips until the end)"
        )
        title = (
            f"{board}{travel}collect the cans {miner.name} jettisons and {keep}, then sell "
            f"the ore at the station in {where}"
        )
    else:
        title = (
            f"{board}{travel}collect the cans {miner.name} jettisons: about {haul.trips} "
            f"trips to the station in {where}, then sell the ore there"
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
                    "m3": haul.carried,
                    "compressed": haul.compresses,
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


def _hauler_plan(
    solo: planner.Plan, step: PlanItem, minutes: float, why: str = "hauling"
) -> planner.Plan:
    """Quick jobs from the helper's own plan that fit beside the hauling (or
    boosting), then that step; its own main activity becomes an alternative."""
    spare = minutes - step.inputs["minutes_needed"]
    quick = []
    for item in solo.items:
        if planner.is_quick_job(item) and item.minutes <= spare:
            quick.append(item)
            spare -= item.minutes
    step = replace(step, minutes=minutes - sum(i.minutes for i in quick))
    given_up = [planner.Alternative(i, why) for i in solo.items if i not in quick]
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


def _can_mine(solo: planner.Plan) -> bool:
    """Mining was at least considered (chosen or not) in the character's own plan."""
    return any(i.kind == "mining" for i in solo.items) or any(
        a.item.kind == "mining" for a in solo.alternatives
    )


def _why_no_mining(mined: planner.Plan, otherwise: str) -> str:
    """Why the miner's plan, made again for working together, doesn't mine: its
    mining was rated Risky (and risky options are off), or it just loses."""
    risky = any(a.item.kind == "mining" and a.why_not == "red_risk" for a in mined.alternatives)
    return "team_mining_risky" if risky else otherwise


def _can_carry(db, hauler: Member) -> bool:
    try:
        ships = fleet.owned_ships(db, hauler.character_id)
    except skills.SkillsError:
        return False
    return any(ship.usable and ship.capacity_for(True) > 0 for ship in ships)


def _one_way(db, miner: Member, hauler: Member, solo, plan, rules: _Rules):
    """The miner mining while the hauler hauls: (shape, None), or (None, why not).
    The cheap checks come first: planning the miner again takes a while."""
    names = {"miner": miner.name, "hauler": hauler.name}
    if not _can_mine(solo[miner.character_id]):
        return None, Reason("no_team_mining", names)
    if not _can_carry(db, hauler):
        return None, Reason("no_team_hauler", names)
    mined = plan(miner, hauled_by=hauler.name)
    mining = next((i for i in mined.items if i.kind == "mining"), None)
    if mining is None:
        return None, Reason(_why_no_mining(mined, "no_team_mining"), names)
    step = _haul_step(db, hauler, miner, mining, rules.minutes, rules.highsec_only)
    if step is None:
        return None, Reason("no_team_hauler", names)
    if step.risk.rating == "red" and not rules.allow_risky:  # as the planner does
        return None, Reason("team_haul_risky", names)
    hauled = _hauler_plan(solo[hauler.character_id], step, rules.minutes)
    return Shape("mine_and_haul", (mined, hauled), ("miner", "hauler")), None  # miner first


def _both_ways(db, found, solo, plan, rules: _Rules, one_way):
    """The better of each character mining while the other helps (hauls or
    boosts), and a note for each way round that doesn't work."""
    best, notes = None, []
    for miner, hauler in ((found[0], found[1]), (found[1], found[0])):
        shape, why = one_way(db, miner, hauler, solo, plan, rules)
        if shape is None:
            notes.append(why)
            continue
        if found[0] is not miner:  # plans and roles in the members' order
            shape = replace(shape, plans=shape.plans[::-1], roles=shape.roles[::-1])
        if best is None or shape.value > best.value:
            best = shape
    return best, notes


def _compressed_m3(db, type_id: int | None, quantity: int) -> float | None:
    row = db.execute(
        "SELECT t.volume FROM sde_compressible_types c JOIN sde_types t "
        "ON t.type_id = c.compressed_type_id WHERE c.type_id = ?",
        (type_id,),
    ).fetchone()
    return quantity * row[0] if row and row[0] else None


def _compresses(db, ship: fleet.FleetShip) -> bool:
    """An ore compressor is fitted: the ship carries ore compressed."""
    return any("Ore Compressor" in skills.type_name(db, t) for t in set(ship.fitted))


@dataclass(frozen=True)
class _Carry:
    reach: tuple  # (boarding, path, minutes a jump)
    carried: float  # m3 in the booster's hold: compressed when it can compress
    compresses: bool
    capacity: float
    trips: int
    needed: float


def _boost_carry(db, booster: Member, boost, mining: PlanItem, rules: _Rules) -> "_Carry | None":
    """Whether the booster's ship gets to the miner and carries the ore in time."""
    inputs = mining.inputs
    system_id, m3 = inputs.get("mining_system_id"), inputs.get("m3") or 0.0
    capacity = boost.ship.capacity_for(True)
    if system_id is None or m3 <= 0 or capacity <= 0:
        return None
    reach = _reach(db, booster.character_id, boost.ship, system_id, rules.highsec_only)
    if reach is None:
        return None
    compresses = _compresses(db, boost.ship)
    squeezed = (
        _compressed_m3(db, inputs.get("type_id"), inputs.get("quantity") or 0)
        if compresses
        else None
    )
    carried = squeezed if squeezed is not None else m3
    trips = max(1, math.ceil(carried / capacity))
    needed = _carry_minutes(*reach, trips)
    if needed > rules.minutes:
        return None
    return _Carry(reach, carried, squeezed is not None, capacity, trips, needed)


def _boost_title(boost, miner: Member, carry: _Carry, where: str, charges: int) -> str:
    board, path, _per_jump = carry.reach
    jumps = len(path) - 1
    switch = (
        f"board your {boost.ship.name} ({board.minutes:.0f} min), then "
        if board.needs_switch
        else ""
    )
    travel = f"fly to {where} ({_jumps(jumps)}) and " if jumps else ""
    load = (
        f"Load {charges} {boost.charge}s first ({boost.charges_loaded} loaded). "
        if boost.charges_loaded < charges
        else ""
    )
    keeps = (
        f" ({'it all fits compressed' if carry.compresses else 'it all fits'}, so no trips "
        "until the end)"
        if carry.trips == 1
        else f" ({carry.trips} trips)"
    )
    title = (
        f"{switch}{travel}boost {miner.name} with your {boost.burst} ({boost.charge}: "
        f"cycles {-boost.cycle_change:.0f}% shorter), take their ore into your "
        f"{boost.ship.name}{', compress it' if carry.compresses else ''}{keeps} and sell it "
        f"at the station in {where}"
    )
    return load + title[0].upper() + title[1:]


def _boost_step(db, booster: Member, miner: Member, mining: PlanItem, boost, rules: _Rules):
    """The booster's step: boost the miner, carry and sell their ore. Its ISK is
    the charges the bursts use; the ore's value is on the miner's step."""
    carry = _boost_carry(db, booster, boost, mining, rules)
    if carry is None:
        return None
    inputs = mining.inputs
    system_id = inputs["mining_system_id"]
    board, path, _per_jump = carry.reach
    here = base.system_info(db, system_id)
    assessed = risk.assess(db, [*board.path, *path[1:]], here["region"] if here else None)
    held = min(carry.capacity, carry.carried) / carry.carried if carry.carried else 0.0
    rating = risk.rate(
        assessed.band,
        boost.ship.value + held * mining.isk,  # its ship and the most it holds at once
        risk.net_worth(db, booster.character_id),
        signals.hull_payout(db, boost.ship.type_id),
        risk.model(db),
    )
    charges = boost.charges_needed(rules.minutes)
    cost = charges * (boost.charge_price or 0.0)
    detail = {
        "miner": miner.name, "ship": boost.ship.name, "burst": boost.burst,
        "charge": boost.charge, "base": boost.base, "strength": boost.strength,
        "cycle_change": boost.cycle_change,
        "factors": [[what, value] for what, value in boost.factors],
        "charges": charges, "loaded": boost.charges_loaded, "charge_cost": cost,
        "priced": boost.charge_price is not None, "compressed": carry.compresses,
        "m3": carry.carried, "trips": carry.trips, "minutes_needed": carry.needed,
    }  # fmt: skip
    where = here["name"] if here else "the miner's system"
    return PlanItem(
        kind="team_boost",
        key=f"{miner.character_id}:{boost.ship.item_id}",
        title=_boost_title(boost, miner, carry, where, charges),
        isk=-cost,
        minutes=rules.minutes,
        risk=rating,
        reasons=(Reason("team_boost", detail), *assessed.reasons),
        inputs={
            "miner_id": miner.character_id,
            "ship_item_id": boost.ship.item_id,
            "mining_system_id": system_id,
            "sell_station_id": inputs.get("sell_station_id"),
            "type_id": inputs.get("type_id"),
            "quantity": inputs.get("quantity"),
            "m3": carry.carried,
            "trips": carry.trips,
            "jumps": len(path) - 1,
            "minutes_needed": carry.needed,
            "charge_type_id": boost.charge_type_id,
            "charges": charges,
            "unit_prices": inputs.get("unit_prices", {}),
        },
    )


def _boost_way(db, miner: Member, booster: Member, solo, plan, rules: _Rules):
    """The miner mining with the booster's bursts on them: (shape, None) or
    (None, why not). The cheap checks come first."""
    names = {"miner": miner.name, "booster": booster.name}
    if not _can_mine(solo[miner.character_id]):
        return None, Reason("no_boost_mining", names)
    boost = boosts.best_boost(db, booster.character_id)
    if boost is None:
        return None, Reason("no_team_booster", names)
    mined = plan(miner, hauled_by=booster.name, boost=(booster.name, boost.cycle_factor))
    mining = next((i for i in mined.items if i.kind == "mining"), None)
    if mining is None:
        return None, Reason(_why_no_mining(mined, "no_boost_mining"), names)
    step = _boost_step(db, booster, miner, mining, boost, rules)
    if step is None:
        return None, Reason("team_boost_out_of_reach", names)
    if step.risk.rating == "red" and not rules.allow_risky:  # as the planner does
        return None, Reason("team_boost_risky", names)
    boosted = _hauler_plan(solo[booster.character_id], step, rules.minutes, why="boosting")
    return Shape("mine_and_boost", (mined, boosted), ("miner", "booster")), None  # miner first


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

    def plan(member: Member, taken=frozenset(), hauled_by=None, boost=None) -> planner.Plan:
        # Played together, a miner may mine what the others have mined in that system.
        partners = tuple(m.character_id for m in found if m is not member)
        return planner.build_plan(
            db, esi, member.character_id, minutes, allow_lowsec=allow_lowsec,
            allow_risky=allow_risky, ore_names=ore_names, taken=taken, hauled_by=hauled_by,
            boost=boost, ore_partners=partners,
        )  # fmt: skip

    side, solo = _side_by_side(found, plan)
    notes: list[Reason] = []
    shapes = [side]
    if len(found) == 2:
        rules = _Rules(minutes, highsec_only, allow_risky)
        for one_way in (_one_way, _boost_way):
            shape, why = _both_ways(db, found, solo, plan, rules, one_way)
            notes += why
            shapes += [shape] if shape else []
    else:
        notes.append(Reason("together_side_by_side_only"))
    shapes.sort(key=lambda shape: -shape.value)  # stable: side by side wins a tie
    chosen, others = shapes[0], tuple(shapes[1:])
    other = others[0] if others else None
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
    return TeamPlan(minutes, tuple(found), chosen, others, tuple(notes))


def _shape_summary(shape: Shape, found: tuple[Member, ...]) -> dict:
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
        "others": [_shape_summary(shape, team.members) for shape in team.others],
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
    chosen = Shape(team.chosen.kind, tuple(stored), team.chosen.roles)
    return TeamPlan(team.minutes, team.members, chosen, team.others, team.notes, team_id)


@dataclass(frozen=True)
class StoredTeam:
    team_id: int
    created_at: str
    minutes_available: float
    shape: str
    members: tuple[tuple[str, planner.StoredPlan], ...]  # (role, plan)
    others: tuple[dict, ...]  # the shapes that lost, best first
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
        # Plans before 8.5 kept one "other" shape.
        others=tuple(details.get("others") or ([details["other"]] if details.get("other") else [])),
        notes=tuple(details.get("notes", [])),
    )


def latest_team_id(db: sqlite3.Connection) -> int | None:
    return db.execute("SELECT MAX(team_id) FROM team_plans").fetchone()[0]


def team_of(db: sqlite3.Connection, plan_id: int) -> int | None:
    """The team plan a plan is part of, if any."""
    row = db.execute("SELECT team_id FROM team_plan_members WHERE plan_id = ?", (plan_id,))
    found = row.fetchone()
    return found[0] if found else None
