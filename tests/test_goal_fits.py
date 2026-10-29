"""Ship goals with a fit (roadmap 8.1): a pasted (EFT) or saved fit, its skills
(the hull's and every item's) and its cost (less what the character owns)."""

import re

import pytest
from fastapi.testclient import TestClient

from eve_assistant import cli, explain, fittings, goals, skills, snapshots, store
from eve_assistant.web import create_app

MINING, BARGE = 1, 2  # skills
HULL, LASER, UPGRADE, CRYSTAL, DRONE = 100, 200, 201, 202, 203
FIT = """[Big Barge, Ice run]
Mining Upgrade
[Empty Low slot]

Strip Laser, Veld Crystal
Strip Laser/OFFLINE

Mining Drone x3
"""


def _db(path=":memory:"):
    conn = store.connect(path)
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, ?, ?, 1)",
        [(1, 16, "Skills"), (2, 6, "Ships"), (3, 7, "Modules")],
    )
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published) VALUES (?, ?, ?, 1)",
        [(MINING, "Mining", 1), (BARGE, "Mining Barge", 1), (HULL, "Big Barge", 2),
         (LASER, "Strip Laser", 3), (UPGRADE, "Mining Upgrade", 3), (CRYSTAL, "Veld Crystal", 3),
         (DRONE, "Mining Drone", 3)],
    )  # fmt: skip
    attributes = [(MINING, 275, 1.0), (BARGE, 275, 2.0)]
    for skill in (MINING, BARGE):
        attributes += [(skill, 180, 167), (skill, 181, 168)]
    attributes += [(HULL, 182, MINING), (HULL, 277, 2),  # the hull needs Mining 2
                   (LASER, 182, BARGE), (LASER, 277, 1),  # the laser needs Mining Barge 1,
                   (BARGE, 182, MINING), (BARGE, 277, 3)]  # which needs Mining 3  # fmt: skip
    conn.executemany("INSERT INTO sde_type_attributes VALUES (?, ?, ?)", attributes)
    conn.executemany(
        "INSERT INTO sde_clone_grade_skills VALUES (1, 'Alpha', ?, 5)", [(MINING,), (BARGE,)]
    )
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, NULL, 'x')",
        [(HULL, 10_000_000.0), (LASER, 1_000_000.0), (UPGRADE, 500_000.0), (DRONE, 20_000.0)],
    )  # the crystal has no price
    conn.execute(
        "INSERT INTO sde_meta (build_number, release_date, loaded_at) VALUES (1, 'x', 'y')"
    )
    snapshots.ensure_character(conn, 7, "Miner")
    for kind, payload in {
        "public": {"race_id": 1},
        "attributes": {"charisma": 20, "intelligence": 20, "memory": 20, "perception": 20,
                       "willpower": 20},
        "skills": {"total_sp": 1000, "skills": [{"skill_id": MINING, "trained_skill_level": 2,
                   "active_skill_level": 2, "skillpoints_in_skill": 1415}]},
        "skillqueue": [],
        "wallet": 1_000_000.0,
        "assets": [{"item_id": 1, "type_id": LASER, "quantity": 1, "location_id": 6,
                    "location_flag": "Hangar"}],
        "fittings": [{"fitting_id": 9, "name": "Ice run", "ship_type_id": HULL, "items": [
            {"type_id": LASER, "flag": "HiSlot0", "quantity": 1},
            {"type_id": LASER, "flag": "HiSlot1", "quantity": 1},
            {"type_id": DRONE, "flag": "DroneBay", "quantity": 3}]}],
    }.items():  # fmt: skip
        snapshots.record(conn, 7, kind, payload)
    conn.commit()
    return conn


@pytest.fixture
def db():
    conn = _db()
    yield conn
    conn.close()


def test_a_pasted_fit_is_read_line_by_line(db):
    fit = fittings.parse_eft(db, FIT)
    assert (fit.name, fit.ship_type_id, fit.source) == ("Ice run", HULL, "pasted")
    # The empty slot is skipped, the loaded crystal counted, /OFFLINE dropped, x3 counted.
    assert dict(fit.items) == {UPGRADE: 1, LASER: 2, CRYSTAL: 1, DRONE: 3}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Strip Laser\nMining Upgrade", "starts with a line like"),
        ("[Flying Brick, x]\nStrip Laser", "No ship is called 'Flying Brick'"),
        ("[Big Barge, x]\nStrip Laser\nLaser of Doom\nWarp Gizmo x2", "Laser of Doom, Warp Gizmo"),
    ],
)
def test_a_pasted_fit_that_cannot_be_read_says_why(db, text, message):
    with pytest.raises(fittings.FitError, match=message):
        fittings.parse_eft(db, text)


def test_a_saved_fit_is_found_by_name(db):
    fit = fittings.saved_by_name(db, 7, "  ice RUN ")
    assert (fit.source, dict(fit.items)) == ("saved", {LASER: 2})  # fitted modules only
    with pytest.raises(fittings.FitError, match="No saved fit called 'Gas run'"):
        fittings.saved_by_name(db, 7, "Gas run")


def test_a_fits_skills_are_the_hulls_and_every_items(db):
    character = skills.load_character_skills(db, 7)
    hull_only = skills.plan(db, character, HULL)
    assert hull_only.usable_now  # Mining 2 is trained
    fitted = skills.plan_fit(db, character, HULL, [LASER, UPGRADE])
    assert not fitted.usable_now
    # The laser needs Mining Barge 1, which needs Mining 3: prerequisites first.
    assert [(s.skill_name, s.level) for s in fitted.steps] == [("Mining", 3), ("Mining Barge", 1)]


def test_a_fitted_goal_costs_what_is_still_to_buy(db):
    goal_id = goals.add_goal(db, "fly_ship", 7, type_id=HULL, fit=fittings.parse_eft(db, FIT))
    goal = next(g for g in goals.active_goals(db) if g.goal_id == goal_id)
    assert goal.label == "Fly a Big Barge, fitted"
    status = goals.evaluate(db, goal)
    cost = status.fit_cost
    # One of the two lasers is in the hangar already; the crystal has no price.
    assert cost.hull == 10_000_000.0
    assert cost.items == pytest.approx(1_000_000 + 500_000 + 3 * 20_000)
    assert cost.owned == ("1 × Strip Laser",)
    assert cost.unpriced == ("Veld Crystal",)
    assert status.target_isk == pytest.approx(11_560_000)
    assert status.isk_needed == pytest.approx(10_560_000)  # less the wallet
    assert not status.requirement.usable_now  # the laser's skills
    text = explain.describe("fit_unpriced", next(r for r in status.reasons
                                                 if r.code == "fit_unpriced").detail)  # fmt: skip
    assert "Veld Crystal" in text


def test_a_fit_must_match_the_ship_and_only_ships_have_fits(db):
    fit = fittings.parse_eft(db, FIT)
    with pytest.raises(goals.GoalError, match="That fit is for a Big Barge, not a Strip Laser"):
        goals.add_goal(db, "fly_ship", 7, type_id=LASER, fit=fit)
    with pytest.raises(goals.GoalError, match="Only a ship goal"):
        goals.add_goal(db, "skill", 7, type_id=MINING, level=3, fit=fit)
    assert goals.fit_from_text(db, "", 7) is None
    with pytest.raises(goals.GoalError, match="Choose the character"):
        goals.fit_from_text(db, "Ice run", None)


def test_the_goals_page_takes_a_pasted_or_saved_fit(tmp_path):
    path = tmp_path / "g.sqlite3"
    _db(path).close()
    with TestClient(create_app(path), base_url="http://127.0.0.1") as client:
        page = client.get("/goals").text
        token = re.search(r'name="token" value="([^"]+)"', page).group(1)
        form = {"token": token, "kind": "ship", "character": "Miner", "target": ""}
        added = client.post("/goals", data={**form, "fit": FIT}).text
        assert "Fly a Big Barge, fitted" in added
        assert '<span class="player-text" title="Fit name chosen by you">“Ice run”</span>' in added
        assert "Items to buy" in added
        assert "1 × Strip Laser" in added
        saved = client.post("/goals", data={**form, "target": "Big Barge", "fit": "Ice run"}).text
        assert "saved in game" in saved
        bad = client.post("/goals", data={**form, "fit": "[Big Barge, x]\nWarp Gizmo"}).text
        assert "Not found in CCP&#39;s data: Warp Gizmo." in bad
        empty = client.post("/goals", data=form).text
        assert "Name the ship, or paste its fit." in empty


def test_the_cli_takes_a_fit_from_a_file(tmp_path, monkeypatch, capsys):
    path = tmp_path / "g.sqlite3"
    _db(path).close()
    real_connect = store.connect
    monkeypatch.setattr(cli.store, "connect", lambda *_a: real_connect(path))
    fit_file = tmp_path / "fit.txt"
    fit_file.write_text(FIT, encoding="utf-8")
    assert cli.main(["goal", "add", "ship", "Big Barge", "--character", "Miner",
                     "--fit", str(fit_file)]) == 0  # fmt: skip
    assert cli.main(["goals"]) == 0
    out = capsys.readouterr().out
    assert 'Fit "Ice run" (your name for it): 11,560,000 ISK to buy' in out
    assert "already yours: 1 × Strip Laser" in out
