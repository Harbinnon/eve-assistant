# Contributing

Thanks for helping. This is a small project with a few firm rules; they are
listed in [CLAUDE.md](CLAUDE.md) and apply to people and AI sessions alike.

## One-time setup

1. Use GitHub's private "noreply" email for commits, so your real address
   never appears in the history (the repository is public). Find it
   under GitHub → Settings → Emails, then:
   ```
   git config --global user.email "<id>+<username>@users.noreply.github.com"
   ```
2. Follow the setup in [README.md](README.md), including
   `pre-commit install`. That installs a check that runs on every commit and
   blocks anything that looks like a secret.
3. Register your own ESI application ([docs/setup-esi.md](docs/setup-esi.md)).
   Never share yours, and never use someone else's.

## The routine

1. Get the latest code: `git switch main` then `git pull`.
2. Make a branch for your change: `git switch -c short-description`.
3. Work, commit and push to your branch as often as you like:
   `git push -u origin short-description`.
4. Open a pull request on GitHub. The template asks a few questions;
   answer them honestly, including what you did not test.
5. The checks run automatically (tests, lint, secret scan). The repository
   owner reviews, may ask for changes, and merges.
6. After it is merged, go back to step 1.

**Never push directly to `main`.** GitHub enforces this: `main` only
accepts pull requests that pass the checks and are approved by the
repository owner.

## Conventions

- Python 3.13. Format and lint with `ruff format` and `ruff check`.
- Every change comes with tests. Network calls are mocked in tests.
- New libraries: propose them in the pull request with a reason. Versions
  are pinned exactly, `constraints.txt` is regenerated, and the library is
  added to the allow list in `.github/dependabot.yml` (a test checks).
- Design changes: update [docs/design.md](docs/design.md) in the same pull
  request.

## If a secret gets committed

Tell the repository owner straight away, and **revoke or regenerate the
secret first**. Deleting it in a later commit is not enough: the history
keeps it.
