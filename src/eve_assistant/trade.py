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
    store,
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
    volumes = _volumes(db) if listed else None
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
    mode: str = "instant"  # instant, listed or station
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
    station: list = field(default_factory=list)  # StationTrade, in station mode
    watches: list = field(default_factory=list)  # the character's orders


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
        trade_fees = character_fees(db, character_id)
    except skills.SkillsError:
        return Board([], limits, ship, wallet, None, [], watch_orders(db, character_id))
    found, station = [], []
    if wanted.mode == "station":
        hubs = [h for h in market.HUBS if wanted.source in (None, h.name)]
        station = station_trades(db, trade_fees, _volumes(db), hubs, isk=limits.isk)
    else:
        found = offers(db, character_id, limits, wanted.mode == "listed", pairs)
        if wanted.by_hour:
            found.sort(key=lambda o: -o.isk_per_hour)
    return Board(found, limits, ship, wallet, trade_fees, station, watch_orders(db, character_id))


def _volumes(db: sqlite3.Connection) -> dict[tuple[int, int], float]:
    store.ensure_market(db)
    return {
        (r["region_id"], r["type_id"]): r["daily_volume"]
        for r in db.execute("SELECT region_id, type_id, daily_volume FROM market.volumes")
    }


def station_volume_candidates(db: sqlite3.Connection, trade_fees: Fees) -> dict[int, list[int]]:
    """Items worth checking the daily volume of for station trading, by region."""
    found: dict[int, list[int]] = {}
    for hub in market.HUBS:
        broker = trade_fees.broker.get(hub.station_id, 0.03)
        rows = station_candidates(db, hub, 1 - trade_fees.sales_tax - broker, broker, limit=30)
        found.setdefault(hub.region_id, []).extend(r["type_id"] for r in rows)
    return found


# Station trading: buying with a buy order a step above the best one and selling
# with a sell order a step under the cheapest, at one hub, for the margin.
STATION_SHARE = 0.1  # of the daily volume one trader is expected to take
MIN_MARGIN = 0.05
TICK = 0.01
CROWDED = 0.01  # orders within 1% of the best price count as competing
MIN_ORDERS = 5  # on each side, for an item to count as trading at the hub
MIN_BUY_SHARE = 0.5  # the best buy, as a share of the best sell, at least


@dataclass(frozen=True)
class StationTrade:
    type_id: int
    name: str
    hub: Hub
    buy_at: float  # your buy order
    sell_at: float  # your sell order
    profit_per_unit: float  # after broker fees both ways and sales tax
    daily_volume: float
    competing_buys: int
    competing_sells: int
    broker: float
    units_a_day: int  # a tenth of the daily volume, or what the ISK to spend buys

    @property
    def margin(self) -> float:
        cost = self.buy_at * (1 + self.broker)
        return self.profit_per_unit / cost if cost else 0.0

    @property
    def isk_a_day(self) -> float:
        return self.units_a_day * self.profit_per_unit

    @property
    def capital(self) -> float:
        return self.units_a_day * self.buy_at * (1 + self.broker)


def _competing(db, locations: list[int], type_id: int, is_buy: bool, best: float) -> int:
    marks = ",".join("?" * len(locations))
    edge = best * (1 - CROWDED) if is_buy else best * (1 + CROWDED)
    return db.execute(
        f"SELECT COUNT(*) FROM market.orders WHERE location_id IN ({marks}) "  # noqa: S608  # placeholders only
        f"AND type_id = ? AND is_buy = ? AND price {'>=' if is_buy else '<='} ?",
        (*locations, type_id, int(is_buy), edge),
    ).fetchone()[0]


def station_candidates(db, hub: Hub, keep: float, broker: float, limit: int = 40) -> list:
    """(type_id, best buy, best sell) at the hub whose spread pays MIN_MARGIN after fees,
    among items that really trade there: MIN_ORDERS orders or more on each side and a
    best buy at least MIN_BUY_SHARE of the best sell (a 0.01 ISK bid against a billion
    ISK ask isn't a spread anyone can trade). Most orders on the thinner side first:
    those are the items that trade every day."""
    locations = list(market_data.kept_locations(db, hub))
    marks = ",".join("?" * len(locations))
    return db.execute(
        f"SELECT type_id, MAX(CASE WHEN is_buy = 1 THEN price END) AS best_buy, "  # noqa: S608  # placeholders only
        f"MIN(CASE WHEN is_buy = 0 THEN price END) AS best_sell, "
        f"SUM(is_buy) AS buys, SUM(1 - is_buy) AS sells FROM market.orders "
        f"WHERE location_id IN ({marks}) GROUP BY type_id "
        f"HAVING buys >= ? AND sells >= ? AND best_buy >= best_sell * ? "
        f"AND ((best_sell - ?) * ? - (best_buy + ?) * (1 + ?)) >= (best_buy + ?) * (1 + ?) * ? "
        f"ORDER BY MIN(buys, sells) DESC LIMIT ?",
        (*locations, MIN_ORDERS, MIN_ORDERS, MIN_BUY_SHARE, TICK, keep, TICK, broker, TICK,
         broker, MIN_MARGIN, limit),
    ).fetchall()  # fmt: skip


def station_trades(
    db: sqlite3.Connection,
    trade_fees: Fees,
    volumes: dict[tuple[int, int], float],
    hubs: list[Hub] | None = None,
    min_daily_units: int = 10,
    limit: int = 25,
    isk: float | None = None,
) -> list[StationTrade]:
    """The best margins at the hubs that trade enough a day, most ISK a day first,
    within the ISK to spend if given."""
    found = []
    for hub in hubs or market.HUBS:
        broker = trade_fees.broker.get(hub.station_id, 0.03)
        keep = 1 - trade_fees.sales_tax - broker
        locations = list(market_data.kept_locations(db, hub))
        for row in station_candidates(db, hub, keep, broker):
            volume = volumes.get((hub.region_id, row["type_id"]))
            if not volume or volume * STATION_SHARE < min_daily_units:
                continue
            buy_at, sell_at = row["best_buy"] + TICK, row["best_sell"] - TICK
            units = int(volume * STATION_SHARE)
            if isk is not None:
                units = min(units, int(isk // (buy_at * (1 + broker))))
            if units <= 0:
                continue
            found.append(
                StationTrade(
                    row["type_id"],
                    skills.type_name(db, row["type_id"]),
                    hub,
                    buy_at,
                    sell_at,
                    sell_at * keep - buy_at * (1 + broker),
                    volume,
                    _competing(db, locations, row["type_id"], True, row["best_buy"]),
                    _competing(db, locations, row["type_id"], False, row["best_sell"]),
                    broker,
                    units,
                )
            )
    return sorted(found, key=lambda t: -t.isk_a_day)[:limit]


@dataclass(frozen=True)
class Watch:
    """One of the character's market orders against the last market read."""

    order_id: int
    type_id: int
    name: str
    place: str
    is_buy: bool
    price: float
    volume_remain: int
    best_other: float | None  # the best competing price there, if read

    @property
    def outbid(self) -> bool:
        if self.best_other is None:
            return False
        return self.best_other > self.price if self.is_buy else self.best_other < self.price


def watch_orders(db: sqlite3.Connection, character_id: int) -> list[Watch]:
    """The character's open orders, each with the best other order at its place."""
    store.ensure_market(db)
    hubs = {h.station_id: h.name for h in market.HUBS}
    found = []
    for order in snapshots.latest(db, character_id, "orders") or []:
        if not isinstance(order, dict) or "order_id" not in order:
            continue
        is_buy = bool(order.get("is_buy_order"))
        row = db.execute(
            f"SELECT {'MAX' if is_buy else 'MIN'}(price) FROM market.orders "  # noqa: S608  # MIN or MAX only
            "WHERE location_id = ? AND type_id = ? AND is_buy = ? AND order_id != ?",
            (order["location_id"], order["type_id"], int(is_buy), order["order_id"]),
        ).fetchone()
        found.append(
            Watch(
                order["order_id"],
                order["type_id"],
                skills.type_name(db, order["type_id"]),
                hubs.get(order["location_id"], "elsewhere"),
                is_buy,
                float(order.get("price", 0.0)),
                int(order.get("volume_remain", 0)),
                row[0] if row else None,
            )
        )
    return found
