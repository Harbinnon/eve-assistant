import json
import zipfile

import httpx
import pytest
import respx

from eve_assistant import sde, store


def _jsonl(rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


@pytest.fixture
def sample_zip(tmp_path):
    """A tiny SDE build in the real file format."""
    path = tmp_path / "sde-42-jsonl.zip"
    files = {
        "_sde.jsonl": [{"_key": "sde", "buildNumber": 42, "releaseDate": "2026-10-01T11:00:00Z"}],
        "categories.jsonl": [
            {"_key": 6, "name": {"en": "Ship", "de": "Schiff"}, "published": True}
        ],
        "groups.jsonl": [
            {"_key": 463, "categoryID": 6, "name": {"en": "Mining Barge"}, "published": True}
        ],
        "types.jsonl": [
            {
                "_key": 17478,
                "name": {"en": "Retriever"},
                "groupID": 463,
                "published": True,
                "volume": 150000.0,
                "packagedVolume": 3750.0,
                "capacity": 450.0,
                "portionSize": 1,
                "basePrice": 28800000.0,
                "marketGroupID": 494,
                "metaGroupID": 1,
                "raceID": 128,
            },
            {"_key": 17477, "name": {"en": "Mining Barge"}, "groupID": 255, "published": True},
        ],
        "typeDogma.jsonl": [
            {
                "_key": 17478,
                "dogmaAttributes": [
                    {"attributeID": 182, "value": 17940.0},
                    {"attributeID": 277, "value": 1.0},
                    {"attributeID": 9, "value": 4000.0},
                    {"attributeID": 3181, "value": 3.0},
                ],
            },
        ],
        "cloneGrades.jsonl": [
            {"_key": 1, "name": "Alpha Caldari", "skills": [{"typeID": 3386, "level": 4}]}
        ],
        "compressibleTypes.jsonl": [{"_key": 1230, "compressedTypeID": 62516}],
        "mapRegions.jsonl": [{"_key": 10000002, "name": {"en": "The Forge"}}],
        "mapSolarSystems.jsonl": [
            {
                "_key": 30000142,
                "name": {"en": "Jita"},
                "regionID": 10000002,
                "constellationID": 20000020,
                "securityStatus": 0.9459,
                "securityClass": "B",
            }
        ],
        "npcStations.jsonl": [
            {
                "_key": 60003760,
                "solarSystemID": 30000142,
                "ownerID": 1000035,
                "typeID": 1529,
                "reprocessingEfficiency": 0.5,
                "reprocessingStationsTake": 0.05,
            }
        ],
        "typeMaterials.jsonl": [
            {"_key": 1230, "materials": [{"materialTypeID": 34, "quantity": 400}]}
        ],
        "mapStargates.jsonl": [
            {"_key": 1, "solarSystemID": 30000142, "destination": {"solarSystemID": 30000144}},
            {"_key": 2, "solarSystemID": 30000144, "destination": {"solarSystemID": 30000142}},
        ],
        "npcCorporations.jsonl": [{"_key": 1000035, "factionID": 500001}],
        "blueprints.jsonl": [
            {
                "_key": 17479,
                "maxProductionLimit": 1,
                "activities": {
                    "manufacturing": {
                        "time": 9000,
                        "materials": [{"typeID": 34, "quantity": 500}],
                        "products": [{"typeID": 17478, "quantity": 1}],
                        "skills": [{"typeID": 3380, "level": 1}],
                    },
                    "research_time": {"time": 100},
                },
            },
            {"_key": 99999, "activities": {"copying": {"time": 5}}},  # makes nothing
        ],
        "npcCharacters.jsonl": [
            {
                "_key": 3008416,
                "name": {"en": "Antaken Kamola"},
                "corporationID": 1000002,
                "locationID": 60000004,
                "agent": {"agentTypeID": 2, "divisionID": 22, "level": 1, "isLocator": False},
            },
            {"_key": 3000001, "name": {"en": "Not an agent"}, "corporationID": 1000002},
        ],
        "dogmaAttributes.jsonl": [
            {"_key": 9, "name": "hp"},
            {"_key": 182, "name": "requiredSkill1"},
            {"_key": 277, "name": "requiredSkill1Level"},
            {"_key": 3181, "name": "miningBargeBonusOreMiningYield"},
        ],
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, rows in files.items():
            archive.writestr(name, _jsonl(rows))
    return path


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    yield conn
    conn.close()


def test_load_fills_tables_with_english_names(db, sample_zip):
    build = sde.load(db, sample_zip)
    assert build.build_number == 42
    assert (
        db.execute("SELECT name FROM sde_types WHERE type_id = 17478").fetchone()[0] == "Retriever"
    )
    assert db.execute("SELECT name FROM sde_categories").fetchone()[0] == "Ship"
    attrs = dict(db.execute("SELECT attribute_id, value FROM sde_type_attributes").fetchall())
    # attribute 9 is not kept; 3181 is kept because of its name
    assert attrs == {182: 17940.0, 277: 1.0, 3181: 3.0}
    assert db.execute("SELECT COUNT(*) FROM sde_dogma_attributes").fetchone()[0] == 4
    assert db.execute("SELECT max_level FROM sde_clone_grade_skills").fetchone()[0] == 4
    assert sde.loaded_build(db).build_number == 42
    assert db.execute("SELECT quantity FROM sde_type_materials").fetchone()[0] == 400
    assert db.execute("SELECT COUNT(*) FROM sde_jumps").fetchone()[0] == 2
    assert db.execute("SELECT faction_id FROM sde_npc_corporations").fetchone()[0] == 500001
    assert tuple(db.execute("SELECT * FROM sde_blueprints").fetchone()) == (
        17479,
        17478,
        1,
        9000,
        1,
    )
    assert tuple(db.execute("SELECT * FROM sde_blueprint_materials").fetchone()) == (17479, 34, 500)
    assert tuple(db.execute("SELECT * FROM sde_blueprint_skills").fetchone()) == (17479, 3380, 1)
    assert tuple(db.execute("SELECT * FROM sde_agents").fetchall()[0]) == (
        3008416,
        "Antaken Kamola",
        1000002,
        60000004,
        22,
        1,
        2,
        0,
    )
    assert db.execute("SELECT COUNT(*) FROM sde_agents").fetchone()[0] == 1
    assert not sde.needs_reload(db)


def test_data_from_an_older_loader_must_be_reloaded(db, sample_zip):
    sde.load(db, sample_zip)
    db.execute("UPDATE sde_meta SET loader_version = 1")
    assert sde.needs_reload(db)
    with pytest.raises(sde.SdeError, match="--force"):
        sde.require_current(db)


def test_a_newer_loader_reloads_from_the_zip_on_disk(db, sample_zip):
    folder = sample_zip.parent
    assert not sde.reload_if_needed(db, folder)  # nothing loaded yet
    sde.load(db, sample_zip)
    assert not sde.reload_if_needed(db, folder)  # current
    db.execute("UPDATE sde_meta SET loader_version = 1")
    db.execute("DELETE FROM sde_agents")
    assert sde.reload_if_needed(db, folder)
    assert not sde.needs_reload(db)
    assert db.execute("SELECT COUNT(*) FROM sde_agents").fetchone()[0] == 1
    db.execute("UPDATE sde_meta SET loader_version = 1")
    assert not sde.reload_if_needed(db, folder / "elsewhere")  # no zip: left for the banner


def test_reload_replaces_rather_than_duplicates(db, sample_zip):
    sde.load(db, sample_zip)
    sde.load(db, sample_zip)
    assert db.execute("SELECT COUNT(*) FROM sde_types").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM sde_meta").fetchone()[0] == 1


@respx.mock
def test_update_skips_download_when_current(db, sample_zip, tmp_path):
    sde.load(db, sample_zip)
    respx.get(sde.LATEST_URL).mock(
        return_value=httpx.Response(
            200, text=json.dumps({"_key": "sde", "buildNumber": 42, "releaseDate": "x"}) + "\n"
        )
    )
    download = respx.get(url__regex=r".*static-data-\d+-jsonl\.zip").mock(
        return_value=httpx.Response(500)
    )
    with httpx.Client() as http:
        build, loaded = sde.update(db, http)
    assert (build.build_number, loaded) == (42, False)
    assert download.call_count == 0


@respx.mock
def test_download_writes_the_zip(tmp_path, sample_zip):
    respx.get(sde.BUILD_URL.format(build=42)).mock(
        return_value=httpx.Response(200, content=sample_zip.read_bytes())
    )
    with httpx.Client() as http:
        path = sde.download(http, sde.SdeBuild(42, "x"), tmp_path / "sde")
    assert zipfile.is_zipfile(path)
    assert not list((tmp_path / "sde").glob("*.part"))
