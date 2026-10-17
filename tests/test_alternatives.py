"""Alternatives that explain themselves, and one way to write ISK."""

import pytest

from eve_assistant import explain, numbers, planner, risk, snapshots, store
from eve_assistant.skills import Reason

CHAR = 7


@pytest.mark.parametrize(
    ("value", "shown"),
    [(2_460_000_000, "2.46B"), (1_530_000_000, "1.53B"), (12_345_678, "12.3M"),
     (928_900_000, "929M"), (3_600_000, "3.6M"), (640_000, "640k"), (950, "950"),
     (999_600, "1M"), (999_960_000, "1B"), (-1_200_000, "-1.2M"), (None, "?")],
)  # fmt: skip
def test_isk_has_one_format(value, shown):
    assert numbers.isk(value) == shown


def test_per_hour():
    assert numbers.per_hour(1_000_000, 30) == pytest.approx(2_000_000)
    assert numbers.per_hour(1_000_000, 0) is None


@pytest.fixture
def db():
    conn = store.connect(":memory:")
    snapshots.ensure_character(conn, CHAR, "Miner")
    yield conn
    conn.close()


def _item(kind, title, isk, minutes, loss=0.0, inputs=None):
    exposure = loss * 50 if loss else 1e5
    rated = risk.rate("low", exposure, 1e9)
    rated = risk.RiskRating(**{**rated.__dict__, "expected_loss": loss})
    return planner.PlanItem(kind=kind, key=title, title=title, isk=isk, minutes=minutes,
                            risk=rated, inputs=inputs or {})  # fmt: skip


def _stored(db, items, alternatives) -> planner.StoredPlan:
    plan = planner.Plan(
        character_id=CHAR,
        minutes_available=60,
        items=tuple(items),
        alternatives=tuple(planner.Alternative(item, why) for item, why in alternatives),
        notes=(Reason("loss_chances_are_guesses"),),
        net_worth=1e9,
    )
    return planner.load_plan(db, planner.record(db, plan, {}).plan_id)


STACK = {"station_id": 60003760, "type_id": 1230}


def test_selling_the_same_stack_elsewhere(db):
    stored = _stored(
        db,
        [_item("sell_hangar", "Sell here", 3_600_000, 4, inputs={**STACK, "jumps": 0}),
         _item("mining", "Mine", 1_600_000, 56)],
        [(_item("sell_hangar", "Sell at Jita", 3_900_000, 19,
                inputs={**STACK, "jumps": 13}), "worse")],
    )  # fmt: skip
    reason = planner.why_not(stored, stored.alternatives[0])
    assert reason.code == "same_stack"
    assert reason.detail["step"] == 1
    text = explain.describe(reason.code, reason.detail)
    assert text == (
        "Step 1 already sells this, 3.6M in 4 min. This way earns 300k more but takes "
        "15 min longer: 1.2M an hour for the extra time."
    )


def test_losing_to_the_main_activity(db):
    stored = _stored(
        db,
        [_item("mining", "Mine Veldspar", 1_600_000, 56)],
        [(_item("mining", "Mine Plagioclase", 1_400_000, 56), "worse"),
         (_item("courier", "Courier to Jita", 2_000_000, 40, loss=900_000), "worse")],
    )  # fmt: skip
    reasons = {a.title: planner.why_not(stored, a) for a in stored.alternatives}
    plain, riskier = reasons["Mine Plagioclase"], reasons["Courier to Jita"]
    assert explain.describe(plain.code, plain.detail) == (
        "Step 1 earns more an hour after expected losses: 1.71M against 1.5M."
    )
    assert explain.describe(riskier.code, riskier.detail) == (
        "Step 1 earns more an hour after expected losses: 1.71M against 1.65M; "
        "this one's expected loss is 900k."
    )


def test_better_per_hour_but_less_in_the_time_you_have(db):
    stored = _stored(
        db,
        [_item("mining", "Mine Veldspar", 1_600_000, 56)],
        [(_item("courier", "Short courier", 1_200_000, 20), "worse")],
    )
    reason = planner.why_not(stored, stored.alternatives[0])
    assert explain.describe(reason.code, reason.detail) == (
        "Earns more an hour than step 1 (3.6M against 1.71M), but step 1 earns more in "
        "the time you have: 1.6M against 1.2M."
    )


def test_other_reasons_keep_their_words(db):
    stored = _stored(
        db,
        [_item("mining", "Mine", 1_600_000, 56)],
        [(_item("courier", "Long haul", 9_000_000, 120), "too_long"),
         (_item("courier", "Lowsec run", 9_000_000, 30), "red_risk")],
    )  # fmt: skip
    reasons = {a.title: planner.why_not(stored, a) for a in stored.alternatives}
    long_haul, lowsec = reasons["Long haul"], reasons["Lowsec run"]
    assert explain.describe(long_haul.code, long_haul.detail) == "Takes 120 min; you have 60."
    assert explain.describe(lowsec.code, lowsec.detail).startswith("Rated Risky.")
