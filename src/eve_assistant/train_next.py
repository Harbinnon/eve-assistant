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


@dataclass(frozen=True)
class _Now:
    """What every candidate is measured against."""

    character: skills.CharacterSkills
    fit: tuple  # ship type, fitted items, implants
    m3_per_minute: float
    isk_per_m3: float | None
    hours_a_week: float
    isk_needed: float | None  # for the first ISK or Omega goal still short


def _skill_id(db: sqlite3.Connection, name: str) -> int | None:
    row = db.execute(
        "SELECT type_id FROM sde_types WHERE name = ? AND published = 1", (name,)
    ).fetchone()
    return row["type_id"] if row else None


def _next_level(character: skills.CharacterSkills, caps: dict, skill_id: int) -> int | None:
    """The next level this character could train, within Alpha's limits; None if none."""
    trained = character.skills.get(skill_id, TrainedSkill(0, 0, 0))
    level = (trained.active_level if character.is_alpha else trained.trained_level) + 1
    if level > 5 or (character.is_alpha and level > caps.get(skill_id, 0)):
        return None
    return level


def _weeks_sooner(now: _Now, isk_gain: float | None) -> float | None:
    """Weeks off the goal if all the usual play time were mining."""
    if not (now.isk_needed and now.isk_per_m3 and isk_gain and now.hours_a_week > 0):
        return None
    rate = now.m3_per_minute * 60 * now.isk_per_m3
    return now.isk_needed / (rate * now.hours_a_week) - now.isk_needed / (
        (rate + isk_gain) * now.hours_a_week
    )


def _suggest(db: sqlite3.Connection, now: _Now, name: str, skill_id: int, level: int):
    """The suggestion for one skill one level up, or None if it adds nothing."""
    character = now.character
    boosted = dataclasses.replace(
        character, skills={**character.skills, skill_id: TrainedSkill(level, level, 0)}
    )
    gain = mining.estimate(db, boosted, *now.fit).m3_per_minute - now.m3_per_minute
    if gain <= 0:
        return None
    plan = skills.plan_skill(db, character, skill_id, level)
    minutes = plan.minutes_as_alpha if character.is_alpha else plan.minutes_as_omega
    isk_gain = gain * 60 * now.isk_per_m3 if now.isk_per_m3 else None
    untrained = character.skills.get(skill_id, TrainedSkill(0, 0, 0)).trained_level == 0
    return Suggestion(
        skill=name,
        level=level,
        training_minutes=plan.minutes_as_omega if minutes is None else minutes,
        m3_gain=gain,
        isk_gain=isk_gain,
        weeks_sooner=_weeks_sooner(now, isk_gain),
        book_price=goals.price_of(db, skill_id) if untrained else None,
        needs_omega=minutes is None,
    )


def suggestions(db: sqlite3.Connection, character_id: int, limit: int = 3) -> list[Suggestion]:
    try:
        character = skills.load_character_skills(db, character_id)
        fit = mining.current_fit(db, character_id)
        m3_per_minute = mining.estimate(db, character, *fit).m3_per_minute
    except (skills.SkillsError, mining.MiningError):
        return []
    if m3_per_minute <= 0:
        return []
    goal = _isk_still_needed(db, character_id)
    now = _Now(
        character,
        fit,
        m3_per_minute,
        omega.isk_per_m3(db, character_id),
        pace.weekly_play(db).minutes / 60,
        goal[1] if goal else None,
    )
    caps = skills.alpha_caps(db, character.race_id)
    found = []
    for name in CANDIDATES:
        skill_id = _skill_id(db, name)
        level = _next_level(character, caps, skill_id) if skill_id else None
        suggestion = _suggest(db, now, name, skill_id, level) if level else None
        if suggestion:
            found.append(suggestion)
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
        skill_id = _skill_id(db, name)
        if skill_id is None:
            continue
        cap = min(caps.get(skill_id, 0), 5)
        level = character.skills.get(skill_id, TrainedSkill(0, 0, 0)).active_level
        if level < cap:
            return False
        capped = capped or cap < 5
    return capped


def goal_for(db: sqlite3.Connection, character_id: int) -> str | None:
    """The goal the 'weeks sooner' figures are for, by its label."""
    found = _isk_still_needed(db, character_id)
    return found[0] if found else None
