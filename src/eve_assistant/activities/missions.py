"""Distribution missions (roadmap 5.3): carrying goods for an NPC agent, paid
in ISK and loyalty points (valued at the agent corporation's LP store).

Agents come from the static data: Distribution division, basic agents, not
locators, within MAX_JUMPS (highsec only unless lowsec is allowed). The
level is limited by standing with the agent, its corporation or its faction
(the best of the three): level 1 always, then 1, 3 and 5 for levels 2 to 4.

EVE doesn't publish what an ordinary mission pays (it depends on the agent
and the distance), so income starts from a guess: one published figure,
about 400k ISK and 750 LP a level 4 mission (EVE University, with
Negotiation V), a third as much a level below, and three missions an hour. It
learns from the mission rewards in the wallet journal (corrections.py).
Corp tax (corporation.py) is taken off the ISK, as EVE does before paying.
"""

import sqlite3

from eve_assistant import (
    corporation,
    corrections,
    loyalty,
    risk,
    routes,
    signals,
    snapshots,
)
from eve_assistant.activities.base import MIN_MINING_MINUTES, Context, PlanItem
from eve_assistant.esi.client import EsiError
from eve_assistant.skills import Reason

DISTRIBUTION = 22
MAX_JUMPS = 5
LEVEL_STANDING = {1: None, 2: 1.0, 3: 3.0, 4: 5.0}
L4_ISK, L4_LP = 400_000.0, 750.0  # a level 4 distribution mission (EVE University)
PER_LEVEL = 3.0  # each level below pays about a third
MISSIONS_AN_HOUR = 3.0


def per_hour(level: int) -> tuple[float, float]:
    """The starting guess: ISK and LP an hour at an agent of this level."""
    scale = PER_LEVEL ** (4 - level)
    return L4_ISK / scale * MISSIONS_AN_HOUR, L4_LP / scale * MISSIONS_AN_HOUR


def _standings(db: sqlite3.Connection, character_id: int) -> dict[int, float]:
    return {
        s["from_id"]: float(s["standing"])
        for s in snapshots.latest(db, character_id, "standings") or []
        if isinstance(s, dict) and "from_id" in s
    }


def best_level(standings: dict[int, float], agent_id: int, corp: int, faction: int | None) -> int:
    """The highest level this standing opens with the agent."""
    best = max(standings.get(i, 0.0) for i in (agent_id, corp, faction))
    return max(
        level for level, needed in LEVEL_STANDING.items() if needed is None or best >= needed
    )


def _nearby_agents(db, origin: int, highsec_only: bool) -> list:
    near = routes.within(db, origin, MAX_JUMPS, highsec_only)
    rows = db.execute(
        "SELECT a.*, st.system_id, c.faction_id FROM sde_agents a "
        "JOIN sde_npc_stations st ON st.station_id = a.station_id "
        "LEFT JOIN sde_npc_corporations c ON c.corporation_id = a.corporation_id "
        "WHERE a.division_id = ? AND a.agent_type_id = 2 AND a.is_locator = 0",
        (DISTRIBUTION,),
    ).fetchall()
    return sorted(
        ((row, near[row["system_id"]]) for row in rows if row["system_id"] in near),
        key=lambda found: found[1],
    )


def _open_agents(db, character_id, origin: int, highsec_only: bool) -> dict[int, tuple]:
    """The nearest agent of each level the character's standing opens: {level: (agent, jumps)}."""
    standings = _standings(db, character_id)
    chosen: dict[int, tuple] = {}
    for agent, jumps in _nearby_agents(db, origin, highsec_only):
        level = agent["level"]
        if level in chosen:
            continue
        opened = best_level(standings, agent["agent_id"], agent["corporation_id"],
                            agent["faction_id"])  # fmt: skip
        if level <= opened:
            chosen[level] = (agent, jumps)
    return chosen


def _distance(jumps: int) -> str:
    if not jumps:
        return "here"
    return f"{jumps} jump{'s' if jumps != 1 else ''} away"


def _lp_value(db, esi, corporation_id: int) -> float | None:
    try:
        return loyalty.store_value(db, esi, corporation_id)
    except EsiError:
        return None


def _mission_item(db, esi, character_id, agent, jumps, working, net_worth, context) -> PlanItem:
    """Missions for this agent for `working` minutes. context: (origin, highsec_only,
    learned correction, jump reason, the plan's minutes)."""
    origin, highsec_only, learned, jump_reason, minutes = context
    level = agent["level"]
    isk_per_lp = _lp_value(db, esi, agent["corporation_id"])
    isk_hour, lp_hour = per_hour(level)
    corp_tax = corporation.tax(db, character_id)
    kept = isk_hour * (1 - corp_tax.rate)  # the journal's rewards are after tax too
    lp = lp_hour * working / 60
    path = routes.jumps_between(db, origin, agent["system_id"], highsec_only) or [origin]
    assessed = risk.assess(db, path)
    ship = snapshots.latest(db, character_id, "ship") or {}
    corp = db.execute(
        "SELECT name FROM sde_npc_corporations WHERE corporation_id = ?",
        (agent["corporation_id"],),
    ).fetchone()
    return PlanItem(
        kind="mission",
        key=f"{agent['agent_id']}",
        title=f"Distribution missions for {agent['name']} (level {level}, {_distance(jumps)}) "
        f"for {working:.0f} min",
        isk=kept * learned.factor * working / 60 + lp * (isk_per_lp or 0.0),
        minutes=minutes,
        risk=risk.rate(
            assessed.band,
            risk.ship_exposure(db, character_id),
            net_worth,
            signals.hull_payout(db, ship.get("ship_type_id")),
        ),
        reasons=(
            Reason(
                "mission_guess", {"level": level, "isk_per_hour": isk_hour, "lp_per_hour": lp_hour}
            ),  # fmt: skip
            corp_tax.reason(),
            learned.reason(),
            Reason(
                "lp_value",
                {
                    "isk_per_lp": isk_per_lp,
                    "lp": lp,
                    "corporation": corp["name"] if corp else None,
                    "have": loyalty.balance(db, character_id, agent["corporation_id"]),
                },
            ),
            Reason("mission_cargo"),
            *([jump_reason] if jumps else []),
            *assessed.reasons,
        ),
        inputs={
            "agent_id": agent["agent_id"],
            "level": level,
            "mission_minutes": working,
            "isk_per_hour": kept,
            "corp_tax": corp_tax.rate,
            "lp_per_hour": lp_hour,
            "isk_per_lp": isk_per_lp,
            "corporation_id": agent["corporation_id"],
        },
    )


def mission_candidates(db, esi, character_id, minutes, net_worth, highsec_only) -> list[PlanItem]:
    """Missions at the nearest agents of the two highest levels open to the character."""
    location = snapshots.latest(db, character_id, "location") or {}
    origin = location.get("solar_system_id")
    if origin is None or db.execute("SELECT 1 FROM sde_agents LIMIT 1").fetchone() is None:
        return []
    learned = corrections.correction(db, character_id, "mission_income")
    per_jump, jump_reason = corrections.minutes_per_jump(db, character_id)
    context = (origin, highsec_only, learned, jump_reason, minutes)
    chosen = _open_agents(db, character_id, origin, highsec_only)
    items = []
    for level in sorted(chosen, reverse=True)[:2]:
        agent, jumps = chosen[level]
        working = minutes - jumps * per_jump - (2.0 if jumps else 0.0)
        if working >= MIN_MINING_MINUTES:
            items.append(
                _mission_item(db, esi, character_id, agent, jumps, working, net_worth, context)
            )
    return items


def candidates(ctx: Context) -> list[PlanItem]:
    return mission_candidates(
        ctx.db, ctx.esi, ctx.character_id, ctx.minutes, ctx.net_worth, ctx.highsec_only
    )


def learned(db: sqlite3.Connection, character_id: int) -> list:
    """Mission income, from the rewards in the wallet journal against the guess."""
    return [corrections.correction(db, character_id, "mission_income")]
