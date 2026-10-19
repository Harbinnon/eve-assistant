"""Planetary Interaction (roadmap 6.3): colonies, extractor timers, what they
make, restarting extractors in plans, and PI in the Omega comparison."""

import dataclasses
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from eve_assistant import config, esi, explain, omega, planets, planner, snapshots, store
from eve_assistant.activities import planets as planet_steps

NOW = datetime.fromisoformat("2026-10-19T12:00:00+00:00")
HOME, AWAY = 30000001, 30000002
PLANET, FAR_PLANET = 40000002, 40000020
WATER, FUEL = 2268, 2390  # a raw resource (P0) and what a basic factory makes of it (P1)
SCHEMATIC = 121


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    conn.executemany(
        "INSERT INTO sde_solar_systems VALUES (?, ?, 1, 1, 0.9, 'B')",
        [(HOME, "Home"), (AWAY, "Away")],
    )
    conn.execute("INSERT INTO sde_regions VALUES (1, 'The Forge')")
    conn.executemany("INSERT INTO sde_jumps VALUES (?, ?)", [(HOME, AWAY), (AWAY, HOME)])
    conn.executemany(
        "INSERT INTO sde_planets VALUES (?, ?, ?, 11)", [(PLANET, HOME, 4), (FAR_PLANET, AWAY, 2)]
    )
    conn.executemany(
        "INSERT INTO sde_groups VALUES (?, ?, ?, 1)",
        [(1032, 42, "Planet Solid - Raw Resource"), (1042, 43, "Basic Commodities - Tier 1")],
    )
    conn.executemany(
        "INSERT INTO sde_types (type_id, name, group_id, published) VALUES (?, ?, ?, 1)",
        [(WATER, "Aqueous Liquids", 1032), (FUEL, "Water", 1042)],
    )
    conn.execute("INSERT INTO sde_planet_schematics VALUES (?, 'Water', 1800)", (SCHEMATIC,))
    conn.executemany(
        "INSERT INTO sde_planet_schematic_types VALUES (?, ?, ?, ?)",
        [(SCHEMATIC, WATER, 3000, 1), (SCHEMATIC, FUEL, 20, 0)],
    )
    conn.executemany(
        "INSERT INTO market_prices VALUES (?, ?, NULL, 'x')", [(WATER, 2.0), (FUEL, 1000.0)]
    )
    snapshots.ensure_character(conn, 7, "Farmer")
    snapshots.record(conn, 7, "location", {"solar_system_id": HOME, "station_id": 60000001})
    yield conn
    conn.close()


def _colony(db, expires: datetime, planet_id=PLANET, system=HOME, with_factory=True):
    installed = expires - timedelta(hours=48)
    pins = [
        {"pin_id": 1, "type_id": 2848, "install_time": _stamp(installed),
         "expiry_time": _stamp(expires),
         "extractor_details": {"product_type_id": WATER, "qty_per_cycle": 1000,
                               "cycle_time": 1800, "heads": [{}, {}]}},
        {"pin_id": 3, "type_id": 2541, "contents": [{"type_id": FUEL, "amount": 100}]},
    ]  # fmt: skip
    if with_factory:
        pins.append({"pin_id": 2, "type_id": 2473, "schematic_id": SCHEMATIC})
    snapshots.record(db, 7, "planets", [{
        "planet_id": planet_id, "planet_type": "barren", "solar_system_id": system,
        "upgrade_level": 0, "num_pins": len(pins), "owner_id": 7}])  # fmt: skip
    snapshots.record(db, 7, planets.LAYOUTS, {str(planet_id): {"pins": pins}})


def test_the_scope_is_asked_for_only_when_switched_on(tmp_path):
    assert esi.PLANETS_SCOPE not in esi.scopes()
    assert esi.scopes(planets=True)[-1] == "esi-planets.manage_planets.v1"
    path = tmp_path / "local.toml"
    path.write_text('[esi]\nclient_id = "abc"\nplanets = true\n', encoding="utf-8")
    assert config.load_config(path).planets
    path.write_text('[esi]\nclient_id = "abc"\nplanets = "yes"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="planets"):
        config.load_config(path)


def test_layouts_are_read_after_the_colony_list(db):
    _colony(db, NOW + timedelta(hours=5))

    class FakeEsi:
        calls = []

        def get(self, path, character_id=None, **_kwargs):
            self.calls.append((path, character_id))
            return SimpleNamespace(data={"pins": [{"pin_id": 9}], "links": [], "routes": []})

    fake = FakeEsi()
    planets.refresh(db, fake, 7)
    assert fake.calls == [(f"/characters/7/planets/{PLANET}", 7)]
    assert snapshots.latest(db, 7, planets.LAYOUTS) == {str(PLANET): {"pins": [{"pin_id": 9}]}}


def test_a_colony_turns_raw_resources_into_goods(db):
    _colony(db, NOW + timedelta(hours=5))
    colony = planets.colonies(db, 7)[0]
    assert (colony.name, colony.system_name) == ("Home IV", "Home")
    extractor = colony.extractors[0]
    assert extractor.per_day == 48_000  # 1,000 every 30 minutes
    assert extractor.program_hours == 48
    # One factory could take 3,000 every 30 minutes (144,000 a day): it gets 48,000.
    assert planets.made_a_day(db, colony, NOW) == pytest.approx({FUEL: 320.0})
    worth, tax = planets.value(db, {FUEL: 320.0})
    assert (worth, tax) == pytest.approx((320_000.0, 320 * 500 * 0.1))
    assert planets.a_day(db, colony, NOW) == pytest.approx(304_000.0)
    assert planets.waiting(db, colony) == pytest.approx(100 * 1000 - 100 * 500 * 0.1)


def test_without_a_factory_the_raw_resource_is_what_it_makes(db):
    _colony(db, NOW + timedelta(hours=5), with_factory=False)
    colony = planets.colonies(db, 7)[0]
    assert planets.made_a_day(db, colony, NOW) == {WATER: 48_000}
    assert planets.a_day(db, colony, NOW) == pytest.approx(48_000 * (2.0 - 5 * 0.1))


def test_timers_say_when_extractors_stop(db):
    _colony(db, NOW + timedelta(hours=5))
    timer = planets.timers(db, 7, NOW)[0]
    assert (timer.planet, timer.stops, timer.stopped) == ("Home IV", NOW + timedelta(hours=5), 0)
    later = planets.timers(db, 7, NOW + timedelta(hours=8))[0]
    assert (later.stops, later.stopped, later.stopped_at) == (None, 1, NOW + timedelta(hours=5))
    assert later.a_day == 0.0  # nothing runs


def test_a_stopped_extractor_here_is_a_quick_job(db):
    _colony(db, NOW - timedelta(hours=3))
    item = planet_steps.planet_candidates(db, 7, 60, 1e9, True, now=NOW)[0]
    assert item.title == "Restart the extractor on Home IV"
    assert item.minutes == planet_steps.MINUTES_TO_RESTART
    assert item.isk == pytest.approx(304_000.0 * 48 / 24)  # one more 48-hour program
    assert item.risk.band == "very_low"
    assert item.risk.exposure == 0.0
    texts = [explain.describe(r.code, r.detail) for r in item.reasons]
    assert texts[0] == (
        "An extractor has stopped on Home IV, 3 hours ago: while stopped, the colony makes "
        "nothing new."
    )
    assert "Remote Sensing, an Omega skill" in texts[2]


def test_one_stopping_during_the_session_counts_but_not_later(db):
    _colony(db, NOW + timedelta(minutes=30))
    assert planet_steps.planet_candidates(db, 7, 60, 1e9, True, now=NOW)
    assert not planet_steps.planet_candidates(db, 7, 20, 1e9, True, now=NOW)


def test_a_planet_elsewhere_means_a_trip(db):
    _colony(db, NOW - timedelta(hours=1), planet_id=FAR_PLANET, system=AWAY)
    item = planet_steps.planet_candidates(db, 7, 60, 1e9, True, now=NOW)[0]
    assert item.title == "Restart the extractor on Away II (1 jump away)"
    assert item.inputs["jumps"] == 1
    assert item.minutes > planet_steps.MINUTES_TO_RESTART + 1


def test_the_planner_takes_a_restart_here_as_a_quick_job(db):
    _colony(db, NOW - timedelta(hours=3))
    here = planet_steps.planet_candidates(db, 7, 60, 1e9, True, now=NOW)[0]
    fit = planner._Fitting(30, 30, set())
    planner._quick_jobs(fit, [here])
    assert fit.chosen == [here]
    away = dataclasses.replace(here, inputs={**here.inputs, "jumps": 2})
    elsewhere = planner._Fitting(30, 30, set())
    planner._quick_jobs(elsewhere, [away])
    assert elsewhere.chosen == []  # a trip: it competes as the main activity instead
    assert planner._main_pool(elsewhere, [away, here], 0) == [away]


def test_omega_counts_five_more_planets_like_your_best(db):
    _colony(db, NOW + timedelta(hours=5))
    comparison = omega.OmegaComparison("Farmer", None, None, None, None, 304_000.0, 1)
    assert comparison.extra_planets == 5
    assert comparison.planets_a_month == pytest.approx(304_000.0 * 5 * 365 / 12)
    assert omega.OmegaComparison("Farmer", None, None, None, None).planets_a_month is None
