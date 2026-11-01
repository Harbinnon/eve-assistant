"""Loading config/local.toml.

The file holds per-person settings that are not secret but stay out of git
(the ESI client ID, the contact for the User-Agent). Secrets never live here;
see secrets_store.

A hosted copy keeps its config and data outside the code, where the server
puts them: EVE_ASSISTANT_CONFIG names the config file and EVE_ASSISTANT_DATA
the data folder (default: config/local.toml and var/ in the project).
"""

import ipaddress
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = Path(
    os.environ.get("EVE_ASSISTANT_CONFIG") or PROJECT_ROOT / "config" / "local.toml"
)
DATA_DIR = Path(os.environ.get("EVE_ASSISTANT_DATA") or PROJECT_ROOT / "var")
DEFAULT_CALLBACK_URL = "http://localhost:8765/callback"
PLACEHOLDER_PREFIX = "<"


class ConfigError(Exception):
    """config/local.toml is missing or incomplete. The message says how to fix it."""


@dataclass(frozen=True)
class Hosted:
    """Where a hosted copy listens and who may reach it (roadmap 7.2)."""

    hostname: str  # the public name, e.g. eve.example.com
    listen: str  # the address to listen on: the server's own, or 0.0.0.0 for all of them
    proxy: str  # the reverse proxy's address: the only way in to the dashboard
    home_network: str  # e.g. 192.168.1.0/24: where the log sender may connect from
    owner_discord_id: str  # the sign-in's X-Auth-Id that this copy belongs to
    port: int = 8700
    dev_identity: str | None = None  # stands in for the sign-in; only on 127.0.0.1
    lan_address: str | None = None  # the address the log sender uses (needed with 0.0.0.0)

    @property
    def public_origin(self) -> str:
        return f"https://{self.hostname}"

    @property
    def ingest_host(self) -> str:
        """The host name the log sender's requests carry: the server's own address."""
        return self.lan_address or self.listen


@dataclass(frozen=True)
class Config:
    client_id: str
    callback_url: str
    contact: str | None
    mode: str
    gamelogs_dir: Path | None = None  # EVE's Gamelogs folder; None: don't read logs
    auto_refresh: bool = True  # refresh from EVE in the background while the dashboard runs
    backups_dir: Path | None = None  # None: var/backups next to the database
    planets: bool = False  # ask for the Planetary Interaction scope at login
    hosted: Hosted | None = None  # set in hosted mode
    send_logs_url: str | None = None  # the hosted copy, for send-logs on the gaming PC
    send_logs_characters: tuple[str, ...] = ()  # whose logs send-logs sends (with any logged in)
    # With auto_refresh: load CCP's new static data by itself, when not playing.
    auto_update_static_data: bool = True


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
    auto_static = app.get("auto_update_static_data", True)
    if not isinstance(auto_static, bool):
        raise ConfigError(f"[app] auto_update_static_data in {path} must be true or false.")
    planets = esi.get("planets", False)
    if not isinstance(planets, bool):
        raise ConfigError(f"[esi] planets in {path} must be true or false.")
    hosted = _hosted(data.get("hosted", {}), path) if mode == "hosted" else None
    callback_url = esi.get("callback_url", DEFAULT_CALLBACK_URL)
    if hosted is not None and callback_url != f"{hosted.public_origin}/callback":
        raise ConfigError(
            f"[esi] callback_url in {path} must be {hosted.public_origin}/callback in hosted "
            "mode (and registered on that ESI application)."
        )
    send_logs = data.get("send_logs", {})
    send_logs_url = send_logs.get("url")
    return Config(
        client_id=client_id.strip(),
        callback_url=callback_url,
        contact=None if _is_placeholder(contact) else contact.strip(),
        mode=mode,
        gamelogs_dir=None if _is_placeholder(gamelogs) else Path(gamelogs.strip()),
        auto_refresh=auto_refresh,
        backups_dir=None if _is_placeholder(backups) else Path(backups.strip()),
        planets=planets,
        hosted=hosted,
        send_logs_url=None if _is_placeholder(send_logs_url) else send_logs_url.strip(),
        send_logs_characters=_names(send_logs.get("characters", []), path),
        auto_update_static_data=auto_static,
    )


def _names(value, path: Path) -> tuple[str, ...]:
    """[send_logs] characters: a list of character names; placeholders are left out."""
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise ConfigError(
            f'[send_logs] characters in {path} must be a list of names, e.g. ["Your Main", '
            '"Your Alt"].'
        )
    return tuple(dict.fromkeys(n.strip() for n in value if not _is_placeholder(n)))


def _address(section: dict, key: str, path: Path, network: bool = False) -> str:
    value = str(section.get(key, "")).strip()
    try:
        (ipaddress.ip_network if network else ipaddress.ip_address)(value)
    except ValueError:
        kind = "a network like 192.168.1.0/24" if network else "an IP address"
        raise ConfigError(f"[hosted] {key} in {path} must be {kind}.") from None
    return value


def _hosted(section: dict, path: Path) -> Hosted:
    """The [hosted] section, checked: a hosted copy refuses to start on a guess."""
    for key in ("hostname", "owner_discord_id"):
        if _is_placeholder(str(section.get(key, ""))):
            raise ConfigError(f"[hosted] {key} is missing in {path}.")
    hosted = Hosted(
        hostname=str(section["hostname"]).strip(),
        listen=_address(section, "listen", path),
        proxy=_address(section, "proxy", path),
        home_network=_address(section, "home_network", path, network=True),
        owner_discord_id=str(section["owner_discord_id"]).strip(),
        port=int(section.get("port", 8700)),
        dev_identity=section.get("dev_identity"),
        lan_address=_address(section, "lan_address", path) if "lan_address" in section else None,
    )
    if ipaddress.ip_address(hosted.listen).is_unspecified and hosted.lan_address is None:
        raise ConfigError(
            f"[hosted] lan_address is missing in {path}: with listen = {hosted.listen}, the app "
            "needs the server's own address to accept the log sender's requests."
        )
    if hosted.dev_identity and not ipaddress.ip_address(hosted.listen).is_loopback:
        raise ConfigError(
            f"[hosted] dev_identity in {path} only works when listen is 127.0.0.1: it stands in "
            "for the sign-in, so it must never be reachable from another machine."
        )
    return hosted
