"""Checks that guard the repository's own rules, not application behaviour."""

import subprocess
import tomllib
from pathlib import Path

import eve_assistant

ROOT = Path(__file__).resolve().parent.parent


def test_package_imports():
    assert eve_assistant.__version__


def test_dependencies_are_pinned_exactly():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    specs = list(project["dependencies"])
    for group in project.get("optional-dependencies", {}).values():
        specs.extend(group)
    unpinned = [spec for spec in specs if "==" not in spec]
    assert not unpinned, f"pin these with ==: {unpinned}"


def test_personal_paths_are_ignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in ("private/", "config/local.toml", "var/"):
        assert entry in gitignore, f"{entry} must be in .gitignore"


def test_no_personal_files_are_tracked():
    tracked = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 - git is expected on PATH in dev and CI
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    forbidden = [p for p in tracked if p.startswith("private/") or p == "config/local.toml"]
    assert not forbidden, f"personal files are tracked: {forbidden}"
