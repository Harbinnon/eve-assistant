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

from eve_assistant import (
    corrections,
    couriers,
    fleet,
    mining,
    ore,
    preferences,
    projects,
    risk,
    routes,
    skills,
    snapshots,
)
from eve_assistant.esi.client import EsiClient
from eve_assistant.risk import RiskRating
from eve_assistant.skills import Reason
from eve_assistant.store import utc_now

MINUTES_PER_HOLD_TRIP = 3.0  # dock, unload, undock between full holds
MINUTES_TO_QUEUE_SKILLS = 1.0
MIN_MINING_MINUTES = 10.0  # shorter than this is not worth undocking for
# What can be "new to you"; reminders never are.
ACTIVITY_KINDS = ("mining", "courier", "project_start")

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


def _fits_in_ship(option: ore.Option, holds: tuple[float, float]) -> bool:
    if option.jumps == 0 or option.hauled_m3 <= 0:
        return True
    cargo, ore_hold = holds
    capacity = cargo + (ore_hold if option.hauled_is_ore else 0.0)
    return option.hauled_m3 <= capacity


def _pick_hauler(
    ships: list[fleet.FleetShip], station_id: int, here_location: int | None, option: ore.Option
) -> tuple[fleet.FleetShip, float] | None:
    """The cheapest flyable ship that can carry this option's load, and the minutes to
    switch into it: the active ship, one waiting at the ore's station, or one where the
    character is now."""
    best = None
    for ship in ships:
        if not ship.usable or ship.capacity_for(option.hauled_is_ore) < option.hauled_m3:
            continue
        if ship.is_active:
            switch = 0.0
        elif ship.location_id in (station_id, here_location):
            switch = fleet.MINUTES_TO_SWITCH_SHIP
        else:
            continue
        if best is None or (ship.value, switch) < (best[0].value, best[1]):
            best = (ship, switch)
    return best


def hangar_candidates(
    db, esi, character_id, ships, net_worth, highsec_only, minutes, notes
) -> list[PlanItem]:
    """Ore in station hangars: where the character is docked (quick jobs) and in
    other NPC stations it can reach (travel there, then sell). Hauls use whichever
    owned ship can carry the load."""
    location = snapshots.latest(db, character_id, "location") or {}
    here_system = location.get("solar_system_id")
    docked_at = location.get("station_id")
    here_location = docked_at or location.get("structure_id")
    active_value = next((s.value for s in ships if s.is_active), 0.0)
    assets = snapshots.latest(db, character_id, "assets") or []
    stacks: dict[int, dict[int, int]] = {}
    for item in assets:
        if item.get("location_flag") == "Hangar" and _is_ore(db, item["type_id"]):
            by_type = stacks.setdefault(item["location_id"], {})
            by_type[item["type_id"]] = by_type.get(item["type_id"], 0) + item.get("quantity", 1)
    items = []
    for station_id, by_type in stacks.items():
        station = db.execute(
            "SELECT st.system_id, s.name FROM sde_npc_stations st JOIN sde_solar_systems s "
            "USING (system_id) WHERE st.station_id = ?",
            (station_id,),
        ).fetchone()
        if station is None:
            notes.append(Reason("ore_in_structure_skipped"))
            continue
        if station_id == docked_at:
            travel_path = [here_system]
        else:
            travel_path = routes.jumps_between(db, here_system, station["system_id"], highsec_only)
            if travel_path is None:
                continue
        travel_jumps = len(travel_path) - 1
        travel_minutes = (
            0.0 if station_id == docked_at else travel_jumps * ore.MINUTES_PER_JUMP + 2.0
        )
        for type_id, quantity in by_type.items():
            analysis = ore.analyse(
                db, esi, character_id, type_id, quantity, station_id, highsec_only
            )
            fitting = []
            multi_trip = []
            for option in analysis.options:
                if not option.instant:
                    continue
                hauler, switch = None, 0.0
                if option.jumps and option.hauled_m3 > 0:
                    picked = _pick_hauler(ships, station_id, here_location, option)
                    if picked is None:
                        trips = _multi_trip_haul(ships, station_id, here_location, option)
                        if trips is not None:
                            multi_trip.append((option, *trips))
                        continue
                    hauler, switch = picked
                total = travel_minutes + switch + option.active_minutes
                if total <= minutes:
                    fitting.append((option, hauler, switch, total))
                elif hauler is not None:
                    multi_trip.append((option, hauler, switch, 1))  # too long: a project
            for option, hauler, _switch, total in fitting[:2]:  # best, and the next best
                if travel_jumps == 0 and option.jumps == 0:
                    rating = risk.rate("very_low", 0.0, net_worth)  # docked: nothing exposed
                else:
                    band = risk.band_for_security(routes.lowest_security(db, travel_path))
                    stake = hauler.value + option.isk if hauler is not None else active_value
                    rating = risk.rate(band, stake, net_worth)
                if station_id == docked_at:
                    where = ""
                elif travel_jumps == 0:
                    where = f"Dock at the station in {station['name']} where it is stored, then: "
                else:
                    where = f"Go to {station['name']} ({travel_jumps} jumps), then: "
                ship_note = f" (in your {hauler.name})" if hauler is not None else ""
                reasons = option.reasons
                if station_id != docked_at:
                    reasons = (
                        Reason(
                            "ore_stored_elsewhere",
                            {"system": station["name"], "jumps": travel_jumps},
                        ),
                        *reasons,
                    )
                if hauler is not None:
                    reasons = (
                        Reason(
                            "hauler_chosen",
                            {
                                "ship": hauler.name,
                                "load_m3": option.hauled_m3,
                                "capacity_m3": hauler.capacity_for(option.hauled_is_ore),
                            },
                        ),
                        *reasons,
                    )
                items.append(
                    PlanItem(
                        kind="sell_hangar",
                        key=f"{station_id}:{type_id}:{option.key}",
                        title=(
                            f"{where}{analysis.type_name} x{quantity:,}: {option.title}{ship_note}"
                        ),
                        isk=option.isk,
                        minutes=total,
                        risk=rating,
                        reasons=reasons,
                        inputs={
                            "type_id": type_id,
                            "quantity": quantity,
                            "station_id": station_id,
                            "travel_jumps": travel_jumps,
                            "jumps": option.jumps,
                            "option": option.key,
                            "ship_item_id": hauler.item_id if hauler else None,
                        },
                    )
                )
            for option, hauler, switch, trips in multi_trip[:1]:
                per_trip = 2 * option.jumps * ore.MINUTES_PER_JUMP + 4.0
                total = (
                    travel_minutes
                    + switch
                    + ore.MINUTES_TO_SELL
                    + trips * per_trip
                    - option.jumps * ore.MINUTES_PER_JUMP  # no trip back after the last load
                )
                band = risk.band_for_security(routes.lowest_security(db, travel_path))
                stake = hauler.value + option.isk / trips
                title = (
                    f"Haul {analysis.type_name} x{quantity:,} from {station['name']} to "
                    f"{option.location} in your {hauler.name}"
                    + (f" ({trips} trips)" if trips > 1 else "")
                    + " and sell it there"
                )
                items.append(
                    _project_item(
                        kind="haul",
                        key=f"{station_id}:{type_id}:{option.key}",
                        title=title,
                        total_minutes=total,
                        isk=option.isk,
                        minutes=minutes,
                        rating=risk.rate(band, stake, net_worth),
                        reasons=(
                            Reason(
                                "multi_trip_haul",
                                {"trips": trips, "ship": hauler.name, "load_m3": option.hauled_m3},
                            ),  # fmt: skip
                            *option.reasons,
                        ),
                        inputs={
                            "type_id": type_id,
                            "quantity": quantity,
                            "station_id": station_id,
                            "trips": trips,
                            "ship_item_id": hauler.item_id,
                        },  # fmt: skip
                    )
                )
    return items


def _multi_trip_haul(
    ships: list[fleet.FleetShip], station_id: int, here_location: int | None, option: ore.Option
) -> tuple[fleet.FleetShip, float, int] | None:
    """The reachable flyable ship needing the fewest trips for a load too big for one."""
    best = None
    for ship in ships:
        capacity = ship.capacity_for(option.hauled_is_ore)
        if not ship.usable or capacity <= 0:
            continue
        if ship.is_active:
            switch = 0.0
        elif ship.location_id in (station_id, here_location):
            switch = fleet.MINUTES_TO_SWITCH_SHIP
        else:
            continue
        trips = math.ceil(option.hauled_m3 / capacity)
        if best is None or (trips, ship.value) < (best[2], best[0].value):
            best = (ship, switch, trips)
    return best


def _project_item(
    *, kind, key, title, total_minutes, isk, minutes, rating, reasons, inputs, deadline_days=None
) -> PlanItem:
    """A plan item that either fits in this session or starts a multi-session project.
    A project's value is its share for this session, discounted for not finishing."""
    if total_minutes <= minutes:
        return PlanItem(
            kind="sell_hangar" if kind == "haul" else kind,
            key=key,
            title=title,
            isk=isk,
            minutes=total_minutes,
            risk=rating,
            reasons=reasons,
            inputs={**inputs, "jumps": 1, "travel_jumps": 1},
        )
    share = minutes / total_minutes
    return PlanItem(
        kind="project_start",
        key=f"{kind}:{key}",
        title=f"Start a project: {title}",
        isk=isk * share * (1 - projects.NOT_FINISHING_CHANCE),
        minutes=minutes,
        risk=rating,
        reasons=(
            Reason(
                "project_value",
                {
                    "total_minutes": total_minutes,
                    "total_isk": isk,
                    "share": share,
                    "not_finishing": projects.NOT_FINISHING_CHANCE,
                },
            ),  # fmt: skip
            *reasons,
        ),
        inputs={
            **inputs,
            "project": {
                "kind": kind,
                "key": key,
                "title": title,
                "total_minutes": total_minutes,
                "isk": isk,
                "deadline_days": deadline_days,
                "risk": {"band": rating.band, "exposure": rating.exposure},
            },
        },
    )


def project_candidates(db, character_id, minutes, net_worth, notes) -> list[PlanItem]:
    """Open projects, to continue."""
    items = []
    for project in projects.open_projects(db, character_id):
        if project.minutes_left <= 0:
            notes.append(Reason("project_time_used", {"title": project.title}))
            continue
        session = min(minutes, project.minutes_left)
        stored = project.details.get("risk", {})
        rating = risk.rate(stored.get("band", "low"), stored.get("exposure", 0.0), net_worth)
        reasons = [
            Reason(
                "project_progress", {"done": project.minutes_done, "total": project.total_minutes}
            ),  # fmt: skip
        ]
        if project.deadline:
            reasons.append(Reason("project_deadline", {"deadline": project.deadline}))
        items.append(
            PlanItem(
                kind="project_continue",
                key=str(project.project_id),
                title=f"Continue project: {project.title}",
                isk=project.isk
                * (session / project.total_minutes)
                * (1 - projects.NOT_FINISHING_CHANCE),
                minutes=session,
                risk=rating,
                reasons=tuple(reasons),
                inputs={"project_id": project.project_id},
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


def _first_station(db: sqlite3.Connection, system_id: int | None) -> int | None:
    row = db.execute(
        "SELECT station_id FROM sde_npc_stations WHERE system_id = ? ORDER BY station_id LIMIT 1",
        (system_id,),
    ).fetchone()
    return row["station_id"] if row else None


def mining_candidates(
    db, esi, character_id, ships, minutes, net_worth, ore_names, notes
) -> list[PlanItem]:
    """A mining session in each owned, flyable ship that has mining modules fitted,
    after the time it takes to get into that ship."""
    try:
        character = skills.load_character_skills(db, character_id)
    except skills.SkillsError as exc:
        notes.append(Reason("mining_unavailable", {"why": str(exc)}))
        return []
    implants = snapshots.latest(db, character_id, "implants") or []
    location = snapshots.latest(db, character_id, "location") or {}
    learned = corrections.correction(db, character_id, "mining_rate")
    items = []
    any_miner = False
    for ship in ships:
        board = fleet.boarding(db, character_id, ship)
        if board is None:
            continue
        try:
            estimate = mining.estimate(db, character, ship.type_id, list(ship.fitted), implants)
        except mining.MiningError as exc:
            notes.append(Reason("mining_unavailable", {"why": str(exc)}))
            return []
        if estimate.m3_per_minute <= 0:
            continue
        any_miner = True
        system_id = location.get("solar_system_id") if board.jumps == 0 else ship.system_id
        if ship.is_active and location.get("station_id") and board.jumps == 0:
            station_id = location["station_id"]
        else:
            station_id = _first_station(db, system_id)
        if station_id is None:
            notes.append(Reason("no_station_to_sell"))
            continue
        here = _system_info(db, system_id)
        time_left = minutes - board.minutes
        mining_minutes = max(0.0, time_left - ore.MINUTES_TO_SELL)
        # The estimate is from the static data; the correction scales it to how
        # fast this player really mines. The raw figure is what gets recorded.
        rate = estimate.m3_per_minute * learned.factor
        hold_minutes = estimate.hold_m3 / rate if estimate.hold_m3 else None
        if hold_minutes:
            trips = math.ceil(mining_minutes / (hold_minutes + MINUTES_PER_HOLD_TRIP))
            mining_minutes = max(0.0, mining_minutes - trips * MINUTES_PER_HOLD_TRIP)
        if mining_minutes < MIN_MINING_MINUTES:
            continue
        volume = rate * mining_minutes
        securities = [here["security_status"]] if here else []
        if board.jumps:
            route_low = routes.lowest_security(db, board.path)
            if route_low is not None:
                securities.append(route_low)
        band = risk.band_for_security(
            min(securities) if securities else None, here["region"] if here else None
        )
        prefix = (
            f"Board your {ship.name} ({board.minutes:.0f} min), then mine"
            if board.needs_switch
            else "Mine"
        )
        for type_id in _ore_choices(db, character_id, system_id, ore_names, notes):
            unit = db.execute(
                "SELECT volume FROM sde_types WHERE type_id = ?", (type_id,)
            ).fetchone()
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
                    key=f"{ship.item_id}:{type_id}",
                    title=f"{prefix} {analysis.type_name} for {mining_minutes:.0f} min, then "
                    f"{best.title[0].lower()}{best.title[1:]}",
                    isk=best.isk,
                    minutes=minutes,
                    risk=risk.rate(band, ship.value + hold_value, net_worth),
                    reasons=(
                        Reason(
                            "mining_estimate",
                            {"m3_per_minute": estimate.m3_per_minute, "ship": estimate.ship_name},
                        ),
                        learned.reason(),
                        *estimate.reasons,
                    ),
                    inputs={
                        "type_id": type_id,
                        "quantity": quantity,
                        "mining_minutes": mining_minutes,
                        "sell_option": best.key,
                        "ship_item_id": ship.item_id,
                        "board_minutes": board.minutes,
                    },
                )
            )
    if not any_miner:
        notes.append(Reason("no_mining_ship"))
    return items


def courier_candidates(db, esi, character_id, ships, minutes, net_worth, highsec_only, notes):
    choices = []
    for ship in ships:
        if not ship.usable or not ship.cargo_m3:
            continue
        board = fleet.boarding(db, character_id, ship, highsec_only)
        if board is None:
            continue
        choices.append(
            couriers.Ship(
                ship.type_id,
                ship.name,
                ship.cargo_m3,
                switch_minutes=board.minutes,
                value=ship.value,
                origin_system_id=ship.system_id if board.jumps else None,
                boarding_lowest_security=(
                    routes.lowest_security(db, board.path) if board.jumps else None
                ),
            )
        )
    try:
        found = couriers.find(
            db,
            esi,
            character_id,
            highsec_only=highsec_only,
            max_minutes=None,
            limit=10,
            ships=choices,
        )
    except couriers.CourierError as exc:
        notes.append(Reason("couriers_unavailable", {"why": str(exc)}))
        return []
    if not found.options and found.excluded:
        notes.append(Reason("no_couriers_fit", dict(found.excluded)))
    items = []
    weekly = projects.weekly_minutes(db)
    for option in found.options:
        band = risk.band_for_security(option.lowest_security)
        in_ship = f" in your {option.ship.name}" if option.ship.switch_minutes else ""
        if option.minutes > minutes:
            if not projects.finishes_in_time(option.minutes, option.days_to_complete, weekly):
                continue
            items.append(
                _project_item(
                    kind="courier",
                    key=str(option.contract_id),
                    title=f"Courier {option.start_system} -> {option.end_system} "
                    f"({option.pickup_jumps}+{option.delivery_jumps} jumps){in_ship}",
                    total_minutes=option.minutes,
                    isk=option.reward,
                    minutes=minutes,
                    rating=risk.rate(band, option.collateral + option.ship.value, net_worth),
                    reasons=(
                        Reason(
                            "deadline_fits_your_pace",
                            {"days": option.days_to_complete, "weekly_minutes": weekly},
                        ),  # fmt: skip
                        *option.reasons,
                    ),
                    inputs={
                        "contract_id": option.contract_id,
                        "collateral": option.collateral,
                        "player_title": option.player_title,
                    },  # fmt: skip
                    deadline_days=option.days_to_complete,
                )
            )
            continue
        items.append(
            PlanItem(
                kind="courier",
                key=str(option.contract_id),
                title=f"Courier {option.start_system} -> {option.end_system} "
                f"({option.pickup_jumps}+{option.delivery_jumps} jumps, "
                f"{option.volume_m3:,.0f} m3){in_ship}",
                isk=option.reward,
                minutes=option.minutes,
                risk=risk.rate(band, option.collateral + option.ship.value, net_worth),
                reasons=option.reasons,
                inputs={
                    "contract_id": option.contract_id,
                    "collateral": option.collateral,
                    "player_title": option.player_title,
                    "ship": option.ship.name,
                },
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
    _selling_station(db, character_id, notes)  # notes where selling happens
    try:
        ships = fleet.owned_ships(db, character_id)
    except skills.SkillsError as exc:
        notes.append(Reason("mining_unavailable", {"why": str(exc)}))
        ships = []

    candidates: list[PlanItem] = []
    candidates += skill_candidates(db, character_id)
    candidates += project_candidates(db, character_id, minutes, worth, notes)
    candidates += hangar_candidates(
        db, esi, character_id, ships, worth, highsec_only, minutes, notes
    )
    candidates += mining_candidates(db, esi, character_id, ships, minutes, worth, ore_names, notes)
    candidates += courier_candidates(
        db, esi, character_id, ships, minutes, worth, highsec_only, notes
    )
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
