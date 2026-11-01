"""Net worth over time: wallet plus valued assets plus market orders, from
the snapshots.

Orders count both ways: items listed on sell orders are still the
character's (they leave the asset list while listed), and ISK held in buy
orders is still theirs (it leaves the wallet). Without them, listing ore for
sale looked like a drop in worth. Orders placed for a corporation don't count.

Wallet, assets and orders are snapshotted only when they change, so the
worth on a day is the day's last of each (or the latest before it). One
point a day, plus one at the latest check with EVE when nothing has changed
since an earlier day (the worth then is the same). Items are valued at
today's CCP average prices for every point, as no price history is kept:
the line shows what changed in wallets and hangars, not price moves.
Blueprint copies count as nothing (their price is the original's).
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from eve_assistant import goals
from eve_assistant.store import utc_now

DAYS = 90
KINDS = ("wallet", "assets", "orders")


@dataclass(frozen=True)
class Point:
    at: str  # the newest snapshot that day, ISO UTC
    wallet: float
    assets: float
    orders: float = 0.0  # items on sell orders (CCP average) plus ISK held in buy orders

    @property
    def total(self) -> float:
        return self.wallet + self.assets + self.orders


def asset_value(db: sqlite3.Connection, assets: list, prices: dict | None = None) -> float:
    """Assets at CCP's average prices; `prices` caches them across calls."""
    prices = {} if prices is None else prices
    total = 0.0
    for item in assets:
        if item.get("is_blueprint_copy"):
            continue
        type_id = item["type_id"]
        if type_id not in prices:
            prices[type_id] = goals.price_of(db, type_id) or 0.0
        total += prices[type_id] * item.get("quantity", 1)
    return total


def order_values(
    db: sqlite3.Connection, orders: list, prices: dict | None = None
) -> tuple[float, float]:
    """(items listed on sell orders at CCP's average prices, ISK held in buy
    orders), for the character's own orders; `prices` caches across calls."""
    prices = {} if prices is None else prices
    listed = escrow = 0.0
    for order in orders:
        if not isinstance(order, dict) or order.get("is_corporation"):
            continue
        if order.get("is_buy_order"):
            escrow += float(order.get("escrow") or 0.0)
            continue
        type_id = order.get("type_id")
        if type_id not in prices:
            prices[type_id] = goals.price_of(db, type_id) or 0.0
        listed += prices[type_id] * order.get("volume_remain", 0)
    return listed, escrow


def _value(db: sqlite3.Connection, kind: str, payload, prices: dict) -> float:
    """What one snapshot of a kind is worth."""
    if kind == "wallet":
        return float(payload or 0.0)
    if kind == "assets":
        return asset_value(db, payload or [], prices)
    return sum(order_values(db, payload or [], prices))


def _days_last(db: sqlite3.Connection, character_id: int, kind: str, since: str) -> list:
    """The last snapshot of each day from `since`, plus the last one before it."""
    rows = db.execute(
        "SELECT snapshot_id, taken_at FROM snapshots WHERE character_id = ? AND kind = ? "
        "ORDER BY taken_at, snapshot_id",
        (character_id, kind),
    ).fetchall()
    chosen: dict[str, int] = {}
    for row in rows:
        day = row["taken_at"][:10]
        chosen[since if day < since else day] = row["snapshot_id"]
    found = []
    for snapshot_id in chosen.values():
        row = db.execute(
            "SELECT taken_at, payload FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        found.append((row["taken_at"], character_id, kind, json.loads(row["payload"])))
    return found


def history(
    db: sqlite3.Connection, character_ids: Iterable[int], days: int = DAYS, now: str | None = None
) -> list[Point]:
    """Net worth a day, oldest first, for one character or several added up."""
    character_ids = list(character_ids)
    since = (datetime.fromisoformat(now or utc_now()) - timedelta(days=days)).date().isoformat()
    events = []
    for character_id in character_ids:
        for kind in KINDS:
            events += _days_last(db, character_id, kind, since)
    events.sort(key=lambda e: e[0])
    latest: dict[str, dict[int, float]] = {kind: {} for kind in KINDS}  # kind -> character -> ISK
    prices: dict[int, float] = {}
    by_day: dict[str, Point] = {}
    for taken_at, character_id, kind, payload in events:
        latest[kind][character_id] = _value(db, kind, payload, prices)
        day = max(taken_at[:10], since)
        at = taken_at if day == taken_at[:10] else f"{since}T00:00:00+00:00"
        by_day[day] = Point(at, *(sum(latest[kind].values()) for kind in KINDS))
    checked = _last_checked(db, character_ids)
    if by_day and checked and checked[:10] > max(by_day):
        last = by_day[max(by_day)]
        by_day[checked[:10]] = Point(checked, last.wallet, last.assets, last.orders)
    return list(by_day.values())


def _last_checked(db: sqlite3.Connection, character_ids: Iterable[int]) -> str | None:
    """The latest successful check of wallet, assets or orders, for any of the characters."""
    latest = None
    for character_id in character_ids:
        row = db.execute(
            "SELECT MAX(checked_at) FROM snapshot_checks WHERE character_id = ? "
            "AND kind IN ('wallet', 'assets', 'orders') AND outcome != 'failed'",
            (character_id,),
        ).fetchone()
        if row[0] and (latest is None or row[0] > latest):
            latest = row[0]
    return latest


def all_characters(db: sqlite3.Connection) -> list[int]:
    return [r["character_id"] for r in db.execute("SELECT character_id FROM characters")]
