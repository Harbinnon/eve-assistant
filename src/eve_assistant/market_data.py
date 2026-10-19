"""The trade tool's market data (roadmap 5.5): every order at the trade hubs.

A scan reads a hub region's whole order book from ESI (The Forge, Jita's
region, is about 400 pages of 1,000 orders; all five hubs about 900 pages,
well within the market routes' 12,000 tokens per 15 minutes), keeps the
orders at the hub station and at player structures in the hub's system that
a character can use, and replaces that region's previous scan in the market
file (see store). Scans run only when the trade tool or a plan wants data
and the last scan is over SCAN_EVERY old; nothing is fetched otherwise. ESI
caches orders for 5 minutes, and regional orders never say who placed them.

Daily volumes come from ESI's market history (the last 30 days), fetched
for the items a trade is being considered for, once a day each.
"""

import sqlite3
from collections.abc import Iterable
from datetime import datetime, timedelta

from eve_assistant import market, store
from eve_assistant.esi.client import EsiClient, EsiError, EsiRateLimited
from eve_assistant.market import Hub, Order

SCAN_EVERY = timedelta(minutes=30)
WORKERS = 6  # pages fetched at a time
HISTORY_DAYS = 30
VOLUME_EVERY = timedelta(days=1)
MAX_HISTORY = 60  # market history requests per call; the rest wait


def kept_locations(db: sqlite3.Connection, hub: Hub) -> set[int]:
    """The hub station, and player structures in its system a character can use."""
    found = {hub.station_id}
    for row in db.execute(
        "SELECT structure_id FROM structures WHERE accessible = 1 AND solar_system_id = ?",
        (hub.system_id,),
    ):
        found.add(row[0])
    return found


def scan_hub(db: sqlite3.Connection, esi: EsiClient, hub: Hub, now: datetime) -> int:
    """Replace the hub region's orders with a fresh scan. Returns how many were kept.
    Raises EsiError (the previous scan stays)."""
    store.ensure_market(db)
    keep = kept_locations(db, hub)
    rows, pages = [], 0
    for page in esi.pages(
        f"/markets/{hub.region_id}/orders", {"order_type": "all"}, workers=WORKERS
    ):
        pages += 1
        rows.extend(
            (o["order_id"], hub.region_id, o["location_id"], o["type_id"],
             int(o["is_buy_order"]), o["price"], o["volume_remain"], o.get("min_volume", 1),
             o.get("issued", ""))
            for o in page
            if isinstance(o, dict) and o.get("location_id") in keep
        )  # fmt: skip
    with db:
        db.execute("DELETE FROM market.orders WHERE region_id = ?", (hub.region_id,))
        db.executemany(
            "INSERT OR REPLACE INTO market.orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        db.execute(
            "INSERT OR REPLACE INTO market.scans VALUES (?, ?, ?, ?)",
            (hub.region_id, now.isoformat(timespec="seconds"), len(rows), pages),
        )
    return len(rows)


def scanned_at(db: sqlite3.Connection, hub: Hub) -> str | None:
    store.ensure_market(db)
    row = db.execute(
        "SELECT scanned_at FROM market.scans WHERE region_id = ?", (hub.region_id,)
    ).fetchone()
    return row[0] if row else None


def stale(db: sqlite3.Connection, now: datetime, hubs: Iterable[Hub] = market.HUBS) -> list[Hub]:
    """Hubs whose last scan is missing or over SCAN_EVERY old."""
    found = []
    for hub in hubs:
        at = scanned_at(db, hub)
        if at is None or now - datetime.fromisoformat(at) >= SCAN_EVERY:
            found.append(hub)
    return found


def scan(
    db: sqlite3.Connection, esi: EsiClient, now: datetime, hubs: Iterable[Hub] = market.HUBS
) -> list[str]:
    """Scan the hubs that are due. Returns problem lines; stops at a rate limit."""
    problems = []
    for hub in stale(db, now, hubs):
        try:
            scan_hub(db, esi, hub, now)
        except EsiRateLimited as exc:
            problems.append(f"Market scan paused: {exc}")
            break
        except EsiError as exc:
            problems.append(f"Market scan of {hub.name}: {exc}")
    return problems


def book(db: sqlite3.Connection, locations: Iterable[int], type_id: int, is_buy: bool) -> list:
    """The orders for an item at these locations, best first (cheapest sell, dearest buy)."""
    store.ensure_market(db)
    places = list(locations)
    marks = ",".join("?" * len(places))
    rows = db.execute(
        f"SELECT o.*, COALESCE(st.system_id, s.solar_system_id) AS system_id "  # noqa: S608  # placeholders only
        f"FROM market.orders o LEFT JOIN sde_npc_stations st ON st.station_id = o.location_id "
        f"LEFT JOIN structures s ON s.structure_id = o.location_id "
        f"WHERE o.location_id IN ({marks}) AND o.type_id = ? AND o.is_buy = ? "
        f"ORDER BY o.price {'DESC' if is_buy else 'ASC'}",
        (*places, type_id, int(is_buy)),
    ).fetchall()
    return [
        Order(r["price"], r["volume_remain"], bool(r["is_buy"]), r["location_id"],
              r["system_id"] or 0, "station", r["order_id"], r["type_id"])
        for r in rows
    ]  # fmt: skip


def volumes(
    db: sqlite3.Connection, esi: EsiClient, region_id: int, type_ids: Iterable[int], now: datetime
) -> dict[int, float]:
    """Average units traded a day in the region over the last 30 days, per item;
    fetched again once a day, at most MAX_HISTORY items per call."""
    store.ensure_market(db)
    found: dict[int, float] = {}
    fetched = 0
    stamp = now.isoformat(timespec="seconds")
    for type_id in dict.fromkeys(type_ids):
        row = db.execute(
            "SELECT daily_volume, fetched_at FROM market.volumes "
            "WHERE region_id = ? AND type_id = ?",
            (region_id, type_id),
        ).fetchone()
        if row and now - datetime.fromisoformat(row["fetched_at"]) < VOLUME_EVERY:
            found[type_id] = row["daily_volume"]
            continue
        if fetched >= MAX_HISTORY:
            if row:
                found[type_id] = row["daily_volume"]
            continue
        fetched += 1
        try:
            history = esi.get(
                f"/markets/{region_id}/history", params={"type_id": type_id}, remember=False
            ).data
        except EsiError:
            continue
        recent = [h for h in history if isinstance(h, dict)][-HISTORY_DAYS:]
        daily = sum(h.get("volume", 0) for h in recent) / HISTORY_DAYS
        average = sum(h.get("average", 0.0) for h in recent) / len(recent) if recent else None
        with db:
            db.execute(
                "INSERT OR REPLACE INTO market.volumes VALUES (?, ?, ?, ?, ?, ?)",
                (region_id, type_id, daily, average, len(recent), stamp),
            )
        found[type_id] = daily
    return found
