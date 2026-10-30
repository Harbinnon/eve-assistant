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

Refining skills (roadmap 8.3) are suggested separately, valued at the ore the
character actually mines: see refining_suggestions.
"""

import dataclasses
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from eve_assistant import goals, mining, omega, ore, pace, sde, skills, snapshots
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


# --- refining skills (roadmap 8.3) -------------------------------------------
#
# Refining skills don't make mining faster: they make ore worth more. So they're
# valued at the ore the character actually mines (the last REFINE_DAYS of its
# mining ledger, which is all ESI keeps), as the ISK a week one more level adds.
# Each ore is worth the better of selling it raw or refining it, both at CCP's
# average prices, so a level only counts where refining pays (or starts to pay).
# Refining happens at the station the character is docked at if it refines,
# else at a typical NPC station (50% base yield, 5% tax at no standing).
#
# One level at a time can hide the point: often refining only beats selling
# raw once several skills are up. So there's also an outlook: whether refining
# pays now, and what every refining skill trained as far as the character can
# would add a week, with the training it takes (each skill level once).

REFINE_DAYS = 30
NPC_STATION = (0.5, 0.05)  # base yield, take
REFINING_SKILLS = ("Reprocessing", "Reprocessing Efficiency")


@dataclass(frozen=True)
class RefiningSuggestion:
    skill: str
    level: int
    training_minutes: float  # prerequisites included; as Omega when needs_omega
    isk_a_week: float  # added at the ore mined lately
    book_price: float | None  # to buy, when the skill isn't trained at all
    needs_omega: bool = False
    starts_refining: bool = False  # refining didn't beat selling raw before this level

    @property
    def payback_weeks(self) -> float | None:
        if not self.book_price or self.isk_a_week <= 0:
            return None
        return self.book_price / self.isk_a_week


@dataclass(frozen=True)
class _Ore:
    units_a_week: float
    raw: float  # ISK a unit sold raw
    minerals: float  # ISK a unit's minerals come to at 100% yield, before tax
    skill_id: int | None  # its ore processing skill


def weekly_ore(db: sqlite3.Connection, character_id: int, today: str) -> dict[int, float]:
    """Units of each ore mined a week, over the last REFINE_DAYS of the ledger."""
    since = (date.fromisoformat(today[:10]) - timedelta(days=REFINE_DAYS)).isoformat()
    totals: dict[int, float] = {}
    for row in snapshots.latest(db, character_id, "mining_ledger") or []:
        if isinstance(row, dict) and str(row.get("date", "")) > since and "type_id" in row:
            totals[row["type_id"]] = totals.get(row["type_id"], 0.0) + row.get("quantity", 0)
    return {t: units * 7 / REFINE_DAYS for t, units in totals.items()}


def _ore(db: sqlite3.Connection, type_id: int, units_a_week: float) -> "_Ore | None":
    raw = goals.price_of(db, type_id)
    materials = db.execute(
        "SELECT material_type_id, quantity FROM sde_type_materials WHERE type_id = ?", (type_id,)
    ).fetchall()
    if not materials or raw is None:
        return None  # can't be refined, or no price to compare with
    portion = db.execute(
        "SELECT portion_size FROM sde_types WHERE type_id = ?", (type_id,)
    ).fetchone()
    per_unit = sum(
        quantity * (goals.price_of(db, mineral) or 0.0) for mineral, quantity in materials
    ) / ((portion[0] if portion else None) or 100)
    skill = db.execute(
        "SELECT value FROM sde_type_attributes WHERE type_id = ? AND attribute_id = ?",
        (type_id, sde.REPROCESSING_SKILL),
    ).fetchone()
    return _Ore(units_a_week, raw, per_unit, int(skill[0]) if skill else None)


def _station(db: sqlite3.Connection, character_id: int) -> tuple[float, float]:
    """(base yield, tax) where the character would refine."""
    location = snapshots.latest(db, character_id, "location") or {}
    row = db.execute(
        "SELECT reprocessing_efficiency, reprocessing_station_take FROM sde_npc_stations "
        "WHERE station_id = ?",
        (location.get("station_id"),),
    ).fetchone()
    if row and row[0]:
        return float(row[0]), float(row[1] or 0.0)
    return NPC_STATION


def _week_value(ores: list, station, levels: dict, reprocessing_ids) -> float:
    """ISK a week the ore is worth, each ore sold raw or refined, whichever pays."""
    efficiency, take = station
    repro, repro_efficiency = (levels.get(i, 0) for i in reprocessing_ids)
    total = 0.0
    for found in ores:
        refined = ore.refine_yield(efficiency, repro, repro_efficiency,
                                   levels.get(found.skill_id, 0)) * (1 - take)  # fmt: skip
        total += found.units_a_week * max(found.raw, found.minerals * refined)
    return total


def _level_now(character: skills.CharacterSkills, skill_id: int) -> int:
    trained = character.skills.get(skill_id, TrainedSkill(0, 0, 0))
    return trained.active_level if character.is_alpha else trained.trained_level


def _refining_suggestion(db, character, skill_id: int, level: int, gain: float, refines_now):
    plan = skills.plan_skill(db, character, skill_id, level)
    minutes = plan.minutes_as_alpha if character.is_alpha else plan.minutes_as_omega
    untrained = character.skills.get(skill_id, TrainedSkill(0, 0, 0)).trained_level == 0
    return RefiningSuggestion(
        skill=skills.type_name(db, skill_id),
        level=level,
        training_minutes=plan.minutes_as_omega if minutes is None else minutes,
        isk_a_week=gain,
        book_price=goals.price_of(db, skill_id) if untrained else None,
        needs_omega=minutes is None,
        starts_refining=not refines_now,
    )


def refining_suggestions(
    db: sqlite3.Connection, character_id: int, limit: int = 3, today: str | None = None
) -> list[RefiningSuggestion]:
    """Refining skills one level up, most ISK a week first, at the ore mined lately."""
    try:
        character, ores, station, reprocessing_ids, candidates, levels = _ores_and_skills(
            db, character_id, today
        )
    except skills.SkillsError:
        return []
    if not ores:
        return []
    now_value = _week_value(ores, station, levels, reprocessing_ids)
    refines_now = now_value > sum(o.units_a_week * o.raw for o in ores) + 1e-6
    caps = skills.alpha_caps(db, character.race_id)
    found = []
    for skill_id in candidates:
        level = _next_level(character, caps, skill_id)
        if level is None:
            continue
        gain = _week_value(ores, station, {**levels, skill_id: level}, reprocessing_ids)
        if gain - now_value > 0.5:
            found.append(_refining_suggestion(db, character, skill_id, level,
                                              gain - now_value, refines_now))  # fmt: skip
    found.sort(key=lambda s: -s.isk_a_week)
    return found[:limit]


@dataclass(frozen=True)
class RefiningOutlook:
    """Refining the ore mined lately: now, and with the refining skills trained up."""

    units_a_week: float
    raw_a_week: float  # selling all of it raw
    now_a_week: float  # each ore sold raw or refined, whichever pays, at today's skills
    best_a_week: float  # the same with every refining skill as high as it can go
    targets: tuple[tuple[str, int], ...]  # (skill, level) to train for best_a_week
    training_minutes: float
    needs_omega: bool  # some of those levels are beyond Alpha
    station_yield: float  # the base yield assumed

    @property
    def refines_now(self) -> bool:
        return self.now_a_week > self.raw_a_week + 0.5

    @property
    def gain_a_week(self) -> float:
        return self.best_a_week - self.now_a_week


def _ores_and_skills(db, character_id: int, today: str | None):
    """(the character, its ores, the station, reprocessing skill IDs, candidates, levels)."""
    character = skills.load_character_skills(db, character_id)
    mined = weekly_ore(db, character_id, today or date.today().isoformat())
    ores = [found for t, units in mined.items() if (found := _ore(db, t, units)) is not None]
    reprocessing_ids = tuple(_skill_id(db, name) for name in REFINING_SKILLS)
    candidates = list(dict.fromkeys(
        [*(i for i in reprocessing_ids if i), *(o.skill_id for o in ores if o.skill_id)]
    ))  # fmt: skip
    levels = {i: _level_now(character, i) for i in candidates}
    return character, ores, _station(db, character_id), reprocessing_ids, candidates, levels


def refining_outlook(
    db: sqlite3.Connection, character_id: int, today: str | None = None
) -> RefiningOutlook | None:
    """Whether refining the ore mined lately pays, and what training every
    refining skill up would add a week. None without recent ore."""
    try:
        character, ores, station, ids, candidates, levels = _ores_and_skills(
            db, character_id, today
        )
    except skills.SkillsError:
        return None
    if not ores:
        return None
    caps = skills.alpha_caps(db, character.race_id)
    best_levels = dict(levels)
    for skill_id in candidates:
        top = min(caps.get(skill_id, 0), 5) if character.is_alpha else 5
        best_levels[skill_id] = max(levels[skill_id], top)
    steps, beyond_alpha = {}, False
    targets = []
    for skill_id in candidates:
        if best_levels[skill_id] > levels[skill_id]:
            targets.append((skills.type_name(db, skill_id), best_levels[skill_id]))
            plan = skills.plan_skill(db, character, skill_id, best_levels[skill_id])
            beyond_alpha = beyond_alpha or plan.minutes_as_alpha is None
            for step in plan.steps:
                steps[(step.skill_id, step.level)] = step
    minutes = sum(step.minutes_omega for step in steps.values())
    if character.is_alpha and not beyond_alpha:
        minutes /= skills.ALPHA_TRAINING_MULTIPLIER
    return RefiningOutlook(
        units_a_week=sum(o.units_a_week for o in ores),
        raw_a_week=sum(o.units_a_week * o.raw for o in ores),
        now_a_week=_week_value(ores, station, levels, ids),
        best_a_week=_week_value(ores, station, best_levels, ids),
        targets=tuple(targets),
        training_minutes=minutes,
        needs_omega=character.is_alpha and beyond_alpha,
        station_yield=station[0],
    )
