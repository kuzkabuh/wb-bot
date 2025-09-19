#!/usr/bin/env python3
"""Update the project's CHANGELOG.md automatically.

This script inserts a new section at the top of the changelog based on
commits made since the last git tag.  It expects a single argument: the
new version number (for example, ``0.8.6``).  It uses ``git`` to
determine the latest existing tag and collects the commit messages
between that tag and ``HEAD``.  The script then prepends a new
section to ``CHANGELOG.md`` with the provided version and the current
date, followed by each commit message as a bullet point.

Usage:

    python scripts/update_changelog.py 0.8.6

The script must be executed from the repository root and requires
``git`` to be installed and configured.  If CHANGELOG.md does not
exist, it will be created.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path


def run_git(args: list[str]) -> str:
    """Run a git command and return its stdout as a string."""
    result = subprocess.run(["git"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def get_latest_tag() -> str | None:
    """Return the name of the most recent tag or None if no tags exist."""
    try:
        return run_git(["describe", "--tags", "--abbrev=0"])
    except Exception:
        return None


def get_commits_since(tag: str | None) -> list[str]:
    """Return a list of commit messages since the given tag (exclusive).

    If ``tag`` is None, returns all commits in the repository.
    """
    args = ["log", "--pretty=%s"]
    if tag:
        args.append(f"{tag}..HEAD")
    commits = run_git(args)
    if not commits:
        return []
    return [line.strip() for line in commits.split("\n") if line.strip()]


def prepend_changelog(version: str, commits: list[str]) -> None:
    """Prepend a new version section to CHANGELOG.md."""
    today = date.today().isoformat()
    header = f"## {version} - {today}\n"
    entries = "\n".join(f"- {msg}" for msg in commits) + "\n\n"
    new_section = header + entries
    changelog_path = Path("CHANGELOG.md")
    if changelog_path.exists():
        content = changelog_path.read_text(encoding="utf-8")
    else:
        content = ""
    changelog_path.write_text(new_section + content, encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: update_changelog.py <version>")
        return 1
    version = argv[1].strip()
    if not version:
        print("Error: version must not be empty")
        return 1
    latest = get_latest_tag()
    commits = get_commits_since(latest)
    if not commits:
        print("No new commits since last tag; nothing to update.")
    else:
        prepend_changelog(version, commits)
        print(f"CHANGELOG.md updated with version {version} and {len(commits)} commits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))