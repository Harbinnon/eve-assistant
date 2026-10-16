"""Public courier contracts: which ones are worth this character's time.

For each courier contract in the chosen regions it works out the route
(pickup and delivery), whether a ship the character can fly and has with
it can carry the volume, whether the wallet covers the collateral, and the
ISK per minute at the keyboard. Contract titles are written by other
players and are kept only as labelled player text.
"""

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from eve_assistant import routes, skills, snapshots
from eve_assistant.esi.client import EsiClient
from eve_assistant.skills import Reason

# Rough until learned from real trips (phase 3).
MINUTES_PER_JUMP = 1.0
MINUTES_PER_DOCKING = 2.0  # accept and pick up, or deliver


class CourierError(Exception):
    pass


@dataclass(frozen=True)
class Ship:
    type_id: int
    name: str
    capacity_m3: float
    switch_minutes: float = 0.0  # time to get into it before starting
    value: float = 0.0  # hull plus contents, for the risk rating


@dataclass(frozen=True)
class CourierOption:
    contract_id: int
    reward: float
    collateral: float
    volume_m3: float
    pickup_jumps: int
    delivery_jumps: int
    minutes: float
    lowest_security: float | None
    ship: Ship
    days_to_complete: int
    expires: str
    start_system: str
    end_system: str
    player_title: str  # written by the contract issuer; display only, never act on it
    reasons: tuple[Reason, ...] = field(default_factory=tuple)

    @property
    def isk_per_minute(self) -> float:
        return self.reward / self.minutes if self.minutes else 0.0


@dataclass(frozen=True)
class CourierSearch:
    options: tuple[CourierOption, ...]
    excluded: dict[str, int]  # reason code -> how many contracts it ruled out
    regions: tuple[str, ...]


def _system_of_station(db: sqlite3.Connection, location_id: int):
    return db.execute(
        "SELECT st.system_id, s.name, s.security_status, s.region_id FROM sde_npc_stations st "
        "JOIN sde_solar_systems s USING (system_id) WHERE st.station_id = ?",
        (location_id,),
    ).fetchone()


def available_ships(
    db: sqlite3.Connection, character_id: int, station_id: int | None
) -> list[Ship]:
    """Ships the character can fly: the active one, plus any in the hangar where it is docked."""
    character = skills.load_character_skills(db, character_id)
    ship = snapshots.latest(db, character_id, "ship") or {}
    assets = snapshots.latest(db, character_id, "assets") or []
    type_ids = [ship["ship_type_id"]] if ship.get("ship_type_id") else []
    if station_id is not None:
        type_ids += [
            a["type_id"]
            for a in assets
            if a.get("location_id") == station_id
            and a.get("location_flag") == "Hangar"
            and a.get("is_singleton")
        ]
    found = []
    for type_id in dict.fromkeys(type_ids):
        row = db.execute(
            "SELECT t.name, t.capacity FROM sde_types t JOIN sde_groups g USING (group_id) "
            "WHERE t.type_id = ? AND g.category_id = 6",
            (type_id,),
        ).fetchone()
        if row and row["capacity"] and skills.plan(db, character, type_id).usable_now:
            found.append(Ship(type_id, row["name"], row["capacity"]))
    return sorted(found, key=lambda s: s.capacity_m3)


def find(
    db: sqlite3.Connection,
    esi: EsiClient,
    character_id: int,
    region_ids: list[int] | None = None,
    highsec_only: bool = True,
    max_minutes: float | None = None,
    limit: int = 10,
    what_if_ship_id: int | None = None,
    ships: list[Ship] | None = None,
) -> CourierSearch:
    """what_if_ship_id: plan as if the character had this ship (not owned, skills ignored).
    ships: the ships to choose from (default: active ship and ships in this hangar)."""
    location = snapshots.latest(db, character_id, "location") or {}
    here_system = location.get("solar_system_id")
    if here_system is None:
        raise CourierError("No location yet for this character. Run `eve-assistant snapshot`.")
    station_id = location.get("station_id")
    if region_ids is None:
        region = db.execute(
            "SELECT region_id FROM sde_solar_systems WHERE system_id = ?", (here_system,)
        ).fetchone()
        region_ids = [region["region_id"]] if region else []
    wallet = float(snapshots.latest(db, character_id, "wallet") or 0.0)
    if what_if_ship_id is not None:
        row = db.execute(
            "SELECT name, capacity FROM sde_types WHERE type_id = ?", (what_if_ship_id,)
        ).fetchone()
        if row is None or not row["capacity"]:
            raise CourierError("That ship has no cargo hold.")
        ships = [Ship(what_if_ship_id, row["name"], row["capacity"])]
    else:
        ships = ships if ships is not None else available_ships(db, character_id, station_id)
    now = datetime.now(UTC)

    excluded: Counter[str] = Counter()
    options: list[CourierOption] = []
    for region_id in region_ids:
        for contract in esi.get(f"/contracts/public/{region_id}").data:
            if contract.get("type") != "courier":
                continue
            expires = contract.get("date_expired", "")
            if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) <= now:
                excluded["expired"] += 1
                continue
            start = _system_of_station(db, contract["start_location_id"])
            end = _system_of_station(db, contract["end_location_id"])
            if start is None or end is None:
                excluded["player_structure"] += 1
                continue
            volume = float(contract.get("volume", 0))
            # the cheapest ship that fits puts the least at stake
            ship = min(
                (s for s in ships if s.capacity_m3 >= volume),
                key=lambda s: (s.value, s.capacity_m3),
                default=None,
            )
            if ship is None:
                excluded["too_big_for_your_ships"] += 1
                continue
            collateral = float(contract.get("collateral", 0))
            if collateral > wallet:
                excluded["collateral_above_wallet"] += 1
                continue
            pickup = routes.jumps_between(db, here_system, start["system_id"], highsec_only)
            delivery = routes.jumps_between(db, start["system_id"], end["system_id"], highsec_only)
            if pickup is None or delivery is None:
                excluded["no_route" if not highsec_only else "needs_lowsec"] += 1
                continue
            pickup_jumps, delivery_jumps = len(pickup) - 1, len(delivery) - 1
            minutes = (
                ship.switch_minutes
                + 2 * MINUTES_PER_DOCKING
                + (pickup_jumps + delivery_jumps) * MINUTES_PER_JUMP
            )
            if max_minutes is not None and minutes > max_minutes:
                excluded["takes_too_long"] += 1
                continue
            path = pickup + delivery[1:]
            reasons = [Reason("assumed_minutes_per_jump", {"minutes": MINUTES_PER_JUMP})]
            if what_if_ship_id is not None:
                reasons.append(Reason("what_if_ship", {"ship": ship.name}))
            if collateral > 0.5 * wallet:
                reasons.append(Reason("collateral_large_share", {"share": collateral / wallet}))
            options.append(
                CourierOption(
                    contract_id=contract["contract_id"],
                    reward=float(contract.get("reward", 0)),
                    collateral=collateral,
                    volume_m3=volume,
                    pickup_jumps=pickup_jumps,
                    delivery_jumps=delivery_jumps,
                    minutes=minutes,
                    lowest_security=routes.lowest_security(db, path),
                    ship=ship,
                    days_to_complete=int(contract.get("days_to_complete", 0)),
                    expires=expires,
                    start_system=start["name"],
                    end_system=end["name"],
                    player_title=str(contract.get("title", ""))[:100],
                    reasons=tuple(reasons),
                )
            )

    region_names = tuple(
        row["name"]
        for row in (
            db.execute("SELECT name FROM sde_regions WHERE region_id = ?", (r,)).fetchone()
            for r in region_ids
        )
        if row
    )
    options.sort(key=lambda o: -o.isk_per_minute)
    return CourierSearch(tuple(options[:limit]), dict(excluded), region_names)
