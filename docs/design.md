# EVE assistant: design

*Working name. Shareable version: contains no personal account,
character or infrastructure details.*

Status: **design agreed. Phases 1 and 2 (section 7) built; phase 3 (learning) under way.**


---

## 1. What it is

A planning assistant for EVE Online players who do **mining, industry and
hauling**. You set a goal, tell it how long you have to play, and it reads
your characters through CCP's official API (ESI) and suggests what to do
with that time to get there fastest. It learns from what actually happened
afterwards.

What it should be able to do:

- **Goals.** For example: "earn enough ISK for 500 PLEX a month", or "buy
  and fly this ship: how do I earn the ISK, and what skills does it need?"
- **Fit the time you have.** "I have an hour" gets a plan for that hour,
  including progress on longer projects that span several sessions.
- **Account for skills.** What the character can do now, and which
  training would pay off.
- **Learn.** Compare each plan with what happened, and get better at
  predicting for *this* player.
- **Let the player say no.** Every suggestion can be declined, or a whole
  category muted.

## 2. Why build it

There are good tools for each piece: ore prices, manufacturing profit,
trade routes, skill lists (section 11). **No tool takes a player's
characters, goal and available time together and says "do this, then this,
and here is what it is worth."** That cross-activity decision is the gap.
It is also where learning genuinely works, because every session produces
a prediction and a measurable result.

Things it deliberately does **not** do, because better tools exist: ship
fitting maths (Pyfa), market-wide arbitrage, appraisal (Janice), corp
management (SeAT, Alliance Auth), live intel (RIFT, Telescope).

## 3. Being honest about goals

The tool's first job is to say whether a goal is realistic. Example, with
numbers from October 2026:

- **Omega costs 500 PLEX a month** (base price; sales happen, and the
  12-month option is cheaper per month).
- **PLEX averaged about 4.7M ISK** on the global market in mid-October
  2026. So Omega bought with ISK is about **2.4 billion ISK a month**.
- **Alpha clones cannot fly mining barges** (Procurer, Retriever, Covetor).
  Alpha miners use the Venture, and later a mining battleship such as the
  Praxis. Alphas train more slowly, their queue stops at **5 million skill
  points**, and their skills are capped (about 19.7M SP reachable, partly
  through the daily Alpha injector).
- **Skills above the Alpha limits are inactive** on a lapsed Omega
  character, so old barge skills don't help until it is Omega again.
- **New characters start in Exordium**, the starter region added in June
  2026. It is PvP-free, but **mission, bounty and LP payouts are 20% lower
  and market orders carry an extra 5% fee**. The only way out is the gate
  from Manifest to Yulai, so "sell outside Exordium" is often the best tip
  for a new character.

A new Alpha mining in a Venture (very roughly 5–10M ISK an hour; to be
replaced by computed figures) for 15–40 hours a month makes very roughly
75–400M ISK a month, far short of 2.4B. So **Omega from ISK is not a
realistic first goal for a new character**, and the tool should say so and
suggest goals the character can reach. For every goal that needs Omega it
shows both "what you can do now" and "what Omega would unlock".

---

## 4. How the advisor works

### 4.1 Goals

| Goal | Example | What the tool works out |
|---|---|---|
| **ISK** (one-off or monthly) | 2.4B a month for Omega; 50M for a ship | The gap, the ISK per hour needed at the player's pace, whether it is realistic |
| **Own and fly a ship** | "a Retriever", later "an Orca" | ISK to buy and fit it (from a fit chosen in Pyfa or EVEShip.fit), skills needed with training time, whether it needs Omega |
| **Skills** | "Mining Barge IV" | Training time, order, what it unlocks |

Goals can stack ("Retriever, then a month of Omega"). Progress is reported
every session, and arrival dates are re-estimated as the tool learns how
the player actually earns.

### 4.2 Activities it compares

Each activity is a small model that answers, for **this character, right
now**: what is needed (ship, skills, capital), how many minutes at the
keyboard it takes and how much runs unattended, expected ISK with a range,
and the risk.

| Activity | How it is estimated | Accuracy at first | What makes it accurate |
|---|---|---|---|
| **Mining** (by ore, belt or anomaly, system) | Ship and module yield, skills, ore prices, travel to the site and to market | Rough | The **mining ledger** (ESI records exactly what was mined, by day) |
| **What to do with ore**: sell raw, compress, refine and sell minerals, or build with it | Prices and order-book depth at nearby hubs, refining skills and the facility's refine rate, sales tax and broker fees (skills and standings) | **Good**: arithmetic on known data | Predicted sale prices against what the wallet shows |
| **Manufacturing** (the character's blueprints) | Material cost, job cost per system (published in ESI), sale price, tax | Good | Real job outcomes and sale prices |
| **Courier contracts** | Public contracts (ESI), route length, cargo capacity and warp speed, collateral against the wallet, route risk | Fair; risk is the weak part | Real travel time per jump; whether contracts completed |
| **Hauling own goods to a better market** | Price difference between hubs against travel time | Good | Wallet results |

Later: missions, exploration, Planetary Industry, reactions. Each is just
another model in the same planner.

### 4.3 The session planner: "I have an hour"

The player says how long they have (optionally "lowsec OK" or "I want to
mine tonight"). The planner answers with a short plan. The measure is
**ISK per minute at the keyboard**, because work that runs while logged
off (a manufacturing job, a sell order) costs only the minutes to set it
up.

> **You have 60 minutes. Goal: 15M toward a Retriever (34% done).**
> 1. Collect the 3 finished jobs, list them at the nearest hub (5 min). ~4.1M
> 2. Start a 2-day manufacturing run with the minerals in your hangar (5 min).
>    ~2.8M when it sells, done Saturday
> 3. Mine Scordite at the anomaly two jumps away (45 min). ~6–8M if sold
>    compressed. Refining would lose ~9% at your skill level.
> 4. Skipped: courier to Amarr, 12M, but 14 jumps won't fit in the hour.
>
> Training: Reprocessing III finishes tomorrow. Queue Mining Frigate IV next:
> +5% yield, pays for its training time after ~6 hours of mining.

Rules:
- Every step must reach a **safe stopping point** (docked, cargo in a
  hangar) within the time given. Whole activities can run longer (4.4).
- Every option is rated by the risk matrix (4.8). Profit is shown **after
  expected losses**, so a risky option has to earn its place.
- Lowsec and nullsec only if the player has allowed them.
- At most one "new to you" activity per plan (4.9).
- When two options are close, it says so rather than pretending to be
  precise.

### 4.4 Projects that span several sessions

Some of the best options don't fit in an hour: mining enough for a full
manufacturing batch, hauling stock in several trips, a courier contract
with days to complete, saving and training toward a ship. So the planner
also thinks in **projects**: chains of steps, each ending at a safe
stopping point.

> **Project: minerals for a 20-run destroyer batch (est. 4 sessions, ~22M total)**
> Sessions 1–2: mine 50 min each, dock and store the ore.
> Session 3: refine, buy the missing minerals, start the job (runs 2 days).
> Session 4: haul the destroyers to the hub and list them.

- **Value is spread over minutes but paid at the end.** An hour of a
  4-hour, 40M project counts as about 10M of progress, discounted for the
  chance of not finishing.
- **Deadlines are checked against the player's real pace** (default 6
  hours a week, then learned). A courier contract is only offered if
  finishing in time is likely; it shows how likely, and the collateral
  tied up meanwhile.
- **Open projects come first** on the Today page ("Continue: 2 of 4
  sessions done"). Pausing or dropping one is a decline, not a failure.
- **Unattended work is scheduled around**, e.g. a job finishing between
  sessions.
- **At most two open projects** to start with.
- **Courier projects track themselves** from the character's own
  contracts: delivered closes the project as done; failed, deleted,
  cancelled, rejected or reversed closes it as dropped. A contract the
  character never accepted isn't in its list, so that project stays open
  until the player closes it. History reads planned couriers from the
  contract too ("delivered", "accepted, not delivered yet").

### 4.5 Skills

- **Can I do this?** Prerequisite chains from the static data; training
  time from attributes, implants and clone state.
- **What should I train next?** Skills ranked by how much they shorten the
  time to the goal (yield skills against hours mined, refining skills
  against ore volume…).
- **Alpha limits are part of every answer.** "This needs Omega" is a real
  result, not an error.

### 4.6 Learning

Each session records the plan (with predictions), what the player did
(from ledger, wallet, jobs and contracts, no input needed) and the result.

**Sessions are marked explicitly.** "Start this plan" on the Today page
(or `eve-assistant session start`) records the start time and takes a
snapshot; "I'm done" records the end and takes another. That is cheap,
accurate and needs no background polling; detecting sessions on its own
can come later. Details:

- One running session per character; others can play meanwhile.
- "I'm done" accepts the minutes actually played, for when the player
  stopped earlier. After 12 hours it insists on it, since the button was
  clearly forgotten.
- "Cancel: I didn't play" closes a session without a snapshot, and it is
  left out of the learning.
- A snapshot problem (ESI down, a character logged out) never blocks the
  session; it is recorded with it.
- Starting and ending go through one service module (`session_flow`) that
  the dashboard and CLI share: it checks first, then takes the snapshot, so
  a mistake never costs one.
- **Sessions are also noticed without the buttons.** ESI's online status
  carries the last login and last logout, and every change is kept in the
  snapshots, so each finished session leaves a (login, logout) pair. Today
  offers them ("Sessions EVE saw you play"): finished, at least 10 minutes,
  within 14 days, not overlapping a recorded session, not dismissed, with
  the plan made from 3 hours before login until logout. **Record it**
  stores the session, noted as having no snapshots at its ends (the outcome
  still comes from timestamped wallet entries and game logs; ore from the
  ledger is often unknown); **Not a session** hides it for good. The server
  works each one out again from the data rather than trusting the form. A
  session is only noticed if the dashboard or a refresh ran between that
  login and the next one.
- ESI caches some data for up to an hour (wallet journal, assets), so the
  end snapshot may not show the whole session yet. Outcomes are worked
  out from timestamped entries once the data has caught up, not at the
  moment "I'm done" is pressed.

**Outcomes** are worked out from data EVE already keeps:

- **ISK:** wallet journal entries dated inside the session (plus two
  minutes for clock differences), grouped as market sales, purchases
  (including buy-order escrow), taxes and fees, contracts, and other.
- **Trades:** wallet transactions inside the session, per item.
- **Ore mined:** the mining ledger only totals per day, so the session's
  share is the ledger after the session minus the ledger at its start.
  Without a ledger snapshot at the start, ore mined is reported as unknown
  rather than guessed. Mining is counted up to two hours after the end, or
  to the next session's start if sooner.
- Ore is valued at EVE's average prices at the time of working out.
- Only amounts, dates, item types and reference types are copied into an
  outcome: never counterparties (other players' IDs) or journal
  descriptions and reasons, which can be player-written.

An outcome is **provisional** until data fetched after ESI's cache time has
been seen (an hour for the journal, ten minutes for the ledger), then
**settled**. It is recomputed after every refresh while provisional, each
change adding an append-only row; settled outcomes are never recomputed.
The Today page lists recent sessions with planned and actual figures; the
per-step comparison is the History page's job.

**Game logs (optional, local mode).** The ledger can't say how fast a
session mined, only how much a day. EVE's game logs have a line per mining
cycle, so when `[logs] gamelogs_dir` is set in `config/local.toml`, every
refresh and session snapshot reads new lines first:

- Only logs whose listener is one of the owner's characters are read, and
  only their "(mining)" lines (mined, critical success, residue). Combat,
  notices and anything naming other people are never stored.
- Logs are read from where the last read stopped, and a half-written last
  line is left for next time. A log from a character who logs in to the
  tool later is picked up then.
- Each outcome then carries the session's cycles, ore per type, residue,
  and m³ per minute both over the whole session and while actually mining.
  On the owner's real logs the per-type totals matched the mining ledger
  exactly.

**Corrections** (built so far: mining speed). Each session whose plan had
a mining step the player didn't decline, with at least ten minutes of
mining in the game logs, gives a ratio: m³ per minute while actually
mining, against the plan's static-data estimate. The factor applied to new
plans starts at 1 and moves towards the average ratio as sessions agree:
`1 + (average − 1) × n / (n + 3)`, kept between 0.5 and 1.5. So one session
moves it a quarter of the way, four sessions a little over half.

- Plans record the **uncorrected** estimate, and the correction as a
  separate reason ("adjusted ×0.86 from 4 of your sessions"). Each new
  session is compared with the static data, never with an already
  corrected figure, so the correction can't feed on itself.
- Without game logs the mining correction stays at 1 and the plan says it
  is the published figure, with how to turn the logs on.
- The Preferences page and `eve-assistant learned` show what has been
  learned per character.
- Still to come: sale prices actually reached, and travel time. Plans
  don't yet record the per-unit price and per-jump time they assumed, so
  those need recording first.

**History page.** Each played session, newest first, with its plan's steps
set against the data:

- **Mining:** ore of that type mined (game logs if read, else the ledger
  difference), and m³ per minute against the estimate.
- **Selling or hauling hangar ore:** units of that type sold, and the ISK
  against the plan's.
- **Courier contracts:** contract ISK in the session. Rewards often land
  in a later session, so no sign of one reads "can't tell", not "failed".
- **Skill reminders and project steps:** not measured.

Each step's verdict is one of "happened", "no sign of it", "waiting for
EVE's data" (not settled yet), "can't tell from the data" or "not
measured". Below that, the mining-speed observations the correction is
built from, session by session.

**Real pace** (`pace.py`). Three figures move from a starting point towards
what the player actually does, blended the same way as the corrections:

- **Play time per week:** clock time of recorded sessions (characters
  playing at the same time count once), averaged over complete weeks with
  at least one session in the last eight. Weeks with no recorded session
  are left out, since they more likely mean the buttons weren't used.
  Blended with the player's setting (default 6 hours), which keeps two
  weeks' weight. Used for courier deadlines and goal dates.
- **Chance of not finishing a project:** dropped against done projects,
  starting from the 10% guess, which keeps three projects' weight. Used to
  discount project steps.
- **Goal dates for ISK:** ISK that reached the wallet per hour played,
  over settled sessions (at least two), times the learned play time. Ore
  still in the hangar isn't counted, so dates err on the late side; the
  Goals page says so.

From that it learns:

1. **Corrections per activity.** "Predicted 8M an hour, made 6M." After a
   few sessions predictions are scaled to the player: travel time, mining
   efficiency, how much prices drop when they sell.
2. **Preferences.** From decline buttons (4.9) and from choices: if the
   player often picks mining over a slightly better haul, it stops treating
   that as a mistake. An optional one-click "was that fun?" at the end of a
   session.
3. **Risk appetite.** Accepting or declining amber and red options moves
   the matrix lines (4.8).
4. **Real play time.** Planned against actual hours, feeding goal dates.
5. **Projects.** How often they finish, how many sessions they really take.

Before it has data it uses published figures and says it is guessing. At a
few hours a week it should become useful within a few weeks.

### 4.7 The dashboard and alerts

A **web dashboard** is the main interface.

| Page | Shows |
|---|---|
| **Today** | Pick a character, enter the time available, get the plan. Above it: what finished, expired or sold since last session; open projects. |
| **Goals** | Progress, estimated arrival date, what is slowing it down, what Omega would change. |
| **Characters** | All characters side by side: training, wallet, net worth, location, ship. |
| **Skills** | Training plan for the current goal, with each skill's payback. |
| **History** | Past plans against results, so the learning can be checked rather than trusted. |
| **Preferences** | Everything declined or muted, the risk matrix lines, with undo. |

Server-rendered pages with a small amount of self-written JavaScript,
everything self-hosted, **no third-party scripts or CDNs**.

**Discord** (optional, via webhook) gets alerts that need action in game:
job finished, order undercut, skill queue empty, goal reached, each linking
back to the dashboard. **Health of the tool itself** (token refresh
failing, errors, rate-limit trouble) goes to a separate, optional push
channel (e.g. ntfy), never mixed with game alerts.

After a game update, a **"what changed for you"** panel lists changes to
items and ships the player owns or plans to buy.

### 4.8 Risk matrix

Each option is rated on two axes.

**How likely is a loss?** Route security; recent kills in those systems,
especially of haulers and miners (zKillboard, last 24 hours and 7 days);
time of day; whether the cargo is worth ganking in highsec. Exordium has
PvP switched off, so it is rated "no player risk" and always green. **Lawless highsec systems cannot be detected** (section
10), which is stated on any route that leaves Exordium.

**How much is exposed, compared with what the character owns?** Ship and
fit, cargo and courier collateral as a share of **net worth** (wallet plus
assets at market value). Losing 20M hurts a new character and barely
registers for an established one.

| Chance of loss \ Exposed share of net worth | under 2% | 2–10% | 10–25% | over 25% |
|---|---|---|---|---|
| **No player risk** (Exordium) | green | green | green | green |
| **Very low** (quiet highsec) | green | green | amber | red |
| **Low** (busy highsec, quiet lowsec) | green | amber | amber | red |
| **Medium** (active lowsec, gank hotspots) | amber | amber | red | red |
| **High** (camped systems, recent hauler kills, most nullsec without local knowledge) | amber | red | red | red |

- **Green**: suggested normally.
- **Amber**: suggested with the reason ("12 hauler kills in Uedama this
  week; cargo 18% of your net worth").
- **Red**: hidden unless the player asks for risky options, then shown
  with the expected loss.

Ranking uses profit minus (chance of loss × amount exposed). The starting
lines are a guess; they move with the player's choices, and every change
is visible on Preferences. A loss is recorded and feeds straight back in.

### 4.9 Declining, and discovering options

Every suggestion has:
- **Do it**: recorded, so the outcome can be checked later.
- **Not now**: no penalty.
- **Not this kind**: mutes a category, specific ("not this route") or
  broad ("no hauling").
- **Why?**: how the estimate was made, with a link to a guide (EVE
  University wiki) for anything unfamiliar.

Mutes live on Preferences with undo. A muted category whose numbers improve
a lot gets one polite mention a month at most, which can also be turned
off.

**Discovering options:** EVE has more ways to make ISK than most players
remember. At most one suggestion per plan is something the player hasn't
done, marked "new to you", and only from activities the tool can model.

---

## 5. Where it gets hard

- **Mining yield** depends on ship, modules, skills and boosts. The main
  multipliers come from the static data, not a Pyfa reimplementation; the
  mining ledger corrects the rest.
- **Selling moves prices.** The tool reads order-book depth and assumes
  the price falls as volume rises.
- **Risk is an estimate** from public kill data, and is presented as one.
- **Market data is regional.** It watches the main hubs plus the
  character's region, and only items the activities use (a few hundred
  types). The regional book includes player structures whose market is
  public; for a structure the character is docked at or stores goods in,
  its own market (structure-markets scope) is merged in by order ID, which
  adds station-range orders and markets that aren't public. Refining at a
  structure isn't offered: its refinery bonuses aren't public.
- **Patches change numbers.** Models read values from the static data, not
  hard-coded tables.
- **The options are endless.** Starting with mining, industry and hauling
  is deliberate.

---

## 6. Running it

### 6.1 Each person runs their own copy

Every copy belongs to one person. Copies never share data with each other.
Each person registers their own ESI application (which means accepting
CCP's developer licence and being responsible for it), and their tokens
never leave their own copy. So there are no user accounts in the data
model.

### 6.2 Local or self-hosted

One codebase, one setting: `mode = "local"` (default) or `mode = "hosted"`.

| | **Local** | **Hosted** (your own server) |
|---|---|---|
| Runs | On your PC, while it is on | Always on |
| Dashboard | `localhost`, this PC only | From anywhere, including a phone, **behind a login** |
| Alerts and background checks | While the PC is on | Around the clock |
| Reading EVE's local log files | Yes | Only via an optional log sender on the gaming PC |
| Secret storage | Windows DPAPI | Encrypted file or the server's secret store |
| Setup | Install and run | Container, reverse proxy, login, backups |

Features declare what they need (*always on*, *reachable remotely*, *local
logs*), and the tool turns on what the mode allows. Anything unavailable
is shown on the dashboard with the reason, never silently missing.

Hosted mode needs three extra things:
1. **A login.** The app refuses to start on a public address without one:
   either trust an existing login at the reverse proxy, or a simple
   built-in one.
2. **Its own ESI application registration.** The developer portal allows
   one callback URL per application, so a hosted copy needs a registration
   with its public `https://…/callback`.
3. **Backups.** The database is one SQLite file; a backup is a scheduled
   copy.

Moving between modes is copying the database file and logging characters
in again. History, goals and learning come along.

### 6.3 Characters and accounts

Any number of characters across any number of accounts. Each character
logs in through EVE's own login page. Characters are grouped into accounts
once in settings, because ESI doesn't say which account a character
belongs to.

Account rules the planner respects (EVE University wiki; to be confirmed
against CCP's current EULA):
- One character per account logged in at a time.
- One skill queue per account, unless Omega with extra training slots.
  The tool warns when a queue sits idle.
- **Alpha accounts cannot multibox**: while one is logged in, no other
  account may be. The planner never suggests it.
- Several Omega accounts can play together (e.g. one mines, one hauls).
  **Planning for several characters at once is a later phase**; the
  account model exists from phase 1 so it can be added cleanly.

### 6.4 Corporations

| Level | What | Plan |
|---|---|---|
| **1. Corp-aware planning** | Corp tax (public in ESI); corp facilities, buyback programs and rules entered once in settings (not in ESI); corp courier contracts as hauling options | Included, phases 2–4 |
| **2. Corp data for officers** | Corp hangars, jobs, moon extraction schedules, wallet, contracts. Needs extra scopes **and** in-game roles. | Deferred, optional module |
| **3. Running a corporation** | Member tracking, recruitment, member tax ledgers, ship replacement | Not built: SeAT, Alliance Auth |

Large corps usually run their own auth, buyback and fitting tools; they
coexist with this one. Corp data comes from CCP's servers through each
person's own login, so "no sharing between copies" still holds.

### 6.5 Nullsec (phase 4)

- **Risk** from kill data, with stricter starting lines (no CONCORD).
- **Jump bridges in routes:** ESI's route planner accepts extra one-way
  connections. An alliance's bridge list is pasted into settings (not
  published in ESI). The September 2026 Ansiblex changes still need
  checking against the patch notes.
- **Sovereignty:** holders are public in ESI. Whether ESI exposes which
  ore anomalies a system's upgrades give is unverified.
- **No live intel.** Reading intel channels means processing other
  players' messages, which conflicts with the privacy rule (section 8).
  The dashboard links to dedicated intel tools.

### 6.6 Later: asking an AI assistant (MCP server)

Not scheduled; kept in mind from the start. The app would expose its data
and planner as an **MCP server** (Model Context Protocol, an open
standard), so a player can ask questions in the assistant they already
use: "refine or sell compressed?", "why the courier tonight?". Answers come
from the app's current data, not the assistant's out-of-date game
knowledge.

| Mode | How | Assistants |
|---|---|---|
| Local | The assistant starts the server on your PC | Claude desktop, Claude Code, Gemini CLI, VS Code / Copilot, Cursor; ChatGPT only via OpenAI's tunnel program |
| Hosted | By web address over HTTPS, behind a login | Claude (Claude Code directly; claude.ai connectors probably connect from Anthropic's servers, so the address must be internet-reachable; verify), ChatGPT (paid plans, Developer Mode), others with remote MCP support |

Rules: read-only tools; opt-in per assistant (data goes to that company);
player-written text (contract descriptions, bios) labelled as such so an
assistant doesn't treat it as instructions; same login as the dashboard;
official MCP library, pinned.

**Do now so it stays cheap:** keep logic in a service layer of plain
read-only functions that the dashboard calls; make planner results carry
structured reasons and inputs; record where each piece of text came from
(ESI, player-written, computed).

---

## 7. Phases

| Phase | What | Done when |
|---|---|---|
| **0. Setup** | Register an ESI application ([setup guide](setup-esi.md)). | Client ID in local config. |
| **1. Foundations and dashboard** | Store, ESI login for several characters, snapshots, static data, market prices, skill trees and training time, goals. Pages: Characters, Goals, Skills. | "To fly a Retriever you need Omega, these 6 skills (11 days) and ~32M ISK", correct per character. |
| **2. First planner** | Mining, ore disposal and courier models; Today page with time limit and projects; risk matrix for highsec and Exordium; decline buttons; every plan recorded. | A 60-minute plan the player agrees is sensible. |
| **3. Learning** | Reconciling plans with ledger, wallet and contracts; per-activity corrections; goal dates; moving risk lines; History page. Built: sessions, outcomes, game logs, mining-speed correction, History, real pace and goal dates. Left: moving risk lines (deferred until there's lowsec data), sale-price and travel-time corrections. | Predictions measurably improve after a few sessions. |
| **3.5 onwards** | Replaced by the milestones in [roadmap.md](roadmap.md) (17 October 2026): foundations (backups, automatic session detection, structure markets, contract tracking), the dashboard's look and flow, a planner restructure with Freelance Jobs and an Omega comparison, risk from real data, new activities (gas, exploration, missions, manufacturing), corporations, nullsec and PI, then alerts and hosted mode. | Each milestone ends with a version tag. |
| **Later** | MCP server; officer corp data. | — |

---

## 8. Rules the project follows

### CCP Developer License Agreement

Summary of developers.eveonline.com/license-agreement, read October 2026.
Not legal advice.

- **Free and non-commercial.** No real-money charges for access or
  features (§4.1–4.2). ISK payments, voluntary donations and unobtrusive
  ads are allowed (§4.4).
- **The app belongs to its developer** (§1.6). CCP may disable apps that
  break the rules without notice (§2.4) and limit API usage (§2.5).
- **Each registrant is responsible for their own app** (§10.1); the
  agreement can't be transferred (§13.7). Each user registers their own.
- **Player privacy:** no tracking of players without express consent
  (§2.3(c)); comply with privacy law (§9.5). **The tool reads only
  characters whose owners log in to it, and never collects other players'
  data.** From Local chat logs it keeps only the player's own system
  changes, never other people's names or messages. The owner's own wallet
  data names other people too (who bought from you, who sent you ISK), so
  wallet snapshots are cleaned before they are stored: other players' IDs,
  journal descriptions (which carry names) and player-written reasons are
  dropped; amounts, dates, item types and reference types are kept. The
  owner's contracts are cleaned the same way: titles (player-written) and
  other players' IDs are dropped. From game logs (optional) only the
  owner's own mining lines are kept.
- **Trademarks:** don't present the app as CCP's or suggest affiliation
  (§2.7, §7.3). README and dashboard footer carry: *"EVE Online and the
  EVE logo are the trademarks or registered trademarks of CCP hf."* (The
  agreement still names CCP hf. despite the Fenris Creations rename.)
- **Open source isn't addressed.** CCP data (static data, images) is
  downloaded at runtime, never committed (§2.1(a)).

### Code licence

**MIT** (`Copyright (c) 2026 EVE Assistant contributors`), added with the
first commit so every contribution is under it.
It covers only code written for this project, not CCP's data, images or
names. MIT allows others to sell modified versions; in practice CCP's
licence still forbids charging real money for any app using ESI or EVE
login (§4.1–4.2), whatever the code licence says.

### Repository

- **Private until the base framework was done, public since 2026-10-16.**
  Before going public, the whole history was scanned to confirm nothing personal or
  secret had ever been committed. Both developers commit with GitHub's private
  "noreply" email address from the first commit.
- Changes arrive through reviewed pull requests, with the repository owner
  as code owner (`CODEOWNERS`). A ruleset on `main` enforces it: pull
  requests only, one code-owner approval, the CI `checks` job must pass,
  squash merges only, no force-pushes or deletion. The repository admin
  can bypass the approval for their own pull requests (GitHub does not let
  anyone approve their own), but never push to `main` directly.
- Tests and a secret scanner run on every pull request; the secret scanner
  also runs before each commit.
- `CONTRIBUTING.md` and a project `CLAUDE.md` so contributors and their AI
  sessions follow the same rules.
- Nothing personal in the repository: client IDs, tokens, webhook URLs and
  personal notes stay in gitignored local files.

### Security: what is in place

- **Login:** PKCE (no client secret exists); access tokens verified against
  EVE's published keys (signature, issuer, audience, expiry); the local
  callback rejects a mismatched `state`; read-only scopes only.
- **Secrets:** refresh tokens encrypted with Windows DPAPI outside the
  project, never printed, logged or put in error messages; secret scanner
  before each commit and in CI.
- **Dashboard:** listens on `127.0.0.1` only and refuses hosted mode until
  a login exists; every form carries a per-process token and cross-site
  posts are rejected; templates escape all output; no third-party scripts.
- **Data:** parameterised SQL; plans, plan items, responses, play sessions,
  session outcomes and snapshots are append-only, enforced by database triggers.
- **Supply chain:** every dependency pinned exactly (direct and indirect,
  `constraints.txt`), versions at least two weeks old; GitHub Actions
  pinned to commit SHAs; CI runs with read-only permissions.
- **Code checks:** ruff with its security rule set (Bandit's) on every
  commit; tests cover forged and expired tokens, cross-site posts and
  escaping of player text.
- **AI assistants:** player-written text is labelled as such wherever it is
  stored or passed on (for the future MCP server).

### Security hardening before going public

1. **Host check (DNS rebinding): done.** The dashboard answers only
   requests addressed to `127.0.0.1`, `localhost` or `::1`. A website that
   points its own domain at 127.0.0.1 still sends its domain in the Host
   header and is refused.
2. **Security headers: done.** Every response carries a strict
   Content-Security-Policy (`default-src 'none'`, only the dashboard's own
   stylesheet, forms only to itself, no framing), plus `X-Frame-Options:
   DENY`, `nosniff`, `Referrer-Policy: same-origin` (no referrer to other sites; `no-referrer` made browsers send `Origin: null` on the dashboard's own forms), same-origin isolation, a
   Permissions-Policy and `Cache-Control: no-store`. Pages contain no
   scripts or inline styles; a test enforces it.
3. **Dependency vulnerability checks: done.** `pip-audit` (approved by the
   owner) checks every pinned library in `constraints.txt` against known
   vulnerabilities on every pull request; Dependabot watches the directly
   chosen libraries and GitHub Actions (weekly, 14-day cooldown to keep the
   two-week rule). Indirect libraries in `constraints.txt` are updated
   deliberately as a matched set, not one at a time.
4. **`SECURITY.md`: done.** Private reporting through GitHub.
5. **Threat model: done** (below).
6. **Security review: done** (below; one finding fixed, the rest recorded).

### Threat model

**What is worth protecting:** the EVE refresh tokens (they give read access
to a character's data for as long as they are valid), the character data
itself (wallet, assets, location: useful to someone planning to rob or gank
the player), and the player's computer.

| Threat | Protection | Remaining risk |
|---|---|---|
| A website in the browser reads or changes the dashboard | Listens on 127.0.0.1 only; Host check against DNS rebinding; form token and Origin check on every change; strict CSP; no framing | None known |
| Another device on the network reaches the dashboard | Bound to 127.0.0.1, not the network | None while in local mode |
| Someone copies the token file | DPAPI ties it to the Windows login | None known |
| Malware running as the same Windows user | Not protected: such a program can ask DPAPI to decrypt, like this tool does | Accepted; out of scope for an app |
| Someone reads `var/eve-assistant.sqlite3` | Holds character data and plan history, **not** tokens or secrets | Unencrypted by design; protect the PC account |
| A malicious or compromised dependency | Exact pins incl. indirect dependencies; two-week-old releases; Actions and hooks pinned to commit SHAs; `pip-audit` in CI; Dependabot | Residual supply-chain risk |
| Secrets committed to git | Pre-commit and CI secret scanner; personal paths blocked; history scan before going public | Depends on the checks running |
| Player-written text (contract titles, structure and ship names) | Escaped in pages; labelled as player-written; never interpreted | An AI assistant (future MCP server) must respect the label |
| Forged or substituted EVE tokens | Signature, issuer, audience, expiry and character checked; PKCE; callback state check | None known |
| Over-use of ESI | Respects cache headers, conditional requests, stops on 420/429 and a low error budget | None known |
| Hosted mode exposed to the internet | Refuses to start in hosted mode until a login exists | Must be designed and reviewed when built |

**Privacy:** the tool stores only data about characters whose owner logged
them in. Public courier contracts (which name other players) are not
cached; a contract's title is kept only inside the plan that suggested it,
labelled as player-written. Wallet history naturally names the other side
of the owner's own trades.

### Security review (2026-10-16)

Reviewed: login and token handling, secret storage, the local callback
server, the ESI client, SQL, templates and forms, the dashboard server,
CI and pre-commit configuration.

| Area | Finding | Outcome |
|---|---|---|
| Dashboard | DNS rebinding could let a website read pages | **Fixed** (Host check) |
| Dashboard | No security headers | **Fixed** (CSP and others) |
| Privacy | Public contracts, naming other players, were kept in the ESI cache | **Fixed** (not cached; old copies removed by migration 0008) |
| Privacy | Wallet snapshots kept other players' IDs (transaction buyers, journal parties), their names inside journal descriptions, and player-written journal reasons | **Fixed** (`snapshots.sanitise` keeps only amounts, dates, types and the owner's own or NPC IDs; these endpoints are not cached; old rows cleaned by migration 0010, with the owner's approval) |
| Supply chain | Pre-commit hooks pinned by tag, not commit | **Fixed** (pinned to commit SHAs) |
| SQL | All queries parameterised; the few built strings use fixed table names or placeholder lists | No change needed |
| Templates | Autoescaping on; no `|safe`; player text escaped (tests) | No change needed |
| Code execution | No `eval`, `exec`, `subprocess`, `pickle` or shell use in the app | No change needed |
| Redirects | All built from fixed paths or validated values | No change needed |
| Tokens | Never printed, logged or put in error messages; login error bodies not echoed | No change needed |
| Callback server | Bound to 127.0.0.1, one request, state checked, 5-minute limit | Recorded: a rebinding site could only abort an in-progress login |
| Error pages | Planning errors show the exception message on the local page | Recorded: acceptable while local-only; revisit for hosted mode |
| Dependencies | No automated vulnerability check in CI | **Fixed** (`pip-audit` in CI; none found at the time of review) |

### Going public checklist

In order; each box is ticked in this document when done.

- [x] Phase 2 finished (hangar sales elsewhere, multi-session projects).
- [x] Security hardening above merged (#14).
- [x] The **whole Git history** scanned for secrets and personal data, not
      just the current files (2026-10-16: 30 commits, 174 file versions,
      every branch; nothing found).
- [x] Every pull request title and description on GitHub read through for
      personal details (they become public too) (2026-10-16: all 19, plus
      comments and reviews; nothing found).
- [x] Repository switched to public (2026-10-16).
- [x] Workflow runs from first-time and outside contributors' pull requests
      need the owner's approval; only GitHub-owned Actions allowed; the
      workflow token is read-only.
- [x] Private vulnerability reporting switched on (`SECURITY.md` relies on
      it).
- [x] `main` protected by a ruleset: changes only through pull requests
      approved by the code owner; the CI `checks` job required to pass;
      no force-pushes or deletion (admin bypass for own pull requests
      only).
- [x] GitHub secret scanning with **push protection** switched on.
- [x] **Dependabot** alerts and security updates switched on.
- [x] **CodeQL** code scanning added (GitHub's default setup).
- [x] **SonarQube Cloud** added: code quality, security hotspots,
      duplication and test coverage, with a pass/fail quality gate on every
      pull request (EU region, CI-based analysis, not Automatic Analysis).
  - [x] SonarQube token stored by the owner as the `SONAR_TOKEN` GitHub
    repository secret (never in chat or the code);
  - [x] `pytest-cov` added as a development library (owner's approval) so
    coverage can be reported;
  - [x] its GitHub Action pinned to a commit SHA like the others, and
    allowed by name in the repository's Actions settings. The step is
    skipped for forks and Dependabot, which do not get the secret;
  - [x] a first pass to fix real findings and mark the rest as reviewed
    (2026-10-16). Fixed: a float equality check, an unused variable,
    duplicated strings, a nested conditional, suppression-comment syntax,
    and test style (one check per assert, one call per `pytest.raises`).
    Deliberate choices are ignored in `sonar-project.properties`, each with
    its reason. Still open on purpose: 16 functions above SonarQube's
    cognitive-complexity limit (the planner's main functions most of all),
    to be split when that code is next changed for another reason.

  Run CodeQL and SonarQube side by side for a few weeks, then drop or tune
  whichever is noisy.

---

## 9. Technical design

### Backups

The history this tool builds can't be fetched again: ESI keeps about 30
days of wallet and mining data, and plans, sessions and outcomes exist
nowhere else. The background task makes a backup once a day (and
`eve-assistant backup` on demand) with SQLite's online-backup API, which is
consistent while the dashboard writes. CCP's static data and the ESI cache
are left out, since they can be downloaded again, so a backup is about 2 MB
against a 25 MB database. Each backup is integrity-checked; the newest of
each of the last 14 days and of each of the last 12 months are kept. A
restore checks the file first, moves the current database aside (never
deletes it), puts the original back if anything fails, and applies newer
migrations. Login tokens live in the secret store, never in the database,
so never in a backup.

### Keeping data current

While the dashboard runs, a background task wakes every minute and fetches
whatever is due, judged from when each kind was last checked (so restarts
don't matter). ESI caches most data anyway, so asking more often gains
nothing:

| Data | Online character | Otherwise |
|---|---|---|
| Wallet, location, ship | every 2 minutes | hourly |
| Online status | every 2 minutes | every 5 minutes (to notice a login) |
| Mining ledger, industry jobs, orders, skill queue | every 15 minutes | hourly |
| Everything else (journal, assets, skills, ...) | hourly | hourly |
| Market prices | every 6 hours | |
| Static data build (CCP's latest) | daily | |

CCP rebuilds the static data with most game deployments; once a day the
background task notes the latest build, and every page shows a banner when
it is newer than the loaded one (or nothing is loaded, or this version of
the tool needs a reload), with the command to run. The 100 MB update itself
stays a terminal command, not a page request.

Game logs are read on every tick. A rate limit stops the tick and the next
one waits as long as ESI asked. All ESI work (the background, the Refresh
button, session snapshots, planning) shares one lock, so login tokens are
never refreshed twice at once. Every page footer says when the last check
ran and shows its first problem, if any. `[app] auto_refresh = false` in
`config/local.toml` turns it off.

### Data sources

- **ESI**, the player's own data. Versioned by date (`X-Compatibility-Date`;
  newest 2026-08-18 when checked), OpenAPI only since August 2026. Rate
  limits per route group per character per 15 minutes, well above what
  this needs. Notifications are tight (15 tokens), not used initially.
- **Static Data Export**: items, skills, blueprints, ore, map. JSON Lines,
  rebuilt with every game deployment.
- **Market**: ESI regional orders and history for hubs and items in use
  (12,000 tokens per 15 minutes for orders, 5-minute cache). Fuzzwork
  aggregates as a fallback.
- **zKillboard**: kills per system, for risk.
- **Local logs** (local mode only): game logs for activity history, Local
  chat logs for the player's own location history. Market logs are not
  used (only written on manual export, and can be blocked by Windows
  Controlled Folder Access).

### ESI login and scopes

The **PKCE** flow needs **no client secret**: the client ID is not secret,
and only refresh tokens are. Tokens are assumed to rotate on every
refresh, and the new one is always saved. Read-only scopes:

| Scope | Why |
|---|---|
| `esi-skills.read_skills.v1`, `esi-skills.read_skillqueue.v1` | What the character can do, training plans |
| `esi-clones.read_clones.v1`, `esi-clones.read_implants.v1` | Training speed, clone locations |
| `esi-assets.read_assets.v1` | Ships, ore, minerals, products owned |
| `esi-wallet.read_character_wallet.v1` | Goal progress, actual sale results |
| `esi-industry.read_character_mining.v1` | Mining ledger, the main learning signal for mining |
| `esi-industry.read_character_jobs.v1`, `esi-characters.read_blueprints.v1` | Manufacturing |
| `esi-markets.read_character_orders.v1` | Own orders; whether sales happened |
| `esi-contracts.read_character_contracts.v1` | Courier outcomes |
| `esi-location.read_location.v1`, `esi-location.read_ship_type.v1`, `esi-location.read_online.v1` | Where, in what, whether a session is happening |
| `esi-fittings.read_fittings.v1` | Fits for ship goals |
| `esi-universe.read_structures.v1` | Naming player stations |
| `esi-characters.read_standings.v1` | Standings lower broker fees and refining tax |
| `esi-markets.structure_markets.v1` | Prices in player-owned markets |

Later, if needed: `esi-planets.manage_planets.v1` (PI), corp scopes for the
officer module. No write scopes, no mail. Adding scopes later means each
character logs in once more.

### Shape

```
  inputs                    core                              outputs
  ESI poller     ─┐                                         ┌─ dashboard (Today, Goals, …)
  static data    ─┤    store (SQLite):                      ├─ CLI
  market prices  ─┼─>  snapshots, goals, plans,  ─> service ─┼─ Discord (game alerts)
  kill data      ─┤    outcomes, corrections       layer    ├─ health channel
  log import     ─┘                                         └─ MCP server (later)
```

- **Python 3.13** with a small set of well-known, **pinned** libraries
  (e.g. a web framework and an HTTP client).
- **One process, one SQLite file**, in both modes.
- **Service layer of plain read-only functions**; the dashboard, CLI and
  later the MCP server are thin layers over it.
- **No user column.** Data is tagged at three levels: **character**
  (skills, assets, wallet, jobs, plans), **account** (Alpha or Omega, which
  characters share it), and **install** (cross-character goals, mutes,
  risk lines, play time, settings).
- **Plans and outcomes are append-only**, so learning history can't be
  quietly rewritten.

### Secrets

Refresh tokens and webhook URLs are entered through a local prompt, never
echoed, and stored outside the project folder, **encrypted with Windows
DPAPI** in local mode. Storage sits behind one small interface, so macOS
or Linux backends can be added when a contributor needs them. In hosted
mode they become protected secrets on the server. The ESI client secret is
not used at all.

---

## 10. Things to know or still to verify

- **Lawless highsec** (corruption level 5): CONCORD doesn't respond to
  ship attacks, and the in-game route overview marks the system ▼ LAWLESS.
  **No API reports it**, so the tool can't check routes for it and reminds
  the player to look.
- Mining ISK-per-hour figures in this document are rough; the tool will
  compute real ones.
- The exact Alpha training-speed penalty.
- Whether ESI exposes Alpha/Omega state directly (otherwise inferred from
  training speed).
- How the mining ledger records compressed and moon ore.
- Whether Exordium's lower payouts apply to mining.
- The final September 2026 Ansiblex numbers.
- Account rules (6.3) against CCP's current EULA.

---

## 11. Existing tools: use, don't rebuild

Checked October 2026.

| Need | Use | Status |
|---|---|---|
| Ship fitting and stats | **Pyfa**, EVEShip.fit, Theorycrafter | Pyfa released Oct 2026; EVEShip.fit Sep 2026 |
| Appraisal | **Janice** | Up. Evepraisal shut down in 2023. |
| Industry cost tables | Fuzzwork, EVE-IPH | Active |
| Market browsing and arbitrage | EVE TradeLooper, ISK.GG, Marquette, Space Markets | Active |
| Map and kills | Dotlan, zKillboard | Up |
| Companion apps with alerts | EVE Empire, Neocom Desk, EVE Buddy | Active; none sends to Discord, sets goals or plans sessions |
| Corp management | SeAT, Alliance Auth | — |
| Live intel | RIFT, Telescope, EVE Empire | — |

## Sources

Official: [Cradle of War expansion notes](https://www.eveonline.com/news/view/cradle-of-war-expansion-notes),
[25% Off All Omega in NES](https://www.eveonline.com/news/view/25-off-all-omega-in-nes),
[Introducing Clone States](https://www.eveonline.com/news/view/introducing-clone-states-and-the-future-of-access-to-eve-online),
[Introducing Exordium](https://www.eveonline.com/news/view/introducing-exordium),
[Force Projection with FC Okami](https://www.eveonline.com/news/view/the-future-of-force-projection-with-fc-okami),
[EVE developers blog](https://developers.eveonline.com/blog),
[ESI rate limiting](https://developers.eveonline.com/docs/services/esi/rate-limiting/),
[EVE SSO](https://developers.eveonline.com/docs/services/sso/),
[Developer License Agreement](https://developers.eveonline.com/license-agreement),
[Community tools](https://developers.eveonline.com/docs/community/).

Community: [EVE University: Skill points](https://wiki.eveuniversity.org/SP),
[EVE University: Corruption and Suppression](https://wiki.eveuniversity.org/Corruption_and_Suppression),
[Brave Collective mining guide](https://wiki.bravecollective.com/public/corps/brave-empire/what-to-do/mining),
[Neocom Desk](https://forums.eveonline.com/t/neocom-desk-industry-mining-markets-courier-skills-mobile-and-desktop/517950),
[EVE Empire](https://forums.eveonline.com/t/eve-empire-cross-platform-companion-app-for-eve-online/511921),
[EVE-IPH](https://github.com/EVEIPH/EVE-IPH).
