"""Planner activities, one module each, with one interface:

- candidates(ctx) -> list[PlanItem]: what it could do in this plan, each with
  its ISK, minutes, risk, structured reasons and inputs;
- learned(db, character_id) -> list[Correction]: what it has learned from
  this character's sessions (shown under "What it has learned").

The planner (planner.build_plan) only rates, ranks and fits candidates into
the time; it knows nothing about how any activity works. A new activity is a
new module here, added to ALL.
"""

from eve_assistant.activities import (
    couriers,
    gas,
    manufacturing,
    mining,
    missions,
    projects,
    selling,
    skills,
)

# In the order candidates are gathered (ties keep this order).
ALL = (skills, projects, selling, manufacturing, mining, gas, missions, couriers)


def learned(db, character_id: int) -> list:
    """Everything the activities have learned from this character's sessions."""
    return [c for activity in ALL for c in activity.learned(db, character_id)]
