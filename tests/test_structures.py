"""Structure lookups: which IDs are looked up, and how answers are remembered."""

import httpx
import pytest
import respx

from eve_assistant import snapshots, store, structures
from eve_assistant.esi.client import EsiClient

BASE = "https://esi.test"
CITADEL, NO_ACCESS, SHIP_ITEM = 1_050_000_000_001, 1_050_000_000_002, 1_060_000_000_009


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, 7, "Pilot")
    snapshots.record(conn, 7, "location", {"solar_system_id": 1, "structure_id": CITADEL})
    snapshots.record(
        conn,
        7,
        "assets",
        [
            {"item_id": SHIP_ITEM, "type_id": 1, "location_id": NO_ACCESS,
             "location_type": "item", "location_flag": "Hangar", "is_singleton": True},
            # a module fitted inside that ship: its location is an item, not a structure
            {"item_id": 5, "type_id": 2, "location_id": SHIP_ITEM, "location_type": "item",
             "location_flag": "HiSlot0", "is_singleton": True},
            {"item_id": 6, "type_id": 3, "location_id": 60003760, "location_type": "station",
             "location_flag": "Hangar", "is_singleton": False},
        ],
    )  # fmt: skip
    yield conn
    conn.close()


def test_only_real_structures_are_referenced(db):
    assert structures.referenced(db, 7) == {CITADEL, NO_ACCESS}


@respx.mock
def test_lookups_are_remembered_including_refusals(db):
    respx.get(f"{BASE}/universe/structures/{CITADEL}").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Owner's <b>Base</b>",
                "solar_system_id": 30000142,
                "type_id": 35832,
                "owner_id": 98000001,
            },
        )  # fmt: skip
    )
    refused = respx.get(f"{BASE}/universe/structures/{NO_ACCESS}").mock(
        return_value=httpx.Response(403)
    )
    with httpx.Client() as http:
        esi = EsiClient(http, db, token_for=lambda cid: "t", base_url=BASE)
        assert structures.refresh(db, esi, 7) == 2
        assert structures.refresh(db, esi, 7) == 0  # both remembered for a week
    assert refused.call_count == 1
    known = structures.get(db, CITADEL)
    assert known.accessible and known.solar_system_id == 30000142
    assert known.player_name == "Owner's <b>Base</b>"  # stored as-is, escaped when shown
    assert structures.system_of(db, NO_ACCESS) is None
