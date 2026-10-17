"""The risk matrix: how likely a loss is, against how much is exposed.

Design: docs/design.md section 4.8. Exposure is measured as a share of the
character's net worth, so losing 20M means more to a new character than to
an established one. The chance bands come from route security for now;
kill data (zKillboard) refines them in phase 4.

ALL loss probabilities below are starting guesses, flagged as such in every
result, and are meant to be replaced by learned values.
"""

import sqlite3
from dataclasses import dataclass

from eve_assistant import goals, snapshots, worth

# "none": Exordium, where PvP is switched off, so other players cannot take anything.
BANDS = ("none", "very_low", "low", "medium", "high")

# Starting guesses: chance of losing what is exposed, per trip or session.
LOSS_CHANCE = {"none": 0.0, "very_low": 0.001, "low": 0.005, "medium": 0.03, "high": 0.10}

# Exposed share of net worth: upper bounds of the four columns.
SHARE_COLUMNS = (0.02, 0.10, 0.25)

MATRIX = {
    "none": ("green", "green", "green", "green"),
    "very_low": ("green", "green", "amber", "red"),
    "low": ("green", "amber", "amber", "red"),
    "medium": ("amber", "amber", "red", "red"),
    "high": ("amber", "red", "red", "red"),
}


@dataclass(frozen=True)
class RiskRating:
    band: str
    rating: str  # green, amber, red
    exposure: float
    share_of_net_worth: float
    expected_loss: float

    def as_dict(self) -> dict:
        return {
            "band": self.band,
            "rating": self.rating,
            "exposure": self.exposure,
            "share_of_net_worth": self.share_of_net_worth,
            "expected_loss": self.expected_loss,
            "chance_is_a_guess": True,
        }


def band_for_security(lowest_security: float | None, region_name: str | None = None) -> str:
    if region_name == "Exordium":
        return "none"  # PvP is switched off there
    if lowest_security is None:
        return "high"
    rounded = round(lowest_security, 1)
    if rounded >= 0.5:
        return "low"
    if rounded > 0.0:
        return "medium"
    return "high"


def rate(band: str, exposure: float, net_worth: float) -> RiskRating:
    if net_worth > 0:
        share = exposure / net_worth
    elif exposure > 0:
        share = 1.0
    else:
        share = 0.0
    column = sum(share > limit for limit in SHARE_COLUMNS)
    return RiskRating(
        band=band,
        rating=MATRIX[band][column],
        exposure=exposure,
        share_of_net_worth=share,
        expected_loss=LOSS_CHANCE[band] * exposure,
    )


def item_value(db: sqlite3.Connection, type_id: int) -> float:
    return goals.price_of(db, type_id) or 0.0


def net_worth(db: sqlite3.Connection, character_id: int) -> float:
    """Wallet plus assets at CCP's average prices (rough, but consistent)."""
    wallet = float(snapshots.latest(db, character_id, "wallet") or 0.0)
    return wallet + worth.asset_value(db, snapshots.latest(db, character_id, "assets") or [])


def ship_exposure(db: sqlite3.Connection, character_id: int) -> float:
    """The active ship's hull plus everything inside it."""
    ship = snapshots.latest(db, character_id, "ship") or {}
    if not ship:
        return 0.0
    assets = snapshots.latest(db, character_id, "assets") or []
    inside = sum(
        item_value(db, a["type_id"]) * a.get("quantity", 1)
        for a in assets
        if a.get("location_id") == ship.get("ship_item_id")
    )
    return item_value(db, ship["ship_type_id"]) + inside
