"""Static Data Export: download CCP's reference data and load what we use.

The SDE is published as a zip of JSON Lines files and rebuilt with every
game deployment. It is downloaded into var/sde/ (never committed) and only
the tables the tool needs are loaded into the database, replacing the
previous build's rows.
"""

import json
import sqlite3
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from eve_assistant.config import DATA_DIR
from eve_assistant.store import utc_now

LATEST_URL = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
BUILD_URL = (
    "https://developers.eveonline.com/static-data/tranquility/"
    "eve-online-static-data-{build}-jsonl.zip"
)
DEFAULT_SDE_DIR = DATA_DIR / "sde"

# Dogma attribute IDs kept from typeDogma. Extend as features need more.
REQUIRED_SKILL_ATTRIBUTES = {182: 277, 183: 278, 184: 279, 1285: 1286, 1289: 1287, 1290: 1288}
SKILL_RANK = 275
PRIMARY_ATTRIBUTE = 180
SECONDARY_ATTRIBUTE = 181
REPROCESSING_SKILL = 790  # the ore processing skill for an ore type
ATTRIBUTE_IDS = frozenset(
    {*REQUIRED_SKILL_ATTRIBUTES, *REQUIRED_SKILL_ATTRIBUTES.values()}
    | {SKILL_RANK, PRIMARY_ATTRIBUTE, SECONDARY_ATTRIBUTE, REPROCESSING_SKILL}
)

# Attributes kept by name (resolved against dogmaAttributes at load time).
ATTRIBUTE_NAMES = frozenset(
    {
        "duration",
        "miningAmount",
        "miningAmountMultiplier",
        "miningAmountBonus",
        "miningCritChance",
        "miningCritBonusYield",
        "generalMiningHoldCapacity",
        "specialOreHoldCapacity",
        "specialGasHoldCapacity",
        # Milestone 5: slots for fitting, industry and exploration skills.
        "turretSlotsLeft",
        "launcherSlotsLeft",
        "hiSlots",
        "manufacturingTimeBonus",
        "advancedIndustrySkillIndustryJobTimeBonus",
        "manufacturingSlotBonus",
        "virusCoherence",
        "virusStrength",
        "virusCoherenceBonus",
        "accessDifficultyBonusAbsolutePercent",
    }
)
ATTRIBUTE_NAME_SUFFIXES = (
    "OreMiningYield",
    "OreMiningDuration",
    "GasHarvestingYield",
    "GasHarvestingDuration",
    "GasCloudHarvestingDuration",
    "GasHarvesterDuration",
)
AGENT_TYPE_BASIC = 2

# Bump when load() starts filling new tables or attributes; a database loaded
# by an older loader is then reported as needing a reload.
LOADER_VERSION = 5


def _kept_attribute_ids(names: dict[int, str]) -> frozenset[int]:
    by_name = {
        attribute_id
        for attribute_id, name in names.items()
        if name in ATTRIBUTE_NAMES or name.endswith(ATTRIBUTE_NAME_SUFFIXES)
    }
    return ATTRIBUTE_IDS | by_name


class SdeError(Exception):
    pass


@dataclass(frozen=True)
class SdeBuild:
    build_number: int
    release_date: str


def _english(value) -> str:
    if isinstance(value, dict):
        return value.get("en") or next(iter(value.values()), "")
    return value or ""


def latest_build(http: httpx.Client) -> SdeBuild:
    try:
        response = http.get(LATEST_URL)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SdeError(
            f"Could not check the latest static data: {exc.__class__.__name__}"
        ) from None
    for line in response.text.splitlines():
        if line.strip():
            record = json.loads(line)
            if record.get("_key") == "sde":
                return SdeBuild(int(record["buildNumber"]), record["releaseDate"])
    raise SdeError("The latest-build file did not contain an 'sde' record.")


def loaded_build(db: sqlite3.Connection) -> SdeBuild | None:
    row = db.execute("SELECT build_number, release_date FROM sde_meta").fetchone()
    return SdeBuild(row[0], row[1]) if row else None


def download(http: httpx.Client, build: SdeBuild, folder: Path = DEFAULT_SDE_DIR) -> Path:
    """Download a build's zip (about 100 MB) unless it is already on disk."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"sde-{build.build_number}-jsonl.zip"
    if target.exists() and zipfile.is_zipfile(target):
        return target
    partial = target.with_suffix(".part")
    try:
        with http.stream("GET", BUILD_URL.format(build=build.build_number)) as response:
            response.raise_for_status()
            with partial.open("wb") as out:
                for chunk in response.iter_bytes(1 << 20):
                    out.write(chunk)
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise SdeError(f"Static data download failed: {exc.__class__.__name__}") from None
    partial.replace(target)
    for old in folder.glob("sde-*-jsonl.zip"):
        if old != target:
            old.unlink()
    return target


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict]:
    with archive.open(name) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def load(db: sqlite3.Connection, zip_path: Path) -> SdeBuild:
    """Replace the static data tables with the contents of a build's zip."""
    with zipfile.ZipFile(zip_path) as archive:
        meta = next(r for r in _rows(archive, "_sde.jsonl") if r["_key"] == "sde")
        build = SdeBuild(int(meta["buildNumber"]), meta["releaseDate"])

        with db:  # one transaction: all or nothing
            for table in (
                "sde_meta",
                "sde_categories",
                "sde_groups",
                "sde_types",
                "sde_type_attributes",
                "sde_clone_grade_skills",
                "sde_compressible_types",
                "sde_regions",
                "sde_solar_systems",
                "sde_npc_stations",
                "sde_type_materials",
                "sde_jumps",
                "sde_npc_corporations",
                "sde_dogma_attributes",
                "sde_blueprints",
                "sde_blueprint_materials",
                "sde_blueprint_skills",
                "sde_agents",
                "sde_campaigns",
                "sde_campaign_objectives",
                "sde_planets",
                "sde_planet_schematics",
                "sde_planet_schematic_types",
            ):
                db.execute(f"DELETE FROM {table}")  # noqa: S608  # fixed table names

            db.executemany(
                "INSERT INTO sde_categories VALUES (?, ?, ?)",
                (
                    (r["_key"], _english(r.get("name")), int(r.get("published", False)))
                    for r in _rows(archive, "categories.jsonl")
                ),
            )
            db.executemany(
                "INSERT INTO sde_groups VALUES (?, ?, ?, ?)",
                (
                    (
                        r["_key"],
                        r["categoryID"],
                        _english(r.get("name")),
                        int(r.get("published", False)),
                    )
                    for r in _rows(archive, "groups.jsonl")
                ),
            )
            db.executemany(
                "INSERT INTO sde_types VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        r["_key"],
                        _english(r.get("name")),
                        r["groupID"],
                        int(r.get("published", False)),
                        r.get("volume"),
                        r.get("packagedVolume"),
                        r.get("capacity"),
                        r.get("portionSize"),
                        r.get("basePrice"),
                        r.get("marketGroupID"),
                        r.get("metaGroupID"),
                        r.get("raceID"),
                    )
                    for r in _rows(archive, "types.jsonl")
                ),
            )
            names = {r["_key"]: r.get("name", "") for r in _rows(archive, "dogmaAttributes.jsonl")}
            db.executemany("INSERT INTO sde_dogma_attributes VALUES (?, ?)", names.items())
            keep = _kept_attribute_ids(names)
            db.executemany(
                "INSERT INTO sde_type_attributes VALUES (?, ?, ?)",
                (
                    (r["_key"], a["attributeID"], a["value"])
                    for r in _rows(archive, "typeDogma.jsonl")
                    for a in r.get("dogmaAttributes", [])
                    if a["attributeID"] in keep
                ),
            )
            db.executemany(
                "INSERT INTO sde_clone_grade_skills VALUES (?, ?, ?, ?)",
                (
                    (r["_key"], _english(r.get("name")), s["typeID"], s["level"])
                    for r in _rows(archive, "cloneGrades.jsonl")
                    for s in r.get("skills", [])
                ),
            )
            db.executemany(
                "INSERT INTO sde_compressible_types VALUES (?, ?)",
                (
                    (r["_key"], r["compressedTypeID"])
                    for r in _rows(archive, "compressibleTypes.jsonl")
                ),
            )
            db.executemany(
                "INSERT INTO sde_regions VALUES (?, ?)",
                ((r["_key"], _english(r.get("name"))) for r in _rows(archive, "mapRegions.jsonl")),
            )
            db.executemany(
                "INSERT INTO sde_solar_systems VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        r["_key"],
                        _english(r.get("name")),
                        r["regionID"],
                        r["constellationID"],
                        r["securityStatus"],
                        r.get("securityClass"),
                    )
                    for r in _rows(archive, "mapSolarSystems.jsonl")
                ),
            )
            db.executemany(
                "INSERT INTO sde_npc_stations VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        r["_key"],
                        r["solarSystemID"],
                        r.get("ownerID"),
                        r.get("typeID"),
                        r.get("reprocessingEfficiency"),
                        r.get("reprocessingStationsTake"),
                    )
                    for r in _rows(archive, "npcStations.jsonl")
                ),
            )
            db.executemany(
                "INSERT INTO sde_type_materials VALUES (?, ?, ?)",
                (
                    (r["_key"], m["materialTypeID"], m["quantity"])
                    for r in _rows(archive, "typeMaterials.jsonl")
                    for m in r.get("materials", [])
                ),
            )
            db.executemany(
                "INSERT OR IGNORE INTO sde_jumps VALUES (?, ?)",
                (
                    (r["solarSystemID"], r["destination"]["solarSystemID"])
                    for r in _rows(archive, "mapStargates.jsonl")
                ),
            )
            db.executemany(
                "INSERT INTO sde_npc_corporations VALUES (?, ?, ?)",
                (
                    (r["_key"], r.get("factionID"), _english(r.get("name")))
                    for r in _rows(archive, "npcCorporations.jsonl")
                ),
            )
            _load_industry_and_agents(db, archive)
            _load_campaigns(db, archive)
            _load_planets(db, archive)
            db.execute(
                "INSERT INTO sde_meta (build_number, release_date, loaded_at, loader_version) "
                "VALUES (?, ?, ?, ?)",
                (build.build_number, build.release_date, utc_now(), LOADER_VERSION),
            )
    return build


def _optional_rows(archive: zipfile.ZipFile, name: str):
    """A file's rows, or none if this build doesn't have it."""
    return _rows(archive, name) if name in archive.namelist() else iter(())


def _load_industry_and_agents(db: sqlite3.Connection, archive: zipfile.ZipFile) -> None:
    """Blueprints' manufacturing (materials, product, time, skills) and NPC agents."""
    for r in _optional_rows(archive, "blueprints.jsonl"):
        making = r.get("activities", {}).get("manufacturing")
        if not making or not making.get("products"):
            continue
        product = making["products"][0]
        blueprint = r["_key"]
        db.execute(
            "INSERT INTO sde_blueprints VALUES (?, ?, ?, ?, ?)",
            (blueprint, product["typeID"], product.get("quantity", 1), making.get("time", 0),
             r.get("maxProductionLimit")),
        )  # fmt: skip
        db.executemany(
            "INSERT OR REPLACE INTO sde_blueprint_materials VALUES (?, ?, ?)",
            [(blueprint, m["typeID"], m["quantity"]) for m in making.get("materials", [])],
        )
        db.executemany(
            "INSERT OR REPLACE INTO sde_blueprint_skills VALUES (?, ?, ?)",
            [(blueprint, s["typeID"], s["level"]) for s in making.get("skills", [])],
        )
    db.executemany(
        "INSERT INTO sde_agents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (r["_key"], _english(r.get("name")), r["corporationID"], r.get("locationID"),
             r["agent"]["divisionID"], r["agent"]["level"], r["agent"]["agentTypeID"],
             int(bool(r["agent"].get("isLocator"))))
            for r in _optional_rows(archive, "npcCharacters.jsonl")
            if r.get("agent") and "corporationID" in r
        ),
    )  # fmt: skip


def reload_if_needed(db: sqlite3.Connection, folder: Path = DEFAULT_SDE_DIR) -> bool:
    """Reload the loaded build from its zip on disk when this loader is newer
    than the one that loaded it (a new version filling new tables). Nothing is
    downloaded. Returns True if it reloaded."""
    build = loaded_build(db)
    if build is None or not needs_reload(db):
        return False
    cached = folder / f"sde-{build.build_number}-jsonl.zip"
    if not cached.exists() or not zipfile.is_zipfile(cached):
        return False
    load(db, cached)
    return True


def _load_campaigns(db: sqlite3.Connection, archive: zipfile.ZipFile) -> None:
    """Military campaigns and their objectives: what each is and pays."""
    db.executemany(
        "INSERT INTO sde_campaigns VALUES (?, ?)",
        (
            (r["_key"], _english(r.get("title")))
            for r in _optional_rows(archive, "militaryCampaigns.jsonl")
        ),
    )
    for r in _optional_rows(archive, "militaryCampaignObjectives.jsonl"):
        rewards = r.get("rewards", {})
        method = r.get("contributionMethodConfiguration", {})
        db.execute(
            "INSERT INTO sde_campaign_objectives VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["_key"], r["campaignID"], _english(r.get("title")), method.get("name", ""),
                r.get("careerPath"),
                rewards.get("isk", {}).get("amountPerInterval", 0.0),
                rewards.get("lp", {}).get("amountPerInterval", 0.0),
                rewards.get("lp", {}).get("issuer", {}).get("corporationID"),
                rewards.get("standing", {}).get("gainPercentPerInterval", 0.0),
                r.get("maxProgressPerParticipant"),
                json.dumps(method.get("parameters", [])),
            ),
        )  # fmt: skip


def _load_planets(db: sqlite3.Connection, archive: zipfile.ZipFile) -> None:
    """Planets (for their names) and Planetary Interaction schematics."""
    db.executemany(
        "INSERT INTO sde_planets VALUES (?, ?, ?, ?)",
        (
            (r["_key"], r["solarSystemID"], r.get("celestialIndex"), r.get("typeID"))
            for r in _optional_rows(archive, "mapPlanets.jsonl")
        ),
    )
    for r in _optional_rows(archive, "planetSchematics.jsonl"):
        db.execute(
            "INSERT INTO sde_planet_schematics VALUES (?, ?, ?)",
            (r["_key"], _english(r.get("name")), r.get("cycleTime", 0)),
        )
        db.executemany(
            "INSERT OR REPLACE INTO sde_planet_schematic_types VALUES (?, ?, ?, ?)",
            [(r["_key"], t["_key"], t["quantity"], int(t["isInput"])) for t in r.get("types", [])],
        )


def needs_reload(db: sqlite3.Connection) -> bool:
    """True when the static data was loaded by an older version of this loader."""
    row = db.execute("SELECT loader_version FROM sde_meta").fetchone()
    return row is not None and row[0] < LOADER_VERSION


def require_current(db: sqlite3.Connection) -> None:
    if loaded_build(db) is None:
        raise SdeError("No static data loaded yet. Run `eve-assistant sde-update`.")
    if needs_reload(db):
        raise SdeError(
            "The static data needs reloading for this feature. "
            "Run `eve-assistant sde-update --force`."
        )


# --- update reminder ---------------------------------------------------------

# CCP rebuilds the static data with most game deployments. The background
# refresh asks once a day which build is current and remembers it here, so
# every page can say when an update is waiting without asking CCP each time.
CHECK_EVERY = timedelta(hours=24)
LATEST_SETTING = "sde_latest_seen"


@dataclass(frozen=True)
class UpdateStatus:
    loaded: SdeBuild | None
    latest: SdeBuild | None  # as of the last daily check; None before the first one
    checked_at: str | None
    reload_needed: bool  # this version of the tool reads more than was loaded
    update: dict | None = None  # the last update by the dashboard: state, at, error

    @property
    def updating(self) -> bool:
        return (self.update or {}).get("state") in ("requested", "running")

    @property
    def newer_available(self) -> bool:
        return (
            self.loaded is not None
            and self.latest is not None
            and self.latest.build_number > self.loaded.build_number
        )

    @property
    def needs_update(self) -> bool:
        return self.loaded is None or self.reload_needed or self.newer_available


def update_status(db: sqlite3.Connection) -> UpdateStatus:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (LATEST_SETTING,)).fetchone()
    seen = json.loads(row[0]) if row else None
    latest = SdeBuild(seen["build_number"], seen["release_date"]) if seen else None
    return UpdateStatus(
        loaded=loaded_build(db),
        latest=latest,
        checked_at=seen["checked_at"] if seen else None,
        reload_needed=needs_reload(db),
        update=update_state(db),
    )


def check_due(db: sqlite3.Connection, now: datetime | None = None) -> bool:
    checked = update_status(db).checked_at
    now = now or datetime.now(UTC)
    return checked is None or now - datetime.fromisoformat(checked) >= CHECK_EVERY


def check_for_update(
    db: sqlite3.Connection, http: httpx.Client, now: datetime | None = None
) -> SdeBuild:
    """Ask CCP which build is current and remember it. Raises SdeError."""
    latest = latest_build(http)
    seen = {
        "build_number": latest.build_number,
        "release_date": latest.release_date,
        "checked_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
    }
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (LATEST_SETTING, json.dumps(seen)),
    )
    db.commit()
    return latest


def update(
    db: sqlite3.Connection, http: httpx.Client, force: bool = False
) -> tuple[SdeBuild, bool]:
    """Bring the static data up to date. Returns (build, whether anything was loaded)."""
    latest = latest_build(http)
    current = loaded_build(db)
    if (
        not force
        and current
        and current.build_number == latest.build_number
        and not needs_reload(db)
    ):
        return current, False
    return load(db, download(http, latest)), True


# --- updating by itself ------------------------------------------------------
#
# With the background refresh on, a newer build is downloaded and loaded by the
# refresh itself, though not while a play session is open: the load holds the
# database for a minute or so. "Update now" on the banner asks the next tick to
# do it anyway. After a failure, the next automatic try waits RETRY_AFTER.

UPDATE_SETTING = "sde_update"
RETRY_AFTER = timedelta(hours=6)
STALE_RUN = timedelta(minutes=30)  # a run that never finished: the process stopped
UPDATE_ERRORS = (SdeError, OSError, sqlite3.Error, zipfile.BadZipFile, KeyError, ValueError)


def update_state(db: sqlite3.Connection) -> dict | None:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (UPDATE_SETTING,)).fetchone()
    return json.loads(row[0]) if row else None


def _set_state(db: sqlite3.Connection, state: str, now: datetime, **extra) -> None:
    value = {"state": state, "at": now.isoformat(timespec="seconds"), **extra}
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (UPDATE_SETTING, json.dumps(value)),
    )
    db.commit()


def request_update(db: sqlite3.Connection, now: datetime | None = None) -> None:
    """The banner's Update now: the next background tick updates, playing or not."""
    _set_state(db, "requested", now or datetime.now(UTC))


def update_due(db: sqlite3.Connection, now: datetime, playing: bool, automatic: bool) -> bool:
    """Whether the background refresh should update the static data now."""
    state = update_state(db) or {}
    kind = state.get("state")
    age = now - datetime.fromisoformat(state["at"]) if state.get("at") else None
    if kind == "requested":
        return True
    if kind == "running" and age is not None and age < STALE_RUN:
        return False  # another tick is at it
    if not automatic or playing or not update_status(db).needs_update:
        return False
    return not (kind == "failed" and age is not None and age < RETRY_AFTER)


def update_now(db: sqlite3.Connection, http: httpx.Client, now: datetime) -> SdeBuild:
    """Download and load the newest build, noting how it went for the banner.
    Raises SdeError."""
    _set_state(db, "running", now)
    try:
        build, _loaded = update(db, http, force=needs_reload(db))
    except UPDATE_ERRORS as exc:
        db.rollback()
        reason = str(exc) if isinstance(exc, SdeError) else exc.__class__.__name__
        _set_state(db, "failed", datetime.now(UTC), error=reason)
        raise SdeError(
            f"Static data update failed: {reason}. It tries again in "
            f"{RETRY_AFTER.total_seconds() / 3600:.0f} hours, or press Update now."
        ) from None
    _set_state(db, "done", datetime.now(UTC), build_number=build.build_number)
    return build
