"""What Omega would change: mining with the ships and skills a character
already has, as Omega rather than Alpha, against what Omega costs in PLEX.

Mining speed comes from the static data (the published figures, before the
learned correction), with each owned ship as it's fitted now. ISK per m3 is
what the character's latest plan priced its ore at (buyers' prices after
tax, as plans value ore); without a plan, only the m3 figures are given.
"""

import dataclasses
import json
import sqlite3
from dataclasses import dataclass

from eve_assistant import fleet, goals, mining, skills

PLEX_TYPE_ID = goals.PLEX_TYPE_ID
PLEX_PER_MONTH = goals.OMEGA_PLEX_PER_MONTH
WEEKS_A_MONTH = 52 / 12


@dataclass(frozen=True)
class Mining:
    ship: str
    m3_per_minute: float


@dataclass(frozen=True)
class OmegaComparison:
    character_name: str
    alpha: Mining | None  # best mining ship flyable now
    omega: Mining | None  # best mining ship flyable as Omega
    isk_per_m3: float | None  # from the latest plan; None without one
    plex_price: float | None

    def per_hour(self, which: Mining | None) -> float | None:
        if which is None or self.isk_per_m3 is None:
            return None
        return which.m3_per_minute * 60 * self.isk_per_m3

    @property
    def gain_per_hour(self) -> float | None:
        omega, alpha = self.per_hour(self.omega), self.per_hour(self.alpha) or 0.0
        return None if omega is None else omega - alpha

    @property
    def month_cost(self) -> float | None:
        return self.plex_price * PLEX_PER_MONTH if self.plex_price else None

    @property
    def hours_to_pay(self) -> float | None:
        """Hours of mining a month at which Omega's extra pays for itself."""
        gain, cost = self.gain_per_hour, self.month_cost
        return cost / gain if gain and gain > 0 and cost else None

    def extra_a_month(self, hours_a_week: float) -> float | None:
        """What Omega would add in a month of mining at this play time."""
        gain = self.gain_per_hour
        return gain * hours_a_week * WEEKS_A_MONTH if gain is not None else None


def _best(db, character: skills.CharacterSkills, ships, implants) -> Mining | None:
    best = None
    for ship in ships:
        if not skills.plan(db, character, ship.type_id).usable_now:
            continue
        try:
            found = mining.estimate(db, character, ship.type_id, list(ship.fitted), implants)
        except mining.MiningError:
            continue
        if found.m3_per_minute > 0 and (best is None or found.m3_per_minute > best.m3_per_minute):
            best = Mining(found.ship_name, found.m3_per_minute)
    return best


def isk_per_m3(db: sqlite3.Connection, character_id: int) -> float | None:
    """What the character's latest mining plan step valued its ore at, per m3."""
    row = db.execute(
        "SELECT i.isk, i.details FROM plan_items i JOIN plans p USING (plan_id) "
        "WHERE p.character_id = ? AND i.kind = 'mining' AND i.position IS NOT NULL "
        "ORDER BY p.plan_id DESC LIMIT 1",
        (character_id,),
    ).fetchone()
    if row is None:
        return None
    inputs = json.loads(row["details"]).get("inputs", {})
    volume = db.execute(
        "SELECT volume FROM sde_types WHERE type_id = ?", (inputs.get("type_id"),)
    ).fetchone()
    if not volume or not volume["volume"] or not inputs.get("quantity"):
        return None
    return row["isk"] / (inputs["quantity"] * volume["volume"])


def compare(db: sqlite3.Connection, character_id: int) -> OmegaComparison | None:
    """Alpha against Omega for one character; None if it can't mine at all."""
    try:
        character = skills.load_character_skills(db, character_id)
        ships = fleet.owned_ships(db, character_id)
        _ship, _fitted, implants = mining.current_fit(db, character_id)
    except (skills.SkillsError, mining.MiningError):
        return None
    as_omega = dataclasses.replace(character, clone_state="omega")
    alpha = _best(db, character, ships, implants) if character.is_alpha else None
    omega = _best(db, as_omega, ships, implants)
    if omega is None:
        return None
    name = db.execute(
        "SELECT name FROM characters WHERE character_id = ?", (character_id,)
    ).fetchone()
    return OmegaComparison(
        character_name=name["name"] if name else str(character_id),
        alpha=alpha,
        omega=omega,
        isk_per_m3=isk_per_m3(db, character_id),
        plex_price=goals.price_of(db, PLEX_TYPE_ID),
    )
