"""Shared ore ledgers: ore mined with friends, pooled and sold together, and
the profit split fairly (design 4.11).

It's the owner's own bookkeeping, kept simple. While a ledger is open, the
owner types who handed over what ore and how much, over as many mining
sessions as it takes. Roles (booster, hauler, escort...) are optional, and so
is a role's share of the profit. Once the ore is sold, the owner enters what
it sold for and the split follows: each role's share first, divided between
the people in it, then the rest by the value of each person's ore.

Ore is weighed at CCP's average prices. What matters is how ores compare (a
unit of one ore can be worth ten of another); the sale sets the total. If an
ore has no price yet, the split falls back to volume and says so.

Names are owner-written and kept for this alone: never looked up in EVE,
matched to wallet data or sent anywhere (design 8). A person's name can be
removed if they ask. Their entries stay, under "Removed person N", so every
ledger still adds up.
"""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from eve_assistant import goals, skills
from eve_assistant.skills import Reason, SkillsError
from eve_assistant.store import utc_now

ORE_CATEGORY = 25  # Asteroid: ore, moon ore and ice, compressed or not
MAX_NAME = 60
MAX_TITLE = 80
MAX_ROLE = 30
MAX_QUANTITY = 10**10
COMMON_ROLES = ("booster", "hauler", "escort", "scout")
REMOVED = "Removed person"
PEOPLE_TABLES = ("ore_ledger_entries", "ore_ledger_roles", "ore_ledger_payments")
CLOSED = "This ledger is closed: its ore was sold. Reopen it to change it."


class LedgerError(Exception):
    pass


@dataclass(frozen=True)
class Ledger:
    ledger_id: int
    name: str  # owner-written
    opened_at: str
    closed_at: str | None
    sold_for: float | None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


@dataclass(frozen=True)
class Entry:
    entry_id: int
    person: str  # owner-written
    type_id: int
    ore: str
    quantity: int
    m3: float
    value: float | None  # at CCP's average price; None while the ore has no price
    added_at: str


@dataclass(frozen=True)
class RolePool:
    role: str
    percent: float
    isk: float
    people: tuple[str, ...]


@dataclass(frozen=True)
class Share:
    person: str
    m3: float
    value: float  # their priced ore at CCP's average prices
    weight: float  # their part of what's left after role shares, 0 to 1
    from_ore: float
    from_roles: tuple[tuple[str, float], ...]  # (role, ISK)
    roles: tuple[str, ...]
    paid_at: str | None

    @property
    def total(self) -> float:
        return self.from_ore + sum(isk for _, isk in self.from_roles)


@dataclass(frozen=True)
class Split:
    total: float  # the ISK split: what the ore sold for, or the estimate before that
    estimated: bool  # no sale entered yet
    by: str  # "value" or "volume" (when an ore has no price)
    ore_pool: float  # what's left for the ore after role shares
    role_pools: tuple[RolePool, ...]
    shares: tuple[Share, ...]
    reasons: tuple[Reason, ...]


def _clean(text: str | None, limit: int, what: str) -> str:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise LedgerError(f"Enter {what}.")
    if len(cleaned) > limit:
        raise LedgerError(f"Keep {what} under {limit} characters.")
    return cleaned


def parse_quantity(text: str) -> int:
    """'12,000' or '12 000' -> 12000."""
    digits = (text or "").replace(",", "").replace(" ", "").replace("_", "")
    if not digits.isdigit() or not 0 < int(digits) <= MAX_QUANTITY:
        raise LedgerError("Enter how many units, like 12,000.")
    return int(digits)


def _ledger(row: sqlite3.Row) -> Ledger:
    return Ledger(row["ledger_id"], row["name"], row["opened_at"], row["closed_at"],
                  row["sold_for"])  # fmt: skip


def start(db: sqlite3.Connection, name: str) -> int:
    title = _clean(name, MAX_TITLE, "a name for the ledger")
    cursor = db.execute(
        "INSERT INTO ore_ledgers (name, opened_at) VALUES (?, ?)", (title, utc_now())
    )
    db.commit()
    return cursor.lastrowid


def all_ledgers(db: sqlite3.Connection) -> list[Ledger]:
    """Open ledgers first, then the newest."""
    rows = db.execute(
        "SELECT * FROM ore_ledgers ORDER BY closed_at IS NOT NULL, opened_at DESC, ledger_id DESC"
    )
    return [_ledger(r) for r in rows]


def get(db: sqlite3.Connection, ledger_id: int) -> Ledger:
    row = db.execute("SELECT * FROM ore_ledgers WHERE ledger_id = ?", (ledger_id,)).fetchone()
    if row is None:
        raise LedgerError(f"No ore ledger {ledger_id}.")
    return _ledger(row)


def _open(db: sqlite3.Connection, ledger_id: int) -> Ledger:
    ledger = get(db, ledger_id)
    if not ledger.is_open:
        raise LedgerError(CLOSED)
    return ledger


def people(db: sqlite3.Connection, ledger_id: int) -> list[str]:
    """Everyone in a ledger, with ore or a role, in the order they came in."""
    found: dict[str, None] = {}
    for table in ("ore_ledger_entries", "ore_ledger_roles"):
        for row in db.execute(
            f"SELECT person FROM {table} WHERE ledger_id = ? ORDER BY rowid",  # noqa: S608  # constant names
            (ledger_id,),
        ):
            found.setdefault(row["person"])
    return list(found)


def named(db: sqlite3.Connection) -> list[str]:
    """Everyone named in any ledger (removed names left out)."""
    names: set[str] = set()
    for table in PEOPLE_TABLES:
        names |= {r[0] for r in db.execute(f"SELECT DISTINCT person FROM {table}")}  # noqa: S608  # constant names
    return sorted((n for n in names if not n.startswith(REMOVED)), key=str.casefold)


def own_names(db: sqlite3.Connection) -> set[str]:
    """The owner's own characters: their share needs no paying out."""
    return {r[0] for r in db.execute("SELECT name FROM characters")}


def known_people(db: sqlite3.Connection) -> list[str]:
    """Names to suggest while typing: everyone in any ledger, and the owner's
    own characters."""
    return sorted(set(named(db)) | own_names(db), key=str.casefold)


def _person(db: sqlite3.Connection, ledger_id: int, name: str) -> str:
    """The name as already written, in this ledger, another one or as one of
    the owner's characters, so "bob" and "Bob" are one person."""
    cleaned = _clean(name, MAX_NAME, "who it was")
    for known in [*people(db, ledger_id), *known_people(db)]:
        if known.casefold() == cleaned.casefold():
            return known
    return cleaned


def _ore(db: sqlite3.Connection, name: str) -> int:
    try:
        type_id = skills.find_type(db, _clean(name, 100, "the ore"))
    except SkillsError as exc:
        raise LedgerError(str(exc)) from None
    row = db.execute(
        "SELECT g.category_id FROM sde_types t JOIN sde_groups g USING (group_id) "
        "WHERE t.type_id = ?",
        (type_id,),
    ).fetchone()
    if row is None or row["category_id"] != ORE_CATEGORY:
        raise LedgerError(f"{skills.type_name(db, type_id)} isn't ore.")
    return type_id


def add_entry(db: sqlite3.Connection, ledger_id: int, person: str, ore: str, quantity: int) -> int:
    _open(db, ledger_id)
    if not 0 < quantity <= MAX_QUANTITY:
        raise LedgerError("Enter how many units, like 12,000.")
    who, type_id = _person(db, ledger_id, person), _ore(db, ore)
    cursor = db.execute(
        "INSERT INTO ore_ledger_entries (ledger_id, person, type_id, quantity, added_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ledger_id, who, type_id, quantity, utc_now()),
    )
    db.commit()
    return cursor.lastrowid


def remove_entry(db: sqlite3.Connection, ledger_id: int, entry_id: int) -> None:
    _open(db, ledger_id)
    cursor = db.execute(
        "DELETE FROM ore_ledger_entries WHERE ledger_id = ? AND entry_id = ?", (ledger_id, entry_id)
    )
    if cursor.rowcount == 0:
        raise LedgerError("That entry isn't in this ledger.")
    db.commit()


def _role(text: str) -> str:
    return _clean(text, MAX_ROLE, "a role").lower()


def add_role(db: sqlite3.Connection, ledger_id: int, person: str, role: str) -> None:
    _open(db, ledger_id)
    db.execute(
        "INSERT OR IGNORE INTO ore_ledger_roles (ledger_id, person, role) VALUES (?, ?, ?)",
        (ledger_id, _person(db, ledger_id, person), _role(role)),
    )
    db.commit()


def remove_role(db: sqlite3.Connection, ledger_id: int, person: str, role: str) -> None:
    _open(db, ledger_id)
    db.execute(
        "DELETE FROM ore_ledger_roles WHERE ledger_id = ? AND person = ? AND role = ?",
        (ledger_id, _person(db, ledger_id, person), _role(role)),
    )
    db.commit()


def set_share(db: sqlite3.Connection, ledger_id: int, role: str, percent: float | None) -> None:
    """A role's share of the whole profit, in percent; None removes it."""
    _open(db, ledger_id)
    name = _role(role)
    if percent is None:
        db.execute("DELETE FROM ore_ledger_shares WHERE ledger_id = ? AND role = ?",
                   (ledger_id, name))  # fmt: skip
        db.commit()
        return
    if not 0 < percent <= 100:
        raise LedgerError("A role's share is a percent of the profit, above 0 and up to 100.")
    others = db.execute(
        "SELECT COALESCE(SUM(percent), 0) FROM ore_ledger_shares WHERE ledger_id = ? AND role != ?",
        (ledger_id, name),
    ).fetchone()[0]
    if others + percent > 100 + 1e-9:
        raise LedgerError(
            f"Role shares can't come to more than 100% of the profit: the other roles already "
            f"take {others:g}%."
        )
    db.execute(
        "INSERT INTO ore_ledger_shares (ledger_id, role, percent) VALUES (?, ?, ?) "
        "ON CONFLICT (ledger_id, role) DO UPDATE SET percent = excluded.percent",
        (ledger_id, name, percent),
    )
    db.commit()


def close(db: sqlite3.Connection, ledger_id: int, sold_for: float) -> None:
    """The pooled ore is sold: record what it brought in, and stop changes."""
    _open(db, ledger_id)
    if sold_for <= 0:
        raise LedgerError("Enter what the ore sold for, in ISK.")
    if not people(db, ledger_id):
        raise LedgerError("Add who mined what first.")
    db.execute(
        "UPDATE ore_ledgers SET closed_at = ?, sold_for = ? WHERE ledger_id = ?",
        (utc_now(), sold_for, ledger_id),
    )
    db.commit()


def reopen(db: sqlite3.Connection, ledger_id: int) -> None:
    """Back to open, to correct it; the sale is entered again afterwards."""
    get(db, ledger_id)
    db.execute(
        "UPDATE ore_ledgers SET closed_at = NULL, sold_for = NULL WHERE ledger_id = ?",
        (ledger_id,),
    )
    db.commit()


def set_paid(db: sqlite3.Connection, ledger_id: int, person: str, paid: bool) -> None:
    if get(db, ledger_id).is_open:
        raise LedgerError("Payments are ticked off once the ore is sold.")
    who = _person(db, ledger_id, person)
    if who not in people(db, ledger_id):
        raise LedgerError(f"{who} isn't in this ledger.")
    if paid:
        db.execute(
            "INSERT OR IGNORE INTO ore_ledger_payments (ledger_id, person, paid_at) "
            "VALUES (?, ?, ?)",
            (ledger_id, who, utc_now()),
        )
    else:
        db.execute("DELETE FROM ore_ledger_payments WHERE ledger_id = ? AND person = ?",
                   (ledger_id, who))  # fmt: skip
    db.commit()


def forget(db: sqlite3.Connection, person: str) -> str:
    """Remove a person's name from every ledger, if they ask. Their entries,
    roles and payments stay under "Removed person N", so the splits still add
    up. Returns the name used instead."""
    cleaned = _clean(person, MAX_NAME, "the name")
    names = set()
    for table in PEOPLE_TABLES:
        names |= {r[0] for r in db.execute(f"SELECT DISTINCT person FROM {table}")}  # noqa: S608  # constant names
    matches = [n for n in names if n.casefold() == cleaned.casefold()]
    if not matches or cleaned.startswith(REMOVED):
        raise LedgerError("No one by that name is in a ledger.")
    number = 1
    while f"{REMOVED} {number}" in names:
        number += 1
    alias = f"{REMOVED} {number}"
    for table in PEOPLE_TABLES:
        for name in matches:
            db.execute(f"UPDATE {table} SET person = ? WHERE person = ?", (alias, name))  # noqa: S608  # constant names
    db.commit()
    return alias


def entries(db: sqlite3.Connection, ledger_id: int) -> list[Entry]:
    rows = db.execute(
        "SELECT e.*, t.name AS ore, t.volume FROM ore_ledger_entries e "
        "LEFT JOIN sde_types t USING (type_id) WHERE e.ledger_id = ? ORDER BY e.entry_id",
        (ledger_id,),
    ).fetchall()
    prices: dict[int, float | None] = {}
    found = []
    for row in rows:
        type_id, quantity = row["type_id"], row["quantity"]
        if type_id not in prices:
            prices[type_id] = goals.price_of(db, type_id)
        price = prices[type_id]
        found.append(
            Entry(
                entry_id=row["entry_id"],
                person=row["person"],
                type_id=type_id,
                ore=row["ore"] or f"type {type_id}",
                quantity=quantity,
                m3=(row["volume"] or 0.0) * quantity,
                value=price * quantity if price else None,
                added_at=row["added_at"],
            )
        )
    return found


def roles(db: sqlite3.Connection, ledger_id: int) -> dict[str, list[str]]:
    """Each person's roles."""
    held: dict[str, list[str]] = defaultdict(list)
    for row in db.execute(
        "SELECT person, role FROM ore_ledger_roles WHERE ledger_id = ? ORDER BY rowid",
        (ledger_id,),
    ):
        held[row["person"]].append(row["role"])
    return dict(held)


def shares(db: sqlite3.Connection, ledger_id: int) -> dict[str, float]:
    """Each role's share of the profit, in percent."""
    rows = db.execute(
        "SELECT role, percent FROM ore_ledger_shares WHERE ledger_id = ? ORDER BY role",
        (ledger_id,),
    )
    return {r["role"]: r["percent"] for r in rows}


def role_table(db: sqlite3.Connection, ledger_id: int) -> list[tuple[str, float | None, list[str]]]:
    """Each role in use: its share of the profit (None without one) and who holds it."""
    held, percents, everyone = roles(db, ledger_id), shares(db, ledger_id), people(db, ledger_id)
    names = sorted({*percents, *(r for rs in held.values() for r in rs)})
    return [
        (role, percents.get(role), [p for p in everyone if role in held.get(p, ())])
        for role in names
    ]


def _payments(db: sqlite3.Connection, ledger_id: int) -> dict[str, str]:
    rows = db.execute(
        "SELECT person, paid_at FROM ore_ledger_payments WHERE ledger_id = ?", (ledger_id,)
    )
    return {r["person"]: r["paid_at"] for r in rows}


def _role_pools(everyone, held, percents, total, reasons) -> list[RolePool]:
    pools = []
    for role, percent in percents.items():
        members = tuple(p for p in everyone if role in held.get(p, ()))
        if members:
            pools.append(RolePool(role, percent, total * percent / 100, members))
        else:
            reasons.append(Reason("ledger_share_unheld", {"role": role, "percent": percent}))
    return pools


def split(db: sqlite3.Connection, ledger_id: int) -> Split:
    """Who gets how much: each role's share first, divided between the people
    in it, then the rest by the value of each person's ore."""
    ledger = get(db, ledger_id)
    items = entries(db, ledger_id)
    everyone, held = people(db, ledger_id), roles(db, ledger_id)
    reasons: list[Reason] = []
    unpriced = sorted({e.ore for e in items if e.value is None})
    by = "volume" if unpriced else "value"
    if unpriced:
        reasons.append(Reason("ledger_unpriced", {"ores": unpriced}))
    weights: dict[str, float] = defaultdict(float)
    m3: dict[str, float] = defaultdict(float)
    value: dict[str, float] = defaultdict(float)
    for e in items:
        m3[e.person] += e.m3
        value[e.person] += e.value or 0.0
        weights[e.person] += e.m3 if by == "volume" else e.value
    estimated = ledger.sold_for is None
    total = sum(value.values()) if estimated else ledger.sold_for
    pools = _role_pools(everyone, held, shares(db, ledger_id), total, reasons)
    ore_pool = total - sum(p.isk for p in pools)
    whole = sum(weights.values())
    if ore_pool > 0 and whole <= 0:
        reasons.append(Reason("ledger_no_ore", {"isk": ore_pool}))
    paid = _payments(db, ledger_id)
    result = []
    for person in everyone:
        weight = weights[person] / whole if whole > 0 else 0.0
        from_roles = tuple((p.role, p.isk / len(p.people)) for p in pools if person in p.people)
        result.append(
            Share(
                person,
                m3[person],
                value[person],
                weight,
                ore_pool * weight,
                from_roles,
                tuple(held.get(person, ())),
                paid.get(person),
            )
        )
    return Split(total, estimated, by, ore_pool, tuple(pools), tuple(result), tuple(reasons))


def to_pay(result: Split, own: set[str]) -> list[Share]:
    """Shares still to pay out: not the owner's own characters, not ticked paid."""
    mine = {n.casefold() for n in own}
    return [
        s for s in result.shares
        if s.total >= 1 and not s.paid_at and s.person.casefold() not in mine
    ]  # fmt: skip


def summary_text(ledger: Ledger, result: Split) -> str:
    """The split as plain text, to paste into Discord or a mail."""
    if result.estimated:
        lines = [f"{ledger.name}: about {result.total:,.0f} ISK (an estimate; not sold yet)"]
    else:
        lines = [f"{ledger.name}: sold for {result.total:,.0f} ISK"]
    for share in sorted(result.shares, key=lambda s: (-s.total, s.person.casefold())):
        parts = [f"ore {share.weight * 100:.1f}%"] if share.weight else []
        parts += [f"{role} share" for role, _ in share.from_roles]
        detail = f" ({', '.join(parts)})" if parts else ""
        lines.append(f"{share.person}: {share.total:,.0f} ISK{detail}")
    return "\n".join(lines)
