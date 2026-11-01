# EVE Assistant

*Working name.* A planning assistant for EVE Online players who mine, build
and haul. Set a goal, say how long you can play, and it reads your
characters through CCP's API (ESI) and suggests the best use of that time,
then learns from what actually happened.

**Status: version 0.11. Built: login, snapshots, skill plans, goals, the
dashboard, the session planner (mining, ore, couriers, Freelance Jobs, your
whole fleet, multi-session projects, risk ratings, decline buttons),
learning from results (play sessions, noticed from EVE's login times;
outcomes; mining game logs; corrections to mining speed, sale prices and
travel time; History), the dashboard's look and flow (a dark "capsuleer
console" theme with a light one, a session view while you play, portraits
and ships, a phone layout), a planner ready to grow (activities as
modules, who should play today, what Omega would change and Omega goals in
PLEX, what to train next, net worth over time), and risk from real data
(kills in the last hour, incursions, insurance payouts, your own losses,
routes around camps, risk lines that move with your choices), and more
things to do (gas harvesting, exploration, distribution missions with LP
store values, manufacturing from your blueprints, military campaigns), and a trade tool
(trade runs between the hubs, station trading, your orders watched, the
best run in plans), and corporations, nullsec and Planetary Interaction
(corp tax and your corp's buyback, the sovereignty map and jump bridges,
your colonies' extractor timers and what they make), and alerts and hosted
mode (in-game actions to Discord, the tool's health to ntfy, an always-on
copy on your own server behind its sign-in). Next: running it on the
server for real, as 1.0.** The design is in
[docs/design.md](docs/design.md) and the plan from here in
[docs/roadmap.md](docs/roadmap.md).

## What it will do

- Goals such as "earn the ISK for a month of Omega" or "buy and fly a
  Retriever", with the skills and ISK each needs.
- "I have an hour": a plan for that hour across mining, industry and
  hauling, including longer projects spread over several sessions.
- Risk ratings that weigh what you could lose against what you own.
- Learning from your real results, so estimates fit how *you* play.

Each person runs their own copy, on their own PC (local mode) or their own
server (hosted mode, later). Copies never share data.

## Setup

Requires Python 3.13 on Windows. (Other platforms need a secret-storage
backend that doesn't exist yet.)

1. Register your own ESI application: [docs/setup-esi.md](docs/setup-esi.md).
2. Copy `config/local.example.toml` to `config/local.toml` and fill in your
   client ID. `config/local.toml` is gitignored.
3. Create a virtual environment and install:

   ```
   py -3.13 -m venv .venv
   .venv\Scripts\python -m pip install -c constraints.txt -e .[dev]
   .venv\Scripts\pre-commit install
   .venv\Scripts\python -m pytest
   ```

## First run

Once, after setup:

1. `eve-assistant sde-update` downloads CCP's static data (about 100 MB).
   After that, the dashboard loads CCP's newer builds by itself when you're
   not in a play session (or press **Update now** on the banner).
2. `eve-assistant login` logs a character in (or, once the dashboard is
   running, **Add a character** on its Characters page). Repeat for each
   character.
3. On the Characters page, **Set account** on each character: give
   characters on the same EVE account the same name, and say whether the
   account is Alpha or Omega, so plans respect Alpha limits and characters
   can play together. (Or `eve-assistant account assign <character id>
   <account name>` and `eve-assistant account state <account name> alpha`.)
4. Optional: set `[logs] gamelogs_dir` in `config/local.toml` to EVE's
   Gamelogs folder (usually `Documents\EVE\logs\Gamelogs`), so mining
   rates are learned cycle by cycle.
5. `eve-assistant shortcut` puts an **EVE Assistant** shortcut on the
   desktop.

## Every time you play

1. Double-click **EVE Assistant** on the desktop (or run
   `eve-assistant serve --open`). It starts the dashboard and opens
   <http://127.0.0.1:8700> in your browser; if it is already running, it
   just opens it. Its window starts minimised: closing that window stops
   the dashboard.
2. On **Today**, pick a character, say how long you have and press
   **Plan my time**. Press **Start this plan** when you begin: Today
   becomes the session view, with the time played, the step to do now and
   a checklist to tick off. Press **I'm done** when you stop.
3. Forgot to press them? While the dashboard runs it notices when you log
   in and out of EVE, and Today offers to record that session.

While it runs, the dashboard keeps your data current from EVE and backs
the database up once a day. **History** shows each plan against what
happened. **Ore ledger** keeps track of ore mined with friends over
several sessions and, once it's sold, works out who gets how much.

## Commands so far

```
eve-assistant login                    log a character in (opens EVE's login page)
eve-assistant characters [--check]     list logged-in characters, optionally test tokens
eve-assistant logout <character id>    forget a character's token on this PC
eve-assistant alerts-setup             enter the Discord webhook and ntfy topic (hidden prompt)
eve-assistant alerts-test              send a test alert to each channel set up
eve-assistant serve --hosted           run as the hosted copy (on your server)
eve-assistant send-logs [--watch 5]    send new mining lines to the hosted copy
eve-assistant send-logs --setup        enter the hosted copy's log token (hidden)
eve-assistant sde-update               download and load CCP's static data (~100 MB)
eve-assistant snapshot                 record every character's current state
eve-assistant accounts                 show characters grouped by account
eve-assistant account assign <id> <name>         put a character on an account
eve-assistant account state <name> alpha|omega   mark an account Alpha or Omega
eve-assistant skills <character> "<item>"        what it takes to use an item
eve-assistant goal add ship "<ship>" --character <name>
eve-assistant goal add skill "<skill>" <level> --character <name>
eve-assistant goal add isk <amount> [--character <name>]
eve-assistant goals                    progress on every active goal
eve-assistant goal done|dropped <id>   close a goal
eve-assistant ore <character> "<ore>" <qty> [--station Jita] [--allow-lowsec]
eve-assistant mining <character> [--ship "Venture" --module "Miner I" ...] [--ore Veldspar]
eve-assistant couriers <character> [--region "The Forge"] [--minutes 60] [--ship "Tayra"]
eve-assistant plan <character> <minutes> [--allow-lowsec] [--risky] [--ore "Veldspar"]
eve-assistant plan-together <minutes> <character> <character> ...   characters on separate Omega accounts
eve-assistant respond <item> do|not_now|mute   record what you did with a suggestion
eve-assistant mute|unmute <category>  / eve-assistant mutes
eve-assistant projects                 list open multi-session projects
eve-assistant project done|dropped <id>
eve-assistant play-time <hours>        your usual play time per week (default 6)
eve-assistant session start <character> [--plan <id>|--no-plan]   start playing (snapshot)
eve-assistant session finish <character> [--minutes <played>]     stop playing (snapshot)
eve-assistant session cancel <character>                          discard a session not played
eve-assistant sessions                 list recent play sessions
eve-assistant detected [record|dismiss <n>]  sessions noticed from EVE's login times
eve-assistant outcomes                 what happened in recent sessions, against the plan
eve-assistant gamelogs                 read mining cycles from EVE's game logs (optional)
eve-assistant learned                  what the planner has learned from your sessions
eve-assistant serve [--port 8700] [--open]   run the dashboard at http://127.0.0.1:8700
eve-assistant shortcut [--folder <path>]     a desktop shortcut that starts it (Windows)
```

Data lives in `var/` (the database and downloaded static data) and is
gitignored. Tokens are stored encrypted outside the project.

### Corporations, nullsec and planets

- **Corp tax** is found by itself; a **buyback** (the share of Jita's buy
  price your corporation pays for ore) goes in Settings.
- **Jump bridges:** paste your alliance's list in Settings, one bridge a
  line (`1DQ1-A » 8QT-H4`).
- **Planetary Interaction** is off until you switch it on, because it needs
  one more permission: tick `esi-planets.manage_planets.v1` on your ESI
  application (docs/setup-esi.md), then add `planets = true` under `[esi]`
  in `config/local.toml`, restart the dashboard and log each character in
  again.

### Backups

The dashboard backs the database up once a day into `var/backups` (or the
folder set under `[backups]` in `config/local.toml`; another drive or a
synced folder also survives a disk failure). It keeps the newest backup of
each of the last 14 days and of each of the last 12 months. Backups leave out
CCP's static data, which can be downloaded again, so they are small.

```
eve-assistant backup                   back up now
eve-assistant backups                  list backups
eve-assistant restore <file> --confirm put a backup back (close the dashboard first)
eve-assistant sde-update               after a restore: reload the static data
```

A restore never deletes your current database; it is moved aside as
`eve-assistant.before-restore-<time>.sqlite3`. Login tokens are never in a
backup; on another PC, log in again.

Contributing: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Code: [MIT](LICENSE). EVE data and images are not part of this repository;
they are downloaded from CCP at runtime.

EVE Online and the EVE logo are the trademarks or registered trademarks of
CCP hf. This project is not affiliated with or endorsed by CCP.
