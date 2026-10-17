"""Manufacturing from the character's own blueprints (roadmap 5.4).

For a blueprint at the station the character is docked in:

- **Materials** per job: max(runs, ceil(base x runs x (1 - ME/100))), from
  the static data's manufacturing materials (NPC stations give no bonus).
- **Time** per run: base x (1 - TE/100), less 4% a level of Industry and 3%
  of Advanced Industry (both read from the skills' dogma).
- **Job fee**: the estimated item value (base materials at CCP's adjusted
  prices, ME ignored) x (the system's cost index + 0.25% NPC facility tax
  + 4% SCC surcharge, + 0.25% for an Alpha).
- **Runs**: as many as finish within JOB_HOURS, within the copy's runs left
  or the blueprint's limit per job.
- **Value**: product and materials at CCP's average prices (rough, not a
  hub quote), the product after sales tax; materials already in the station
  hangar are counted at the same price, as selling them is the alternative.

Alpha can build Tech 1 with Industry up to 5; research and invention aren't
modelled. Job slots: one plus a level of Mass Production.
"""

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from eve_assistant import goals, mining, skills, snapshots
from eve_assistant.esi.client import EsiClient

JOB_HOURS = 24.0  # runs that finish within a day
FACILITY_TAX = 0.0025  # NPC stations
SCC_SURCHARGE = 0.04
ALPHA_TAX = 0.0025
COST_INDEX_EVERY = timedelta(days=1)
SETTING = "industry_checked"


@dataclass(frozen=True)
class Material:
    type_id: int
    name: str
    quantity: int
    in_hangar: int
    unit_price: float


@dataclass(frozen=True)
class Build:
    blueprint_item_id: int
    blueprint_name: str
    product_type_id: int
    product_name: str
    runs: int
    units: int
    seconds: float
    materials: tuple[Material, ...]
    job_fee: float
    value: float  # the product, after sales tax
    is_copy: bool
    me: int
    te: int

    @property
    def materials_cost(self) -> float:
        return sum(m.quantity * m.unit_price for m in self.materials)

    @property
    def cost(self) -> float:
        return self.materials_cost + self.job_fee

    @property
    def profit(self) -> float:
        return self.value - self.cost

    @property
    def to_buy(self) -> tuple[Material, ...]:
        return tuple(m for m in self.materials if m.quantity > m.in_hangar)


def due(db: sqlite3.Connection, now: datetime) -> bool:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (SETTING,)).fetchone()
    return row is None or now - datetime.fromisoformat(row[0]) >= COST_INDEX_EVERY


def refresh(db: sqlite3.Connection, esi: EsiClient, now: datetime) -> None:
    """Read every system's manufacturing cost index. Raises EsiError."""
    stamp = now.isoformat(timespec="seconds")
    rows = []
    for system in esi.get("/industry/systems").data:
        for index in system.get("cost_indices", []) if isinstance(system, dict) else []:
            if index.get("activity") == "manufacturing":
                rows.append((system["solar_system_id"], index["cost_index"], stamp))
    with db:
        db.execute("DELETE FROM industry_cost_indices")
        db.executemany("INSERT OR REPLACE INTO industry_cost_indices VALUES (?, ?, ?)", rows)
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SETTING, stamp),
        )


def cost_index(db: sqlite3.Connection, system_id: int | None) -> float | None:
    row = db.execute(
        "SELECT manufacturing FROM industry_cost_indices WHERE system_id = ?", (system_id,)
    ).fetchone()
    return row[0] if row else None


def job_slots(db: sqlite3.Connection, character: skills.CharacterSkills) -> int:
    return 1 + mining.skill_level(db, character, "Mass Production")


def jobs_running(db: sqlite3.Connection, character_id: int) -> int:
    jobs = snapshots.latest(db, character_id, "industry_jobs") or []
    return sum(1 for j in jobs if isinstance(j, dict) and j.get("status") == "active")


def _time_factor(db: sqlite3.Connection, character: skills.CharacterSkills) -> float:
    factor = 1.0
    for name, attribute in (
        ("Industry", "manufacturingTimeBonus"),
        ("Advanced Industry", "advancedIndustrySkillIndustryJobTimeBonus"),
    ):
        row = db.execute(
            "SELECT type_id FROM sde_types WHERE name = ? AND published = 1", (name,)
        ).fetchone()
        bonus = mining.attributes(db, row["type_id"]).get(attribute, 0.0) if row else 0.0
        factor *= 1 + bonus / 100 * mining.skill_level(db, character, name)
    return factor


def _level(character: skills.CharacterSkills, skill_id: int) -> int:
    """The level that counts: the active one for an Alpha, the trained one for Omega."""
    trained = character.skills.get(skill_id)
    if trained is None:
        return 0
    return trained.active_level if character.is_alpha else trained.trained_level


def _can_build(db: sqlite3.Connection, character: skills.CharacterSkills, bp_type: int) -> bool:
    for row in db.execute(
        "SELECT skill_id, level FROM sde_blueprint_skills WHERE blueprint_type_id = ?", (bp_type,)
    ):
        if _level(character, row["skill_id"]) < row["level"]:
            return False
    return True


def _hangar(db: sqlite3.Connection, character_id: int, station_id: int) -> dict[int, int]:
    found: dict[int, int] = {}
    for item in snapshots.latest(db, character_id, "assets") or []:
        if item.get("location_id") == station_id and item.get("location_flag") == "Hangar":
            found[item["type_id"]] = found.get(item["type_id"], 0) + item.get("quantity", 1)
    return found


def plan_build(
    db: sqlite3.Connection,
    character: skills.CharacterSkills,
    blueprint: dict,
    system_id: int | None,
    hangar: dict[int, int],
    tax_rate: float,
) -> Build | None:
    """The job worth starting with this blueprint, or None if it can't be built."""
    spec = db.execute(
        "SELECT * FROM sde_blueprints WHERE blueprint_type_id = ?", (blueprint["type_id"],)
    ).fetchone()
    index = cost_index(db, system_id)
    if spec is None or index is None or not _can_build(db, character, spec["blueprint_type_id"]):
        return None
    me, te = blueprint.get("material_efficiency", 0), blueprint.get("time_efficiency", 0)
    per_run = spec["seconds"] * (1 - te / 100) * _time_factor(db, character)
    limit = blueprint["runs"] if blueprint.get("runs", -1) > 0 else (spec["max_runs"] or 1)
    runs = max(1, min(limit, int(JOB_HOURS * 3600 // per_run) if per_run else limit))
    materials, estimated_value = [], 0.0
    for row in db.execute(
        "SELECT m.material_type_id, m.quantity, p.adjusted_price FROM sde_blueprint_materials m "
        "LEFT JOIN market_prices p ON p.type_id = m.material_type_id "
        "WHERE m.blueprint_type_id = ?",
        (spec["blueprint_type_id"],),
    ):
        quantity = max(runs, math.ceil(round(row["quantity"] * runs * (1 - me / 100), 2)))
        estimated_value += row["quantity"] * runs * (row["adjusted_price"] or 0.0)
        price = goals.price_of(db, row["material_type_id"])
        if price is None:
            return None  # can't cost it
        materials.append(
            Material(
                row["material_type_id"],
                skills.type_name(db, row["material_type_id"]),
                quantity,
                hangar.get(row["material_type_id"], 0),
                price,
            )
        )
    product_price = goals.price_of(db, spec["product_type_id"])
    if product_price is None or not materials:
        return None
    rate = index + FACILITY_TAX + SCC_SURCHARGE + (ALPHA_TAX if character.is_alpha else 0.0)
    units = runs * spec["quantity"]
    return Build(
        blueprint_item_id=blueprint["item_id"],
        blueprint_name=skills.type_name(db, spec["blueprint_type_id"]),
        product_type_id=spec["product_type_id"],
        product_name=skills.type_name(db, spec["product_type_id"]),
        runs=runs,
        units=units,
        seconds=per_run * runs,
        materials=tuple(materials),
        job_fee=estimated_value * rate,
        value=units * product_price * (1 - tax_rate),
        is_copy=blueprint.get("quantity") == -2 or blueprint.get("runs", -1) > 0,
        me=me,
        te=te,
    )


def builds_here(
    db: sqlite3.Connection, character_id: int, station_id: int, system_id: int, tax_rate: float
) -> list[Build]:
    """Builds worth starting from the blueprints in this station's hangar, best first."""
    character = skills.load_character_skills(db, character_id)
    hangar = _hangar(db, character_id, station_id)
    found = []
    for blueprint in snapshots.latest(db, character_id, "blueprints") or []:
        if not isinstance(blueprint, dict) or blueprint.get("location_id") != station_id:
            continue
        build = plan_build(db, character, blueprint, system_id, hangar, tax_rate)
        if build is not None and build.profit > 0:
            found.append(build)
    return sorted(found, key=lambda b: -b.profit)
