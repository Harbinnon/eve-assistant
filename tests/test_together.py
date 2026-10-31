"""Playing several characters at once (roadmap 8.4): who may play together,
the two shapes compared, recording, and the dashboard and CLI.

World (as in test_planner): one highsec system with an NPC station, Veldspar
refining into Tritanium. Character 7 mines in a frigate with one laser (100 m³
a minute, a 1,000 m³ ore hold); character 8 sits in a hauler at the station.
"""

import re
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from eve_assistant import cli, planner, risk, sde, snapshots, store, together
from eve_assistant.activities.base import PlanItem
from eve_assistant.activities.mining import _mining_minutes
from eve_assistant.esi.client import EsiClient
from eve_assistant.skills import Reason
from eve_assistant.web import create_app

HOME, STATION, REGION = 31000001, 61000001, 10000032
FRIGATE, LASER, HAULER, ORE, MINERAL = 100, 200, 300, 1230, 34
BASE = "https://esi.test"
LOCAL = "http://127.0.0.1"


def _character(conn, character_id, name, ship_type, ship_item, assets):
    snapshots.ensure_character(conn, character_id, name)
    names = ("charisma", "intelligence", "memory", "perception", "willpower")
    for kind, payload in {
        "public": {"race_id": 1},
        "attributes": {n: 20 for n in names},
        "skills": {"total_sp": 1, "skills": []},
        "skillqueue": [],
        "location": {"solar_system_id": HOME, "station_id": STATION},
        "ship": {"ship_type_id": ship_type, "ship_item_id": ship_item},
        "assets": assets,
        "wallet": 50_000_000.0,
        "standings": [],
    }.items():
        snapshots.record(conn, character_id, kind, payload)


def _world(path=":memory:", hauler_m3=5000.0, second_account="second", state="omega"):
    conn = store.connect(path)
    conn.execute("INSERT INTO sde_regions VALUES (?, 'Home Region')", (REGION,))
    conn.execute("INSERT INTO sde_solar_systems VALUES (?, 'Home', ?, 1, 0.9, 'B')",
                 (HOME, REGION))  # fmt: skip
    conn.execute(
        "INSERT INTO sde_npc_stations VALUES (?, ?, 1000001, 1, 0.5, 0.05)", (STATION, HOME)
    )
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, ?, ?, 1)",
        [(1, 6, "Frigate"), (28, 6, "Hauler"), (54, 7, "Mining Laser"),
         (462, 25, "Veldspar"), (18, 4, "Mineral")],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume, portion_size, "
        "capacity) VALUES (?, ?, ?, 1, ?, ?, ?)",
        [(FRIGATE, "Test Frigate", 1, 1000, 1, 50), (HAULER, "Test Hauler", 28, 5000, 1, hauler_m3),
         (LASER, "Test Miner", 54, 5, 1, 0), (ORE, "Veldspar", 462, 0.1, 100, 0),
         (MINERAL, "Tritanium", 18, 0.01, 1, 0)],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO sde_dogma_attributes VALUES (?, ?)",
        [(73, "duration"), (77, "miningAmount"), (1556, "generalMiningHoldCapacity")],
    )
    conn.executemany(
        "INSERT INTO sde_type_attributes VALUES (?, ?, ?)",
        [(LASER, 73, 60000.0), (LASER, 77, 100.0), (FRIGATE, 1556, 1000.0)],
    )
    conn.execute("INSERT INTO sde_type_materials VALUES (?, ?, 400)", (ORE, MINERAL))
    conn.execute("INSERT INTO sde_meta VALUES (1, 'x', 'y', ?)", (sde.LOADER_VERSION,))
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, ?, 'now')",
        [(FRIGATE, 1e6, 1e6), (HAULER, 2e6, 2e6), (LASER, 1e5, 1e5), (ORE, 10, 10)],
    )
    _character(conn, 7, "Miner", FRIGATE, 9001,
               [{"type_id": LASER, "location_id": 9001, "location_flag": "HiSlot0",
                 "quantity": 1}])  # fmt: skip
    snapshots.record(conn, 7, "mining_ledger", [{"type_id": ORE, "solar_system_id": HOME,
                                                 "quantity": 5}])  # fmt: skip
    _character(conn, 8, "Hauler", HAULER, 9002, [])
    conn.executemany(
        "INSERT INTO accounts (account_id, label, clone_state) VALUES (?, ?, ?)",
        [(1, "main", "omega"), (2, "second", state)],
    )
    conn.execute("UPDATE characters SET account_id = 1 WHERE character_id = 7")
    if second_account:
        account = 1 if second_account == "main" else 2
        conn.execute("UPDATE characters SET account_id = ? WHERE character_id = 8", (account,))
    conn.commit()
    return conn


def _mock_esi():
    def books(request):
        price = {ORE: 10.0, MINERAL: 2.0}.get(int(request.url.params["type_id"]))
        data = [] if price is None else [
            {"price": price, "volume_remain": 10_000_000, "is_buy_order": True,
             "location_id": STATION, "system_id": HOME, "range": "station"}
        ]  # fmt: skip
        return httpx.Response(200, json=data)

    respx.get(url__regex=rf"{BASE}/markets/\d+/orders").mock(side_effect=books)
    respx.get(f"{BASE}/contracts/public/{REGION}").mock(return_value=httpx.Response(200, json=[]))


def _build(db, minutes=60, **options):
    with httpx.Client() as http:
        return together.build(db, EsiClient(http, db, base_url=BASE), [7, 8], minutes, **options)


# --- who may play together ---------------------------------------------------


@pytest.mark.parametrize(
    "second, state, codes",
    [
        ("second", "omega", []),
        ("main", "omega", ["together_same_account"]),
        ("second", "alpha", ["together_not_omega"]),
        ("second", "unknown", ["together_not_omega"]),
        (None, "omega", ["together_no_account"]),
    ],
)
def test_each_character_needs_its_own_omega_account(second, state, codes):
    db = _world(second_account=second, state=state)
    assert [r.code for r in together.check(together.members(db, [7, 8]))] == codes


def test_one_character_is_not_a_group():
    db = _world()
    assert [r.code for r in together.check(together.members(db, [7, 7]))] == ["together_too_few"]


def test_building_refuses_characters_that_cant_play_together():
    db = _world(second_account="main")
    with pytest.raises(together.TogetherError) as refused:
        together.build(db, None, [7, 8], 60)
    assert [r.code for r in refused.value.reasons] == ["together_same_account"]


# --- the two shapes ----------------------------------------------------------


def test_with_a_hauler_the_miner_mines_longer():
    # Alone: 58 min after selling, 5 dock trips of 3 min. With a hauler: 54 min
    # after the last can's trip and the sale, 6 jettisons of half a minute.
    assert _mining_minutes(60, 100, 1000, hauled=False) == pytest.approx(43)
    assert _mining_minutes(60, 100, 1000, hauled=True) == pytest.approx(51)


@respx.mock
def test_one_mining_while_the_other_hauls_wins_when_it_makes_more():
    _mock_esi()
    db = _world()
    team = _build(db)
    assert team.chosen.kind == "mine_and_haul"
    assert team.chosen.roles == ("miner", "hauler")
    mined, hauled = team.chosen.plans
    mining = next(i for i in mined.items if i.kind == "mining")
    assert "jettisoning each full hold into a can for Hauler" in mining.title
    assert mining.inputs["hauled_by"] == "Hauler"
    haul = hauled.items[-1]
    assert haul.kind == "team_haul"
    assert haul.isk == 0.0  # the ore's value is on the miner's step
    assert haul.inputs["trips"] == 2  # 5,100 m³ in a 5,000 m³ hauler
    assert haul.title.startswith("Collect the cans Miner jettisons: about 2 trips")
    assert [shape.kind for shape in team.others] == ["side_by_side"]
    assert team.chosen.value > team.others[0].value
    codes = [n.code for n in team.notes]
    assert codes[0] == "together_chosen"
    assert "no_team_mining" in codes  # the hauler has no mining ship


@respx.mock
def test_side_by_side_wins_when_the_hauler_cant_keep_up():
    _mock_esi()
    db = _world(hauler_m3=10.0)  # 510 trips: far too many
    team = _build(db)
    assert team.chosen.kind == "side_by_side"
    assert team.others == ()
    assert {"no_team_hauler", "no_team_mining"} <= {n.code for n in team.notes}
    assert team.chosen.roles == ("solo", "solo")


@respx.mock
def test_risky_hauling_is_left_out_unless_allowed():
    _mock_esi()
    db = _world()
    snapshots.record(db, 8, "wallet", 0.0)  # the hauler's ship would be all it has
    db.commit()
    careful = _build(db)
    assert careful.chosen.kind == "side_by_side"
    assert "team_haul_risky" in {n.code for n in careful.notes}
    bold = _build(db, allow_risky=True)
    assert bold.chosen.kind == "mine_and_haul"
    assert bold.chosen.plans[1].items[-1].risk.rating == "red"


@respx.mock
def test_plans_and_roles_follow_the_order_the_characters_were_given():
    _mock_esi()
    db = _world()
    with httpx.Client() as http:
        team = together.build(db, EsiClient(http, db, base_url=BASE), [8, 7], 60)
    assert [m.name for m in team.members] == ["Hauler", "Miner"]
    assert team.chosen.roles == ("hauler", "miner")
    assert [p.character_id for p in team.chosen.plans] == [8, 7]


def _item(kind, key, isk):
    return PlanItem(kind, key, f"{kind} {key}", isk, 30, risk.rate("none", 0, 1))


def _plan(character_id, *items):
    return planner.Plan(character_id, 60, tuple(items), (), (), 0.0)


def test_a_contract_both_want_goes_to_whoever_makes_most_from_it():
    members = [together.Member(7, "A", "main", "omega"), together.Member(8, "B", "two", "omega")]
    asked = []

    def plan(member, taken=frozenset(), hauled_by=None):
        asked.append((member.name, taken))
        if member.name == "A":
            return _plan(7, _item("courier", "c1", 1_000_000))
        if ("courier", "c1") in taken:
            return _plan(8, _item("mining", "m", 500_000))
        return _plan(8, _item("courier", "c1", 900_000))

    shape, _solo = together._side_by_side(members, plan)
    a, b = shape.plans
    assert [i.key for i in a.items] == ["c1"]
    assert [i.key for i in b.items] == ["m"]  # B planned again without the contract
    assert ("B", frozenset({("courier", "c1")})) in asked


# --- recording ---------------------------------------------------------------


@respx.mock
def test_a_team_plan_is_recorded_as_one_plan_each_and_is_append_only():
    _mock_esi()
    db = _world()
    team = together.record(db, _build(db), {"source": "test"})
    stored = together.load(db, team.team_id)
    assert stored.shape == "mine_and_haul"
    assert [(role, p.character_name) for role, p in stored.members] == [
        ("miner", "Miner"), ("hauler", "Hauler")]  # fmt: skip
    assert stored.others[0]["shape"] == "side_by_side"
    assert stored.value == pytest.approx(team.chosen.value)
    inputs = db.execute(
        "SELECT inputs FROM plans WHERE plan_id = ?", (stored.members[1][1].plan_id,)
    ).fetchone()[0]
    assert '"role": "hauler"' in inputs
    assert '"with": ["Miner"]' in inputs
    assert together.team_of(db, stored.members[0][1].plan_id) == team.team_id
    assert together.latest_team_id(db) == team.team_id
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("UPDATE team_plans SET shape = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute("DELETE FROM team_plan_members")


# --- the dashboard -----------------------------------------------------------


def _token(html: str) -> str:
    return re.search(r'name="token" value="([^"]+)"', html).group(1)


@respx.mock
def test_the_dashboard_plans_together_and_starts_each_character(tmp_path):
    _mock_esi()
    path = tmp_path / "t.sqlite3"
    db = _world(path)
    team_id = together.record(db, _build(db), {"source": "test"}).team_id
    db.close()
    asked = []

    def make_team_plan(ids, minutes, lowsec, risky):
        asked.append((ids, minutes))
        return team_id

    app = create_app(path, make_team_plan=make_team_plan, snapshot=lambda _cid: [])
    with TestClient(app, base_url=LOCAL) as client:
        today = client.get("/today").text
        assert "Play together" in today
        made = client.post(
            "/today/together",
            data={"token": _token(today), "characters": ["Miner", "Hauler"], "minutes": "60"},
            follow_redirects=False,
        )
        assert made.headers["location"].startswith("/teams/planning/")
        assert _finished(client, made.headers["location"]) == f"/teams/{team_id}"
        assert asked == [([7, 8], 60.0)]
        page = client.get(f"/teams/{team_id}").text
        assert "Playing together" in page
        assert "Miner · mines" in page
        assert "Hauler · hauls" in page
        assert "one mining while the other hauls" in page
        assert "The other way: each playing their own plan" in page
        assert "Start Miner's plan" in page
        plan_id = re.search(r'name="plan_id" value="(\d+)"', page).group(1)
        started = client.post(
            "/sessions/start",
            data={"token": _token(page), "plan_id": plan_id},
            follow_redirects=False,
        )
        assert started.headers["location"].startswith(f"/teams/{team_id}")
        page = client.get(f"/teams/{team_id}").text
        assert "Miner is playing." in page
        assert "Start Hauler's plan" in page
        assert "Your last plan together" in client.get("/today").text


def _finished(client, waiting: str) -> str:
    """Where the waiting page sends you once planning in the background is done."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(waiting, follow_redirects=False)
        if response.status_code == 303:
            return response.headers["location"]
        time.sleep(0.02)
    raise AssertionError("planning never finished")


def test_planning_together_runs_in_the_background_with_a_waiting_page(tmp_path):
    path = tmp_path / "t.sqlite3"
    _world(path).close()
    release = threading.Event()

    def slow(*_a):
        release.wait(5)
        return 41

    with TestClient(create_app(path, make_team_plan=slow), base_url=LOCAL) as client:
        token = _token(client.get("/today").text)
        waiting = client.post(
            "/today/together",
            data={"token": token, "characters": ["Miner", "Hauler"], "minutes": "240"},
            follow_redirects=False,
        ).headers["location"]
        page = " ".join(client.get(waiting).text.split())
        assert "<h1>Planning together" in page
        assert "240 minutes" in page
        assert "<strong>Miner and Hauler</strong>" in page
        assert 'http-equiv="refresh" content="5;' in page  # checks back by itself
        release.set()
        assert _finished(client, waiting) == "/teams/41"
        assert client.get("/teams/planning/nonsense").status_code == 404


def test_a_failed_plan_together_shows_why(tmp_path):
    path = tmp_path / "t.sqlite3"
    _world(path).close()

    def broken(*_a):
        raise RuntimeError("markets unreachable")

    with TestClient(create_app(path, make_team_plan=broken), base_url=LOCAL) as client:
        token = _token(client.get("/today").text)
        waiting = client.post(
            "/today/together",
            data={"token": token, "characters": ["Miner", "Hauler"], "minutes": "60"},
            follow_redirects=False,
        ).headers["location"]
        deadline = time.monotonic() + 5
        page = ""
        while "markets unreachable" not in page and time.monotonic() < deadline:
            page = client.get(waiting).text
            time.sleep(0.02)
    assert "Could not build a plan: markets unreachable" in page


@respx.mock
def test_a_character_that_cant_mine_or_carry_isnt_planned_again(monkeypatch):
    _mock_esi()
    db = _world()
    built = []
    real = planner.build_plan

    def counting(db_, esi, character_id, minutes, **options):
        built.append((character_id, options.get("hauled_by")))
        return real(db_, esi, character_id, minutes, **options)

    monkeypatch.setattr(planner, "build_plan", counting)
    _build(db)
    # Own plans for both, then Miner mining for Hauler; Hauler has no mining ship,
    # so Hauler mining for Miner is never planned.
    assert built == [(7, None), (8, None), (7, "Hauler")]


def test_the_dashboard_says_why_characters_cant_play_together(tmp_path):
    path = tmp_path / "t.sqlite3"
    _world(path, second_account="main").close()
    app = create_app(path, make_team_plan=lambda *_a: pytest.fail("should not plan"))
    with TestClient(app, base_url=LOCAL) as client:
        token = _token(client.get("/today").text)
        page = client.post(
            "/today/together",
            data={"token": token, "characters": ["Miner", "Hauler"], "minutes": "60"},
        ).text
    assert "Miner and Hauler are on the same account (main)" in page


# --- the CLI -----------------------------------------------------------------


def _offline_cli(monkeypatch, path):
    real_connect = store.connect
    monkeypatch.setattr(cli.store, "connect", lambda *_a: real_connect(path))
    monkeypatch.setattr(cli, "load_config", lambda: None)
    monkeypatch.setattr(cli, "default_store", lambda: None)
    monkeypatch.setattr(cli.auth, "http_client", lambda _config: httpx.Client())
    monkeypatch.setattr(
        cli.auth, "AccessTokens", lambda *_a: SimpleNamespace(get=lambda *_b: "token")
    )


def test_the_cli_says_why_characters_cant_play_together(tmp_path, monkeypatch, capsys):
    path = tmp_path / "t.sqlite3"
    _world(path, state="alpha").close()
    _offline_cli(monkeypatch, path)
    assert cli.main(["plan-together", "60", "Miner", "Hauler"]) == 1
    assert "The second account (Hauler) is Alpha" in capsys.readouterr().err


@respx.mock
def test_the_cli_prints_and_records_the_plan(tmp_path, monkeypatch, capsys):
    _mock_esi()
    path = tmp_path / "t.sqlite3"
    db = _world(path)
    built = _build(db)
    db.close()
    _offline_cli(monkeypatch, path)
    monkeypatch.setattr(cli.together, "build", lambda *_a, **_k: built)
    assert cli.main(["plan-together", "60", "Miner", "Hauler"]) == 0
    out = capsys.readouterr().out
    assert "minutes, one mining while the other hauls." in out
    assert "Miner (mines), plan" in out
    assert "Hauler (hauls), plan" in out
    assert "Best between you: one mining while the other hauls" in out
    db = store.connect(path)
    assert together.latest_team_id(db) is not None


def test_reason_texts_exist_for_every_together_code():
    from eve_assistant import explain

    for code in ("together_too_few", "together_no_account", "together_same_account",
                 "together_not_omega", "together_chosen", "together_side_by_side_only",
                 "no_team_mining", "no_team_hauler", "team_haul_risky", "team_mining",
                 "team_haul"):  # fmt: skip
        assert explain.describe(code, {}) != code, code
    assert explain.describe(Reason("x").code) == "x"  # unknown codes fall back


def test_a_miner_may_mine_what_a_partner_mined_in_that_system():
    from eve_assistant.activities import mining as mining_activity

    db = _world()
    db.execute(
        "INSERT INTO sde_types (type_id, name, group_id, published, volume) "
        "VALUES (1231, 'Ytirium', 462, 1, 0.1)"
    )
    snapshots.record(db, 8, "mining_ledger", [
        {"type_id": 1231, "solar_system_id": HOME, "quantity": 9},
        {"type_id": ORE, "solar_system_id": HOME, "quantity": 9},
        {"type_id": 1231, "solar_system_id": 1, "quantity": 9}])  # fmt: skip
    notes = []
    alone = mining_activity._ore_choices(db, 7, HOME, None, [])
    together_ = mining_activity._ore_choices(db, 7, HOME, None, notes, partners=(8,))
    assert alone == [ORE]
    assert together_ == [ORE, 1231]  # own first, then the partner's new ones
    assert [n.detail for n in notes if n.code == "ores_from_partners"] == [{"count": 1}]


@respx.mock
def test_every_plan_together_counts_the_partners_ores(monkeypatch):
    _mock_esi()
    db = _world()
    partners = []
    real = planner.build_plan

    def noting(db_, esi, character_id, minutes, **options):
        partners.append((character_id, options.get("ore_partners")))
        return real(db_, esi, character_id, minutes, **options)

    monkeypatch.setattr(planner, "build_plan", noting)
    _build(db)
    assert set(partners) == {(7, (8,)), (8, (7,))}
