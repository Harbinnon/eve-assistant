"""Courier contracts: ones the character's ships can carry in the time,
alone or as multi-session projects.
"""

import sqlite3

from eve_assistant import (
    corrections,
    couriers,
    fleet,
    pace,
    projects,
    risk,
    routes,
)
from eve_assistant.activities import projects as project_steps
from eve_assistant.activities.base import (
    Context,
    PlanItem,
)
from eve_assistant.skills import Reason


def courier_candidates(db, esi, character_id, ships, minutes, net_worth, highsec_only, notes):
    per_jump = corrections.minutes_per_jump(db, character_id)
    choices = []
    for ship in ships:
        if not ship.usable or not ship.cargo_m3:
            continue
        board = fleet.boarding(db, character_id, ship, highsec_only, per_jump[0])
        if board is None:
            continue
        choices.append(
            couriers.Ship(
                ship.type_id,
                ship.name,
                ship.cargo_m3,
                switch_minutes=board.minutes,
                value=ship.value,
                origin_system_id=ship.system_id if board.jumps else None,
                boarding_lowest_security=(
                    routes.lowest_security(db, board.path) if board.jumps else None
                ),
            )
        )
    try:
        found = couriers.find(
            db,
            esi,
            character_id,
            highsec_only=highsec_only,
            max_minutes=None,
            limit=10,
            ships=choices,
            per_jump=per_jump,
        )
    except couriers.CourierError as exc:
        notes.append(Reason("couriers_unavailable", {"why": str(exc)}))
        return []
    if not found.options and found.excluded:
        notes.append(Reason("no_couriers_fit", dict(found.excluded)))
    items = []
    weekly = pace.weekly_play(db).minutes
    finish = pace.finish_rate(db)
    for option in found.options:
        band = risk.band_for_security(option.lowest_security)
        in_ship = f" in your {option.ship.name}" if option.ship.switch_minutes else ""
        if option.minutes > minutes:
            if not projects.finishes_in_time(option.minutes, option.days_to_complete, weekly):
                continue
            items.append(
                project_steps.project_item(
                    finish=finish,
                    kind="courier",
                    key=str(option.contract_id),
                    title=f"Courier {option.start_system} -> {option.end_system} "
                    f"({option.pickup_jumps}+{option.delivery_jumps} jumps){in_ship}",
                    total_minutes=option.minutes,
                    isk=option.reward,
                    minutes=minutes,
                    rating=risk.rate(band, option.collateral + option.ship.value, net_worth),
                    reasons=(
                        Reason(
                            "deadline_fits_your_pace",
                            {"days": option.days_to_complete, "weekly_minutes": weekly},
                        ),  # fmt: skip
                        *option.reasons,
                    ),
                    inputs={
                        "contract_id": option.contract_id,
                        "collateral": option.collateral,
                        "player_title": option.player_title,
                    },  # fmt: skip
                    deadline_days=option.days_to_complete,
                )
            )
            continue
        items.append(
            PlanItem(
                kind="courier",
                key=str(option.contract_id),
                title=f"Courier {option.start_system} -> {option.end_system} "
                f"({option.pickup_jumps}+{option.delivery_jumps} jumps, "
                f"{option.volume_m3:,.0f} m3){in_ship}",
                isk=option.reward,
                minutes=option.minutes,
                risk=risk.rate(band, option.collateral + option.ship.value, net_worth),
                reasons=option.reasons,
                inputs={
                    "contract_id": option.contract_id,
                    "collateral": option.collateral,
                    "player_title": option.player_title,
                    "ship": option.ship.name,
                },
            )
        )
    return items


def candidates(ctx: Context) -> list[PlanItem]:
    return courier_candidates(
        ctx.db, ctx.esi, ctx.character_id, ctx.ships, ctx.minutes, ctx.net_worth,
        ctx.highsec_only, ctx.notes,
    )  # fmt: skip


def learned(db: sqlite3.Connection, character_id: int) -> list:
    """Travel time: every activity that travels uses it, hauling most."""
    return [corrections.correction(db, character_id, "minutes_per_jump")]
