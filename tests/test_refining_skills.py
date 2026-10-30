"""Refining skills in "what to train next" (roadmap 8.3): valued at the ore the
character actually mines, the better of selling it raw or refining it."""

import datetime

import pytest
from fastapi.testclient import TestClient

from eve_assistant import snapshots, store, train_next
from eve_assistant.web import create_app

REPRO, EFFICIENCY, SIMPLE = 3385, 3389, 60377
ORE, TRIT, NEPHRITE = 1230, 34, 90000
TODAY = "2026-10-29"


def _db(path=":memory:", raw=10.5, alpha=False):
    conn = store.connect(path)
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, ?, ?, 1)",
        [(1, 16, "Skills"), (2, 25, "Veldspar"), (3, 4, "Mineral")],
    )
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published, portion_size) "
        "VALUES (?, ?, ?, 1, ?)",
        [(REPRO, "Reprocessing", 1, 1), (EFFICIENCY, "Reprocessing Efficiency", 1, 1),
         (SIMPLE, "Simple Ore Processing", 1, 1), (ORE, "Veldspar", 2, 100),
         (TRIT, "Tritanium", 3, 1), (NEPHRITE, "Nephrite", 2, 100)],
    )  # fmt: skip
    attributes = [(ORE, 790, SIMPLE)]  # the ore's processing skill
    for skill in (REPRO, EFFICIENCY, SIMPLE):
        attributes += [(skill, 275, 1.0), (skill, 180, 167), (skill, 181, 168)]
    conn.executemany("INSERT INTO sde_type_attributes VALUES (?, ?, ?)", attributes)
    conn.execute("INSERT INTO sde_type_materials VALUES (?, ?, 400)", (ORE, TRIT))
    caps = [(REPRO, 3), (EFFICIENCY, 0), (SIMPLE, 0)] if alpha else [(REPRO, 5)]
    conn.executemany("INSERT INTO sde_clone_grade_skills VALUES (1, 'Alpha', ?, ?)", caps)
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, NULL, 'x')",
        [(ORE, raw), (TRIT, 5.0), (NEPHRITE, 50.0), (REPRO, 100_000.0)],
    )  # 400 Tritanium a portion of 100: 20 ISK of minerals a unit at 100%
    conn.execute(
        "INSERT INTO sde_meta (build_number, release_date, loaded_at) VALUES (1, 'x', 'y')"
    )
    conn.execute("INSERT INTO accounts (account_id, label, clone_state) VALUES (1, 'main', ?)",
                 ("alpha" if alpha else "omega",))  # fmt: skip
    snapshots.ensure_character(conn, 7, "Miner")
    conn.execute("UPDATE characters SET account_id = 1 WHERE character_id = 7")
    for kind, payload in {
        "public": {"race_id": 1},
        "attributes": {"charisma": 20, "intelligence": 20, "memory": 20, "perception": 20,
                       "willpower": 20},
        "skills": {"total_sp": 1000, "skills": []},
        "skillqueue": [],
        "mining_ledger": [
            {"date": "2026-10-20", "quantity": 30_000, "type_id": ORE, "solar_system_id": 1},
            {"date": "2026-09-01", "quantity": 99_999, "type_id": ORE, "solar_system_id": 1},
        ],
    }.items():  # fmt: skip
        snapshots.record(conn, 7, kind, payload)
    conn.commit()
    return conn


def test_a_weeks_ore_comes_from_the_last_30_days_of_the_ledger():
    db = _db()
    assert train_next.weekly_ore(db, 7, TODAY) == {ORE: pytest.approx(30_000 * 7 / 30)}


def test_when_selling_raw_pays_more_the_outlook_says_what_training_would_change():
    # Refined now: 20 x 50% x 95% = 9.5 a unit, under 10.5 raw. One level more
    # gives at most 9.5 x 1.03 = 9.8, still under.
    db = _db(raw=10.5)
    outlook = train_next.refining_outlook(db, 7, TODAY)
    assert not outlook.refines_now
    assert outlook.now_a_week == pytest.approx(7_000 * 10.5)
    # Every skill at 5: 50% x 1.15 x 1.10 x 1.10 x 95% = 66.1% of 20 ISK = 13.2 a unit.
    best = 7_000 * 20 * 0.5 * 1.15 * 1.10 * 1.10 * 0.95
    assert outlook.best_a_week == pytest.approx(best)
    assert outlook.targets == (("Reprocessing", 5), ("Reprocessing Efficiency", 5),
                               ("Simple Ore Processing", 5))  # fmt: skip
    assert outlook.training_minutes > 0
    assert train_next.refining_suggestions(db, 7, today=TODAY) == []  # no single level does it


def test_when_refining_pays_each_level_is_worth_isk_a_week():
    db = _db(raw=5.0)  # refining already beats selling raw
    outlook = train_next.refining_outlook(db, 7, TODAY)
    assert outlook.refines_now
    found = train_next.refining_suggestions(db, 7, today=TODAY)
    first = found[0]
    assert (first.skill, first.level) == ("Reprocessing", 1)  # 3% a level, the most
    assert first.isk_a_week == pytest.approx(7_000 * 20 * 0.5 * 0.95 * 0.03)
    assert first.book_price == 100_000.0
    assert first.payback_weeks == pytest.approx(100_000.0 / first.isk_a_week)
    assert not first.starts_refining


def test_an_alpha_only_counts_levels_it_can_train():
    db = _db(raw=10.5, alpha=True)  # Alpha: Reprocessing up to 3, the others not at all
    outlook = train_next.refining_outlook(db, 7, TODAY)
    assert outlook.targets == (("Reprocessing", 3),)
    assert not outlook.needs_omega
    # Reprocessing 3 alone: 9.5 x 1.09 = 10.36 a unit, still under 10.5 raw.
    assert outlook.gain_a_week == 0
    assert outlook.best_a_week == outlook.now_a_week


def test_docked_at_a_station_that_refines_uses_its_yield_and_tax():
    db = _db(raw=5.0)
    db.execute("INSERT INTO sde_npc_stations VALUES (60003760, 1, 1, 1, 0.55, 0.02)")
    snapshots.record(db, 7, "location", {"solar_system_id": 1, "station_id": 60003760})
    outlook = train_next.refining_outlook(db, 7, TODAY)
    assert outlook.station_yield == 0.55
    assert outlook.now_a_week == pytest.approx(7_000 * 20 * 0.55 * 0.98)


def test_ore_that_cannot_be_refined_gets_no_outlook():
    db = _db()
    snapshots.record(
        db,
        7,
        "mining_ledger",
        [{"date": "2026-10-20", "quantity": 500, "type_id": NEPHRITE, "solar_system_id": 1}],
    )
    assert train_next.refining_outlook(db, 7, TODAY) is None


class _Today(datetime.date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 29)


def test_the_skills_page_shows_refining(tmp_path, monkeypatch):
    path = tmp_path / "r.sqlite3"
    _db(path).close()
    monkeypatch.setattr(train_next, "date", _Today)  # the ledger is from October 2026
    with TestClient(create_app(path), base_url="http://127.0.0.1") as client:
        page = client.get("/skills?character=Miner").text
    assert "Refining your ore · Miner" in page
    assert "about 7,000 units of ore a week" in page
    assert "selling it raw pays more than refining it" in page
    assert "<strong>Reprocessing Efficiency 5</strong>" in page
