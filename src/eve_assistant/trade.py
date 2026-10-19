"""Trade runs (roadmap 5.5): buy at one trade hub, carry, sell at another.

Two kinds, as EVE Trade and its like offer them:

- **Instant**: buy from the sell orders at A, sell into the buy orders at B,
  so the ISK comes the moment the goods arrive. Both books are walked: each
  next unit is bought at A's next cheapest order and sold to B's next dearest
  one, while that still makes money after sales tax, within the ISK to spend
  and the cargo space.
- **Listed**: buy the same way at A, then list at B a hundredth under B's
  cheapest sell order. It pays more but waits for buyers: units are capped at
  a share of B's daily volume, and the broker fee is paid as well.

Fees are the character's own: sales tax from Accounting, broker fee from
Broker Relations and standing with the station's owner (fees.py). Volumes
are packaged (ships travel packed). Travel is the route the planner would
fly, at the learned minutes a jump; the risk is the live risk along it.
Prices are from the last market scan (market_data.py), up to 30 minutes old.
"""

import math
import sqlite3
from dataclasses import dataclass, field

from eve_assistant import (
    corrections,
    fees,
    fleet,
    market,
    market_data,
    mining,
    risk,
    routes,
    skills,
    snapshots,
)
from eve_assistant.market import Hub, Order

CANDIDATES = 200  # items per hub pair whose books are walked
LISTING_SHARE = 0.1  # of the destination's daily volume a listing is expected to sell
MINUTES_TO_TRADE = 5.0  # buying, then selling, at the keyboard
MINUTES_TO_DOCK = 2.0


@dataclass(frozen=True)
class Limits:
    isk: float  # to spend on goods
    cargo_m3: float
    highsec_only: bool = True
    min_profit: float = 0.0


@dataclass(frozen=True)
class Fees:
    sales_tax: float
    broker: dict[int, float] = field(default_factory=dict)  # by station


@dataclass(frozen=True)
class Run:
    type_id: int
    name: str
    source: Hub
    dest: Hub
    units: int
    m3: float
    buy_cost: float
    sell_value: float  # after tax (and broker fee, listed)
    listed: bool
    first_buy: float
    last_buy: float
    first_sell: float
    last_sell: float
    daily_volume: float | None = None

    @property
    def profit(self) -> float:
        return self.sell_value - self.buy_cost

    @property
    def margin(self) -> float:
        return self.profit / self.buy_cost if self.buy_cost else 0.0


def character_fees(db: sqlite3.Connection, character_id: int) -> Fees:
    character = skills.load_character_skills(db, character_id)
    standings = fees.load_standings(db, character_id)
    broker_level = mining.skill_level(db, character, "Broker Relations")
    broker = {}
    for hub in market.HUBS:
        corp, faction = fees.station_owner(db, hub.station_id)
        broker[hub.station_id] = fees.broker_fee(
            broker_level, standings.toward(faction), standings.toward(corp)
        )
    return Fees(fees.sales_tax(mining.skill_level(db, character, "Accounting")), broker)


def _unit_m3(db: sqlite3.Connection, type_id: int) -> float | None:
    row = db.execute(
        "SELECT COALESCE(packaged_volume, volume) AS m3, name FROM sde_types WHERE type_id = ?",
        (type_id,),
    ).fetchone()
    return row["m3"] if row and row["m3"] else None


def _candidates(db, source: set[int], dest: set[int], keep: float, listed: bool) -> list:
    """(type_id, best price to buy at the source, best price to sell at the destination)
    for items whose best prices leave something after `keep` (the share kept after fees)."""
    store_side = 0 if listed else 1  # listed: under the destination's cheapest sell order
    sources, dests = list(source), list(dest)
    marks_s, marks_d = ",".join("?" * len(sources)), ",".join("?" * len(dests))
    return db.execute(
        f"SELECT s.type_id, s.best AS buy_at, d.best AS sell_at FROM "  # noqa: S608  # placeholders only
        f"(SELECT type_id, MIN(price) AS best FROM market.orders WHERE is_buy = 0 "
        f" AND location_id IN ({marks_s}) GROUP BY type_id) s JOIN "
        f"(SELECT type_id, {'MIN' if listed else 'MAX'}(price) AS best FROM market.orders "
        f" WHERE is_buy = ? AND location_id IN ({marks_d}) GROUP BY type_id) d USING (type_id) "
        f"WHERE d.best * ? > s.best ORDER BY (d.best * ? - s.best) / s.best DESC LIMIT ?",
        (*sources, store_side, *dests, keep, keep, CANDIDATES),
    ).fetchall()


def walk(buy_from: list[Order], sell_to: list[Order], keep: float, isk: float, units: int):
    """Units bought from the cheapest sell orders and sold to the dearest buy orders while
    each still makes money after fees: (units, cost, gross, first/last buy, first/last sell)."""
    bought = cost = 0
    gross = 0.0
    i = j = 0
    left_a = buy_from[0].volume if buy_from else 0
    left_b = sell_to[0].volume if sell_to else 0
    first_buy = first_sell = last_buy = last_sell = 0.0
    while i < len(buy_from) and j < len(sell_to) and bought < units:
        price_a, price_b = buy_from[i].price, sell_to[j].price
        if price_b * keep <= price_a:
            break
        take = min(left_a, left_b, units - bought, int((isk - cost) // price_a))
        if take <= 0:
            break
        first_buy, first_sell = first_buy or price_a, first_sell or price_b
        last_buy, last_sell = price_a, price_b
        bought += take
        cost += take * price_a
        gross += take * price_b
        left_a, left_b = left_a - take, left_b - take
        if left_a == 0 and (i := i + 1) < len(buy_from):
            left_a = buy_from[i].volume
        if left_b == 0 and (j := j + 1) < len(sell_to):
            left_b = sell_to[j].volume
    return bought, cost, gross, first_buy, last_buy, first_sell, last_sell


def _instant(db, type_id, name, source, dest, m3, limits, tax, sources, dests) -> Run | None:
    keep = 1 - tax
    units, cost, gross, b1, b2, s1, s2 = walk(
        market_data.book(db, sources, type_id, is_buy=False),
        market_data.book(db, dests, type_id, is_buy=True),
        keep,
        limits.isk,
        int(limits.cargo_m3 // m3),
    )
    if units <= 0:
        return None
    return Run(type_id, name, source, dest, units, units * m3, cost, gross * keep, False,
               b1, b2, s1, s2)  # fmt: skip


def _listed(db, type_id, name, source, dest, m3, limits, keep, sources, listing, volume):
    cap = int(min(limits.cargo_m3 // m3, (volume or 0) * LISTING_SHARE))
    price = listing - 0.01
    resale = [Order(price, cap, True, dest.station_id, dest.system_id, "station")]
    units, cost, gross, b1, b2, _s1, _s2 = walk(
        market_data.book(db, sources, type_id, is_buy=False), resale, keep, limits.isk, cap
    )
    if units <= 0:
        return None
    return Run(type_id, name, source, dest, units, units * m3, cost, gross * keep, True,
               b1, b2, price, price, volume)  # fmt: skip


def runs(
    db: sqlite3.Connection,
    limits: Limits,
    trade_fees: Fees,
    listed: bool = False,
    volumes: dict[tuple[int, int], float] | None = None,
    pairs: list[tuple[Hub, Hub]] | None = None,
    limit: int = 25,
) -> list[Run]:
    """The best runs between the hubs (or the given pairs), most profit first.
    Listed runs need `volumes`: (region, type) -> daily volume at the destination."""
    pairs = pairs or [(a, b) for a in market.HUBS for b in market.HUBS if a != b]
    found = []
    for source, dest in pairs:
        sources = market_data.kept_locations(db, source)
        dests = market_data.kept_locations(db, dest)
        broker = trade_fees.broker.get(dest.station_id, 0.0) if listed else 0.0
        keep = 1 - trade_fees.sales_tax - broker
        for row in _candidates(db, sources, dests, keep, listed):
            m3 = _unit_m3(db, row["type_id"])
            if not m3:
                continue
            name = skills.type_name(db, row["type_id"])
            if listed:
                volume = (volumes or {}).get((dest.region_id, row["type_id"]))
                run = _listed(db, row["type_id"], name, source, dest, m3, limits, keep,
                              sources, row["sell_at"], volume)  # fmt: skip
            else:
                run = _instant(db, row["type_id"], name, source, dest, m3, limits,
                               trade_fees.sales_tax, sources, dests)  # fmt: skip
            if run is not None and run.profit >= limits.min_profit:
                found.append(run)
    return sorted(found, key=lambda r: -r.profit)[:limit]


def listing_candidates(db: sqlite3.Connection, trade_fees: Fees) -> dict[int, list[int]]:
    """Items worth checking the daily volume of for listed runs, by destination region."""
    found: dict[int, list[int]] = {}
    for source in market.HUBS:
        for dest in market.HUBS:
            if source == dest:
                continue
            keep = 1 - trade_fees.sales_tax - trade_fees.broker.get(dest.station_id, 0.0)
            rows = _candidates(db, market_data.kept_locations(db, source),
                               market_data.kept_locations(db, dest), keep, True)  # fmt: skip
            found.setdefault(dest.region_id, []).extend(r["type_id"] for r in rows[:20])
    return found


@dataclass(frozen=True)
class Trip:
    path_to_source: list[int]
    path: list[int]  # source to destination
    minutes: float

    @property
    def jumps(self) -> int:
        return len(self.path) - 1


def trip(db, origin: int | None, run: Run, minutes_a_jump: float, highsec_only: bool):
    """Getting to the source, then to the destination, with buying and selling."""
    to_source = (
        routes.jumps_between(db, origin, run.source.system_id, highsec_only) if origin else None
    ) or [run.source.system_id]
    path = routes.jumps_between(db, run.source.system_id, run.dest.system_id, highsec_only)
    if path is None:
        return None
    jumps = len(to_source) - 1 + len(path) - 1
    minutes = jumps * minutes_a_jump + 2 * MINUTES_TO_DOCK + MINUTES_TO_TRADE
    return Trip(to_source, path, minutes)


def days_to_sell(run: Run) -> float | None:
    """For a listed run, days until the units sell at LISTING_SHARE of daily volume."""
    if not run.listed or not run.daily_volume:
        return None
    return math.ceil(run.units / (run.daily_volume * LISTING_SHARE) * 10) / 10


@dataclass(frozen=True)
class Offer:
    """A run as the character would make it: the trip, its ISK an hour and its risk."""

    run: Run
    trip: Trip
    rating: risk.RiskRating  # the goods are at stake on the way
    reasons: tuple

    @property
    def isk_per_hour(self) -> float:
        return self.run.profit / (self.trip.minutes / 60) if self.trip.minutes else 0.0


def offers(
    db: sqlite3.Connection,
    character_id: int,
    limits: Limits,
    listed: bool = False,
    pairs: list[tuple[Hub, Hub]] | None = None,
    limit: int = 25,
) -> list[Offer]:
    """The best runs for this character, with the trip from where it is now."""
    origin = (snapshots.latest(db, character_id, "location") or {}).get("solar_system_id")
    per_jump = corrections.minutes_per_jump(db, character_id)[0]
    trade_fees = character_fees(db, character_id)
    worth = risk.net_worth(db, character_id)
    model = risk.model(db)
    volumes = None
    if listed:
        volumes = {
            (r["region_id"], r["type_id"]): r["daily_volume"]
            for r in db.execute("SELECT region_id, type_id, daily_volume FROM market.volumes")
        }
    found = []
    for run in runs(db, limits, trade_fees, listed, volumes, pairs, limit * 2):
        journey = trip(db, origin, run, per_jump, limits.highsec_only)
        if journey is None:
            continue
        assessed = risk.assess(db, [*journey.path_to_source, *journey.path])
        rating = risk.rate(assessed.band, run.buy_cost, worth, model=model)
        found.append(Offer(run, journey, rating, assessed.reasons))
    return found[:limit]


@dataclass(frozen=True)
class Wanted:
    """The Trade page's filters. A negative ISK or cargo means "what I have"."""

    source: str | None = None  # a hub's name, or any
    dest: str | None = None
    isk: float = -1.0
    cargo_m3: float = -1.0
    listed: bool = False
    highsec_only: bool = True
    min_profit: float = 0.0
    by_hour: bool = False  # sort by ISK an hour rather than profit


@dataclass(frozen=True)
class Board:
    offers: list[Offer]
    limits: Limits
    ship: object | None  # fleet.FleetShip whose cargo is the default
    wallet: float
    fees: Fees | None = None


def board(db: sqlite3.Connection, character_id: int, wanted: Wanted) -> Board:
    """The runs for this character and these filters: by default the wallet's ISK
    and the cargo of the active ship (or the roomiest ship it can fly)."""
    wallet = float(snapshots.latest(db, character_id, "wallet") or 0.0)
    try:
        ships = [s for s in fleet.owned_ships(db, character_id) if s.usable and s.cargo_m3]
    except skills.SkillsError:
        ships = []
    ship = next((s for s in ships if s.is_active), None) or max(
        ships, key=lambda s: s.cargo_m3, default=None
    )
    limits = Limits(
        isk=wanted.isk if wanted.isk >= 0 else wallet,
        cargo_m3=wanted.cargo_m3 if wanted.cargo_m3 >= 0 else (ship.cargo_m3 if ship else 0.0),
        highsec_only=wanted.highsec_only,
        min_profit=wanted.min_profit,
    )
    pairs = [
        (a, b)
        for a in market.HUBS
        for b in market.HUBS
        if a != b and wanted.source in (None, a.name) and wanted.dest in (None, b.name)
    ]
    try:
        found = offers(db, character_id, limits, wanted.listed, pairs)
        if wanted.by_hour:
            found.sort(key=lambda o: -o.isk_per_hour)
        trade_fees = character_fees(db, character_id)
    except skills.SkillsError:
        found, trade_fees = [], None
    return Board(found, limits, ship, wallet, trade_fees)
