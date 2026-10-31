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
    "teammate": "Another character you're playing with does this.",
    "hauling": "Hauling for the character you're playing with makes more between you.",
}

# Playing together (roadmap 8.4): the two shapes a team plan compares.
SHAPE_NAMES = {
    "side_by_side": "each playing their own plan",
    "mine_and_haul": "one mining while the other hauls",
}
ROLE_NAMES = {"solo": "", "miner": "mines", "hauler": "hauls"}


LEARNED = {
    "mining_rate": "Mining speed",
    "sale_price": "Sale prices",
    "minutes_per_jump": "Travel time",
    "gas_rate": "Gas harvesting speed",
    "mission_income": "Mission income",
    "exploration_income": "Exploration income",
}
# What a plan uses until a correction has sessions to learn from, and where it learns from.
NOT_LEARNED = {
    "mining_rate": (
        "so this is the published figure. It learns from the sessions you record while EVE's "
        "game logs are being read."
    ),
    "sale_price": (
        "so plans expect what buyers' orders pay now. It learns from what your market sales "
        "in recorded sessions actually got."
    ),
    "exploration_income": (
        "so this is a starting guess from players' reports. It learns from the change in "
        "your net worth across exploration sessions."
    ),
    "mission_income": (
        "so this is a starting guess from one published figure. It learns from the "
        "mission rewards in your wallet journal."
    ),
    "gas_rate": (
        "so this is the published figure. It learns from the gas in EVE's mining ledger, "
        "if the ledger records it."
    ),
    "minutes_per_jump": (
        "so plans assume about a minute a jump. It learns from your travel in recorded "
        "sessions (the location EVE reports every couple of minutes)."
    ),
}
RISK_WORTH_MENTIONING = 0.05  # of an option's ISK


def _and(parts: list[str]) -> str:
    """["a", "b", "c"] -> "a, b and c"."""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _where_most(most: list) -> str:
    """ ", most in Sivala (63) and Jita (44)", or ", in Jufvitte" for one system."""
    if not most:
        return ""
    if len(most) == 1:
        return f", in {most[0][0]}"
    return ", most in " + _and([f"{name} ({n})" for name, n in most])


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


def _lp_value(d: dict) -> str:
    if d.get("isk_per_lp"):
        worth = (
            f", worth about {d['isk_per_lp']:,.0f} ISK each at the "
            f"{d.get('corporation') or 'agent corporation'} LP store (a typical good offer, "
            "CCP average prices)"
        )
    else:
        worth = ", not valued (the LP store couldn't be priced)"
    have = f"; you have {d['have']:,} LP with them" if d.get("have") else ""
    return f"About {d.get('lp', 0):,.0f} LP{worth}{have}."


def _campaign_objective(d: dict) -> str:
    where = f", in {d['systems']} systems" if d.get("systems") else ""
    return (
        f"Military campaign “{d.get('campaign')}” is running: "
        f"“{d.get('title')}” pays {numbers.isk(d.get('isk'))} ISK and "
        f"{d.get('lp', 0):,.0f} LP each time, plus standing ({d.get('activity')}{where}). "
        "See the campaign in game for what counts."
    )


def _trade_run(d: dict) -> str:
    def prices(first, last) -> str:
        if not first:
            return ""
        return numbers.isk(first) if first == last else f"{numbers.isk(first)}-{numbers.isk(last)}"

    return (
        f"Buy {d.get('units', 0):,} {d.get('item')} in {d.get('from')} from sell orders at "
        f"{prices(d.get('first_buy'), d.get('last_buy'))} ({numbers.isk(d.get('buy_cost'))} ISK); "
        f"sell them to buy orders in {d.get('to')} at "
        f"{prices(d.get('first_sell'), d.get('last_sell'))} "
        f"({numbers.isk(d.get('sell_value'))} ISK after tax). Prices from the last market read: "
        "check them in game first."
    )


def _manufacture_costs(d: dict) -> str:
    runs = d.get("runs", 1)
    cost = d.get("materials", 0) + d.get("job_fee", 0)
    on_cost = f", {d['profit'] / cost:.0%} on what it costs" if cost > 0 else ""
    to_buy = f"; to buy first: {', '.join(d['to_buy'])}" if d.get("to_buy") else ""
    return (
        f"Materials {numbers.isk(d.get('materials'))} ISK and job fee "
        f"{numbers.isk(d.get('job_fee'))} ISK against {numbers.isk(d.get('value'))} ISK "
        f"when sold (after tax): {numbers.isk(d.get('profit'))} ISK profit over "
        f"{runs} run{'s' if runs != 1 else ''}{on_cost}. Prices are CCP averages, not hub "
        f"quotes{to_buy}."
    )


def _extractor_stopped(d: dict) -> str:
    count = d.get("extractors", 1)
    what = "An extractor has" if count == 1 else f"{count} extractors have"
    hours = d.get("hours_ago")
    when = f", {hours:.0f} hours ago" if hours and hours >= 1 else ""
    return (
        f"{what} stopped on {d.get('planet', 'your planet')}{when}: while stopped, the "
        "colony makes nothing new."
    )


def _corp_tax(d: dict) -> str:
    rate = d.get("rate", 0.0)
    who = d.get("corporation") or "your corporation"
    if d.get("source") == "unknown":
        return f"No corp tax counted: {who}'s rate isn't known yet."
    source = {
        "corporation": "its rate on EVE's public record",
        "journal": "what your wallet journal shows it took from rewards",
        "npc": "what NPC corporations take",
    }.get(d.get("source"), "")
    return f"{who} takes {rate:.1%} of mission rewards and bounties ({source})."


def _together_chosen(d: dict) -> str:
    text = (
        f"Best between you: {SHAPE_NAMES.get(d.get('shape'), d.get('shape'))}, about "
        f"{numbers.isk(d.get('value', 0))} ISK after expected losses"
    )
    if d.get("other_shape") is None:
        return text + "."
    gap = d.get("value", 0) - (d.get("other_value") or 0)
    return (
        f"{text}, {numbers.isk(gap)} more than "
        f"{SHAPE_NAMES.get(d['other_shape'], d['other_shape'])}."
    )


def _not_omega(d: dict) -> str:
    state = d.get("clone_state")
    what = "is Alpha" if state == "alpha" else "isn't marked Omega"
    return (
        f"The {d.get('account')} account ({_and(d.get('characters') or ['?'])}) {what}: "
        "an Alpha account can't be logged in alongside another account."
    )


def describe(code: str, detail: dict | None = None) -> str:
    d = detail or {}
    texts = {
        "same_stack": lambda: _same_stack(d),
        "freelance_mining": lambda: (
            f"The Freelance Job \u201c{d.get('job', '')}\u201d (named by its corporation) pays "
            f"{numbers.isk(d.get('reward_per_unit', 0))} ISK a unit mined, for "
            f"{d.get('counted', 0):,} of the {d.get('quantity', 0):,} units. Counted at the job's "
            f"pay only: if the ore stays yours, selling it adds about "
            f"{numbers.isk(d.get('ore_value', 0))}."
            + (
                ""
                if d.get("committed")
                else " Commit to the job in game first; it pays only after that."
            )
        ),
        "freelance_delivery": lambda: (
            f"The Freelance Job \u201c{d.get('job', '')}\u201d (named by its corporation) pays "
            f"{numbers.isk(d.get('reward_per_unit', 0))} ISK an item delivered to "
            f"{d.get('place', '?')}: {d.get('units', 0):,} items, which go to the corporation."
            + (
                ""
                if d.get("committed")
                else " Commit to the job in game first; it pays only after that."
            )
        ),
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
            f"Travel time assumes about {d.get('minutes', 1):.2g} minutes a jump, learned from "
            f"{d['sessions']} of your sessions."
            if d.get("sessions")
            else f"Travel time assumes about {d.get('minutes', 1):g} minute a jump until the "
            "tool learns your pace."
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
            f"sessions, {NOT_LEARNED.get(d.get('what'), 'so this is the published figure.')}"
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
        "exploration_guess": lambda: (
            f"About {numbers.isk(d.get('isk_per_hour'))} ISK an hour from "
            f"{' and '.join(d.get('sites', []))} sites, a starting guess (players report "
            "500k-1.5M in highsec, EVE forums; loot is very much luck). Sites have to be "
            "scanned down first."
        ),
        "exploration_needs_fit": lambda: (
            "Exploration: fit a probe launcher and a relic or data analyzer to see it in plans."
        ),
        "mission_guess": lambda: (
            f"Level {d.get('level')} distribution missions: about "
            f"{numbers.isk(d.get('isk_per_hour'))} ISK and {d.get('lp_per_hour', 0):,.0f} LP an "
            "hour, a starting guess (EVE doesn't publish mission pay; level 4 is about 400k ISK "
            "and 750 LP a mission, EVE University, a third less a level below, three an hour)."
        ),
        "lp_value": lambda: _lp_value(d),
        "corp_tax": lambda: _corp_tax(d),
        "already_owned": lambda: (
            f"You already have a {d.get('item', 'one')}, so there's nothing to buy: only "
            "the skills are left."
        ),
        "fit_unpriced": lambda: (
            "No market price yet for " + ", ".join(d.get("items", [])) + ", so the target "
            "leaves them out. Prices are read with the next refresh."
        ),
        "buyback": lambda: (
            f"Your corporation's buyback in {d.get('system', '?')} pays {d.get('share', 0):.0%} "
            f"of Jita's best buy ({numbers.isk(d.get('jita_buy', 0))} ISK a unit), by contract; "
            "no sales tax. From Settings: check it's still what your corporation pays."
        ),
        "friendly_space": lambda: (
            "Nullsec held by your alliance: quiet, so rated like lowsec. Still check local."
        ),
        "hostile_null": lambda: (
            "Nullsec not held by your alliance: anyone can shoot you there, and help is far."
        ),
        "extractor_stopped": lambda: _extractor_stopped(d),
        "planet_output": lambda: (
            f"Running, the colony makes about {numbers.isk(d.get('a_day', 0))} ISK a day more, "
            f"so about {d.get('program_hours', 24):.0f} hours (your last program) is counted. "
            "Valued at CCP's average prices less a guessed "
            f"{d.get('customs_tax', 0.1):.0%} customs office export tax; each cycle's yield "
            "varies and falls over a program."
            + (
                f" {numbers.isk(d['waiting'])} ISK of goods are waiting to be exported."
                if d.get("waiting", 0) >= 1000
                else ""
            )  # fmt: skip
        ),
        "planet_in_system": lambda: (
            f"Done from {d.get('system', 'the planet')}'s own system: managing a planet from "
            "further away takes Remote Sensing, an Omega skill."
        ),
        "campaign_objective": lambda: _campaign_objective(d),
        "manufacture_costs": lambda: _manufacture_costs(d),
        "trade_run": lambda: _trade_run(d),
        "trade_data_old": lambda: (
            "No trade runs: the trade hubs' markets haven't been read in the last two hours. "
            "They're read in the background now; open Trade to see them."
        ),
        "mission_cargo": lambda: (
            "Distribution missions need cargo space: a few m3 at level 1, up to thousands at "
            "level 4. Some send you to lowsec; decline those if you don't go there."
        ),
        "job_slots_full": lambda: (
            f"No build suggested: all {d.get('slots')} of your job slots are busy."
        ),
        "no_cost_index": lambda: (
            "No build suggested yet: the system's industry cost index hasn't been read "
            "(once a day while the dashboard runs)."
        ),
        "not_refinable": lambda: (
            f"{d.get('name', 'This ore')} can't be refined (CCP's data lists nothing it refines "
            "into), so only selling it raw is compared."
        ),
        "gas_estimate": lambda: (
            f"{d.get('ship')}: about {d.get('m3_per_minute', 0):,.0f} m3 of gas a minute with "
            f"{d.get('harvesters', 0)} harvester{'s' if d.get('harvesters', 0) != 1 else ''} "
            "(the published figures; Gas Cloud Harvesting allows one active harvester a level)."
        ),
        "gas_site_search": lambda: (
            f"Gas sites (nebulae) have to be scanned down first: about {d.get('minutes', 0):.0f} "
            "minutes allowed. A Venture can't fit a probe launcher, so use a scanning ship or "
            "a bookmark."
        ),
        "gas_valued_as": lambda: (
            f"Valued at {d.get('isk_per_m3', 0):,.0f} ISK a m3, the average CCP price of the "
            f"highsec booster gases, after {d.get('tax', 0):.1%} sales tax; which gas a site "
            "holds depends on the site."
        ),
        "gas_skill_limit": lambda: (
            f"{d.get('fitted')} gas harvesters fitted but only {d.get('active')} can run at your "
            "Gas Cloud Harvesting level (or turret slots)."
        ),
        "gas_needs_scoops": lambda: (
            "Gas harvesting: fit Gas Cloud Scoops to a mining frigate to see gas sites in plans."
        ),
        "gas_unpriced": lambda: "Gas isn't in plans: no market prices for it yet.",
        "no_gas_modules": lambda: "No gas harvesters this character can use are fitted.",
        "no_live_signals": lambda: (
            "No fresh kill data from EVE, so the chance of loss comes from security alone "
            "(highsec low, lowsec medium). It's read every hour while the dashboard runs."
        ),
        "recent_kills": lambda: (
            f"{d.get('kills', 0)} {'ship or pod' if d.get('kills', 0) == 1 else 'ships or pods'}"
            f" destroyed in the last hour along the way{_where_most(d.get('most', []))}."
        ),
        "hotspot": lambda: (
            f"Gank hotspot or busy hub: {d.get('kills', 0)} ships and pods destroyed in "
            f"{d.get('system', '?')} in the last hour."
        ),
        "no_recent_kills": lambda: "No ships destroyed along the way in the last hour.",
        "camped": lambda: (
            f"Possibly camped: {d.get('kills', 0)} ships and pods destroyed in "
            f"{d.get('system', '?')} in the last hour."
        ),
        "incursion": lambda: (
            f"Incursion in {', '.join(d.get('systems', []))}: Sansha's ships there attack "
            "anyone, miners included."
        ),
        "loss_chances_learned": lambda: (
            f"Loss chances are learned from {d.get('trips', 0)} of your trips and "
            f"{d.get('losses', 0)} loss{'es' if d.get('losses', 0) != 1 else ''}, starting "
            "from guesses that keep 100 trips' weight."
        ),
        "loss_chances_are_guesses": lambda: (
            "Loss chances are starting guesses until your own trips and losses refine them."
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
        "together_too_few": lambda: "Pick two or more characters to play together.",
        "together_no_account": lambda: (
            f"{d.get('character')} isn't on an account yet (set it on the Characters page), "
            "so whether it can play alongside the others can't be checked."
        ),
        "together_same_account": lambda: (
            f"{_and(d.get('characters') or ['?'])} are on the same account "
            f"({d.get('account')}), and an account logs in one character at a time."
        ),
        "together_not_omega": lambda: _not_omega(d),
        "together_chosen": lambda: _together_chosen(d),
        "together_side_by_side_only": lambda: (
            "With more than two characters, only each playing their own plan is worked out."
        ),
        "no_team_mining": lambda: (
            f"{d.get('miner')} mining while {d.get('hauler')} hauls wasn't worked out: "
            f"{d.get('miner')}'s best plan for this time doesn't mine."
        ),
        "no_team_hauler": lambda: (
            f"{d.get('miner')} mining while {d.get('hauler')} hauls wasn't possible: "
            f"{d.get('hauler')} has no ship it can fly that gets there and carries the ore "
            "in time."
        ),
        "team_haul_risky": lambda: (
            f"{d.get('miner')} mining while {d.get('hauler')} hauls wasn't offered: the "
            f"hauling is rated Risky for {d.get('hauler')}. Tick “Include risky options” "
            "to allow it."
        ),
        "team_mining": lambda: (
            f"{d.get('hauler')} collects the cans and sells the ore, so you don't dock to "
            "unload; you stop in time for the last can to be carried in and sold."
        ),
        "team_haul": lambda: (
            f"About {d.get('m3', 0):,.0f} m³ from {d.get('miner')}, in your "
            f"{d.get('ship')} ({d.get('capacity', 0):,.0f} m³ a trip): {d.get('trips', 0)} "
            f"trips, about {d.get('minutes_needed', 0):.0f} min of flying, collecting and "
            f"selling. The ore's ISK is counted on {d.get('miner')}'s mining step."
        ),
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
        f"{risk.get('expected_loss', 0):,.0f} ISK"
        + (
            f", after the {risk['payout']:,.0f} ISK EVE pays for the hull (uninsured)."
            if risk.get("payout")
            else "."
        )
        + (
            " The chance is a starting guess."
            if risk.get("chance_is_a_guess")
            else f" The chance ({risk.get('chance', 0):.2%} a trip) is learned from "
            f"{risk.get('trips', 0)} of your trips."
        )
    )
