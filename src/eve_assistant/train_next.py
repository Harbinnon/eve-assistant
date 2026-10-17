"""What to train next: each skill the mining estimate uses, one level up,
ranked by how much sooner it brings the character's ISK goal.

For every candidate the mining estimate is worked out again with that
skill one level higher (the ship as fitted now; Alpha limits respected),
giving the gain in m3 a minute, in ISK an hour (at the latest plan's price
for the ore), and in weeks off the ISK goal at the player's real play time
(as if all of it were mining). A skill not trained at all also has its skill
book to buy: the payback is the hours of mining that would cover it.

An Alpha past the skill-point limit can't train at all: its suggestions are
still worked out within Alpha's skill limits, with the time as Omega and a
flag saying Omega is needed to train them.
"""

import dataclasses
import sqlite3
from dataclasses import dataclass

from eve_assistant import goals, mining, omega, pace, skills
from eve_assistant.skills import TrainedSkill

# Every skill the mining estimate reads, once each.
CANDIDATES = tuple(
    dict.fromkeys(
        (
            *mining.MINING_SKILLS,
            *mining.PER_LEVEL_YIELD.values(),
            *mining.PER_LEVEL_DURATION.values(),
        )
    )
)


@dataclass(frozen=True)
class Suggestion:
    skill: str
    level: int
    training_minutes: float  # prerequisites included; as Omega when needs_omega
    m3_gain: float  # per minute
    isk_gain: float | None  # per hour, at the latest plan's price
    weeks_sooner: float | None  # off the ISK goal, at the player's play time
    book_price: float | None  # to buy, when the skill isn't trained at all
    needs_omega: bool = False  # Alpha can't train it (the skill-point limit)

    @property
    def payback_hours(self) -> float | None:
        """Hours of mining whose extra ISK covers the skill book."""
        if not self.book_price or not self.isk_gain:
            return None
        return self.book_price / self.isk_gain


def _isk_still_needed(db: sqlite3.Connection, character_id: int) -> tuple[str, float] | None:
    """The character's first ISK or Omega goal still short of its target."""
    for goal in goals.active_goals(db):
        if goal.kind not in ("isk", "omega") or goal.character_id not in (None, character_id):
            continue
        status = goals.evaluate(db, goal)
        if status.isk_needed:
            return goal.label, status.isk_needed
    return None


def suggestions(db: sqlite3.Connection, character_id: int, limit: int = 3) -> list[Suggestion]:
    try:
        character = skills.load_character_skills(db, character_id)
        ship_type_id, fitted, implants = mining.current_fit(db, character_id)
        now = mining.estimate(db, character, ship_type_id, fitted, implants).m3_per_minute
    except (skills.SkillsError, mining.MiningError):
        return []
    if now <= 0:
        return []
    isk_per_m3 = omega.isk_per_m3(db, character_id)
    hours_a_week = pace.weekly_play(db).minutes / 60
    goal = _isk_still_needed(db, character_id)
    caps = skills.alpha_caps(db, character.race_id)
    found = []
    for name in CANDIDATES:
        row = db.execute(
            "SELECT type_id FROM sde_types WHERE name = ? AND published = 1", (name,)
        ).fetchone()
        if row is None:
            continue
        skill_id = row["type_id"]
        trained = character.skills.get(skill_id, TrainedSkill(0, 0, 0))
        level = trained.active_level if character.is_alpha else trained.trained_level
        next_level = level + 1
        if next_level > 5 or (character.is_alpha and next_level > caps.get(skill_id, 0)):
            continue
        boosted = dataclasses.replace(
            character,
            skills={**character.skills, skill_id: TrainedSkill(next_level, next_level, 0)},
        )
        gain = mining.estimate(db, boosted, ship_type_id, fitted, implants).m3_per_minute - now
        if gain <= 0:
            continue
        plan = skills.plan_skill(db, character, skill_id, next_level)
        minutes = plan.minutes_as_alpha if character.is_alpha else plan.minutes_as_omega
        needs_omega = minutes is None
        if needs_omega:
            minutes = plan.minutes_as_omega
        isk_gain = gain * 60 * isk_per_m3 if isk_per_m3 else None
        weeks = None
        if goal and isk_per_m3 and hours_a_week > 0:
            rate = now * 60 * isk_per_m3
            weeks = goal[1] / (rate * hours_a_week) - goal[1] / ((rate + isk_gain) * hours_a_week)
        found.append(
            Suggestion(
                skill=name,
                level=next_level,
                training_minutes=minutes,
                m3_gain=gain,
                isk_gain=isk_gain,
                weeks_sooner=weeks,
                book_price=goals.price_of(db, skill_id) if trained.trained_level == 0 else None,
                needs_omega=needs_omega,
            )
        )
    found.sort(key=lambda s: (-(s.weeks_sooner or 0), -(s.isk_gain or 0), -s.m3_gain))
    return found[:limit]


def at_alpha_limits(db: sqlite3.Connection, character_id: int) -> bool:
    """True for an Alpha whose every mining skill is at Alpha's limit, with
    Omega allowing more: nothing is left to train for mining as Alpha."""
    try:
        character = skills.load_character_skills(db, character_id)
    except skills.SkillsError:
        return False
    if not character.is_alpha:
        return False
    caps = skills.alpha_caps(db, character.race_id)
    capped = False
    for name in CANDIDATES:
        row = db.execute(
            "SELECT type_id FROM sde_types WHERE name = ? AND published = 1", (name,)
        ).fetchone()
        if row is None:
            continue
        cap = min(caps.get(row["type_id"], 0), 5)
        level = character.skills.get(row["type_id"], TrainedSkill(0, 0, 0)).active_level
        if level < cap:
            return False
        capped = capped or cap < 5
    return capped


def goal_for(db: sqlite3.Connection, character_id: int) -> str | None:
    """The goal the 'weeks sooner' figures are for, by its label."""
    found = _isk_still_needed(db, character_id)
    return found[0] if found else None
