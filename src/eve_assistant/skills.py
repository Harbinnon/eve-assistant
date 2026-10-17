"""Skill requirements and training time.

Read-only service functions over the static data and the latest snapshots.
Results carry structured reasons (codes plus numbers), not just text, so the
dashboard, CLI and a future MCP server can all explain them.
"""

import math
import sqlite3
from dataclasses import dataclass, field

from eve_assistant import sde, snapshots

# Cumulative skill points to reach each level, for a rank 1 skill.
SP_FOR_LEVEL = (0, 250, 1415, 8000, 45255, 256000)

# Alpha clones train more slowly. CCP's announcement says "reduced" without a
# number; half speed is the commonly cited figure. UNVERIFIED: to be checked
# against real skill queue timings once an Alpha character is training.
ALPHA_TRAINING_MULTIPLIER = 0.5

# Alpha training stops once trained plus unallocated SP reach this.
ALPHA_QUEUE_SP_LIMIT = 5_000_000

ATTRIBUTE_NAMES = {164: "charisma", 165: "intelligence", 166: "memory", 167: "perception",
                   168: "willpower"}  # fmt: skip


class SkillsError(Exception):
    pass


@dataclass(frozen=True)
class TrainedSkill:
    trained_level: int
    active_level: int  # lower than trained when Alpha caps it
    skillpoints: int


@dataclass(frozen=True)
class CharacterSkills:
    character_id: int
    race_id: int
    clone_state: str  # unknown, alpha, omega
    attributes: dict[str, int]
    skills: dict[int, TrainedSkill]
    total_sp: int
    unallocated_sp: int

    @property
    def is_alpha(self) -> bool:
        # Unknown is treated as Alpha: it is the cautious assumption.
        return self.clone_state != "omega"

    @property
    def alpha_training_blocked(self) -> bool:
        return self.total_sp + self.unallocated_sp >= ALPHA_QUEUE_SP_LIMIT


@dataclass(frozen=True)
class Reason:
    code: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TrainingStep:
    skill_id: int
    skill_name: str
    level: int
    skillpoints: int
    minutes_omega: float
    allowed_for_alpha: bool


@dataclass(frozen=True)
class Requirement:
    type_id: int
    type_name: str
    usable_now: bool
    usable_with_omega_now: bool  # trained skills unlocked by Omega, no training needed
    needs_omega: bool
    steps: tuple[TrainingStep, ...]
    minutes_as_alpha: float | None  # None when Alpha cannot get there at all
    minutes_as_omega: float
    reasons: tuple[Reason, ...]
    total_skillpoints: int = 0  # every required level, from nothing
    trained_skillpoints: int = 0  # of those, already trained

    @property
    def progress(self) -> float:
        """Share of the required skill points already trained (1.0 when none are needed)."""
        return self.trained_skillpoints / self.total_skillpoints if self.total_skillpoints else 1.0


def load_character_skills(db: sqlite3.Connection, character_id: int) -> CharacterSkills:
    skills = snapshots.latest(db, character_id, "skills")
    attributes = snapshots.latest(db, character_id, "attributes")
    public = snapshots.latest(db, character_id, "public")
    if skills is None or attributes is None or public is None:
        raise SkillsError(
            f"No skill data for character {character_id} yet. Run `eve-assistant snapshot`."
        )
    row = db.execute(
        "SELECT a.clone_state FROM characters c LEFT JOIN accounts a USING (account_id) "
        "WHERE c.character_id = ?",
        (character_id,),
    ).fetchone()
    return CharacterSkills(
        character_id=character_id,
        race_id=public["race_id"],
        clone_state=(row["clone_state"] if row and row["clone_state"] else "unknown"),
        attributes={name: attributes[name] for name in ATTRIBUTE_NAMES.values()},
        skills={
            s["skill_id"]: TrainedSkill(
                s["trained_skill_level"], s["active_skill_level"], s["skillpoints_in_skill"]
            )
            for s in skills["skills"]
        },
        total_sp=skills.get("total_sp", 0),
        unallocated_sp=skills.get("unallocated_sp", 0),
    )


def type_name(db: sqlite3.Connection, type_id: int) -> str:
    row = db.execute("SELECT name FROM sde_types WHERE type_id = ?", (type_id,)).fetchone()
    return row["name"] if row else f"type {type_id}"


SHIP_CATEGORY, SKILL_CATEGORY = 6, 16


def is_ship(db: sqlite3.Connection, type_id: int) -> bool:
    row = db.execute(
        "SELECT g.category_id FROM sde_types t JOIN sde_groups g USING (group_id) "
        "WHERE t.type_id = ?",
        (type_id,),
    ).fetchone()
    return bool(row) and row["category_id"] == SHIP_CATEGORY


def names_in(db: sqlite3.Connection, *categories: int) -> list[str]:
    """Published item names in some categories, for suggestions while typing."""
    marks = ",".join("?" * len(categories))
    rows = db.execute(
        "SELECT t.name FROM sde_types t JOIN sde_groups g USING (group_id) "  # noqa: S608  # placeholders only
        f"WHERE g.category_id IN ({marks}) AND t.published = 1 AND g.published = 1 "
        "ORDER BY t.name",
        categories,
    )
    return [r["name"] for r in rows]


def find_type(db: sqlite3.Connection, name: str) -> int:
    """Look up a published item by exact name (case-insensitive)."""
    row = db.execute(
        "SELECT type_id FROM sde_types WHERE name = ? COLLATE NOCASE AND published = 1", (name,)
    ).fetchone()
    if row:
        return row["type_id"]
    if db.execute("SELECT 1 FROM sde_meta").fetchone() is None:
        raise SkillsError("No static data loaded yet. Run `eve-assistant sde-update`.")
    close = [
        r["name"]
        for r in db.execute(
            "SELECT name FROM sde_types WHERE name LIKE ? AND published = 1 ORDER BY length(name) "
            "LIMIT 5",
            (f"%{name}%",),
        )
    ]
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    raise SkillsError(f"No item called '{name}'.{hint}")


def direct_requirements(db: sqlite3.Connection, type_id: int) -> dict[int, int]:
    rows = dict(
        db.execute(
            "SELECT attribute_id, value FROM sde_type_attributes WHERE type_id = ?", (type_id,)
        ).fetchall()
    )
    needed = {}
    for skill_attr, level_attr in sde.REQUIRED_SKILL_ATTRIBUTES.items():
        if skill_attr in rows and level_attr in rows:
            needed[int(rows[skill_attr])] = int(rows[level_attr])
    return needed


def full_requirements(db: sqlite3.Connection, type_id: int) -> list[tuple[int, int]]:
    """Every skill and level needed, prerequisites before the skills that need them."""
    levels: dict[int, int] = {}
    order: list[int] = []

    def visit(skill_id: int, level: int, path: tuple[int, ...]) -> None:
        if skill_id in path:  # defensive: the SDE should never contain a cycle
            return
        for pre_id, pre_level in direct_requirements(db, skill_id).items():
            visit(pre_id, pre_level, (*path, skill_id))
        if skill_id not in levels:
            order.append(skill_id)
        levels[skill_id] = max(levels.get(skill_id, 0), level)

    for skill_id, level in direct_requirements(db, type_id).items():
        visit(skill_id, level, (type_id,))
    return [(skill_id, levels[skill_id]) for skill_id in order]


def alpha_caps(db: sqlite3.Connection, race_id: int) -> dict[int, int]:
    return dict(
        db.execute(
            "SELECT skill_id, max_level FROM sde_clone_grade_skills WHERE grade_id = ?", (race_id,)
        ).fetchall()
    )


def sp_per_minute(db: sqlite3.Connection, character: CharacterSkills, skill_id: int) -> float:
    attrs = dict(
        db.execute(
            "SELECT attribute_id, value FROM sde_type_attributes WHERE type_id = ? "
            "AND attribute_id IN (?, ?)",
            (skill_id, sde.PRIMARY_ATTRIBUTE, sde.SECONDARY_ATTRIBUTE),
        ).fetchall()
    )
    # A skill without attribute data (should not happen with a full SDE) falls
    # back to the character's average attribute rather than failing.
    average = sum(character.attributes.values()) / len(character.attributes)

    def value(attribute_id: int) -> float:
        name = ATTRIBUTE_NAMES.get(int(attrs.get(attribute_id, 0)))
        return character.attributes.get(name, average) if name else average

    return value(sde.PRIMARY_ATTRIBUTE) + value(sde.SECONDARY_ATTRIBUTE) / 2


def skill_rank(db: sqlite3.Connection, skill_id: int) -> float:
    row = db.execute(
        "SELECT value FROM sde_type_attributes WHERE type_id = ? AND attribute_id = ?",
        (skill_id, sde.SKILL_RANK),
    ).fetchone()
    return row["value"] if row else 1.0


def plan(db: sqlite3.Connection, character: CharacterSkills, type_id: int) -> Requirement:
    """What this character needs to use an item (ship, module, ...)."""
    return _plan(db, character, type_id, full_requirements(db, type_id))


def plan_skill(
    db: sqlite3.Connection, character: CharacterSkills, skill_id: int, level: int
) -> Requirement:
    """What this character needs to train a skill to a level, prerequisites included."""
    if not 1 <= level <= 5:
        raise SkillsError("Skill levels go from 1 to 5.")
    needed = [(s, lvl) for s, lvl in full_requirements(db, skill_id) if s != skill_id]
    return _plan(db, character, skill_id, [*needed, (skill_id, level)])


def _plan(
    db: sqlite3.Connection,
    character: CharacterSkills,
    type_id: int,
    needed: list[tuple[int, int]],
) -> Requirement:
    caps = alpha_caps(db, character.race_id)
    steps: list[TrainingStep] = []
    reasons: list[Reason] = []
    usable_now = True
    usable_with_omega_now = True
    alpha_possible = not character.alpha_training_blocked
    total_points = trained_points = 0

    for skill_id, level in needed:
        trained = character.skills.get(skill_id, TrainedSkill(0, 0, 0))
        active = trained.active_level if character.is_alpha else trained.trained_level
        cap = caps.get(skill_id, 0)
        if active < level:
            usable_now = False
        if trained.trained_level < level:
            usable_with_omega_now = False
        if level > cap:
            alpha_possible = False
            reasons.append(
                Reason(
                    "alpha_cap",
                    {
                        "skill_id": skill_id,
                        "skill": type_name(db, skill_id),
                        "needed": level,
                        "alpha_max": cap,
                    },
                )  # fmt: skip
            )
        rank = skill_rank(db, skill_id)
        full = rank * SP_FOR_LEVEL[level]
        total_points += full
        trained_points += min(trained.skillpoints, full)
        rate = sp_per_minute(db, character, skill_id)
        for step_level in range(trained.trained_level + 1, level + 1):
            target = rank * SP_FOR_LEVEL[step_level]
            start = max(trained.skillpoints, rank * SP_FOR_LEVEL[step_level - 1])
            points = max(0, math.ceil(target - start))
            steps.append(
                TrainingStep(
                    skill_id=skill_id,
                    skill_name=type_name(db, skill_id),
                    level=step_level,
                    skillpoints=points,
                    minutes_omega=points / rate,
                    allowed_for_alpha=step_level <= cap,
                )
            )

    if character.is_alpha and character.alpha_training_blocked and steps:
        reasons.append(
            Reason(
                "alpha_sp_limit",
                {
                    "total_sp": character.total_sp + character.unallocated_sp,
                    "limit": ALPHA_QUEUE_SP_LIMIT,
                },
            )  # fmt: skip
        )
    if character.is_alpha and not usable_now and usable_with_omega_now:
        reasons.append(Reason("trained_but_alpha_locked"))

    minutes_omega = sum(s.minutes_omega for s in steps)
    needs_omega = character.is_alpha and not (usable_now or alpha_possible)
    return Requirement(
        type_id=type_id,
        type_name=type_name(db, type_id),
        usable_now=usable_now,
        usable_with_omega_now=usable_with_omega_now,
        needs_omega=needs_omega,
        steps=tuple(steps),
        minutes_as_alpha=(minutes_omega / ALPHA_TRAINING_MULTIPLIER) if alpha_possible else None,
        minutes_as_omega=minutes_omega,
        reasons=tuple(reasons),
        total_skillpoints=round(total_points),
        trained_skillpoints=round(trained_points),
    )


def describe(reason: Reason) -> str:
    """Plain-English text for a reason code."""
    d = reason.detail
    if reason.code == "alpha_cap":
        return f"{d['skill']} {d['needed']} is above the Alpha limit ({d['alpha_max']})."
    if reason.code == "alpha_sp_limit":
        return (
            f"Alpha training stops at {d['limit']:,} skill points; this character has "
            f"{d['total_sp']:,}, so it cannot train anything until Omega."
        )
    if reason.code == "trained_but_alpha_locked":
        return "The skills are already trained; Omega would unlock them immediately."
    return reason.code


def format_minutes(minutes: float) -> str:
    if minutes < 1:
        return "under a minute"
    days, rest = divmod(round(minutes), 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days}d" if days else "", f"{hours}h" if hours else "", f"{mins}m" if mins else ""]
    return " ".join(p for p in parts if p)
