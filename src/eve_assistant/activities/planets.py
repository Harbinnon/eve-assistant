"""Planetary Interaction in plans (roadmap 6.3): restart a stopped extractor.

A short job in the planet's own system (without Remote Sensing, which needs
Omega, a planet can't be managed from anywhere else). Its ISK is what the
colony then makes over one more extraction program as long as the last one,
less what it makes anyway: the goods come later, like a build job's.
"""

import sqlite3
from datetime import UTC, datetime

from eve_assistant import corrections, planets, risk, routes, signals, snapshots
from eve_assistant.activities.base import Context, PlanItem, system_info
from eve_assistant.skills import Reason

MINUTES_TO_RESTART = 5.0  # open the planet, survey, restart each extractor
DEFAULT_PROGRAM_HOURS = 24.0  # when the last program's length isn't known


def _program_hours(stopped: list[planets.Extractor]) -> float:
    lengths = [e.program_hours for e in stopped if e.program_hours]
    return max(lengths) if lengths else DEFAULT_PROGRAM_HOURS


def _rating(db, character_id, colony, path, net_worth) -> tuple[risk.RiskRating, tuple]:
    """Docked in the planet's system: nothing at stake. Otherwise the trip there."""
    if len(path) == 1:
        here = system_info(db, colony.system_id)
        band = "none" if here and here["region"] == "Exordium" else "very_low"
        return risk.rate(band, 0.0, net_worth), ()
    assessed = risk.assess(db, path)
    ship = snapshots.latest(db, character_id, "ship") or {}
    rating = risk.rate(
        assessed.band,
        risk.ship_exposure(db, character_id),
        net_worth,
        signals.hull_payout(db, ship.get("ship_type_id")),
    )
    return rating, assessed.reasons


def _restart(db, character_id: int, colony, stopped, path, now, context) -> PlanItem:
    """The step restarting a colony's stopped extractors.
    context: (net worth, minutes a jump, the reason for that pace)."""
    net_worth, per_jump, jump_reason = context
    jumps = len(path) - 1
    gain = planets.a_day(db, colony, now, all_running=True) - planets.a_day(db, colony, now)
    hours = _program_hours(stopped)
    rating, risk_reasons = _rating(db, character_id, colony, path, net_worth)
    ago = [(now - e.expires).total_seconds() / 3600 for e in stopped if e.expires is not None]
    plural = "s" if len(stopped) > 1 else ""
    away = ""
    if jumps:
        away = f" ({jumps} jump{'s' if jumps != 1 else ''} away)"
    return PlanItem(
        kind="planet",
        key=str(colony.planet_id),
        title=f"Restart the extractor{plural} on {colony.name}{away}",
        isk=gain * hours / 24,
        minutes=MINUTES_TO_RESTART + jumps * per_jump + (2.0 if jumps else 0.0),
        risk=rating,
        reasons=(
            Reason(
                "extractor_stopped",
                {
                    "planet": colony.name,
                    "extractors": len(stopped),
                    "hours_ago": max(ago) if ago else None,
                },
            ),
            Reason(
                "planet_output",
                {
                    "a_day": gain,
                    "program_hours": hours,
                    "customs_tax": planets.CUSTOMS_TAX,
                    "waiting": planets.waiting(db, colony),
                },
            ),
            Reason("planet_in_system", {"system": colony.system_name}),
            *([jump_reason] if jumps else []),
            *risk_reasons,
        ),
        inputs={
            "planet_id": colony.planet_id,
            "system_id": colony.system_id,
            "jumps": jumps,
            "gain_per_day": gain,
            "program_hours": hours,
            "extractors": len(stopped),
        },
    )


def planet_candidates(
    db: sqlite3.Connection,
    character_id: int,
    minutes: float,
    net_worth: float,
    highsec_only: bool,
    now: datetime | None = None,
) -> list[PlanItem]:
    now = now or datetime.now(UTC)
    origin = (snapshots.latest(db, character_id, "location") or {}).get("solar_system_id")
    if origin is None:
        return []
    per_jump, jump_reason = corrections.minutes_per_jump(db, character_id)
    items = []
    for colony in planets.colonies(db, character_id):
        stopped = colony.stopped(now, within_minutes=minutes)
        path = routes.jumps_between(db, origin, colony.system_id, highsec_only) if stopped else None
        if path is not None:
            items.append(_restart(db, character_id, colony, stopped, path, now,
                                  (net_worth, per_jump, jump_reason)))  # fmt: skip
    return items


def candidates(ctx: Context) -> list[PlanItem]:
    return planet_candidates(ctx.db, ctx.character_id, ctx.minutes, ctx.net_worth, ctx.highsec_only)


def learned(_db: sqlite3.Connection, _character_id: int) -> list:
    return []  # what colonies make isn't learned yet
