"""The Wealth page (design 4.10): what you own and where, and where the ISK
comes from and goes, per character, per account and in total.

Read-only, over data the app already keeps: wallet, assets and market orders
for net worth; the wallet journal for income and spending. Items are valued
at CCP's average prices, as net worth is everywhere else (the owner's
choice, 1 Nov 2026): every item has one, though it isn't exactly what a
sale would fetch.

The journal is cleaned before it's stored (design 8): other players' IDs
are gone, so a payment is "between your characters" only when both sides
are the owner's own; anything else with a player is "other players".
"""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from eve_assistant import goals, snapshots, structures, worth

# --- net worth now -----------------------------------------------------------


@dataclass(frozen=True)
class Holding:
    """One character's worth now."""

    character_id: int
    name: str
    account: str | None
    wallet: float
    assets: float
    listed: float  # items on sell orders, at CCP's average prices
    escrow: float  # ISK held in buy orders

    @property
    def total(self) -> float:
        return self.wallet + self.assets + self.listed + self.escrow


def holdings(db: sqlite3.Connection, character_ids: Iterable[int] | None = None) -> list[Holding]:
    """Each character's worth from their latest wallet, assets and orders,
    by account, then name."""
    wanted = None if character_ids is None else set(character_ids)
    rows = db.execute(
        "SELECT c.character_id, c.name, a.label FROM characters c "
        "LEFT JOIN accounts a ON a.account_id = c.account_id "
        "ORDER BY a.label IS NULL, a.label, c.name"
    )
    prices: dict[int, float] = {}
    found = []
    for row in rows:
        character_id = row["character_id"]
        if wanted is not None and character_id not in wanted:
            continue
        orders = snapshots.latest(db, character_id, "orders") or []
        listed, escrow = worth.order_values(db, orders, prices)
        found.append(
            Holding(
                character_id=character_id,
                name=row["name"],
                account=row["label"],
                wallet=float(snapshots.latest(db, character_id, "wallet") or 0.0),
                assets=worth.asset_value(
                    db, snapshots.latest(db, character_id, "assets") or [], prices
                ),
                listed=listed,
                escrow=escrow,
            )
        )
    return found


def by_account(found: list[Holding]) -> list[tuple[str | None, list[Holding]]]:
    """Holdings grouped by account, in the order given (None: no account set)."""
    groups: dict[str | None, list[Holding]] = {}
    for holding in found:
        groups.setdefault(holding.account, []).append(holding)
    return list(groups.items())


@dataclass(frozen=True)
class Change:
    days: int
    isk: float | None  # None: the line doesn't reach back that far


def changes(points: list[worth.Point], spans: tuple[int, ...] = (7, 30)) -> list[Change]:
    """How net worth moved over the last few days, from its daily line."""
    if not points:
        return [Change(days, None) for days in spans]
    now = datetime.fromisoformat(points[-1].at)
    found = []
    for days in spans:
        cutoff = now - timedelta(days=days)
        before = [p for p in points if datetime.fromisoformat(p.at) <= cutoff]
        found.append(Change(days, points[-1].total - before[-1].total if before else None))
    return found


# --- income and spending -----------------------------------------------------

CATEGORIES = (
    ("market_sales", "Market sales"),
    ("market_purchases", "Market purchases"),
    ("taxes_fees", "Taxes and fees"),
    ("skills", "Skill books"),
    ("rewards", "Missions and rewards"),
    ("bounties", "Bounties"),
    ("insurance", "Insurance"),
    ("industry", "Industry and planets"),
    ("contracts", "Contracts"),
    ("players", "Other players"),
    ("own", "Between your characters"),
    ("other", "Other"),
)
FEES = {
    "transaction_tax", "brokers_fee", "reprocessing_tax", "contract_brokers_fee",
    "contract_sales_tax", "contract_deposit_sales_tax", "asset_safety_recovery_tax",
    "jump_clone_activation_fee", "jump_clone_installation_fee", "docking_fee",
    "office_rental_fee", "acceleration_gate_fee", "structure_gate_jump", "market_fine_paid",
    "security_processing_fee", "repair_bill", "clone_activation", "clone_transfer",
}  # fmt: skip
INDUSTRY = {"industry_job_tax", "manufacturing", "copying", "reaction", "reverse_engineering"}
REWARDS = {"redeemed_isk_token", "inheritance", "agents_preward", "lp_store"}
BETWEEN_PLAYERS = {"player_donation", "player_trading"}
PERIODS = {"week": 8, "month": 6}  # how many of each the table shows


def category(entry: dict, own: set[int]) -> str:
    """The Wealth page's group for a journal entry."""
    ref, amount = entry.get("ref_type", ""), entry.get("amount") or 0.0
    if ref == "market_transaction":
        return "market_sales" if amount > 0 else "market_purchases"
    if ref == "market_escrow":
        return "market_purchases"  # buy orders placed (refunded when cancelled)
    if ref in BETWEEN_PLAYERS:
        parties = {entry.get("first_party_id"), entry.get("second_party_id")}
        return "own" if parties <= own else "players"
    if ref == "skill_purchase":
        return "skills"
    if ref == "insurance":
        return "insurance"
    if ref.startswith("bounty") or ref == "ess_escrow_transfer":
        return "bounties"
    if ref in INDUSTRY or ref.startswith(("planetary_", "researching_")):
        return "industry"
    if ref in FEES or ref.endswith(("_tax", "_fee")):
        return "taxes_fees"
    if ref.startswith("contract_"):
        return "contracts"
    if ref in REWARDS or any(word in ref for word in ("reward", "payout", "mission")):
        return "rewards"
    return "other"


def journal(db: sqlite3.Connection, character_ids: Iterable[int], since: str = "") -> list[dict]:
    """Every journal entry kept for these characters since a date, each once:
    the snapshots overlap, as each holds the last 30 days or so."""
    seen: dict[tuple[int, int], dict] = {}
    for character_id in character_ids:
        rows = db.execute(
            "SELECT payload FROM snapshots WHERE character_id = ? AND kind = 'wallet_journal' "
            "AND taken_at >= ? ORDER BY snapshot_id",
            (character_id, since),
        )
        for row in rows:
            for entry in json.loads(row["payload"]) or []:
                if isinstance(entry, dict) and "id" in entry and entry.get("date", "") >= since:
                    seen[(character_id, entry["id"])] = entry
    return list(seen.values())


@dataclass(frozen=True)
class Period:
    start: date
    label: str


@dataclass(frozen=True)
class FlowRow:
    code: str
    label: str
    amounts: tuple[float, ...]  # one a period, newest first

    @property
    def total(self) -> float:
        return sum(self.amounts)


@dataclass(frozen=True)
class Flows:
    by: str  # "week" or "month"
    periods: tuple[Period, ...]  # newest first
    rows: tuple[FlowRow, ...]
    net: tuple[float, ...]
    first: str | None  # the oldest entry seen, so the page can say how far back it goes
    left_out_own: bool  # payments between the owner's characters cancel out, so aren't shown


def _period_start(day: date, by: str) -> date:
    return day.replace(day=1) if by == "month" else day - timedelta(days=day.weekday())


def _periods(today: date, by: str) -> list[Period]:
    found, start = [], _period_start(today, by)
    for _ in range(PERIODS[by]):
        label = start.strftime("%b %Y") if by == "month" else f"{start.day} {start:%b}"
        found.append(Period(start, label))
        start = _period_start(start - timedelta(days=1), by)
    return found


def flows(
    db: sqlite3.Connection,
    character_ids: Iterable[int],
    by: str = "week",
    today: date | None = None,
) -> Flows:
    """Income and spending by group and period (weeks from Monday, or months)."""
    by = "month" if by == "month" else "week"
    character_ids = list(character_ids)
    own = set(worth.all_characters(db))
    left_out_own = set(character_ids) >= own  # all of them: transfers cancel out
    periods = _periods(today or datetime.now(UTC).date(), by)
    entries = journal(db, character_ids, periods[-1].start.isoformat())
    first = min((e["date"] for e in entries), default=None)
    if first:  # no empty columns from before the journal starts
        opening = _period_start(date.fromisoformat(first[:10]), by)
        periods = [p for p in periods if p.start >= opening]
    index = {p.start: i for i, p in enumerate(periods)}
    sums: dict[str, list[float]] = defaultdict(lambda: [0.0] * len(periods))
    for entry in entries:
        group = category(entry, own)
        if group == "own" and left_out_own:
            continue
        start = _period_start(date.fromisoformat(entry["date"][:10]), by)
        if start in index:
            sums[group][index[start]] += entry.get("amount") or 0.0
    rows = tuple(
        FlowRow(code, label, tuple(sums[code]))
        for code, label in CATEGORIES
        if any(sums.get(code, ()))
    )
    net = tuple(sum(r.amounts[i] for r in rows) for i in range(len(periods)))
    return Flows(by, tuple(periods), rows, net, first, left_out_own)


# --- assets ------------------------------------------------------------------

GROUPS = {
    6: "Ships", 7: "Modules", 32: "Modules", 8: "Ammo and charges", 18: "Drones",
    87: "Drones", 25: "Ore and ice", 4: "Minerals and materials", 42: "Planetary",
    43: "Planetary", 9: "Blueprints", 20: "Implants", 16: "Skill books",
}  # fmt: skip
SELLABLE = {4, 25, 42, 43}  # raw materials: worth selling if left in a hangar
SELL_FROM = 100_000  # ISK: smaller heaps aren't worth the trip


@dataclass(frozen=True)
class Stack:
    """One item type at one place."""

    type_id: int
    name: str
    group: str
    quantity: int
    value: float


@dataclass(frozen=True)
class Place:
    location_id: int | None
    label: str
    system: str | None
    security: float | None
    player_named: bool  # a structure's name, written by its owner
    stacks: tuple[Stack, ...]

    @property
    def value(self) -> float:
        return sum(s.value for s in self.stacks)


@dataclass(frozen=True)
class Idle:
    """Raw materials left loose in a hangar: worth selling."""

    place: Place
    stack: Stack


@dataclass(frozen=True)
class Assets:
    places: tuple[Place, ...]  # most valuable first
    idle: tuple[Idle, ...]

    @property
    def value(self) -> float:
        return sum(p.value for p in self.places)


def _top(by_id: dict[int, dict], item: dict) -> tuple[int | None, str | None]:
    """Where an item is in the end: through ships and containers to a station,
    a structure or space."""
    location, kind = item.get("location_id"), item.get("location_type")
    seen = set()
    while kind == "item" and location in by_id and location not in seen:
        seen.add(location)
        parent = by_id[location]
        location, kind = parent.get("location_id"), parent.get("location_type")
    return location, kind


def _system(db: sqlite3.Connection, system_id) -> tuple[str | None, float | None]:
    row = db.execute(
        "SELECT name, security_status FROM sde_solar_systems WHERE system_id = ?", (system_id,)
    ).fetchone()
    return (row["name"], row["security_status"]) if row else (None, None)


def _place(db: sqlite3.Connection, location_id, kind) -> tuple[str, str | None, float | None, bool]:
    """(label, system, security, player-named) for a place."""
    if location_id is None:
        return "Somewhere EVE didn't say", None, None, False
    if kind == "solar_system":
        system, security = _system(db, location_id)
        return f"In space in {system or 'an unknown system'}", system, security, False
    station = db.execute(
        "SELECT st.system_id, c.name AS corp FROM sde_npc_stations st "
        "LEFT JOIN sde_npc_corporations c ON c.corporation_id = st.owner_id "
        "WHERE st.station_id = ?",
        (location_id,),
    ).fetchone()
    if station:
        system, security = _system(db, station["system_id"])
        owner = f"{station['corp']} station" if station["corp"] else "A station"
        return f"{owner} in {system or 'an unknown system'}", system, security, False
    if structures.is_structure_id(location_id):
        known = structures.get(db, location_id)
        system, security = _system(db, known.solar_system_id) if known else (None, None)
        if known and known.accessible and known.player_name:
            return known.player_name, system, security, True
        where = f" in {system}" if system else ""
        return f"A structure{where}", system, security, False
    return "Asset safety or elsewhere", None, None, False


def _types(db: sqlite3.Connection, type_ids: set[int]) -> dict[int, tuple[str, int | None]]:
    if not type_ids:
        return {}
    marks = ",".join("?" * len(type_ids))
    rows = db.execute(
        "SELECT t.type_id, t.name, g.category_id FROM sde_types t "  # noqa: S608  # placeholders only
        f"LEFT JOIN sde_groups g USING (group_id) WHERE t.type_id IN ({marks})",
        sorted(type_ids),
    )
    return {r["type_id"]: (r["name"], r["category_id"]) for r in rows}


def _items(db: sqlite3.Connection, character_ids: Iterable[int]) -> list[tuple]:
    """Every item the characters own, as (place key, loose in a hangar, item)."""
    found = []
    for character_id in character_ids:
        payload = snapshots.latest(db, character_id, "assets") or []
        by_id = {a["item_id"]: a for a in payload if isinstance(a, dict) and "item_id" in a}
        for item in by_id.values():
            location, kind = _top(by_id, item)
            loose = item.get("location_id") == location and item.get("location_flag") == "Hangar"
            found.append(((location, kind), loose and kind != "solar_system", item))
    return found


class _Catalogue:
    """Names, groups and CCP average prices of the items in view."""

    def __init__(self, db: sqlite3.Connection, type_ids: set[int]):
        self._db, self._types, self._prices = db, _types(db, type_ids), {}

    def price(self, type_id: int) -> float:
        if type_id not in self._prices:
            self._prices[type_id] = goals.price_of(self._db, type_id) or 0.0
        return self._prices[type_id]

    def category(self, type_id: int) -> int | None:
        return self._types.get(type_id, ("", None))[1]

    def stack(self, type_id: int, quantity: int, value: float) -> Stack:
        name = self._types.get(type_id, (f"type {type_id}", None))[0]
        return Stack(type_id, name, GROUPS.get(self.category(type_id), "Other"), quantity, value)


def assets(db: sqlite3.Connection, character_ids: Iterable[int]) -> Assets:
    """Everything the characters own, by place, and what's worth selling."""
    items = _items(db, character_ids)
    catalogue = _Catalogue(db, {item["type_id"] for *_, item in items})
    heaps: dict[tuple, dict[int, list]] = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    loose_heaps: dict[tuple, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for key, loose, item in items:
        type_id, quantity = item["type_id"], item.get("quantity", 1)
        heap = heaps[key][type_id]
        heap[0] += quantity
        if not item.get("is_blueprint_copy"):
            heap[1] += catalogue.price(type_id) * quantity
        if loose and catalogue.category(type_id) in SELLABLE:
            loose_heaps[key][type_id] += quantity
    places = {}
    for key, heap in heaps.items():
        stacks = [catalogue.stack(t, quantity, value) for t, (quantity, value) in heap.items()]
        stacks.sort(key=lambda s: (-s.value, s.name))
        places[key] = Place(key[0], *_place(db, *key), tuple(stacks))
    idle = [
        Idle(places[key], catalogue.stack(type_id, quantity, catalogue.price(type_id) * quantity))
        for key, heap in loose_heaps.items()
        for type_id, quantity in heap.items()
        if catalogue.price(type_id) * quantity >= SELL_FROM
    ]
    return Assets(
        places=tuple(sorted(places.values(), key=lambda p: (-p.value, p.label))),
        idle=tuple(sorted(idle, key=lambda i: -i.stack.value)),
    )
