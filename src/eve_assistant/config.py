"""Loading config/local.toml.

The file holds per-person settings that are not secret but stay out of git
(the ESI client ID, the contact for the User-Agent). Secrets never live here;
see secrets_store.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "local.toml"
DEFAULT_CALLBACK_URL = "http://localhost:8765/callback"
PLACEHOLDER_PREFIX = "<"


class ConfigError(Exception):
    """config/local.toml is missing or incomplete. The message says how to fix it."""


@dataclass(frozen=True)
class Config:
    client_id: str
    callback_url: str
    contact: str | None
    mode: str
    gamelogs_dir: Path | None = None  # EVE's Gamelogs folder; None: don't read logs
    auto_refresh: bool = True  # refresh from EVE in the background while the dashboard runs
    backups_dir: Path | None = None  # None: var/backups next to the database


def _is_placeholder(value: str | None) -> bool:
    return not value or value.strip().startswith(PLACEHOLDER_PREFIX)


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy config/local.example.toml to config/local.toml "
            "and fill in your ESI client ID (see docs/setup-esi.md)."
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    esi = data.get("esi", {})
    app = data.get("app", {})

    client_id = esi.get("client_id")
    if _is_placeholder(client_id):
        raise ConfigError(
            f"No ESI client ID in {path}. Register an application (docs/setup-esi.md) "
            "and put its client ID under [esi] client_id."
        )

    mode = app.get("mode", "local")
    if mode not in ("local", "hosted"):
        raise ConfigError(f'[app] mode in {path} must be "local" or "hosted", not "{mode}".')

    contact = esi.get("contact")
    gamelogs = data.get("logs", {}).get("gamelogs_dir")
    backups = data.get("backups", {}).get("folder")
    auto_refresh = app.get("auto_refresh", True)
    if not isinstance(auto_refresh, bool):
        raise ConfigError(f"[app] auto_refresh in {path} must be true or false.")
    return Config(
        client_id=client_id.strip(),
        callback_url=esi.get("callback_url", DEFAULT_CALLBACK_URL),
        contact=None if _is_placeholder(contact) else contact.strip(),
        mode=mode,
        gamelogs_dir=None if _is_placeholder(gamelogs) else Path(gamelogs.strip()),
        auto_refresh=auto_refresh,
        backups_dir=None if _is_placeholder(backups) else Path(backups.strip()),
    )
