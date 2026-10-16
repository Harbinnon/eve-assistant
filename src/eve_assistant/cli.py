"""Command line: eve-assistant <command>."""

import argparse
import sys

from eve_assistant import __version__, auth
from eve_assistant.config import ConfigError, load_config
from eve_assistant.esi.sso import SSOError
from eve_assistant.secrets_store import SecretStoreError, default_store


def _cmd_login(_args) -> int:
    config = load_config()
    store = default_store()
    print("Opening EVE's login page in your browser. Pick the character to add.")
    print("Waiting up to 5 minutes...")
    with auth.http_client(config) as client:
        character = auth.login_character(config, store, client)
    print(f"Logged in: {character.name} ({character.character_id}).")
    if character.missing_scopes:
        print("Warning: these permissions were not granted, so some features won't work:")
        for scope in character.missing_scopes:
            print(f"  {scope}")
        print("Check the scopes on your ESI application (docs/setup-esi.md), then log in again.")
    if config.contact is None:
        print("Tip: set [esi] contact in config/local.toml so CCP can reach you about API use.")
    return 0


def _cmd_characters(args) -> int:
    store = default_store()
    characters = auth.list_characters(store)
    if not characters:
        print("No characters logged in. Run: eve-assistant login")
        return 0
    config = load_config() if args.check else None
    failures = 0
    for character in characters:
        line = f"{character.name} ({character.character_id})"
        if config is not None:
            try:
                with auth.http_client(config) as client:
                    auth.access_token(config, store, client, character.character_id)
                line += "  token OK"
            except SSOError as exc:
                failures += 1
                line += f"  token FAILED: {exc}"
        if character.missing_scopes:
            line += f"  ({len(character.missing_scopes)} permissions missing)"
        print(line)
    return 1 if failures else 0


def _cmd_logout(args) -> int:
    store = default_store()
    if auth.logout(store, args.character_id):
        print(f"Removed the stored token for {args.character_id} from this PC.")
        print(f"To revoke the app's access entirely, also visit {auth.REVOKE_PAGE}")
        return 0
    print(f"No stored token for {args.character_id}.")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eve-assistant", description="EVE Assistant")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("login", help="log a character in through EVE's login page").set_defaults(
        func=_cmd_login
    )

    characters = commands.add_parser("characters", help="list logged-in characters")
    characters.add_argument(
        "--check", action="store_true", help="also check each character's token with EVE"
    )
    characters.set_defaults(func=_cmd_characters)

    logout = commands.add_parser("logout", help="forget a character's token on this PC")
    logout.add_argument("character_id", type=int)
    logout.set_defaults(func=_cmd_logout)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, SSOError, SecretStoreError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
