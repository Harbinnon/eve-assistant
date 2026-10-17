"""Net worth over time: wallet plus valued assets, from the snapshots.

Wallet and assets are snapshotted only when they change, so the worth on a
day is the day's last wallet plus its last asset list (or the latest before
it). One point a day, plus one at the latest check with EVE when nothing has
changed since an earlier day (the worth then is the same). Assets are valued at
today's CCP average prices for every point, as no price history is kept: the
line shows what changed in wallets and hangars, not price moves. Blueprint
copies count as nothing (their price is the original's).
"""

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from eve_assistant import goals
from eve_assistant.store import utc_now

DAYS = 90
KINDS = ("wallet", "assets")


@dataclass(frozen=True)
class Point:
    at: str  # the newest snapshot that day, ISO UTC
    wallet: float
    assets: float

    @property
    def total(self) -> float:
        return self.wallet + self.assets


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
    wallets: dict[int, float] = {}
    holdings: dict[int, float] = {}
    prices: dict[int, float] = {}
    by_day: dict[str, Point] = {}
    for taken_at, character_id, kind, payload in events:
        if kind == "wallet":
            wallets[character_id] = float(payload or 0.0)
        else:
            holdings[character_id] = asset_value(db, payload or [], prices)
        day = max(taken_at[:10], since)
        at = taken_at if day == taken_at[:10] else f"{since}T00:00:00+00:00"
        by_day[day] = Point(at, sum(wallets.values()), sum(holdings.values()))
    checked = _last_checked(db, character_ids)
    if by_day and checked and checked[:10] > max(by_day):
        last = by_day[max(by_day)]
        by_day[checked[:10]] = Point(checked, last.wallet, last.assets)
    return list(by_day.values())


def _last_checked(db: sqlite3.Connection, character_ids: Iterable[int]) -> str | None:
    """The latest successful check of wallet or assets, for any of the characters."""
    latest = None
    for character_id in character_ids:
        row = db.execute(
            "SELECT MAX(checked_at) FROM snapshot_checks WHERE character_id = ? "
            "AND kind IN ('wallet', 'assets') AND outcome != 'failed'",
            (character_id,),
        ).fetchone()
        if row[0] and (latest is None or row[0] > latest):
            latest = row[0]
    return latest


def all_characters(db: sqlite3.Connection) -> list[int]:
    return [r["character_id"] for r in db.execute("SELECT character_id FROM characters")]
