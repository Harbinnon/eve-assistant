"""Military campaigns (roadmap 5.6, Cradle of War, June 2026): empire
campaigns of several months whose objectives pay ISK, loyalty points and
standing for things players already do.

What each objective is and pays comes from the static data; whether its
campaign is running comes from ESI's public /military-campaigns, read once
a day. Plans mention the running objectives that fit what the planner
models: mining, agent missions, hacking sites and manufacturing. Combat and
Faction Warfare objectives aren't planned here. The four campaigns of June
2026 all ended in September 2026, so until new ones start nothing is shown.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from eve_assistant.esi.client import EsiClient

EVERY = timedelta(days=1)
SETTING = "campaigns_checked"
RUNNING = "Active"
# Contribution methods, and the planner activity each fits.
FITS = {
    "MineOre": "mining",
    "CompleteAgentMission": "mission",
    "HackSomething": "exploration",
    "Manufacture": "manufacturing",
}


@dataclass(frozen=True)
class Objective:
    objective_id: str
    campaign: str
    title: str
    method: str
    activity: str
    isk: float
    lp: float
    standing_percent: float
    systems: tuple[int, ...]  # where it counts; empty = anywhere


def due(db: sqlite3.Connection, now: datetime) -> bool:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (SETTING,)).fetchone()
    return row is None or now - datetime.fromisoformat(row[0]) >= EVERY


def _store(db: sqlite3.Connection, kind: str, entry: dict, stamp: str) -> None:
    db.execute(
        "INSERT OR REPLACE INTO campaign_states VALUES (?, ?, ?, ?, ?, ?)",
        (entry["id"], kind, entry.get("state", "unknown"), entry.get("progress"),
         entry.get("finished"), stamp),
    )  # fmt: skip


def refresh(db: sqlite3.Connection, esi: EsiClient, now: datetime) -> None:
    """Every campaign's state, and the objectives of running ones. Raises EsiError."""
    stamp = now.isoformat(timespec="seconds")
    listing = esi.get("/military-campaigns").data
    running = []
    with db:
        db.execute("DELETE FROM campaign_states")
        for campaign in listing.get("campaigns", []) if isinstance(listing, dict) else []:
            _store(db, "campaign", campaign, stamp)
            if campaign.get("state") == RUNNING:
                running.append(campaign["id"])
    for campaign_id in running:
        objectives = esi.get(f"/military-campaigns/{campaign_id}/objectives").data
        with db:
            for objective in objectives.get("objectives", []):
                _store(db, "objective", objective, stamp)
    with db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SETTING, stamp),
        )


def _systems(parameters: list) -> tuple[int, ...]:
    for parameter in parameters:
        if parameter.get("key") != "location":
            continue
        for value in parameter.get("matcher", {}).get("values", []):
            if value.get("valueType") == "solarsystem":
                return tuple(int(v) for v in value.get("values", []))
    return ()


def open_objectives(db: sqlite3.Connection) -> list[Objective]:
    """Running objectives that fit an activity the planner models."""
    rows = db.execute(
        "SELECT o.*, c.title AS campaign FROM sde_campaign_objectives o "
        "JOIN sde_campaigns c USING (campaign_id) "
        "JOIN campaign_states cs ON cs.id = o.campaign_id AND cs.state = ? "
        "JOIN campaign_states os ON os.id = o.objective_id AND os.state = ?",
        (RUNNING, RUNNING),
    )
    return [
        Objective(
            r["objective_id"],
            r["campaign"],
            r["title"],
            r["method"],
            FITS[r["method"]],
            r["isk"],
            r["lp"],
            r["standing_percent"],
            _systems(json.loads(r["parameters"])),
        )
        for r in rows
        if r["method"] in FITS
    ]


def campaigns_known(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(*) FROM campaign_states WHERE kind = 'campaign'").fetchone()[0]
