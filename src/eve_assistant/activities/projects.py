"""Multi-session projects: starting one (a long haul, a courier run) and
continuing those already open.
"""

import sqlite3

from eve_assistant import (
    pace,
    projects,
    risk,
)
from eve_assistant.activities.base import (
    Context,
    PlanItem,
)
from eve_assistant.skills import Reason


def project_item(
    *,
    kind,
    key,
    title,
    total_minutes,
    isk,
    minutes,
    rating,
    reasons,
    inputs,
    deadline_days=None,
    finish: pace.FinishRate | None = None,
) -> PlanItem:
    """A plan item that either fits in this session or starts a multi-session project.
    A project's value is its share for this session, discounted for not finishing."""
    if total_minutes <= minutes:
        return PlanItem(
            kind="sell_hangar" if kind == "haul" else kind,
            key=key,
            title=title,
            isk=isk,
            minutes=total_minutes,
            risk=rating,
            reasons=reasons,
            inputs={**inputs, "jumps": 1, "travel_jumps": 1},
        )
    share = minutes / total_minutes
    finish = finish or pace.FinishRate(projects.NOT_FINISHING_CHANCE, 0, 0)
    return PlanItem(
        kind="project_start",
        key=f"{kind}:{key}",
        title=f"Start a project: {title}",
        isk=isk * share * (1 - finish.not_finishing),
        minutes=minutes,
        risk=rating,
        reasons=(
            Reason(
                "project_value",
                {
                    "total_minutes": total_minutes,
                    "total_isk": isk,
                    "share": share,
                    "not_finishing": finish.not_finishing,
                    "learned_from": finish.done + finish.dropped,
                },
            ),  # fmt: skip
            *reasons,
        ),
        inputs={
            **inputs,
            "project": {
                "kind": kind,
                "key": key,
                "title": title,
                "total_minutes": total_minutes,
                "isk": isk,
                "deadline_days": deadline_days,
                "risk": {"band": rating.band, "exposure": rating.exposure},
            },
        },
    )


def project_candidates(db, character_id, minutes, net_worth, notes) -> list[PlanItem]:
    """Open projects, to continue."""
    items = []
    not_finishing = pace.finish_rate(db).not_finishing
    for project in projects.open_projects(db, character_id):
        if project.minutes_left <= 0:
            notes.append(Reason("project_time_used", {"title": project.title}))
            continue
        session = min(minutes, project.minutes_left)
        stored = project.details.get("risk", {})
        rating = risk.rate(stored.get("band", "low"), stored.get("exposure", 0.0), net_worth)
        reasons = [
            Reason(
                "project_progress", {"done": project.minutes_done, "total": project.total_minutes}
            ),  # fmt: skip
        ]
        if project.deadline:
            reasons.append(Reason("project_deadline", {"deadline": project.deadline}))
        items.append(
            PlanItem(
                kind="project_continue",
                key=str(project.project_id),
                title=f"Continue project: {project.title}",
                isk=project.isk * (session / project.total_minutes) * (1 - not_finishing),
                minutes=session,
                risk=rating,
                reasons=tuple(reasons),
                inputs={"project_id": project.project_id},
            )
        )
    return items


def candidates(ctx: Context) -> list[PlanItem]:
    return project_candidates(ctx.db, ctx.character_id, ctx.minutes, ctx.net_worth, ctx.notes)


def learned(db: sqlite3.Connection, character_id: int) -> list:
    return []
