"""What actually happened in a play session, set against its plan.

Worked out from data EVE already keeps, with no input from the player:

- **ISK**: wallet journal entries dated inside the session, grouped
  (market sales, purchases, taxes and fees, contracts, other).
- **Market trades**: wallet transactions inside the session, per item.
- **Ore mined**: the mining ledger counts ore per day, not per session, so
  the session's share is the ledger at the end minus the ledger at the start.
- **Travel**: jumps and minutes travelled, from the location checks
  (travel.py), so travel time can be learned.
- **Ore value**: the way plans value ore, so the two can be compared: what
  buyers pay, after sales tax, per unit. The price is the one this session's
  plan used, else what the ore sold for in the session, else the one the
  latest plan in the week before the session ended used. Ore none of these
  priced is left out.

Only amounts, dates, types and reference types are copied. Counterparties
(other players' IDs) and journal descriptions or reasons (which can be
player-written) are never carried into an outcome.

ESI caches these for a while (journal and transactions an hour, the mining
ledger ten minutes), so an outcome first counts as provisional. It is
recomputed after each refresh and becomes settled once data fetched after
the cache time has been seen. Outcomes are append-only: each recompute that
changes something adds a row, and a settled outcome is never recomputed.
"""

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eve_assistant import fees, gamelogs, sessions, travel
from eve_assistant.sessions import PlaySession
from eve_assistant.store import utc_now

# How long ESI caches each kind. Any response seen at time t was fetched no
# earlier than t minus this, so a check this long after the session ended
# is guaranteed to include the whole session.
JOURNAL_CACHE = timedelta(hours=1)
LEDGER_CACHE = timedelta(minutes=10)
SLACK = timedelta(minutes=1)
# EVE's clock and this PC's can differ a little.
END_GRACE = timedelta(minutes=2)
# The start snapshot is taken just after the start time.
START_WINDOW = timedelta(minutes=5)
# A ledger check older than this before the start can't be the start point.
START_STALE = timedelta(minutes=15)
# Mining after this long past the end isn't counted as the session's, and
# the ledger part settles with whatever was seen by then.
LEDGER_CAP = timedelta(hours=2)
# Plans older than this before the session ended don't price its ore.
PRICE_MAX_AGE = timedelta(days=7)

NOTES = {
    "provisional": "May still change: EVE holds wallet and mining data back for up to an "
    "hour, so this is final after {settles_at}.",
    "ledger_start_unknown": "Ore mined is unknown: the mining ledger wasn't read when the "
    "session started.",
    "ledger_end_late": "The mining ledger wasn't checked soon enough after the session, "
    "so ore mined may be incomplete.",
    "after_next_session": "The next session started soon after; mining was counted up "
    "to its start.",
    "ore_unpriced": "Left out of the ore's value because no plan from the week before "
    "priced it and it wasn't sold in the session: {names}.",
}


@dataclass(frozen=True)
class Outcome:
    session_id: int
    computed_at: str
    settled: bool
    details: dict

    @property
    def net_isk(self) -> float:
        return self.details["isk"]["net"]

    @property
    def mined_value(self) -> float | None:
        return self.details["mined_value"]

    @property
    def log_mining(self) -> dict | None:
        return self.details.get("log_mining")

    @property
    def planned_isk(self) -> float | None:
        plan = self.details["plan"]
        return plan["isk"] if plan else None

    @property
    def planned_minutes(self) -> float | None:
        plan = self.details["plan"]
        return plan["minutes"] if plan else None

    def notes(self) -> list[str]:
        shown = []
        for note in self.details["notes"]:
            fields = dict(note)
            if "settles_at" in fields:  # '2026-10-17T07:40:21+00:00' -> '07:40 UTC'
                fields["settles_at"] = f"{fields['settles_at'][11:16]} UTC"
            shown.append(NOTES[note["code"]].format(**fields))
        return shown


def _parse(stamp: str) -> datetime:
    value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _payload_at(db: sqlite3.Connection, character_id: int, kind: str, at: datetime):
    """The latest snapshot of a kind taken at or before `at`: (taken_at, payload)."""
    row = db.execute(
        "SELECT taken_at, payload FROM snapshots WHERE character_id = ? AND kind = ? "
        "AND taken_at <= ? ORDER BY taken_at DESC, snapshot_id DESC LIMIT 1",
        (character_id, kind, _stamp(at)),
    ).fetchone()
    return (row["taken_at"], json.loads(row["payload"])) if row else (None, None)


def _checked_between(
    db: sqlite3.Connection, character_id: int, kind: str, start: datetime, end: datetime | None
) -> str | None:
    """The latest successful check of a kind in [start, end], or None."""
    query = (
        "SELECT MAX(checked_at) FROM snapshot_checks WHERE character_id = ? AND kind = ? "
        "AND outcome != 'failed' AND checked_at >= ?"
    )
    params: list = [character_id, kind, _stamp(start)]
    if end is not None:
        query += " AND checked_at <= ?"
        params.append(_stamp(end))
    return db.execute(query, params).fetchone()[0]


def _in_window(entry: dict, start: datetime, end: datetime) -> bool:
    return start <= _parse(entry["date"]) <= end


def _journal_group(entry: dict) -> str:
    ref, amount = entry.get("ref_type", ""), entry.get("amount", 0.0)
    if ref == "market_transaction":
        return "market_sales" if amount > 0 else "market_purchases"
    if ref == "market_escrow":
        return "market_purchases"
    if ref in ("transaction_tax", "brokers_fee"):
        return "taxes_fees"
    if ref.startswith("contract_"):
        return "contracts"
    return "other"


def _types(db: sqlite3.Connection, type_ids) -> dict[int, sqlite3.Row]:
    ids = sorted(set(type_ids))
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = db.execute(
        f"SELECT type_id, name, volume FROM sde_types WHERE type_id IN ({marks})",  # noqa: S608  # placeholders only
        ids,
    )
    return {r["type_id"]: r for r in rows}


def _plan_prices(db: sqlite3.Connection, session: PlaySession, type_ids, end) -> dict[int, dict]:
    """ISK per unit for each ore, as plans priced it: a mining step's ISK (sold
    where the plan would sell it, after tax) over its quantity. This session's
    plan first, then the newest plan in the week before the session ended;
    chosen steps before alternatives."""
    wanted = set(type_ids)
    if not wanted:
        return {}
    rows = db.execute(
        "SELECT p.plan_id, p.created_at, i.isk, i.details FROM plan_items i "
        "JOIN plans p USING (plan_id) WHERE p.character_id = ? AND i.kind = 'mining' "
        "AND (p.plan_id IS ? OR p.created_at BETWEEN ? AND ?) "
        "ORDER BY p.plan_id IS ? DESC, p.created_at DESC, i.position IS NULL, i.item_id",
        (session.character_id, session.plan_id, _stamp(end - PRICE_MAX_AGE), _stamp(end),
         session.plan_id),
    )  # fmt: skip
    prices: dict[int, dict] = {}
    for row in rows:
        inputs = json.loads(row["details"]).get("inputs", {})
        type_id, quantity = inputs.get("type_id"), inputs.get("quantity")
        if type_id in wanted and type_id not in prices and quantity and row["isk"] > 0:
            prices[type_id] = {
                "unit_isk": row["isk"] / quantity,
                "from": "plan",
                "plan_id": row["plan_id"],
                "priced_at": row["created_at"],
            }
    return prices


def _sale_prices(sold: list[dict], tax_rate: float | None) -> dict[int, dict]:
    """ISK per unit, after sales tax, for ore sold in the session. Without
    the journal's tax the full rate is assumed, so value isn't overstated."""
    rate = tax_rate if tax_rate is not None else fees.sales_tax(0)
    return {
        s["type_id"]: {"unit_isk": s["isk"] / s["units"] * (1 - rate), "from": "sales"}
        for s in sold
        if s["units"] > 0
    }


def _ore_prices(db, session: PlaySession, type_ids, end, sales: dict[int, dict]) -> dict:
    """This session's plan first, then the session's own sales, then plans
    from the week before."""
    planned = _plan_prices(db, session, type_ids, end)
    prices = {}
    for type_id in type_ids:
        plan_price = planned.get(type_id)
        if plan_price and plan_price["plan_id"] == session.plan_id:
            prices[type_id] = plan_price
        elif type_id in sales:
            prices[type_id] = sales[type_id]
        elif plan_price:
            prices[type_id] = plan_price
    return prices


def _isk(db, character_id, start, end, now) -> tuple[dict, str | None, float | None]:
    """ISK by group, when the journal was read, and the sales tax rate paid
    in the session (None without sales)."""
    checked_at, journal = _payload_at(db, character_id, "wallet_journal", now)
    groups = dict.fromkeys(
        ("market_sales", "market_purchases", "taxes_fees", "contracts", "other"), 0.0
    )
    sales = tax = 0.0
    for entry in journal or []:
        if _in_window(entry, start, end):
            amount = float(entry.get("amount", 0.0))
            groups[_journal_group(entry)] += amount
            if entry.get("ref_type") == "market_transaction" and amount > 0:
                sales += amount
            elif entry.get("ref_type") == "transaction_tax":
                tax -= amount
    rate = tax / sales if sales > 0 else None
    return {"net": sum(groups.values()), **groups}, checked_at, rate


def _trades(db, character_id, start, end, now) -> tuple[list[dict], list[dict]]:
    _, transactions = _payload_at(db, character_id, "wallet_transactions", now)
    totals: dict[tuple[bool, int], list[float]] = defaultdict(lambda: [0, 0.0])
    for t in transactions or []:
        if _in_window(t, start, end):
            total = totals[(bool(t["is_buy"]), t["type_id"])]
            total[0] += t["quantity"]
            total[1] += t["quantity"] * t["unit_price"]
    names = _types(db, (type_id for _, type_id in totals))
    sold, bought = [], []
    for (is_buy, type_id), (units, isk) in sorted(totals.items()):
        row = names.get(type_id)
        line = {
            "type_id": type_id,
            "name": row["name"] if row else str(type_id),
            "units": units,
            "isk": isk,
        }
        (bought if is_buy else sold).append(line)
    return sold, bought


def _ledger_totals(payload) -> dict[tuple[str, int, int], int]:
    totals: dict[tuple[str, int, int], int] = defaultdict(int)
    for row in payload or []:
        totals[(row["date"], row["type_id"], row["solar_system_id"])] += row["quantity"]
    return totals


def _mined(
    db, session: PlaySession, start, end, next_start, now, sales: dict[int, dict]
) -> tuple[dict, list, bool]:
    """Ore mined in the session from the ledger difference, valued per unit
    (see _ore_prices). Returns (facts, notes, settled)."""
    cid = session.character_id
    notes: list[dict] = []
    facts: dict = {"mined": None, "mined_m3": None, "mined_value": None}
    if (
        _checked_between(db, cid, "mining_ledger", start - START_STALE, start + START_WINDOW)
        is None
    ):
        notes.append({"code": "ledger_start_unknown"})
        return {**facts, "ledger_start_at": None, "ledger_end_at": None}, notes, True
    start_at, start_payload = _payload_at(db, cid, "mining_ledger", start + START_WINDOW)

    cap = end + LEDGER_CAP
    if next_start is not None and next_start < cap:
        cap = next_start
        notes.append({"code": "after_next_session"})
    settle_from = end + LEDGER_CACHE + SLACK
    settled_check = _checked_between(db, cid, "mining_ledger", settle_from, cap)
    settled = settled_check is not None or now > cap
    if settled_check is None and now > cap:
        notes.append({"code": "ledger_end_late"})
    end_at, end_payload = _payload_at(db, cid, "mining_ledger", min(now, cap))

    before, after = _ledger_totals(start_payload), _ledger_totals(end_payload)
    by_type: dict[int, int] = defaultdict(int)
    for key, quantity in after.items():
        gained = quantity - before.get(key, 0)
        if gained > 0:  # old days drop out of the ledger after 30 days
            by_type[key[1]] += gained
    types = _types(db, by_type)
    prices = _ore_prices(db, session, by_type, end, sales)
    mined = []
    for type_id, units in sorted(by_type.items()):
        row = types.get(type_id)
        volume = row["volume"] if row and row["volume"] is not None else None
        price = prices.get(type_id)
        mined.append(
            {
                "type_id": type_id,
                "name": row["name"] if row else str(type_id),
                "units": units,
                "m3": units * volume if volume is not None else None,
                "value": units * price["unit_isk"] if price else None,
                "price": price,
            }
        )
    unpriced = [m["name"] for m in mined if m["price"] is None]
    if unpriced:
        notes.append({"code": "ore_unpriced", "names": ", ".join(unpriced)})
    priced = [m["value"] for m in mined if m["value"] is not None]
    facts = {
        "mined": mined,
        "mined_m3": sum(m["m3"] or 0 for m in mined),
        # Unknown rather than zero when ore was mined but none of it was priced.
        "mined_value": sum(priced) if priced or not mined else None,
        "ledger_start_at": start_at,
        "ledger_end_at": end_at,
    }
    return facts, notes, settled


def _plan(db: sqlite3.Connection, plan_id: int | None) -> dict | None:
    if plan_id is None:
        return None
    rows = db.execute(
        "SELECT i.item_id, i.kind, i.key, i.isk, i.minutes, "
        "(SELECT r.response FROM plan_responses r WHERE r.item_id = i.item_id "
        " ORDER BY r.response_id DESC LIMIT 1) AS response "
        "FROM plan_items i WHERE i.plan_id = ? AND i.position IS NOT NULL ORDER BY i.position",
        (plan_id,),
    ).fetchall()
    items = [dict(r) for r in rows]
    return {
        "plan_id": plan_id,
        "isk": sum(i["isk"] for i in items),
        "minutes": sum(i["minutes"] for i in items),
        "items": items,
    }


def _next_start(db: sqlite3.Connection, session: PlaySession) -> datetime | None:
    row = db.execute(
        "SELECT MIN(started_at) FROM play_sessions WHERE character_id = ? AND started_at > ?",
        (session.character_id, session.started_at),
    ).fetchone()
    return _parse(row[0]) if row[0] else None


def compute(
    db: sqlite3.Connection, session: PlaySession, now: str | None = None
) -> tuple[bool, dict]:
    """Work out a finished session's outcome. Returns (settled, details)."""
    if session.is_open or session.how != "finished":
        raise sessions.SessionError(f"Session {session.session_id} is not a finished session.")
    now_dt = _parse(now or utc_now())
    start, end = _parse(session.started_at), _parse(session.ended_at)
    window_end = end + END_GRACE

    isk, journal_at, tax_rate = _isk(db, session.character_id, start, window_end, now_dt)
    sold, bought = _trades(db, session.character_id, start, window_end, now_dt)
    journal_settles = end + JOURNAL_CACHE + SLACK
    journal_settled = (
        _checked_between(db, session.character_id, "wallet_journal", journal_settles, None)
        is not None
    )
    mining, notes, ledger_settled = _mined(
        db, session, start, end, _next_start(db, session), now_dt, _sale_prices(sold, tax_rate)
    )
    settled = journal_settled and ledger_settled
    if not settled:
        notes.insert(0, {"code": "provisional", "settles_at": _stamp(journal_settles)})
    details = {
        "window": {"start": _stamp(start), "end": _stamp(window_end)},
        "minutes": session.minutes(),
        "isk": isk,
        "sold": sold,
        "bought": bought,
        **mining,
        # Per-cycle mining from EVE's game logs, when they are read (local mode).
        "log_mining": gamelogs.session_mining(db, session.character_id, _stamp(start), _stamp(end)),
        "travel": travel.session_travel(db, session.character_id, _stamp(start), _stamp(end)),
        "plan": _plan(db, session.plan_id),
        "data": {
            "journal_snapshot_at": journal_at,
            "journal_settled": journal_settled,
            "ledger_settled": ledger_settled,
        },
        "notes": notes,
    }
    return settled, details


def _hash(details: dict) -> str:
    canonical = json.dumps(details, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def latest(db: sqlite3.Connection, session_id: int) -> Outcome | None:
    row = db.execute(
        "SELECT * FROM session_outcomes WHERE session_id = ? ORDER BY outcome_id DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return Outcome(
        session_id=row["session_id"],
        computed_at=row["computed_at"],
        settled=bool(row["settled"]),
        details=json.loads(row["details"]),
    )


def update(db: sqlite3.Connection, character_id: int | None = None, now: str | None = None) -> int:
    """Recompute outcomes for finished sessions that aren't settled yet.
    Returns how many outcome rows were written."""
    query = (
        "SELECT s.session_id FROM play_sessions s JOIN play_session_ends e USING (session_id) "
        "WHERE e.how = 'finished' AND NOT EXISTS (SELECT 1 FROM session_outcomes o "
        "WHERE o.session_id = s.session_id AND o.settled = 1)"
    )
    params: tuple = ()
    if character_id is not None:
        query += " AND s.character_id = ?"
        params = (character_id,)
    written = 0
    for row in db.execute(query, params).fetchall():
        session = sessions.get(db, row["session_id"])
        settled, details = compute(db, session, now)
        digest = _hash(details)
        previous = latest(db, session.session_id)
        if (
            previous is not None
            and previous.settled == settled
            and _hash(previous.details) == digest
        ):
            continue
        db.execute(
            "INSERT INTO session_outcomes "
            "(session_id, computed_at, settled, content_hash, details) VALUES (?, ?, ?, ?, ?)",
            (session.session_id, now or utc_now(), int(settled), digest, json.dumps(details)),
        )
        written += 1
    db.commit()
    return written


def recent(db: sqlite3.Connection, limit: int = 10) -> list[tuple[PlaySession, Outcome | None]]:
    """Recent finished sessions with their latest outcome (None if not worked out yet)."""
    found = [s for s in sessions.recent(db, limit * 2) if s.how == "finished"][:limit]
    return [(s, latest(db, s.session_id)) for s in found]
