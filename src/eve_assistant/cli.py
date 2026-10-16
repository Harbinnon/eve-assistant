"""Command line: eve-assistant <command>."""

import argparse
import sys

from eve_assistant import __version__, accounts, auth, sde, snapshots, store
from eve_assistant.accounts import AccountError
from eve_assistant.config import ConfigError, load_config
from eve_assistant.esi.client import EsiClient, EsiError
from eve_assistant.esi.sso import SSOError
from eve_assistant.sde import SdeError
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


def _cmd_sde_update(args) -> int:
    config = load_config()
    with store.connect() as db, auth.http_client(config) as client:
        current = sde.loaded_build(db)
        print(f"Loaded static data: {current.build_number if current else 'none'}.")
        print("Checking for a newer build (the first download is about 100 MB)...")
        build, loaded = sde.update(db, client, force=args.force)
    print(
        f"Static data build {build.build_number} ({build.release_date}) "
        + ("loaded." if loaded else "is already current.")
    )
    return 0


def _cmd_snapshot(_args) -> int:
    config = load_config()
    secrets = default_store()
    characters = auth.list_characters(secrets)
    if not characters:
        print("No characters logged in. Run: eve-assistant login")
        return 0
    failed = 0
    with store.connect() as db, auth.http_client(config) as client:
        tokens = auth.AccessTokens(config, secrets, client)
        esi = EsiClient(client, db, token_for=tokens.get)
        for character in characters:
            snapshots.ensure_character(db, character.character_id, character.name)
            results = snapshots.collect(db, esi, character.character_id)
            changed = [r.kind for r in results if r.outcome == "changed"]
            problems = [r for r in results if r.outcome == "failed"]
            failed += len(problems)
            print(
                f"{character.name}: {len(changed)} changed, "
                f"{len(results) - len(changed) - len(problems)} unchanged, "
                f"{len(problems)} failed"
            )
            for problem in problems:
                print(f"  {problem.kind}: {problem.detail}")
    return 1 if failed else 0


def _cmd_accounts(_args) -> int:
    with store.connect() as db:
        views = accounts.overview(db)
    if not views:
        print("No characters yet. Run `eve-assistant snapshot` first.")
        return 0
    for view in views:
        print(f"{view.label} [{view.clone_state}]: {', '.join(view.characters)}")
    return 0


def _cmd_account_assign(args) -> int:
    with store.connect() as db:
        accounts.assign(db, args.character_id, args.label)
    print(f"Character {args.character_id} is now on account '{args.label}'.")
    return 0


def _cmd_account_state(args) -> int:
    with store.connect() as db:
        accounts.set_clone_state(db, args.label, args.state)
    print(f"Account '{args.label}' is now marked {args.state}.")
    return 0


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

    sde_update = commands.add_parser("sde-update", help="download and load CCP's static data")
    sde_update.add_argument("--force", action="store_true", help="reload even if current")
    sde_update.set_defaults(func=_cmd_sde_update)

    commands.add_parser(
        "snapshot", help="record the current state of every logged-in character"
    ).set_defaults(func=_cmd_snapshot)

    commands.add_parser("accounts", help="show characters grouped by account").set_defaults(
        func=_cmd_accounts
    )
    account = commands.add_parser("account", help="set up accounts").add_subparsers(
        dest="account_command", required=True
    )
    assign = account.add_parser("assign", help="put a character on an account")
    assign.add_argument("character_id", type=int)
    assign.add_argument("label", help="any name for the account, e.g. main")
    assign.set_defaults(func=_cmd_account_assign)
    state = account.add_parser("state", help="mark an account Alpha or Omega")
    state.add_argument("label")
    state.add_argument("state", choices=accounts.CLONE_STATES)
    state.set_defaults(func=_cmd_account_state)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, SSOError, SecretStoreError, EsiError, SdeError, AccountError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
