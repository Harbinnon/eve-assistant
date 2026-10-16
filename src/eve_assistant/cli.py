"""Command line: eve-assistant <command>."""

import argparse
import sys

from eve_assistant import (
    __version__,
    accounts,
    auth,
    goals,
    market,
    ore,
    refresh,
    sde,
    skills,
    snapshots,
    store,
)
from eve_assistant.accounts import AccountError
from eve_assistant.config import ConfigError, load_config
from eve_assistant.esi.client import EsiClient, EsiError
from eve_assistant.esi.sso import SSOError
from eve_assistant.goals import GoalError
from eve_assistant.sde import SdeError
from eve_assistant.secrets_store import SecretStoreError, default_store
from eve_assistant.skills import SkillsError


def _cmd_login(_args) -> int:
    config = load_config()
    secrets = default_store()
    print("Opening EVE's login page in your browser. Pick the character to add.")
    print("Waiting up to 5 minutes...")
    with auth.http_client(config) as client:
        character = auth.login_character(config, secrets, client)
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
    secrets = default_store()
    characters = auth.list_characters(secrets)
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
                    auth.access_token(config, secrets, client, character.character_id)
                line += "  token OK"
            except SSOError as exc:
                failures += 1
                line += f"  token FAILED: {exc}"
        if character.missing_scopes:
            line += f"  ({len(character.missing_scopes)} permissions missing)"
        print(line)
    return 1 if failures else 0


def _cmd_logout(args) -> int:
    secrets = default_store()
    if auth.logout(secrets, args.character_id):
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
    report = refresh.refresh_all(load_config(), default_store())
    for line in report.lines:
        print(line)
    if report.priced:
        print(f"Market prices: {report.priced:,} items.")
    for problem in report.problems:
        print(f"  {problem}")
    return 1 if report.problems else 0


def _cmd_serve(args) -> int:
    import uvicorn

    from eve_assistant.web import create_app

    config = load_config()
    if config.mode != "local":
        print("Hosted mode is not built yet: the dashboard has no login, so it only runs locally.")
        return 2
    secrets = default_store()

    def do_refresh() -> list[str]:
        return refresh.refresh_all(config, secrets).problems

    print(f"Dashboard: http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(
        create_app(refresh=do_refresh), host="127.0.0.1", port=args.port, log_level="warning"
    )
    return 0


def _print_requirement(req: skills.Requirement, is_alpha: bool) -> None:
    if req.usable_now:
        print("  Can use it now.")
        return
    if req.usable_with_omega_now:
        print("  Skills are trained; Omega would unlock it straight away.")
    for step in req.steps:
        flag = "" if step.allowed_for_alpha else "  (Omega only)"
        print(
            f"  {step.skill_name} {step.level}: {step.skillpoints:,} SP, "
            f"{skills.format_minutes(step.minutes_omega)} as Omega{flag}"
        )
    if req.steps:
        print(f"  Total as Omega: {skills.format_minutes(req.minutes_as_omega)}")
        if is_alpha:
            if req.minutes_as_alpha is None:
                print("  As Alpha: not possible.")
            else:
                print(
                    f"  Total as Alpha: about {skills.format_minutes(req.minutes_as_alpha)} "
                    "(Alpha speed assumed half; not yet verified)"
                )
    for reason in req.reasons:
        print(f"  - {skills.describe(reason)}")


def _cmd_skills(args) -> int:
    with store.connect() as db:
        character_id = goals.resolve_character(db, args.character)
        character = skills.load_character_skills(db, character_id)
        type_id = skills.find_type(db, args.item)
        req = skills.plan(db, character, type_id)
        print(f"{req.type_name} for character {character_id} [{character.clone_state}]:")
        _print_requirement(req, character.is_alpha)
    return 0


def _cmd_goal_add(args) -> int:
    with store.connect() as db:
        character_id = goals.resolve_character(db, args.character) if args.character else None
        if args.kind == "ship":
            goal_id = goals.add_goal(
                db, "fly_ship", character_id, type_id=skills.find_type(db, args.target)
            )
        elif args.kind == "skill":
            goal_id = goals.add_goal(
                db,
                "skill",
                character_id,
                type_id=skills.find_type(db, args.target),
                level=args.level,
            )
        else:
            amount = float(args.target.replace(",", "").replace("_", ""))
            goal_id = goals.add_goal(db, "isk", character_id, isk=amount)
    print(f"Added goal {goal_id}. See it with: eve-assistant goals")
    return 0


def _cmd_goals(_args) -> int:
    with store.connect() as db:
        active = goals.active_goals(db)
        if not active:
            print("No goals yet. Example: eve-assistant goal add ship Retriever --character <name>")
            return 0
        for goal in active:
            status = goals.evaluate(db, goal)
            who = f" ({status.character_name})" if status.character_name else ""
            print(f"[{goal.goal_id}] {goal.label}{who}")
            if status.requirement is not None:
                _print_requirement(status.requirement, is_alpha=bool(status.is_alpha))
            if status.price is not None:
                print(f"  Rough price: {status.price:,.0f} ISK (CCP average, not a hub quote)")
            if status.isk_needed is not None:
                print(f"  ISK still needed: {status.isk_needed:,.0f} (wallet {status.wallet:,.0f})")
            for reason in status.reasons:
                if reason.code == "no_price":
                    print("  - No market price yet; run `eve-assistant snapshot`.")
                elif reason.code == "clone_state_unknown":
                    print("  - Account clone state unknown; assumed Alpha (`account state`).")
    return 0


def _cmd_goal_status(args) -> int:
    with store.connect() as db:
        goals.set_status(db, args.goal_id, args.status)
    print(f"Goal {args.goal_id} marked {args.status}.")
    return 0


def _resolve_station(db, character_id: int, station: str | None) -> int:
    if station:
        for hub in market.HUBS:
            if hub.name.lower() == station.lower():
                return hub.station_id
        if station.isdigit():
            return int(station)
        raise ore.OreError(f"Unknown station '{station}'. Use a hub name or a station ID.")
    location = snapshots.latest(db, character_id, "location") or {}
    if location.get("station_id"):
        return location["station_id"]
    raise ore.OreError(
        "The character is not docked at an NPC station. Pass --station, e.g. --station Jita."
    )


def _cmd_ore(args) -> int:
    config = load_config()
    secrets = default_store()
    with store.connect() as db, auth.http_client(config) as client:
        character_id = goals.resolve_character(db, args.character)
        type_id = skills.find_type(db, args.ore)
        station_id = _resolve_station(db, character_id, args.station)
        esi = EsiClient(client, db, token_for=auth.AccessTokens(config, secrets, client).get)
        result = ore.analyse(
            db, esi, character_id, type_id, args.quantity, station_id,
            highsec_only=not args.allow_lowsec,
        )  # fmt: skip
    print(
        f"{result.quantity:,} {result.type_name} ({result.volume_m3:,.0f} m3"
        + (
            f", {result.compressed_volume_m3:,.0f} m3 compressed"
            if result.compressed_volume_m3
            else ""
        )  # fmt: skip
        + f"). Refining here: {result.refine_yield:.1%} yield, {result.refine_tax:.1%} tax."
    )
    for option in result.options:
        flag = "" if option.instant else "  (waits for buyers)"
        print(
            f"  {option.isk:>14,.0f} ISK  {option.active_minutes:>5.0f} min  {option.title}{flag}"
        )
        for reason in option.reasons:
            if reason.code == "not_enough_buyers":
                print(f"{'':24}not enough buyers for {reason.detail['unsold']:,} units")
    print("  Travel time assumes about 1 minute per jump until the tool learns your pace.")
    return 0


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

    skills_cmd = commands.add_parser("skills", help="what a character needs to use an item")
    skills_cmd.add_argument("character", help="character name or ID")
    skills_cmd.add_argument("item", help='item name, e.g. "Retriever"')
    skills_cmd.set_defaults(func=_cmd_skills)

    ore_cmd = commands.add_parser("ore", help="compare selling, refining and hauling ore")
    ore_cmd.add_argument("character", help="character name or ID")
    ore_cmd.add_argument("ore", help='ore name, e.g. "Veldspar"')
    ore_cmd.add_argument("quantity", type=int)
    ore_cmd.add_argument("--station", help="hub name or station ID (default: where docked)")
    ore_cmd.add_argument("--allow-lowsec", action="store_true", help="allow lowsec routes")
    ore_cmd.set_defaults(func=_cmd_ore)

    serve = commands.add_parser("serve", help="run the dashboard on this PC")
    serve.add_argument("--port", type=int, default=8700)
    serve.set_defaults(func=_cmd_serve)

    commands.add_parser("goals", help="show goals and how far away they are").set_defaults(
        func=_cmd_goals
    )
    goal = commands.add_parser("goal", help="add or close goals").add_subparsers(
        dest="goal_command", required=True
    )
    add = goal.add_parser("add", help="add a goal")
    add.add_argument("kind", choices=("ship", "skill", "isk"))
    add.add_argument("target", help="ship or skill name, or an ISK amount")
    add.add_argument("level", nargs="?", type=int, help="skill level (skill goals)")
    add.add_argument("--character", help="character name or ID")
    add.set_defaults(func=_cmd_goal_add)
    for status in ("done", "dropped"):
        close = goal.add_parser(status, help=f"mark a goal {status}")
        close.add_argument("goal_id", type=int)
        close.set_defaults(func=_cmd_goal_status, status=status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (
        ConfigError,
        SSOError,
        SecretStoreError,
        EsiError,
        SdeError,
        AccountError,
        SkillsError,
        GoalError,
        ore.OreError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
