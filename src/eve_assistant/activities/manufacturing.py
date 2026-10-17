"""Manufacturing (roadmap 5.4): start a build job from a blueprint in the
station the character is docked in. A quick job at the keyboard (buy what's
missing, start the job); the ISK comes when the product sells.
"""

import sqlite3

from eve_assistant import (
    corrections,
    fees,
    industry,
    mining,
    risk,
    skills,
    snapshots,
)
from eve_assistant.activities import base
from eve_assistant.activities.base import Context, PlanItem
from eve_assistant.skills import Reason

MINUTES_TO_START = 5.0  # open the industry window, pick the blueprint, start
MINUTES_TO_BUY = 1.0  # each material that has to be bought first


def manufacturing_candidates(db, character_id, net_worth, notes) -> list[PlanItem]:
    location = snapshots.latest(db, character_id, "location") or {}
    station = location.get("station_id")
    if not station or not snapshots.latest(db, character_id, "blueprints"):
        return []
    try:
        character = skills.load_character_skills(db, character_id)
    except skills.SkillsError:
        return []
    if industry.cost_index(db, location.get("solar_system_id")) is None:
        notes.append(Reason("no_cost_index"))
        return []
    if industry.jobs_running(db, character_id) >= industry.job_slots(db, character):
        notes.append(Reason("job_slots_full", {"slots": industry.job_slots(db, character)}))
        return []
    tax = fees.sales_tax(mining.skill_level(db, character, "Accounting"))
    sale = corrections.correction(db, character_id, "sale_price")
    here = base.system_info(db, location.get("solar_system_id"))
    builds = industry.builds_here(db, character_id, station, location["solar_system_id"], tax)
    items = []
    for build in builds[:3]:
        hours = build.seconds / 3600
        items.append(
            PlanItem(
                kind="manufacturing",
                key=f"{build.blueprint_item_id}:{build.runs}",
                title=f"Build {build.units:,} {build.product_name} from your "
                f"{'copy' if build.is_copy else 'blueprint'} (ME {build.me}): "
                f"done in {skills.format_minutes(hours * 60)}",
                isk=build.value * sale.factor - build.cost,
                minutes=MINUTES_TO_START + MINUTES_TO_BUY * len(build.to_buy),
                risk=risk.rate(
                    "none" if here and here["region"] == "Exordium" else "very_low", 0.0, net_worth
                ),  # fmt: skip
                reasons=(
                    Reason(
                        "manufacture_costs",
                        {
                            "materials": build.materials_cost,
                            "job_fee": build.job_fee,
                            "value": build.value,
                            "profit": build.profit,
                            "hours": hours,
                            "to_buy": [m.name for m in build.to_buy],
                            "runs": build.runs,
                        },
                    ),
                    sale.reason(),
                ),
                inputs={
                    "blueprint_item_id": build.blueprint_item_id,
                    "product_type_id": build.product_type_id,
                    "runs": build.runs,
                    "units": build.units,
                    "job_fee": build.job_fee,
                    "materials_cost": build.materials_cost,
                    "materials": [[m.type_id, m.quantity, m.in_hangar] for m in build.materials],
                    "station_id": station,
                },
            )
        )
    return items


def candidates(ctx: Context) -> list[PlanItem]:
    return manufacturing_candidates(ctx.db, ctx.character_id, ctx.net_worth, ctx.notes)


def learned(db: sqlite3.Connection, character_id: int) -> list:
    return []  # sale prices are learned by selling (selling.learned)
