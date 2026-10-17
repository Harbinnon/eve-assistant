"""Live risk signals from ESI's public data (roadmap 4.1).

- **Kills per system in the last hour** (/universe/system_kills, updated by
  CCP hourly): ships and pods destroyed. Counts only, no one is named.
- **Active incursions** (/incursions): the systems Sansha's forces are in,
  where NPCs attack anyone, miners included.
- **Insurance prices** (/insurance/prices): what each hull pays out when
  lost. A hull that isn't insured still pays 40% of the Platinum payout
  (EVE University's insurance guide), so that much is always taken off a
  hull's loss; nothing is assumed about whether the player insures.

Each reading replaces the last. Kills and incursions older than STALE_AFTER
aren't used: the risk then falls back to route security alone, and says so.
"""

import sqlite3
from datetime import datetime, timedelta

from eve_assistant.esi.client import EsiClient
from eve_assistant.store import utc_now

SIGNALS_EVERY = timedelta(hours=1)  # ESI caches system kills for an hour
INSURANCE_EVERY = timedelta(days=1)
STALE_AFTER = timedelta(hours=3)
# Ships and pods lost in one system in the last hour from which it's a hotspot.
# Calibrated on a real hour (17 Oct 2026): 86% of highsec systems had none and
# 99% under 14, the top being gank spots and hubs (Sivala 63, Jita 44, Uedama
# 35); in lowsec 95% had under 5, the top being PvP hubs (Obe 65, Amamake 55).
HIGHSEC_HOTSPOT = 20  # a gank hotspot or a busy hub
LOWSEC_CAMP = 10  # likely camped
UNINSURED_SHARE = 0.4  # of the Platinum payout, for a hull with no insurance
SETTING = "signals_checked"
INSURANCE_SETTING = "insurance_checked"


def _due(db: sqlite3.Connection, key: str, every: timedelta, now: datetime) -> bool:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row is None or now - datetime.fromisoformat(row[0]) >= every


def _mark(db: sqlite3.Connection, key: str, now: datetime) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, now.isoformat(timespec="seconds")),
    )


def due(db: sqlite3.Connection, now: datetime) -> bool:
    return _due(db, SETTING, SIGNALS_EVERY, now)


def refresh(db: sqlite3.Connection, esi: EsiClient, now: datetime) -> None:
    """Read kills and incursions, and insurance prices once a day. Raises EsiError."""
    stamp = now.isoformat(timespec="seconds")
    kills = esi.get("/universe/system_kills").data
    incursions = esi.get("/incursions").data
    with db:
        db.execute("DELETE FROM system_kills")
        db.executemany(
            "INSERT INTO system_kills VALUES (?, ?, ?, ?, ?)",
            [
                (k["system_id"], k.get("ship_kills", 0), k.get("pod_kills", 0),
                 k.get("npc_kills", 0), stamp)
                for k in kills
                if isinstance(k, dict) and "system_id" in k
            ],
        )  # fmt: skip
        db.execute("DELETE FROM incursion_systems")
        db.executemany(
            "INSERT OR REPLACE INTO incursion_systems VALUES (?, ?, ?)",
            [
                (system_id, incursion.get("state", "unknown"), stamp)
                for incursion in incursions
                if isinstance(incursion, dict)
                for system_id in incursion.get("infested_solar_systems", [])
            ],
        )
        _mark(db, SETTING, now)
    if _due(db, INSURANCE_SETTING, INSURANCE_EVERY, now):
        prices = esi.get("/insurance/prices").data
        with db:
            db.execute("DELETE FROM insurance_prices")
            db.executemany(
                "INSERT OR REPLACE INTO insurance_prices VALUES (?, ?, ?, ?)",
                [
                    (hull["type_id"], level["name"], level["cost"], level["payout"])
                    for hull in prices
                    if isinstance(hull, dict)
                    for level in hull.get("levels", [])
                ],
            )
            _mark(db, INSURANCE_SETTING, now)


def fresh(db: sqlite3.Connection, now: str | None = None) -> bool:
    """Whether kills and incursions were read recently enough to use."""
    row = db.execute("SELECT value FROM settings WHERE key = ?", (SETTING,)).fetchone()
    if row is None:
        return False
    moment = datetime.fromisoformat(now or utc_now())
    return moment - datetime.fromisoformat(row[0]) < STALE_AFTER


def kills_in(db: sqlite3.Connection, system_ids) -> dict[int, int]:
    """Ships and pods destroyed in the last hour, for the given systems that had any."""
    found = {}
    for system_id in dict.fromkeys(system_ids):
        row = db.execute(
            "SELECT ship_kills + pod_kills FROM system_kills WHERE system_id = ?", (system_id,)
        ).fetchone()
        if row and row[0]:
            found[system_id] = row[0]
    return found


def hotspots(db: sqlite3.Connection, now: str | None = None) -> dict[int, tuple[int, bool]]:
    """Systems over their security's threshold right now, with (kills, is
    highsec); none when the signals are stale. Highsec gank hotspots and hubs
    from HIGHSEC_HOTSPOT, likely camps elsewhere from LOWSEC_CAMP."""
    if not fresh(db, now):
        return {}
    rows = db.execute(
        "SELECT k.system_id, k.ship_kills + k.pod_kills AS kills, "
        "ROUND(s.security_status, 1) >= 0.5 AS highsec FROM system_kills k "
        "JOIN sde_solar_systems s USING (system_id) "
        "WHERE k.ship_kills + k.pod_kills >= CASE WHEN ROUND(s.security_status, 1) >= 0.5 "
        "THEN ? ELSE ? END",
        (HIGHSEC_HOTSPOT, LOWSEC_CAMP),
    )
    return {r["system_id"]: (r["kills"], bool(r["highsec"])) for r in rows}


def incursions_in(db: sqlite3.Connection, system_ids) -> list[int]:
    return [
        system_id
        for system_id in dict.fromkeys(system_ids)
        if db.execute(
            "SELECT 1 FROM incursion_systems WHERE system_id = ?", (system_id,)
        ).fetchone()
    ]


def hull_payout(db: sqlite3.Connection, type_id: int | None) -> float:
    """What losing this hull pays without any insurance bought (0 if unknown)."""
    if type_id is None:
        return 0.0
    row = db.execute(
        "SELECT payout FROM insurance_prices WHERE type_id = ? AND level = 'Platinum'",
        (type_id,),
    ).fetchone()
    return row["payout"] * UNINSURED_SHARE if row else 0.0
