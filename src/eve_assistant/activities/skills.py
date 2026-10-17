"""Skill reminders: queue skills when the queue is about to run dry."""

import sqlite3

from eve_assistant import (
    risk,
    skills,
    snapshots,
)
from eve_assistant.activities.base import (
    MINUTES_TO_QUEUE_SKILLS,
    Context,
    PlanItem,
)


def skill_candidates(db, character_id) -> list[PlanItem]:
    queue = snapshots.latest(db, character_id, "skillqueue")
    if queue is None or queue:
        return []
    try:
        character = skills.load_character_skills(db, character_id)
    except skills.SkillsError:
        return []
    if character.is_alpha and character.alpha_training_blocked:
        return []
    return [
        PlanItem(
            kind="skill",
            key="queue_empty",
            title="Your skill queue is empty: queue some training (see Goals and Skills)",
            isk=0.0,
            minutes=MINUTES_TO_QUEUE_SKILLS,
            risk=risk.rate("very_low", 0.0, 1.0),
        )
    ]


def candidates(ctx: Context) -> list[PlanItem]:
    return skill_candidates(ctx.db, ctx.character_id)


def learned(db: sqlite3.Connection, character_id: int) -> list:
    return []
