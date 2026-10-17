"""Loyalty points (roadmap 5.3): what an LP is worth at a corporation's store,
and how many the character has.

An offer turns LP plus ISK (and sometimes items) into an item. Its value per
LP is (the item's price x quantity - the ISK - the required items' prices) /
the LP, at CCP's average prices. A store's value is the median of its ten
best offers: the very best is often an item that rarely trades, whose
average price can't be trusted. The balance comes from the snapshot kind
"loyalty" (scope esi-characters.read_loyalty.v1).
"""

import json
import sqlite3
from datetime import datetime, timedelta
from statistics import median

from eve_assistant import goals, snapshots
from eve_assistant.esi.client import EsiClient
from eve_assistant.store import utc_now

EVERY = timedelta(days=1)
TOP_OFFERS = 10


def _offer_value(db: sqlite3.Connection, offer: dict) -> float | None:
    price = goals.price_of(db, offer.get("type_id"))
    lp = offer.get("lp_cost") or 0
    if not price or lp <= 0:
        return None
    cost = offer.get("isk_cost", 0.0)
    for item in offer.get("required_items", []):
        item_price = goals.price_of(db, item.get("type_id"))
        if item_price is None:
            return None
        cost += item_price * item.get("quantity", 1)
    return (price * offer.get("quantity", 1) - cost) / lp


def store_value(
    db: sqlite3.Connection, esi: EsiClient, corporation_id: int, now: str | None = None
) -> float | None:
    """ISK per LP at this corporation's store, read again once a day. Raises EsiError."""
    stamp = now or utc_now()
    row = db.execute(
        "SELECT isk_per_lp, fetched_at FROM lp_store_values WHERE corporation_id = ?",
        (corporation_id,),
    ).fetchone()
    if row and datetime.fromisoformat(stamp) - datetime.fromisoformat(row["fetched_at"]) < EVERY:
        return row["isk_per_lp"]
    offers = esi.get(f"/loyalty/stores/{corporation_id}/offers").data
    valued = []
    for offer in offers:
        value = _offer_value(db, offer)
        if value is not None:
            valued.append((value, offer))
    valued.sort(key=lambda v: -v[0])
    top = valued[:TOP_OFFERS]
    value = median(v for v, _ in top) if top else None
    example = top[len(top) // 2][1] if top else None
    with db:
        db.execute(
            "INSERT OR REPLACE INTO lp_store_values VALUES (?, ?, ?, ?, ?)",
            (corporation_id, value, len(valued), json.dumps(example), stamp),
        )
    return value


def balance(db: sqlite3.Connection, character_id: int, corporation_id: int) -> int:
    for entry in snapshots.latest(db, character_id, "loyalty") or []:
        if isinstance(entry, dict) and entry.get("corporation_id") == corporation_id:
            return int(entry.get("loyalty_points", 0))
    return 0
