"""What to do with a stack of ore: sell it, refine it, or haul it to a hub.

Every option carries its ISK result, the minutes at the keyboard it takes,
and structured reasons, so the planner can compare it with other activities
and explain the choice. Hauling time uses a flat minutes-per-jump figure
until the tool has learned the player's real pace.
"""

import math
import sqlite3
from dataclasses import dataclass, field

from eve_assistant import fees, market, routes, sde, skills
from eve_assistant.esi.client import EsiClient
from eve_assistant.skills import Reason

# Rough until learned from real trips (phase 3).
MINUTES_PER_JUMP = 1.0
MINUTES_TO_SELL = 2.0
MINUTES_TO_REFINE = 2.0
MINUTES_TO_LIST = 3.0

SKILL_NAMES = ("Accounting", "Broker Relations", "Reprocessing", "Reprocessing Efficiency")


class OreError(Exception):
    pass


@dataclass(frozen=True)
class Option:
    key: str  # stable identifier, e.g. "refine_sell_here"
    title: str
    isk: float
    active_minutes: float
    instant: bool  # False when ISK arrives only once buyers come (sell orders)
    location: str
    jumps: int = 0
    reasons: tuple[Reason, ...] = field(default_factory=tuple)

    @property
    def isk_per_minute(self) -> float:
        return self.isk / self.active_minutes if self.active_minutes else self.isk


@dataclass(frozen=True)
class OreAnalysis:
    type_id: int
    type_name: str
    quantity: int
    volume_m3: float
    compressed_volume_m3: float | None
    refine_yield: float
    refine_tax: float
    options: tuple[Option, ...]
    reasons: tuple[Reason, ...]


def _level(db: sqlite3.Connection, character: skills.CharacterSkills, name: str) -> int:
    row = db.execute(
        "SELECT type_id FROM sde_types WHERE name = ? AND published = 1", (name,)
    ).fetchone()
    if row is None:
        return 0
    trained = character.skills.get(row["type_id"])
    if trained is None:
        return 0
    return trained.active_level if character.is_alpha else trained.trained_level


def _skill_level_by_id(character: skills.CharacterSkills, skill_id: int | None) -> int:
    if skill_id is None or skill_id not in character.skills:
        return 0
    trained = character.skills[skill_id]
    return trained.active_level if character.is_alpha else trained.trained_level


def _attribute(db: sqlite3.Connection, type_id: int, attribute_id: int) -> float | None:
    row = db.execute(
        "SELECT value FROM sde_type_attributes WHERE type_id = ? AND attribute_id = ?",
        (type_id, attribute_id),
    ).fetchone()
    return row[0] if row else None


def refine_yield(
    station_efficiency: float, reprocessing: int, efficiency_skill: int, ore_skill: int
) -> float:
    return (
        station_efficiency
        * (1 + 0.03 * reprocessing)
        * (1 + 0.02 * efficiency_skill)
        * (1 + 0.02 * ore_skill)
    )


def _station(db: sqlite3.Connection, station_id: int):
    row = db.execute(
        "SELECT st.system_id, s.name AS system_name, s.region_id, r.name AS region_name, "
        "st.reprocessing_efficiency, st.reprocessing_station_take "
        "FROM sde_npc_stations st JOIN sde_solar_systems s USING (system_id) "
        "JOIN sde_regions r USING (region_id) WHERE st.station_id = ?",
        (station_id,),
    ).fetchone()
    if row is None:
        raise OreError(
            "That location is not an NPC station. Player structures are not supported yet."
        )
    return row


def analyse(
    db: sqlite3.Connection,
    esi: EsiClient,
    character_id: int,
    type_id: int,
    quantity: int,
    station_id: int,
    highsec_only: bool = True,
) -> OreAnalysis:
    """Compare selling, refining and hauling for `quantity` units of an ore at a station."""
    sde.require_current(db)
    if quantity <= 0:
        raise OreError("Quantity must be at least 1.")
    character = skills.load_character_skills(db, character_id)
    standings = fees.load_standings(db, character_id)
    here = _station(db, station_id)
    owner_corp, owner_faction = fees.station_owner(db, station_id)

    item = db.execute(
        "SELECT name, volume, portion_size FROM sde_types WHERE type_id = ?", (type_id,)
    ).fetchone()
    materials = db.execute(
        "SELECT material_type_id, quantity FROM sde_type_materials WHERE type_id = ?", (type_id,)
    ).fetchall()
    if item is None or not materials:
        raise OreError("That item cannot be refined, so it is not handled as ore.")
    compressed = db.execute(
        "SELECT t.volume FROM sde_compressible_types c JOIN sde_types t "
        "ON t.type_id = c.compressed_type_id WHERE c.type_id = ?",
        (type_id,),
    ).fetchone()

    accounting, broker, reprocessing, efficiency = (_level(db, character, n) for n in SKILL_NAMES)
    ore_skill_id = _attribute(db, type_id, sde.REPROCESSING_SKILL)
    ore_skill = _skill_level_by_id(character, int(ore_skill_id) if ore_skill_id else None)
    tax_rate = fees.sales_tax(accounting)
    reasons: list[Reason] = [
        Reason(
            "skills_used",
            {
                "accounting": accounting,
                "broker_relations": broker,
                "reprocessing": reprocessing,
                "reprocessing_efficiency": efficiency,
                "ore_processing": ore_skill,
            },
        ),  # fmt: skip
        Reason("assumed_minutes_per_jump", {"minutes": MINUTES_PER_JUMP}),
    ]

    portion = item["portion_size"] or 100
    portions = quantity // portion
    leftover = quantity - portions * portion
    yield_here = refine_yield(here["reprocessing_efficiency"] or 0.5, reprocessing, efficiency,
                              ore_skill)  # fmt: skip
    refine_tax = fees.reprocess_tax(
        here["reprocessing_station_take"] or 0.05, standings.toward(owner_corp)
    )
    minerals = {
        m["material_type_id"]: math.floor(m["quantity"] * portions * yield_here * (1 - refine_tax))
        for m in materials
    }
    if leftover:
        reasons.append(Reason("leftover_not_refined", {"units": leftover, "portion": portion}))

    books: dict[tuple[int, int], list[market.Order]] = {}

    def book(region_id: int, item_id: int) -> list[market.Order]:
        if (region_id, item_id) not in books:
            books[(region_id, item_id)] = market.order_book(esi, region_id, item_id)
        return books[(region_id, item_id)]

    def instant_value(region_id, station, system, item_id, qty) -> tuple[float, int]:
        fill = market.sell_into_buy_orders(book(region_id, item_id), qty, station, system)
        return fill.gross * (1 - tax_rate), qty - fill.quantity

    options: list[Option] = []

    def add_instant(key, title, region_id, station, system, location, jumps):
        raw, unsold_raw = instant_value(region_id, station, system, type_id, quantity)
        options.append(
            Option(
                key=f"sell_raw_{key}",
                title=f"Sell the ore {title}",
                isk=raw,
                active_minutes=MINUTES_TO_SELL + jumps * MINUTES_PER_JUMP,
                instant=True,
                location=location,
                jumps=jumps,
                reasons=(Reason("not_enough_buyers", {"unsold": unsold_raw}),)
                if unsold_raw
                else (),
            )
        )
        refined, unsold = 0.0, 0
        for mineral, amount in minerals.items():
            value, left = instant_value(region_id, station, system, mineral, amount)
            refined += value
            unsold += left
        if leftover:
            value, _ = instant_value(region_id, station, system, type_id, leftover)
            refined += value
        options.append(
            Option(
                key=f"refine_sell_{key}",
                title=f"Refine here, sell the minerals {title}",
                isk=refined,
                active_minutes=MINUTES_TO_REFINE + MINUTES_TO_SELL + jumps * MINUTES_PER_JUMP,
                instant=True,
                location=location,
                jumps=jumps,
                reasons=(Reason("not_enough_buyers", {"unsold": unsold}),) if unsold else (),
            )
        )

    add_instant("here", f"in {here['system_name']}", here["region_id"], station_id,
                here["system_id"], here["system_name"], 0)  # fmt: skip

    for hub in market.HUBS:
        if hub.station_id == station_id:
            continue
        jumps = routes.jump_count(db, here["system_id"], hub.system_id, highsec_only)
        if jumps is None:
            continue
        add_instant(hub.name.lower(), f"at {hub.name} ({jumps} jumps)", hub.region_id,
                    hub.station_id, hub.system_id, hub.name, jumps)  # fmt: skip
        hub_owner, hub_faction = fees.station_owner(db, hub.station_id)
        ask = market.lowest_sell(book(hub.region_id, type_id), hub.station_id)
        if ask:
            listing = ask - 0.01
            broker_rate = fees.broker_fee(
                broker, standings.toward(hub_faction), standings.toward(hub_owner)
            )
            options.append(
                Option(
                    key=f"list_raw_{hub.name.lower()}",
                    title=f"List the ore for sale at {hub.name} ({jumps} jumps)",
                    isk=quantity * listing * (1 - tax_rate - broker_rate),
                    active_minutes=MINUTES_TO_LIST + jumps * MINUTES_PER_JUMP,
                    instant=False,
                    location=hub.name,
                    jumps=jumps,
                    reasons=(
                        Reason(
                            "waits_for_buyers",
                            {
                                "competing_units": market.sell_volume_at(
                                    book(hub.region_id, type_id), hub.station_id
                                ),
                                "broker_fee": broker_rate,
                            },
                        ),  # fmt: skip
                    ),
                )
            )

    if here["region_name"] == "Exordium":
        reasons.append(Reason("exordium_fees", {"surcharge": fees.EXORDIUM_ORDER_SURCHARGE}))

    options.sort(key=lambda o: -o.isk)
    return OreAnalysis(
        type_id=type_id,
        type_name=item["name"],
        quantity=quantity,
        volume_m3=quantity * (item["volume"] or 0),
        compressed_volume_m3=quantity * compressed["volume"] if compressed else None,
        refine_yield=yield_here,
        refine_tax=refine_tax,
        options=tuple(options),
        reasons=tuple(reasons),
    )
