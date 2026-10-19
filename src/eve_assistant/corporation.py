"""The character's corporation (roadmap 6.1): corp tax, and the corp's buyback.

**Corp tax** is taken from agent mission rewards and bounties before they
reach the wallet. The rate a plan uses comes from, best first:

1. a player corporation's own rate, from ESI's public /corporations/{id};
2. the wallet journal, which says what was taken from each reward
   (entries paid to the character's current corporation only);
3. 11% for an NPC corporation: ESI gives no rate for them, and the journal of
   a character in one showed 11% taken from mission rewards (18 Oct 2026).

**The buyback** is a corporation's offer to buy its members' ore at a share
of Jita's best buy price, usually by contract at one of its stations. EVE
doesn't publish it, so the owner enters it in Settings: the share and the
system it's in. Plans then compare it with the market.

Corp contracts and corporation projects need two more scopes and a player
corporation, so they wait until a character joins one.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from eve_assistant import snapshots
from eve_assistant.esi.client import EsiClient
from eve_assistant.skills import Reason
from eve_assistant.store import utc_now

NPC_TAX = 0.11
TAXED_REFS = ("agent_mission_reward", "agent_mission_time_bonus_reward", "bounty_prizes")
REFRESH_EVERY = timedelta(days=1)
BUYBACK_SETTING = "buyback"


class BuybackError(Exception):
    pass


@dataclass(frozen=True)
class Corporation:
    corporation_id: int
    name: str | None
    is_npc: bool
    alliance_id: int | None


@dataclass(frozen=True)
class Tax:
    rate: float
    source: str  # corporation, journal, npc, unknown
    corporation: str | None = None

    def reason(self) -> Reason:
        return Reason(
            "corp_tax", {"rate": self.rate, "source": self.source, "corporation": self.corporation}
        )


@dataclass(frozen=True)
class Buyback:
    share: float  # of Jita's best buy price, e.g. 0.9
    system_id: int
    system_name: str


def membership(db: sqlite3.Connection, character_id: int) -> tuple[int | None, int | None]:
    """(corporation ID, alliance ID) from the character's public record."""
    public = snapshots.latest(db, character_id, "public") or {}
    return public.get("corporation_id"), public.get("alliance_id")


def is_npc(db: sqlite3.Connection, corporation_id: int | None) -> bool:
    return (
        corporation_id is not None
        and db.execute(
            "SELECT 1 FROM sde_npc_corporations WHERE corporation_id = ?", (corporation_id,)
        ).fetchone()
        is not None
    )


def of(db: sqlite3.Connection, character_id: int) -> Corporation | None:
    corporation_id, alliance_id = membership(db, character_id)
    if corporation_id is None:
        return None
    row = (
        db.execute(
            "SELECT name FROM corporations WHERE corporation_id = ?", (corporation_id,)
        ).fetchone()
        or db.execute(
            "SELECT name FROM sde_npc_corporations WHERE corporation_id = ?", (corporation_id,)
        ).fetchone()
    )
    return Corporation(
        corporation_id, row["name"] if row else None, is_npc(db, corporation_id), alliance_id
    )


def refresh(
    db: sqlite3.Connection, esi: EsiClient, character_id: int, now: datetime | None = None
) -> None:
    """Read the character's corporation from ESI (public), at most once a day.
    Raises EsiError."""
    corporation_id, _ = membership(db, character_id)
    if corporation_id is None:
        return
    moment = now or datetime.fromisoformat(utc_now())
    row = db.execute(
        "SELECT fetched_at FROM corporations WHERE corporation_id = ?", (corporation_id,)
    ).fetchone()
    if row and moment - datetime.fromisoformat(row["fetched_at"]) < REFRESH_EVERY:
        return
    data = esi.get(f"/corporations/{corporation_id}").data
    if not isinstance(data, dict):
        return
    db.execute(
        "INSERT OR REPLACE INTO corporations VALUES (?, ?, ?, ?, ?, ?)",
        (
            corporation_id,
            data.get("name") or str(corporation_id),
            data.get("ticker"),
            data.get("alliance_id"),
            data.get("tax_rate"),
            moment.isoformat(timespec="seconds"),
        ),
    )
    db.commit()


def journal_rate(db: sqlite3.Connection, character_id: int, corporation_id: int) -> float | None:
    """The share taken from rewards paid to this corporation, per the wallet journal."""
    taken = gross = 0.0
    for entry in snapshots.latest(db, character_id, "wallet_journal") or []:
        if (
            isinstance(entry, dict)
            and entry.get("ref_type") in TAXED_REFS
            and entry.get("tax_receiver_id") == corporation_id
            and entry.get("tax")
        ):
            taken += entry["tax"]
            gross += entry.get("amount", 0.0) + entry["tax"]
    return taken / gross if gross > 0 else None


def tax(db: sqlite3.Connection, character_id: int) -> Tax:
    """The corp tax taken from the character's mission rewards and bounties."""
    corp = of(db, character_id)
    if corp is None:
        return Tax(0.0, "unknown")
    if not corp.is_npc:
        row = db.execute(
            "SELECT tax_rate FROM corporations WHERE corporation_id = ?", (corp.corporation_id,)
        ).fetchone()
        if row and row["tax_rate"] is not None:
            return Tax(float(row["tax_rate"]), "corporation", corp.name)
    learned = journal_rate(db, character_id, corp.corporation_id)
    if learned is not None:
        return Tax(learned, "journal", corp.name)
    if corp.is_npc:
        return Tax(NPC_TAX, "npc", corp.name)
    return Tax(0.0, "unknown", corp.name)


def buyback(db: sqlite3.Connection) -> Buyback | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (BUYBACK_SETTING,)).fetchone()
    if row is None:
        return None
    saved = json.loads(row["value"])
    system = db.execute(
        "SELECT name FROM sde_solar_systems WHERE system_id = ?", (saved.get("system_id"),)
    ).fetchone()
    if system is None or not saved.get("share"):
        return None
    return Buyback(float(saved["share"]), int(saved["system_id"]), system["name"])


def set_buyback(db: sqlite3.Connection, percent: float, system_name: str) -> Buyback:
    """Save the corp's buyback: the percent of Jita's best buy it pays, and its system."""
    if not 1 <= percent <= 100:
        raise BuybackError("The buyback's share must be between 1% and 100% of Jita's buy price.")
    system = db.execute(
        "SELECT system_id, name FROM sde_solar_systems WHERE name = ? COLLATE NOCASE",
        (system_name.strip(),),
    ).fetchone()
    if system is None:
        raise BuybackError(f"No solar system is called '{system_name.strip()}'.")
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (BUYBACK_SETTING, json.dumps({"share": percent / 100, "system_id": system["system_id"]})),
    )
    db.commit()
    return Buyback(percent / 100, system["system_id"], system["name"])


def clear_buyback(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM settings WHERE key = ?", (BUYBACK_SETTING,))
    db.commit()
