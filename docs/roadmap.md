# Roadmap

Written 17 October 2026, from two reviews done that day: one of the
application and the plan, one of the dashboard's usability and style.
It replaces phases 4 and 5 in [design.md](design.md#7-phases) with smaller
milestones, and adds a "3.5" milestone of foundations to do first.

**How to read it.** Work goes top to bottom. Each numbered item is roughly
one pull request; the repository owner reviews and merges each one. Items
marked **◆** need a decision from the owner before they start (listed again
at the end). A milestone is finished when all its items are merged; the
repository gets a version tag then.

Phases 0–3 are done: setup, foundations and dashboard, the first planner,
and learning (sessions, outcomes, game logs, mining-speed correction,
History, real pace and goal dates).

---

## Milestone 1: Protect the data, fix the foundations (phase 3.5)

| # | What | Why (finding) | Done when |
|---|---|---|---|
| 1.1 | **Backups.** A daily rotating copy of the database while the dashboard runs (14 daily, then monthly), an `eve-assistant backup` command, and restore steps in the README. | There are none. ESI keeps only about 30 days of wallet and mining history, so the history this tool builds can't be recovered once lost. | A backup appears each day; a restore from it is tested. |
| 1.2 | **Session flow in the service layer.** One function each for "start with a snapshot" and "finish with a snapshot, then work out outcomes", used by both the dashboard and the CLI. | The same steps are written twice, against the repository's own rule; a future MCP server needs them in one place. | Dashboard and CLI call the same functions; `cli.py` coverage rises. |
| 1.3 | **Automatic session detection.** From the online status that auto-refresh already polls every 2 minutes, notice when a character played and offer it on Today: "Looks like you played 18:02–19:10. Record it?" Nothing is recorded without a yes. | Learning only works if the buttons are pressed, and forgetting is easy. | A real log-in and log-out produces a suggestion that records a session. |
| 1.4 | **Static data update reminder.** Check once a day for a newer CCP build and show a banner with how to update. | Updates are manual; new or renamed items go unrecognised after a patch. | The banner appears when CCP ships a build. |
| 1.5 | **Sell at your structure.** Use the structure-markets scope (already held) for structures you are docked at or store goods in. | Corrected while building it: the regional order book already includes structures with a public market, so prices were right. What was missing: ore stored in structures was skipped; orders that only count at the structure (station-range orders, non-public markets) were never seen; a misleading note. | Ore at an accessible structure is offered and sold there, counting its own market. |
| 1.6 | **Track your own contracts.** Use the character-contracts scope (already held): accepted couriers, deadlines, completion. | Courier projects are closed by hand; outcomes guess contract income from the journal. | Courier projects progress and close themselves. |
| 1.7 | **◆ Fittings scope:** use it (the planner reads your saved fits for "what if" plans) or stop requesting it. Blueprints stays, for manufacturing (6.4). | Scopes held but unused, against the "request only what you need" rule. | Every requested scope is used or documented as planned. |
| 1.8 | **One way to value ore.** Plans and outcomes use the same price method. | Plans use buyers' bids, outcomes use CCP's average price. | Both use the same method. |
| 1.9 | **Housekeeping.** SonarQube stops counting `.sql` files as untested code; a desktop shortcut that starts the dashboard and opens the browser; README start-up steps. | Small things noticed during review. | Tag `v0.4.0`. |

## Milestone 2: The dashboard's look and flow

The style comes first so the flow fixes are built with the new pieces
instead of being restyled afterwards.

| # | What | Why (finding) | Done when |
|---|---|---|---|
| 2.1 | **◆ Style prototype** on a branch: the "capsuleer console" direction (dark first, cyan for actions, amber for ISK and attention, EVE's security colours, big tabular numbers). Viewed in the browser and changed until approved. **◆ Fonts:** self-hosted open-licence font files, if wanted. | The current look is a generic template with no hierarchy or EVE identity. | The owner approves the direction. |
| 2.2 | **Design system applied.** Colours, type and components (buttons, pills, progress bars, security badges) on every page; the light theme kept, with a switch (a cookie set by a form, no JavaScript). Contrast at least 4.5:1 everywhere, checked by a test. | Dark-mode primary buttons are 2.33:1, light 4.24:1. | Every page uses the system; the contrast test passes. |
| 2.3 | **Friendly errors and plain language.** HTML error pages with a way back; no internal codes on screen (`kind:sell_hangar`, config paths, "snapshot"); risk shown as Safe / Caution / Risky with an icon, not colour names; the risk matrix axes labelled; repeated lines removed. | Raw `{"detail": …}` errors; jargon; colour-only risk. | A wording pass over every page. |
| 2.4 | **One way to commit, and a session view.** "Start this plan" is the commitment; steps keep only "Not now" and "Not this kind" (and "Start project" for project steps). While playing, Today becomes the session view: timer, current step, checklist, "I'm done". Pages reload themselves while a session runs. | "Do it" and "Start this plan" overlap; Today doesn't change while playing; pages go stale. | A full session can be run from the session view. |
| 2.5 | **Plan age and staying in place.** "Made 6 hours ago, prices have moved: plan again"; after a response the page returns to that step, not the top. | Old plans look current; the page jumps to the top. | Both visible on Today. |
| 2.6 | **Alternatives that explain themselves.** ISK per hour for every option, and a reason specific to each row. | Every row says the same thing, even one that earns more. | Each alternative says why it wasn't picked. |
| 2.7 | **Active character switcher**, defaulting to whoever is online, used by Today, Skills and Goals. | The character is picked again on every page. | One switch in the header. |
| 2.8 | **Skills to Goals, and better goals.** "Set as goal" from a skills check; name suggestions while typing (`<datalist>`, no JavaScript); goal progress bars; consistent ISK formatting; Drop asks first; relative times next to EVE time. | Skills is a dead end; goals show no progress. | A ship can go from "can I fly it" to a goal with progress in two clicks. |
| 2.9 | **Navigation.** "What it has learned" and the risk-matrix explanation move to History ("History and learning"); Preferences becomes Settings. | Settings and insights are mixed. | — |
| 2.10 | **Add a character from the dashboard.** A button that runs the same PKCE login. | Setup needs the terminal. | A new character can be added without the CLI. |
| 2.11 | **Portraits and ship renders.** Downloaded at runtime from CCP's image server into `var/` and served by the dashboard (never committed; the security policy stays "self only"). Today gets a "next up" strip (ISK, ISK per hour, minutes, risk) and steps as a timeline. | No imagery; the totals look like footnotes. | Characters and Today show them. |
| 2.12 | **Phone layout.** A compact menu instead of two rows of links; tables turn into cards on narrow screens. | Tables are unreadable at phone width (matters once hosted mode reaches a phone). | Every page works at 375 px. Tag `v0.5.0`. |

## Milestone 3: A planner ready to grow (phase 4a)

| # | What | Why | Done when |
|---|---|---|---|
| 3.1 | **Planner restructure.** Each activity becomes a module with one interface (its candidates, what it learns from outcomes, its corrections); the planner only ranks and fits them into the time. No change in behaviour; the existing tests prove it. | `build_plan` has a cognitive complexity of 91; several new activities are coming. | Mining, selling, hauling, couriers, skills and projects are modules. |
| 3.2 | **◆ New read-only scopes, in one re-login:** freelance jobs, loyalty points, your own killmails (planets and military campaigns later, when used). Each with its reason in the design doc. | Needed by 3.3, 4.2 and 5.3; batching means logging in again once, not three times. | All characters logged in again. |
| 3.3 | **Freelance Jobs** as an activity, with the player-written text labelled. | Corporations pay ISK for mining and deliveries; it fits this player directly. | Suitable jobs appear in plans. |
| 3.4 | **Who should play today.** Compare the best plan for each character. | Three characters on one Alpha account; only one plays at a time. | Today suggests the character as well as the plan. |
| 3.5 | **Omega comparison and a PLEX goal.** What Omega would earn per hour (for example the Procurer you own); a goal type for Omega via PLEX at the current PLEX price. | The 2.46B goal is really "500 PLEX"; Omega's value is only shown for skills. | The Omega figure and the PLEX goal are on Goals. |
| 3.6 | **Train this next.** Skills ranked by how much they shorten the time to the goal (from the design, section 4.5). | Planned since the design; fits with 3.5. | Skills page suggests the next skill with its payback. |
| 3.7 | **Net worth over time.** Wallet plus valued assets, from the snapshots, on Characters and Goals. | Goals only see the wallet; hangar value is invisible. | A net-worth line per character. |
| 3.8 | **Sale-price and travel-time corrections.** Plans record the unit price and minutes per jump they assumed; outcomes correct them like mining speed. | Deferred from phase 3. | Both appear under "What it has learned". Tag `v0.6.0`. |

## Milestone 4: Risk from real data (phase 4b)

| # | What | Why | Done when |
|---|---|---|---|
| 4.1 | **Live risk signals:** kills per system in the last hour, active incursions, and insurance payouts taken off the expected loss. | Risk is estimated only from a system's security rating. | Ratings change with real activity. |
| 4.2 | **Your own losses** (killmails) feed the loss chances. | The chances are still published guesses. | Learned loss chances shown with their sample size. |
| 4.3 | **Lowsec planning** with those signals, then the deferred **moving risk lines** from your accept and decline choices. | Deferred until there was lowsec data. | Tag `v0.7.0`. |

## Milestone 5: More things to do (phase 4c)

Each is a module under 3.1. Alpha limits are checked from CCP's clone-grade
data, not assumed.

| # | What | Alpha? | Done when |
|---|---|---|---|
| 5.1 | **Gas harvesting** (Venture). | Yes, Gas Cloud Harvesting 3. | Gas sites in plans, learned like mining. |
| 5.2 | **Exploration** (relic and data sites). | Yes, skills to 3. | Offered as "new to you"; income learned from outcomes. |
| 5.3 | **Distribution missions and LP store values.** | Yes. | Missions offered with ISK plus LP value. |
| 5.4 | **Manufacturing** from your blueprints, with job costs. | Yes, Industry 5 (no research or invention). | A build suggested with its payback. |
| 5.5 | **◆ Trade hauling** (buy to haul and sell elsewhere): build it in, or keep using external tools as the design says. Decided 18 Oct: build our own, covering what EVE Trade, Trading Matrix, Eve-hub.ru and EVE Courier offer. | Partly (Trade 3, Broker Relations 2). | Built: hub order books, trade runs (instant and listed), station trading, your orders, trade runs in plans. Tag `v0.9.0`. |
| 5.6 | **Military campaigns** (Cradle of War): check what is open to Alpha and add it if it fits. | To check. | Tag `v0.8.0`. |

## Milestone 6: Corporations, nullsec and Planetary Interaction (phase 4d)

| # | What | Done when |
|---|---|---|
| 6.1 | **Corp-aware planning:** corp tax, buyback, corp contracts, corporation projects. | Plans account for the character's corp. Built: corp tax (a player corp's public rate, the wallet journal, or NPC corps' 11%) in mission income; a buyback entered in Settings, compared with the market for ore. Corp contracts and corporation projects wait for a player corp (two more scopes). |
| 6.2 | **Nullsec basics** (sovereignty, jump bridges) for the coworker's corp. | Routes and risk work in their space. Built: the sovereignty map (public), jump bridges pasted in Settings and used in routes, nullsec camps from 5 kills an hour, quiet space held by your alliance rated medium instead of high. |
| 6.3 | **Planetary Interaction.** Alpha clones can't train any PI skill, so it is one planet with a basic command centre until Omega; worth it alongside the Omega comparison. | Extractor timers and PI income in plans. Built behind `[esi] planets = true` (the scope must be ticked first): colonies, timers, what each makes a day, restarting stopped extractors in plans, PI in the Omega comparison. Tag `v0.10.0` (0.9 went to the trade tool). |

## Milestone 7: Alerts and hosted mode (phase 5)

| # | What | Done when |
|---|---|---|
| 7.1 | **Alerts.** In-game actions to Discord (job finished, courier deadline near, order sold, extractor done); tool health to ntfy; nothing else. **◆** A minimal Discord alert could come earlier if wanted. | A finished job reaches Discord with a link to the dashboard. Built: jobs, orders sold or undercut, empty skill queues, couriers due, stopped extractors and goals reached to Discord, refresh problems to ntfy, each once; addresses at a hidden prompt (`alerts-setup`, `alerts-test`). |
| 7.2 | **Hosted mode:** a secret store that works on Linux, a login, and a log sender for game logs. Home-lab changes go to the lab session as a build request, never made from here. | Runs on the owner's server behind a login. Tag `v1.0.0`. Built (`v0.11.0`): `serve --hosted` behind the home lab's Discord sign-in, a key-encrypted secret file, EVE login through the dashboard's `/callback`, `send-logs` from the gaming PC, streaming market reads. Fixed on the first deployment: surviving a boot race and listening on 0.0.0.0 (`v0.11.1`), the stylesheet over https and the Add button (`v0.11.2`), the login redirect under the CSP (`v0.11.3`), and `send-logs` hardened to run unattended every 15 minutes (`v0.11.4`). Running on the owner's server since 28 Oct 2026, characters added through it; the owner chose to hold `v1.0.0` for now. |

## What's next

Things the design promised that aren't built yet (checked against
[design.md](design.md) on 28 Oct 2026), leaving out what was decided
against. The owner's priorities, in order:

### First: finish the half-built goals (design 4.1, 4.5)

| # | What | Done when |
|---|---|---|
| 8.1 | **Ship goals with a fit:** the ISK to buy *and fit* a ship, from a saved in-game fit (the fittings scope is already held) or one pasted from Pyfa or EVEShip.fit, not the hull alone. | A ship goal's target counts the hull and its modules, with the fit named. Built (`v0.12.0`): a saved fit by name or a pasted EFT fit, on the Goals page and `goal add ship --fit`; the target is the hull and items still to buy (CCP averages), less what's owned; the skills cover every item. |
| 8.2 | **Goals in sequence:** "a Retriever, then a month of Omega", with each goal's date following on from the one before. | Stacked goals show in order with dates that add up. Built (`v0.14.0`): "After" on the Goals page and `goal add --after`; a following goal's ISK, date and training count the goals before it, each skill level once; goal-reached alerts use the totals. |
| 8.3 | **Refining skills in "what to train next":** weighed against how much ore the character actually mines. | Reprocessing skills are suggested with the ISK they'd add at the character's real volume. Built (`v0.15.0`): a "Refining your ore" card on the Skills page from the last 30 days of the mining ledger, each ore sold raw or refined, whichever pays; it says whether refining pays now, what every refining skill trained up would add a week and how long that takes, and lists the single levels worth the most ISK a week. |

### Second: plan several characters at once (design 6.3)

| # | What | Done when |
|---|---|---|
| 8.4 | **One plan for characters played together.** Omega accounts can be logged in at once (the owner plays two), so a plan can split the session between them: one mines while the other hauls or sells, and the ore one mines is what the other carries. Alpha accounts still can't multibox, and the planner never suggests it. | A 60-minute plan for two characters that the owner agrees is sensible, recorded and learned from like any other. |

### Then, in no set order

| What | From |
|---|---|
| **An AI assistant connection (MCP server):** ask Claude or ChatGPT about plans and goals, read-only, respecting the player-written labels. | design 6.6 |
| **"What changed for you":** after a game update, changes to the items and ships the player owns or plans to buy. | design 4.7 |
| **Reactions** as an activity. | design 4.2 |
| **zKillboard data** for hauler and miner kills over longer periods, beyond the last hour. | design 9 |
| **Corp contracts and corporation projects:** when a character joins a player corporation (two more scopes). | design 6.4, roadmap 6.1 |
| **Corp data for officers** (hangars, jobs, moon extractions): optional, needs in-game roles. | design 6.4 |
| **A changelog** with each version, and **GitHub issues** for these items so the coworker can pick some up. | below |

Still to verify (design 10): the Alpha training-speed penalty; whether ESI
reports Alpha or Omega directly; how the mining ledger records compressed
and moon ore; whether Exordium's lower payouts apply to mining; the
September 2026 jump bridge numbers; account rules against CCP's current
EULA. (Ore anomalies were checked: no API shows them.)

---

## Alongside everything

- **Play real sessions.** The learning needs data; check History after a
  few sessions and report anything that looks wrong before building on it.
- **GitHub issues** for the items above, so the coworker can pick some up.
- **A changelog** with each version tag.

## Decisions for the owner

| Item | Decision |
|---|---|
| 1.7 | Decided 17 Oct: use it (what-if notes from saved fits). |
| 2.1 | Decided 17 Oct: the direction is approved, with Windows' own fonts (Bahnschrift, Segoe UI); no font files. |
| 3.2 | The new read-only scopes, and when to log in again. |
| 5.5 | Decided 18 Oct: build our own trade tool, covering what the external ones offer. |
| 6 | Decided 19 Oct (the recommendations): the PI scope, switched on in config once ticked; corp contracts and projects wait for a player corp; nullsec built for the coworker's corp. |
| 7.1 | Moot: alerts were built in milestone 7. |
