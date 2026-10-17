# 0001: Libraries

Status: **approved by the repository owner, 2026-10-15.**
Date: 2026-10-15.

The rule from the design: Python 3.13, a **small set of well-known
libraries, pinned exactly**, standard library wherever it does the job.
Versions below are the newest releases at least two weeks old on the date
above.

## Proposed runtime libraries

| Library | Version | What for | Why this one |
|---|---|---|---|
| **FastAPI** | 0.142.2 | Web framework for the dashboard and, later, the API the MCP server and log sender use | Widely known, so a contributor can work in it quickly. Async, which suits one process that serves pages *and* polls ESI in the background. Brings Starlette (its foundation) with it. |
| **Uvicorn** | 0.54.0 | The web server that runs FastAPI | The standard pairing with FastAPI. |
| **Jinja2** | 3.1.6 | HTML templates for the server-rendered pages | The usual Python template engine; FastAPI supports it directly. |
| **python-multipart** | 0.0.32 | Reading form submissions (decline buttons, goal forms) | FastAPI needs it for HTML forms. |
| **HTTPX** | 0.28.1 | Calling ESI, the static data download, zKillboard, Fuzzwork | Async and sync in one client, HTTP/2, easy to mock in tests. |
| **PyJWT** with **cryptography** | 2.15.1 / 50.0.2 | Checking the signature and claims of EVE login tokens | CCP's SSO docs say to verify the token's RSA signature against EVE's published keys. Doing RSA by hand is not an option. |

## Proposed development libraries

Already in use: **pytest** 9.1.1, **ruff** 0.16.9, **pre-commit** 4.6.2,
**detect-secrets** 1.5.0.

To add: **RESPX** 0.23.1, which mocks HTTPX calls so tests never touch the
real ESI.

Added 2026-10-16 with the owner's approval: **pip-audit** 2.10.1, which checks
every pinned library for known vulnerabilities in CI.

## Standard library instead of a package

| Need | Standard library | Instead of |
|---|---|---|
| Database | `sqlite3`, with numbered SQL migration files and a ~50-line runner | SQLAlchemy + Alembic |
| Config | `tomllib` | pydantic-settings, dynaconf |
| Secret storage on Windows | `ctypes` calling DPAPI | pywin32, keyring |
| Background polling | `asyncio` tasks started with the web app | APScheduler, Celery |
| PKCE login | `secrets`, `hashlib`, `base64`, `webbrowser` | an OAuth library |
| CLI | `argparse` | Click, Typer |
| Logging | `logging` | loguru |

**Why plain `sqlite3` rather than SQLAlchemy:** the schema is modest, much
of it is append-only, and plain SQL is easy for any contributor to read
and review. SQLAlchemy is the obvious upgrade if the schema grows or a
second database (PostgreSQL in hosted mode) is ever needed. Moving later
means rewriting the store module, not the rest.

## Pinning transitive dependencies

`pyproject.toml` pins the libraries we choose directly. Their own
dependencies are pinned in a `constraints.txt` generated with
`pip freeze`. CI installs with `pip install -c constraints.txt -e .[dev]`,
so everyone runs the same versions. Updating is a deliberate pull request.

## Decided later, not now

- **htmx** (a small JavaScript file, saved in the repository, never loaded
  from a CDN) for in-page interactions such as decline buttons without
  writing much JavaScript. Decide in phase 2, when those pages exist.
- **mcp** (the official MCP library) for the later MCP server.
- **A type checker** (mypy or pyright). Worth adding once there is real
  code; it is a development tool only.

## Considered and not chosen

- **Flask:** simpler, but synchronous. Background polling alongside a sync
  web app needs threads or a second process.
- **Django:** far more than a single-person local app needs.
- **uv** for packaging: fast and gives a proper lock file, but it is not
  installed here and adds a tool both developers must learn. `pip` plus
  `constraints.txt` does the job. Easy to revisit.
