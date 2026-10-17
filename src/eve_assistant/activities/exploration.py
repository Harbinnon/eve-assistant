"""Exploration (roadmap 5.2): scanning down relic and data sites and hacking
their cans, in a ship with a probe launcher and an analyzer fitted.

Loot is random, so income starts from a labelled guess: about 1M ISK an
hour in highsec (players' reports of 500k-1.5M, EVE forums; very much luck).
Loot isn't sold in the session, so it's learned from the change in net
worth (wallet plus items at CCP averages) across exploration sessions
(corrections.py). Always offered as new to you until the player has done it.
"""

import sqlite3

from eve_assistant import (
    corrections,
    fleet,
    mining,
    risk,
    signals,
    skills,
    snapshots,
)
from eve_assistant.activities import base
from eve_assistant.activities.base import MIN_MINING_MINUTES, Context, PlanItem
from eve_assistant.skills import Reason

ISK_PER_HOUR = 1_000_000.0  # highsec relic and data sites (a guess, see above)
LAUNCHERS = "Scan Probe Launcher"
SKILLS = {"Archaeology": "relic", "Hacking": "data"}


def _group(db: sqlite3.Connection, type_id: int) -> str | None:
    row = db.execute(
        "SELECT g.name FROM sde_types t JOIN sde_groups g USING (group_id) WHERE t.type_id = ?",
        (type_id,),
    ).fetchone()
    return row["name"] if row else None


def site_kinds(db: sqlite3.Connection, character, fitted: list[int]) -> list[str]:
    """Which sites this fit can open ("relic", "data"), if it can probe at all."""
    usable = [t for t in dict.fromkeys(fitted) if skills.plan(db, character, t).usable_now]
    if not any(_group(db, t) == LAUNCHERS for t in usable):
        return []
    kinds = []
    for type_id in usable:
        attrs = mining.attributes(db, type_id)
        if "virusCoherence" not in attrs:
            continue
        needed = {
            skills.type_name(db, int(attrs[a])) for a in ("requiredSkill1", "requiredSkill2")
            if a in attrs
        }  # fmt: skip
        kinds += [kind for name, kind in SKILLS.items() if name in needed and kind not in kinds]
    return kinds


def exploration_candidates(db, character_id, ships, minutes, net_worth, notes) -> list[PlanItem]:
    try:
        character = skills.load_character_skills(db, character_id)
    except skills.SkillsError:
        return []
    learned = corrections.correction(db, character_id, "exploration_income")
    per_jump = corrections.minutes_per_jump(db, character_id)
    location = snapshots.latest(db, character_id, "location") or {}
    items = []
    for ship in ships:
        kinds = site_kinds(db, character, list(ship.fitted))
        if not kinds:
            continue
        board = fleet.boarding(db, character_id, ship, minutes_a_jump=per_jump[0])
        if board is None:
            continue
        working = minutes - board.minutes
        if working < MIN_MINING_MINUTES:
            continue
        system_id = location.get("solar_system_id") if board.jumps == 0 else ship.system_id
        here = base.system_info(db, system_id)
        assessed = risk.assess(
            db, [system_id, *(board.path if board.jumps else [])], here["region"] if here else None
        )
        sites = " and ".join(kinds)
        prefix = f"Board your {ship.name}, then scan" if board.needs_switch else "Scan"
        items.append(
            PlanItem(
                kind="exploration",
                key=f"{ship.item_id}:exploration",
                title=f"{prefix} for {sites} sites and hack them for {working:.0f} min",
                isk=ISK_PER_HOUR * learned.factor * working / 60,
                minutes=minutes,
                risk=risk.rate(
                    assessed.band, ship.value, net_worth, signals.hull_payout(db, ship.type_id)
                ),
                reasons=(
                    Reason("exploration_guess", {"isk_per_hour": ISK_PER_HOUR, "sites": kinds}),
                    learned.reason(),
                    *assessed.reasons,
                ),
                inputs={
                    "ship_item_id": ship.item_id,
                    "exploration_minutes": working,
                    "isk_per_hour": ISK_PER_HOUR,
                    "sites": kinds,
                },
            )
        )
    if not items and any(mining.skill_level(db, character, name) for name in SKILLS):
        notes.append(Reason("exploration_needs_fit"))
    return items


def candidates(ctx: Context) -> list[PlanItem]:
    return exploration_candidates(
        ctx.db, ctx.character_id, ctx.ships, ctx.minutes, ctx.net_worth, ctx.notes
    )


def learned(db: sqlite3.Connection, character_id: int) -> list:
    """Exploration income, from the change in net worth across its sessions."""
    return [corrections.correction(db, character_id, "exploration_income")]
