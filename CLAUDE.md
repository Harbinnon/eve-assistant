# Instructions for AI coding sessions

These rules apply to every AI session working in this repository, for every
contributor. Read [docs/design.md](docs/design.md) before changing behaviour.

## Never

- **Commit secrets or personal data.** No ESI refresh tokens, client
  secrets, webhook URLs, API keys, character IDs tied to a person, emails
  or real names. Client IDs and personal settings live in the gitignored
  `config/local.toml`; personal notes live in the gitignored `private/`
  folder. Do not read `private/` unless the person you are working for asks.
- **Ask a person to paste a secret into chat**, or print one back.
- **Commit CCP data**: static data export files, images, icons. They are
  downloaded at runtime into `var/` (gitignored).
- **Collect other players' data.** The tool reads only characters whose
  owners log in to it. From Local chat logs it keeps only the owner's own
  system changes, never other people's names or messages. No intel-channel
  reading. (CCP Developer License §2.3(c).)
- **Request write ESI scopes.** Everything is read-only. A new scope needs a
  reason in docs/design.md, and means every character logs in again.
- **Load third-party scripts or CDNs** in dashboard pages. Everything is
  self-hosted. No inline `<script>`, `<style>` or `style=""` either: the
  Content-Security-Policy blocks them (a test checks every page).
- **Weaken the dashboard's Host check or security headers**, or bind it to
  anything but 127.0.0.1, without the repository owner's approval.
- **Deploy to or change anyone's servers or infrastructure.** Hosted mode is
  set up by its owner.
- **Push to `main` or merge pull requests.** Work on a branch and open a
  pull request; the repository owner reviews and merges.

## Always

- **Pin dependencies exactly** (`==`) in `pyproject.toml`. Adding a library
  is a decision for the repository owner; propose it with a reason first.
- **Keep logic in the service layer** (plain, read-only functions). The
  dashboard, CLI and future MCP server are thin layers over it.
- **Planner results carry structured reasons and inputs**, not only display
  text.
- **Plans and outcomes are append-only.** Never update or delete them.
- **Tag data by character, account or install.** There is no user column:
  each copy belongs to one person.
- **Label player-written text** (contract titles and descriptions, bios)
  as player-written wherever it is stored or passed on.
- **ESI etiquette:** send `X-Compatibility-Date` (a pinned date, moved
  deliberately), a descriptive `User-Agent` with the operator's contact
  from config, respect `Cache-Control`, use conditional requests, and back
  off on 420/429 using the rate-limit headers.
- **Tests with every change** (`pytest`), and `ruff check` / `ruff format`
  clean. Say plainly what was and was not tested.
- **Keep docs/design.md current** when a decision changes.

## Layout

```
src/eve_assistant/   the package
tests/               pytest tests
docs/                shareable design and setup docs
config/              local.example.toml (local.toml is gitignored)
var/                 runtime data (gitignored)
private/             personal notes (gitignored)
```
