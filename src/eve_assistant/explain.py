"""Plain-English text for the structured reasons the service layer returns.

Reasons are stored as codes plus numbers so they can be checked and learned
from; this turns them into sentences for the dashboard and CLI. Unknown
codes fall back to the code itself, so nothing is hidden.
"""

from eve_assistant import numbers, skills
from eve_assistant.skills import Reason

WHY_NOT = {
    "worse": "Less ISK after expected losses than what was chosen.",
    "too_long": "Does not fit in the time you have.",
    "red_risk": "Rated Risky. Tick “Include risky options” to allow it.",
    "muted": "You muted this kind of suggestion.",
    "new_limit": "Only one new activity per plan.",
    "too_many_projects": "You already have the most open projects; finish or drop one first.",
}


LEARNED = {"mining_rate": "Mining speed"}
RISK_WORTH_MENTIONING = 0.05  # of an option's ISK


def _same_stack(d: dict) -> str:
    step, isk = d["step"], numbers.isk
    extra_isk = d["isk"] - d["rival_isk"]
    extra_minutes = d["minutes"] - d["rival_minutes"]
    if extra_isk > 0 and extra_minutes > 0:
        return (
            f"Step {step} already sells this, {isk(d['rival_isk'])} in "
            f"{d['rival_minutes']:.0f} min. "
            f"This way earns {isk(extra_isk)} more but takes {extra_minutes:.0f} min longer: "
            f"{isk(numbers.per_hour(extra_isk, extra_minutes))} an hour for the extra time."
        )
    return (
        f"Step {step} already sells this, for more: {isk(d['rival_isk'])} against {isk(d['isk'])}."
    )


def _less_after_losses(d: dict) -> str:
    """Compared per hour after expected losses: a mining step in the plan is
    cut to the time left, so totals over different minutes would mislead."""
    step, isk = d["step"], numbers.isk
    mine = numbers.per_hour(d["isk"] - d["loss"], d["minutes"]) or 0.0
    theirs = numbers.per_hour(d["rival_isk"] - d["rival_loss"], d["rival_minutes"]) or 0.0
    if mine > theirs:
        return (
            f"Earns more an hour than step {step} ({isk(mine)} against {isk(theirs)}), but "
            f"step {step} earns more in the time you have: "
            f"{isk(d['rival_isk'] - d['rival_loss'])} against {isk(d['isk'] - d['loss'])}."
        )
    # Mention the expected loss only when it matters (5% of the ISK or more).
    risky = d["loss"] >= RISK_WORTH_MENTIONING * max(d["isk"], 1)
    risk = f"; this one's expected loss is {isk(d['loss'])}" if risky else ""
    return (
        f"Step {step} earns more an hour after expected losses: "
        f"{isk(theirs)} against {isk(mine)}{risk}."
    )


def describe(code: str, detail: dict | None = None) -> str:
    d = detail or {}
    texts = {
        "same_stack": lambda: _same_stack(d),
        "less_after_losses": lambda: _less_after_losses(d),
        "too_long": lambda: (
            f"Takes {d.get('minutes', 0):.0f} min; you have {d.get('available', 0):.0f}."
            if d
            else WHY_NOT["too_long"]
        ),
        "alpha_cap": lambda: skills.describe(Reason(code, d)),
        "alpha_sp_limit": lambda: skills.describe(Reason(code, d)),
        "trained_but_alpha_locked": lambda: skills.describe(Reason(code, d)),
        "assumed_minutes_per_jump": lambda: (
            f"Travel time assumes about {d.get('minutes', 1):g} minute per jump until the tool "
            "learns your pace."
        ),
        "not_enough_buyers": lambda: (
            f"Not enough buyers for {d.get('unsold', 0):,} units; they would be left over."
        ),
        "waits_for_buyers": lambda: (
            f"A sell order waits for buyers; {d.get('competing_units', 0):,} units already "
            f"listed. Broker fee {d.get('broker_fee', 0):.1%}."
        ),
        "leftover_not_refined": lambda: (
            f"{d.get('units', 0):,} units are below one refining batch "
            f"({d.get('portion', 100)}) and are sold as ore."
        ),
        "skills_used": lambda: (
            "Skills used: Accounting {accounting}, Broker Relations {broker_relations}, "
            "Reprocessing {reprocessing}, Reprocessing Efficiency {reprocessing_efficiency}, "
            "ore processing {ore_processing}.".format(
                **{
                    k: d.get(k, 0)
                    for k in (
                        "accounting",
                        "broker_relations",
                        "reprocessing",
                        "reprocessing_efficiency",
                        "ore_processing",
                    )
                }
            )  # fmt: skip
        ),
        "exordium_fees": lambda: (
            "Exordium adds a 5% fee to market orders; selling elsewhere avoids it."
        ),
        "mining_estimate": lambda: (
            f"Estimated {d.get('m3_per_minute', 0):,.0f} m3 per minute in your "
            f"{d.get('ship', 'ship')}, from the static data."
        ),
        "learned_correction": lambda: (
            f"{LEARNED.get(d.get('what'), d.get('what'))}: adjusted ×{d.get('factor', 1):.2f} "
            f"from {d.get('sessions', 0)} of your sessions (you averaged "
            f"×{d.get('average_ratio', 1):.2f} of the estimate; the adjustment grows as "
            "more sessions agree)."
        ),
        "not_learned_yet": lambda: (
            f"{LEARNED.get(d.get('what'), d.get('what'))}: not yet checked against your real "
            "sessions, so this is the published figure."
            + (
                " It learns from the sessions you record while EVE's game logs are being read."
                if d.get("what") == "mining_rate"
                else ""
            )
        ),
        "better_saved_fit": lambda: (
            f"What if: your saved fit \u201c{d.get('fit_name', '')}\u201d (your own name for it) "
            f"would mine about {d.get('with_fit', 0):,.0f} m3 per minute instead of "
            f"{d.get('now', 0):,.0f}. "
            + (
                f"Not where the ship is: {', '.join(d['missing'])}."
                if d.get("missing")
                else "Everything it needs is on the ship or in that hangar."
            )
        ),
        "not_modelled": lambda: f"Not counted: {', '.join(d.get('items', []))}.",
        "crystals_not_modelled": lambda: "Mining crystals are not counted yet.",
        "modules_not_usable": lambda: (
            f"Left out, skills missing or Alpha-locked: {', '.join(d.get('modules', []))}."
        ),
        "ship_not_usable": lambda: f"You cannot fly a {d.get('ship', 'this ship')} yet.",
        "no_mining_modules": lambda: "No mining modules fitted.",
        "collateral_large_share": lambda: f"Collateral is {d.get('share', 0):.0%} of your wallet.",
        "what_if_ship": lambda: f"Assumes you had a {d.get('ship')}.",
        "ores_from_mining_ledger": lambda: (
            "Ores chosen from what you mined in this system recently."
        ),
        "ores_assumed": lambda: (
            f"No mining here on record; assumed common ores: {', '.join(d.get('ores', []))}."
        ),
        "loss_chances_are_guesses": lambda: (
            "Loss chances are starting guesses until kill data and your own history refine them."
        ),
        "no_couriers_fit": lambda: "No courier contracts fit your ships, wallet and time.",
        "selling_at_nearby_station": lambda: (
            "You aren't docked where you can sell, so selling prices are for the nearest NPC "
            "station in this system."
        ),
        "no_npc_station_here": lambda: "No NPC station in this system to sell at.",
        "no_mining_ship": lambda: "Your current ship has no mining modules.",
        "no_station_to_sell": lambda: "No NPC station nearby to sell ore at.",
        "mining_unavailable": lambda: f"Mining not considered: {d.get('why', '')}",
        "couriers_unavailable": lambda: f"Couriers not considered: {d.get('why', '')}",
        "nothing_fits": lambda: "Nothing fits in this amount of time.",
        "ore_stored_elsewhere": lambda: (
            f"This ore is stored in {d.get('system')}, {d.get('jumps', 0)} jumps away."
            if d.get("jumps")
            else f"This ore is stored at another station in {d.get('system')}."
        ),
        "ore_in_structure_skipped": lambda: (
            "Ore in a structure this character can't access (or not looked up yet) is skipped."
        ),
        "selling_at_structure_here": lambda: (
            "You're docked at a player structure: selling there counts its own market as well."
        ),
        "selling_at_structure": lambda: (
            "Sold at a player structure: its own market is counted along with region-wide orders."
        ),
        "refining_at_structure_not_modelled": lambda: (
            "Refining at a player structure isn't offered: its refinery bonuses aren't public."
        ),
        "structure_market_unavailable": lambda: (
            "This structure's own market couldn't be read; only region-wide orders are counted."
        ),
        "multi_trip_haul": lambda: (
            f"{d.get('load_m3', 0):,.0f} m3 is more than your {d.get('ship')} holds, so it takes "
            f"{d.get('trips')} trips."
        ),
        "project_value": lambda: (
            f"Takes about {d.get('total_minutes', 0):,.0f} minutes in all for "
            f"{d.get('total_isk', 0):,.0f} ISK, paid at the end. This session counts as "
            f"{d.get('share', 0):.0%} of it, less {d.get('not_finishing', 0):.0%} for the chance "
            "of not finishing "
            + (
                f"(from your {d['learned_from']} closed projects)."
                if d.get("learned_from")
                else "(a starting guess)."
            )
        ),
        "project_progress": lambda: (
            f"Done so far: about {d.get('done', 0):,.0f} of {d.get('total', 0):,.0f} minutes."
        ),
        "project_deadline": lambda: (
            f"Deadline: {str(d.get('deadline', ''))[:16].replace('T', ' ')} UTC."
        ),
        "project_time_used": lambda: (
            f"The project “{d.get('title')}” has used its estimated time; mark it done or drop it."
        ),
        "deadline_fits_your_pace": lambda: (
            f"{d.get('days')} days to deliver; at about {d.get('weekly_minutes', 0) / 60:.1f} "
            "hours a week you should finish in time."
        ),
        "play_time_setting": lambda: (
            f"Play time: {d.get('weekly_minutes', 0) / 60:.1f} hours a week, your setting. It "
            "is learned from the sessions you record."
        ),
        "play_time_learned": lambda: (
            f"Play time: about {d.get('weekly_minutes', 0) / 60:.1f} hours a week. Your recorded "
            f"sessions average {d.get('observed_minutes', 0) / 60:.1f} hours over "
            f"{d.get('weeks')} weeks, blended with your setting of "
            f"{d.get('setting_minutes', 0) / 60:.1f} until more weeks agree."
        ),
        "finish_rate_guess": lambda: (
            f"Projects: assumed {d.get('not_finishing', 0):.0%} are not finished (a starting "
            "guess, until you close some)."
        ),
        "finish_rate_learned": lambda: (
            f"Projects: {d.get('done')} finished, {d.get('dropped')} dropped, so plans now allow "
            f"{d.get('not_finishing', 0):.0%} for not finishing."
        ),
        "hauler_chosen": lambda: (
            f"Carried in your {d.get('ship')}: {d.get('load_m3', 0):,.0f} m3 of the "
            f"{d.get('capacity_m3', 0):,.0f} m3 it can hold."
        ),
    }
    maker = texts.get(code)
    if maker:
        return maker()
    return WHY_NOT.get(code, code)


def risk_summary(risk: dict) -> str:
    band = {"none": "no player risk", "very_low": "very low", "low": "low",
            "medium": "medium", "high": "high"}.get(risk.get("band"), risk.get("band"))  # fmt: skip
    return (
        f"Chance of loss: {band}. At stake: {risk.get('exposure', 0):,.0f} ISK "
        f"({risk.get('share_of_net_worth', 0):.1%} of net worth). Expected loss: "
        f"{risk.get('expected_loss', 0):,.0f} ISK."
        + (" The chance is a starting guess." if risk.get("chance_is_a_guess") else "")
    )
