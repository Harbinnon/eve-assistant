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
import math
import sqlite3
from dataclasses import dataclass, field

from eve_assistant import couriers, mining, ore, preferences, risk, skills, snapshots
from eve_assistant.esi.client import EsiClient
from eve_assistant.risk import RiskRating
from eve_assistant.skills import Reason
from eve_assistant.store import utc_now

MINUTES_PER_HOLD_TRIP = 3.0  # dock, unload, undock between full holds
MINUTES_TO_QUEUE_SKILLS = 1.0
MIN_MINING_MINUTES = 10.0
ACTIVITY_KINDS = (
    "mining",
    "courier",
)  # what can be "new to you"; reminders never are  # shorter than this is not worth undocking for

# Used only when the mining ledger shows nothing mined in this system yet.
# Common in highsec belts; an assumption, reported as such.
DEFAULT_ORES = ("Veldspar", "Scordite", "Pyroxeres", "Plagioclase")


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class PlanItem:
    kind: str  # sell_hangar, mining, courier, skill
    key: str
    title: str
    isk: float
    minutes: float
    risk: RiskRating
    reasons: tuple[Reason, ...] = field(default_factory=tuple)
    inputs: dict = field(default_factory=dict)
    new_to_you: bool = False

    @property
    def adjusted_isk(self) -> float:
        return self.isk - self.risk.expected_loss

    def details(self) -> dict:
        return {
            "reasons": [{"code": r.code, **r.detail} for r in self.reasons],
            "inputs": self.inputs,
            "risk": self.risk.as_dict(),
            "new_to_you": self.new_to_you,
        }


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


def _selling_station(db: sqlite3.Connection, character_id: int, notes: list[Reason]) -> int | None:
    """Where to sell: the NPC station the character is docked at, or else the
    first NPC station in its current system."""
    location = snapshots.latest(db, character_id, "location") or {}
    station = location.get("station_id")
    if (
        station
        and db.execute("SELECT 1 FROM sde_npc_stations WHERE station_id = ?", (station,)).fetchone()
    ):
        return station
    row = db.execute(
        "SELECT station_id FROM sde_npc_stations WHERE system_id = ? ORDER BY station_id LIMIT 1",
        (location.get("solar_system_id"),),
    ).fetchone()
    if row:
        notes.append(Reason("selling_at_nearby_station", {"station_id": row["station_id"]}))
        return row["station_id"]
    notes.append(Reason("no_npc_station_here"))
    return None


def _system_info(db: sqlite3.Connection, system_id: int | None):
    return db.execute(
        "SELECT s.name, s.security_status, r.name AS region FROM sde_solar_systems s "
        "JOIN sde_regions r USING (region_id) WHERE s.system_id = ?",
        (system_id,),
    ).fetchone()


def _is_ore(db: sqlite3.Connection, type_id: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sde_types t JOIN sde_groups g USING (group_id) "
            "WHERE t.type_id = ? AND g.category_id = 25 "
            "AND EXISTS (SELECT 1 FROM sde_type_materials m WHERE m.type_id = t.type_id)",
            (type_id,),
        ).fetchone()
        is not None
    )


def hangar_candidates(
    db, esi, character_id, station_id, net_worth, highsec_only, minutes
) -> list[PlanItem]:
    location = snapshots.latest(db, character_id, "location") or {}
    if location.get("station_id") != station_id:
        return []  # only suggest selling where the character is actually docked
    assets = snapshots.latest(db, character_id, "assets") or []
    stacks: dict[int, int] = {}
    for item in assets:
        if (
            item.get("location_id") == station_id
            and item.get("location_flag") == "Hangar"
            and _is_ore(db, item["type_id"])
        ):
            stacks[item["type_id"]] = stacks.get(item["type_id"], 0) + item.get("quantity", 1)
    ship_value = risk.ship_exposure(db, character_id)
    here = _system_info(db, location.get("solar_system_id"))
    items = []
    for type_id, quantity in stacks.items():
        analysis = ore.analyse(db, esi, character_id, type_id, quantity, station_id, highsec_only)
        fitting = [o for o in analysis.options if o.instant and o.active_minutes <= minutes]
        if not fitting:
            continue
        for option in fitting[:2]:  # best, and the best alternative
            if option.jumps == 0:
                rating = risk.rate("very_low", 0.0, net_worth)  # docked: nothing exposed
            else:
                band = risk.band_for_security(here["security_status"] if here else None)
                rating = risk.rate(band, option.isk + ship_value, net_worth)
            items.append(
                PlanItem(
                    kind="sell_hangar",
                    key=f"{type_id}:{option.key}",
                    title=f"{analysis.type_name} x{quantity:,}: {option.title}",
                    isk=option.isk,
                    minutes=option.active_minutes,
                    risk=rating,
                    reasons=option.reasons,
                    inputs={
                        "type_id": type_id,
                        "quantity": quantity,
                        "jumps": option.jumps,
                        "option": option.key,
                    },  # fmt: skip
                )
            )
    return items


def _ore_choices(db, character_id, system_id, named: list[str] | None, notes) -> list[int]:
    if named:
        return [skills.find_type(db, name) for name in named]
    ledger = snapshots.latest(db, character_id, "mining_ledger") or []
    mined = list(
        dict.fromkeys(e["type_id"] for e in ledger if e.get("solar_system_id") == system_id)
    )
    if mined:
        notes.append(Reason("ores_from_mining_ledger"))
        return mined
    notes.append(Reason("ores_assumed", {"ores": list(DEFAULT_ORES)}))
    found = []
    for name in DEFAULT_ORES:
        row = db.execute(
            "SELECT type_id FROM sde_types WHERE name = ? AND published = 1", (name,)
        ).fetchone()
        if row:
            found.append(row["type_id"])
    return found


def mining_candidates(
    db, esi, character_id, station_id, minutes, net_worth, ore_names, notes
) -> list[PlanItem]:
    try:
        estimate = mining.estimate_current(db, character_id)
    except mining.MiningError as exc:
        notes.append(Reason("mining_unavailable", {"why": str(exc)}))
        return []
    if estimate.m3_per_minute <= 0 or station_id is None:
        notes.append(Reason("no_mining_ship" if station_id else "no_station_to_sell"))
        return []
    location = snapshots.latest(db, character_id, "location") or {}
    here = _system_info(db, location.get("solar_system_id"))
    mining_minutes = max(0.0, minutes - ore.MINUTES_TO_SELL)
    hold_minutes = estimate.minutes_to_fill_hold
    if hold_minutes:
        trips = math.ceil(mining_minutes / (hold_minutes + MINUTES_PER_HOLD_TRIP))
        mining_minutes = max(0.0, mining_minutes - trips * MINUTES_PER_HOLD_TRIP)
    volume = estimate.m3_per_minute * mining_minutes
    ship_value = risk.ship_exposure(db, character_id)
    band = risk.band_for_security(
        here["security_status"] if here else None, here["region"] if here else None
    )
    items = []
    for type_id in _ore_choices(db, character_id, location.get("solar_system_id"), ore_names,
                                notes):  # fmt: skip
        unit = db.execute("SELECT volume FROM sde_types WHERE type_id = ?", (type_id,)).fetchone()
        if not unit or not unit["volume"]:
            continue
        quantity = int(volume / unit["volume"])
        if quantity <= 0:
            continue
        analysis = ore.analyse(db, esi, character_id, type_id, quantity, station_id)
        here_options = [o for o in analysis.options if o.instant and o.jumps == 0]
        if not here_options:
            continue
        best = here_options[0]
        hold_value = (best.isk / volume) * (estimate.hold_m3 or volume) if volume else 0.0
        items.append(
            PlanItem(
                kind="mining",
                key=f"{type_id}",
                title=f"Mine {analysis.type_name} for {mining_minutes:.0f} min, then "
                f"{best.title[0].lower()}{best.title[1:]}",
                isk=best.isk,
                minutes=minutes,
                risk=risk.rate(band, ship_value + hold_value, net_worth),
                reasons=(
                    Reason(
                        "mining_estimate",
                        {"m3_per_minute": estimate.m3_per_minute, "ship": estimate.ship_name},
                    ),  # fmt: skip
                    *estimate.reasons,
                ),
                inputs={
                    "type_id": type_id,
                    "quantity": quantity,
                    "mining_minutes": mining_minutes,
                    "sell_option": best.key,
                },  # fmt: skip
            )
        )
    return items


def courier_candidates(db, esi, character_id, minutes, net_worth, highsec_only, notes):
    try:
        found = couriers.find(
            db, esi, character_id, highsec_only=highsec_only, max_minutes=minutes, limit=5
        )
    except couriers.CourierError as exc:
        notes.append(Reason("couriers_unavailable", {"why": str(exc)}))
        return []
    if not found.options and found.excluded:
        notes.append(Reason("no_couriers_fit", dict(found.excluded)))
    ship_value = risk.ship_exposure(db, character_id)
    items = []
    for option in found.options:
        band = risk.band_for_security(option.lowest_security)
        items.append(
            PlanItem(
                kind="courier",
                key=str(option.contract_id),
                title=f"Courier {option.start_system} -> {option.end_system} "
                f"({option.pickup_jumps}+{option.delivery_jumps} jumps, "
                f"{option.volume_m3:,.0f} m3)",
                isk=option.reward,
                minutes=option.minutes,
                risk=risk.rate(band, option.collateral + ship_value, net_worth),
                reasons=option.reasons,
                inputs={
                    "contract_id": option.contract_id,
                    "collateral": option.collateral,
                    "player_title": option.player_title,
                },  # fmt: skip
            )
        )
    return items


def skill_candidates(db, character_id) -> list[PlanItem]:
    queue = snapshots.latest(db, character_id, "skillqueue")
    if queue is None or queue:
        return []
    try:
        character = skills.load_character_skills(db, character_id)
    except skills.SkillsError:
        return []
    if character.is_alpha and character.alpha_training_blocked:
        return []
    return [
        PlanItem(
            kind="skill",
            key="queue_empty",
            title="Your skill queue is empty: queue some training (see Goals and Skills)",
            isk=0.0,
            minutes=MINUTES_TO_QUEUE_SKILLS,
            risk=risk.rate("very_low", 0.0, 1.0),
        )
    ]


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
    station = _selling_station(db, character_id, notes)

    candidates: list[PlanItem] = []
    candidates += skill_candidates(db, character_id)
    if station is not None:
        candidates += hangar_candidates(db, esi, character_id, station, worth, highsec_only,
                                        minutes)  # fmt: skip
    candidates += mining_candidates(db, esi, character_id, station, minutes, worth, ore_names,
                                    notes)  # fmt: skip
    candidates += courier_candidates(db, esi, character_id, minutes, worth, highsec_only, notes)
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
        (i for i in usable if i.kind == "sell_hangar" and i.inputs.get("jumps") == 0),
        key=lambda i: -i.adjusted_isk,
    )
    sold_types = set()
    for item in sells:
        if item.inputs["type_id"] in sold_types:
            continue
        if item.minutes <= time_left:
            take(item)
            sold_types.add(item.inputs["type_id"])

    # 2. one main activity for the remaining time
    main_pool = [
        i
        for i in usable
        if i.kind in ("mining", "courier")
        or (
            i.kind == "sell_hangar"
            and i.inputs.get("jumps", 0) > 0
            and i.inputs["type_id"] not in sold_types
        )  # fmt: skip
    ]
    main_pool.sort(key=lambda i: -i.adjusted_isk)
    picked_main = None
    for item in main_pool:
        if item.kind != "mining" and item.minutes > time_left + 1e-9:
            continue
        if item.kind == "mining" and time_left < MIN_MINING_MINUTES:
            continue
        if item.new_to_you and new_used:
            continue
        if item.kind == "mining":
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
        if item.kind == "sell_hangar" and item.inputs["type_id"] in sold_types:
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
        notes=tuple(notes),
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
    row = db.execute("SELECT kind FROM plan_items WHERE item_id = ?", (item_id,)).fetchone()
    if row is None:
        raise PlanError(f"No plan item {item_id}.")
    with db:
        db.execute(
            "INSERT INTO plan_responses (item_id, response, responded_at) VALUES (?, ?, ?)",
            (item_id, response, utc_now()),
        )
    if response == "mute":
        preferences.mute(db, f"kind:{row['kind']}")


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
    )


def latest_plan_id(db: sqlite3.Connection, character_id: int | None = None) -> int | None:
    if character_id is None:
        row = db.execute("SELECT MAX(plan_id) FROM plans").fetchone()
    else:
        row = db.execute(
            "SELECT MAX(plan_id) FROM plans WHERE character_id = ?", (character_id,)
        ).fetchone()
    return row[0]
