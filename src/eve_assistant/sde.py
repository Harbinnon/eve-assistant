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
from pathlib import Path

import httpx

from eve_assistant.config import PROJECT_ROOT
from eve_assistant.store import utc_now

LATEST_URL = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
BUILD_URL = (
    "https://developers.eveonline.com/static-data/tranquility/"
    "eve-online-static-data-{build}-jsonl.zip"
)
DEFAULT_SDE_DIR = PROJECT_ROOT / "var" / "sde"

# Dogma attribute IDs kept from typeDogma. Extend as features need more.
REQUIRED_SKILL_ATTRIBUTES = {182: 277, 183: 278, 184: 279, 1285: 1286, 1289: 1287, 1290: 1288}
SKILL_RANK = 275
PRIMARY_ATTRIBUTE = 180
SECONDARY_ATTRIBUTE = 181
ATTRIBUTE_IDS = frozenset(
    {*REQUIRED_SKILL_ATTRIBUTES, *REQUIRED_SKILL_ATTRIBUTES.values()}
    | {SKILL_RANK, PRIMARY_ATTRIBUTE, SECONDARY_ATTRIBUTE}
)


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
            ):
                db.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed table names

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
            db.executemany(
                "INSERT INTO sde_type_attributes VALUES (?, ?, ?)",
                (
                    (r["_key"], a["attributeID"], a["value"])
                    for r in _rows(archive, "typeDogma.jsonl")
                    for a in r.get("dogmaAttributes", [])
                    if a["attributeID"] in ATTRIBUTE_IDS
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
            db.execute(
                "INSERT INTO sde_meta VALUES (?, ?, ?)",
                (build.build_number, build.release_date, utc_now()),
            )
    return build


def update(
    db: sqlite3.Connection, http: httpx.Client, force: bool = False
) -> tuple[SdeBuild, bool]:
    """Bring the static data up to date. Returns (build, whether anything was loaded)."""
    latest = latest_build(http)
    current = loaded_build(db)
    if not force and current and current.build_number == latest.build_number:
        return current, False
    return load(db, download(http, latest)), True
