# EVE Assistant

*Working name.* A planning assistant for EVE Online players who mine, build
and haul. Set a goal, say how long you can play, and it reads your
characters through CCP's API (ESI) and suggests the best use of that time,
then learns from what actually happened.

**Status: phase 1 in progress. Nothing usable yet.** The design is in
[docs/design.md](docs/design.md).

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

Contributing: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Code: [MIT](LICENSE). EVE data and images are not part of this repository;
they are downloaded from CCP at runtime.

EVE Online and the EVE logo are the trademarks or registered trademarks of
CCP hf. This project is not affiliated with or endorsed by CCP.
