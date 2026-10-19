"""Trade runs in plans (roadmap 5.5): the best instant runs between the trade
hubs that fit the session, from the last market read (trade.py) when it's
under MAX_AGE old. A plan never waits for a market read: the dashboard reads
the markets in the background when they're old, for the next plan.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

from eve_assistant import (
    corrections,
    fleet,
    market,
    market_data,
    risk,
    signals,
    snapshots,
    trade,
)
from eve_assistant.activities.base import Context, PlanItem
from eve_assistant.skills import Reason

MAX_AGE = timedelta(hours=2)
OFFERED = 3


def _fresh(db: sqlite3.Connection, now: datetime) -> bool:
    for hub in market.HUBS:
        at = market_data.scanned_at(db, hub)
        if at is None or now - datetime.fromisoformat(at) > MAX_AGE:
            return False
    return True


def _hauler(db, character_id, ships):
    """The ship to carry the goods: the active one if it has cargo space, else the
    roomiest one in the same station. (ship, minutes to get into it)."""
    here = [s for s in ships if s.usable and s.cargo_m3]
    active = next((s for s in here if s.is_active), None)
    if active is not None:
        return active, 0.0
    boardable = []
    for ship in here:
        board = fleet.boarding(db, character_id, ship)
        if board is not None and board.jumps == 0:
            boardable.append((ship, board.minutes))
    return max(boardable, key=lambda found: found[0].cargo_m3, default=(None, 0.0))


def trade_candidates(db, character_id, ships, minutes, net_worth, highsec_only, notes):
    if not _fresh(db, datetime.now(UTC)):
        notes.append(Reason("trade_data_old"))
        return []
    ship, switch = _hauler(db, character_id, ships)
    if ship is None:
        return []
    wallet = float(snapshots.latest(db, character_id, "wallet") or 0.0)
    limits = trade.Limits(isk=wallet, cargo_m3=ship.cargo_m3, highsec_only=highsec_only)
    sale = corrections.correction(db, character_id, "sale_price")
    tax = trade.character_fees(db, character_id).sales_tax
    items = []
    for offer in trade.offers(db, character_id, limits, listed=False, limit=10):
        run, total = offer.run, offer.trip.minutes + switch
        if total > minutes:
            continue
        unit_price = run.sell_value / (1 - tax) / run.units  # before tax, for sale learning
        items.append(
            PlanItem(
                kind="trade",
                key=f"{run.type_id}:{run.source.station_id}:{run.dest.station_id}",
                title=f"Trade: buy {run.units:,} {run.name} in {run.source.name}, sell them in "
                f"{run.dest.name} ({offer.trip.jumps} jumps)"
                + (f" in your {ship.name}" if switch else ""),
                isk=run.sell_value * sale.factor - run.buy_cost,
                minutes=total,
                risk=risk.rate(
                    offer.rating.band,
                    run.buy_cost + ship.value,
                    net_worth,
                    signals.hull_payout(db, ship.type_id),
                ),
                reasons=(
                    Reason(
                        "trade_run",
                        {
                            "item": run.name,
                            "units": run.units,
                            "from": run.source.name,
                            "to": run.dest.name,
                            "buy_cost": run.buy_cost,
                            "sell_value": run.sell_value,
                            "first_buy": run.first_buy,
                            "last_buy": run.last_buy,
                            "first_sell": run.first_sell,
                            "last_sell": run.last_sell,
                        },
                    ),
                    sale.reason(),
                    *offer.reasons,
                ),
                inputs={
                    "type_id": run.type_id,
                    "quantity": run.units,
                    "source_station": run.source.station_id,
                    "station_id": run.dest.station_id,
                    "buy_cost": run.buy_cost,
                    "unit_prices": {str(run.type_id): unit_price},
                    "ship_item_id": ship.item_id,
                },
            )
        )
        if len(items) >= OFFERED:
            break
    return items


def candidates(ctx: Context) -> list[PlanItem]:
    return trade_candidates(
        ctx.db, ctx.character_id, ctx.ships, ctx.minutes, ctx.net_worth, ctx.highsec_only,
        ctx.notes,
    )  # fmt: skip


def learned(_db: sqlite3.Connection, _character_id: int) -> list:
    return []  # what sales really fetch is learned by selling (selling.learned)
