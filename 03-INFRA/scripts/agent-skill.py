#!/usr/bin/env python3
"""Query the private, on-demand agent skill library.

The runtime-visible ``~/.agents/skills`` directory is deliberately tiny so
CLIs that enumerate skills eagerly do not pay for the whole library on every
session.  Bodies live under ``~/.agents/skill-library`` and are printed only
when an agent explicitly asks for one.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


HOME = Path.home()
ACTIVE = HOME / ".agents" / "skills"
LIBRARY = HOME / ".agents" / "skill-library"
NAME = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def _force_utf8_streams() -> None:
    """Keep Unicode skill bodies printable on legacy Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def _description(skill_md: Path) -> str:
    """Return a compact, dependency-free frontmatter description."""
    try:
        lines = skill_md.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if not lines or lines[0].strip() != "---":
        return ""
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if separator and key.strip() == "description":
            value = value.strip().strip('"\'')
            if value not in {"|", ">"}:
                return " ".join(value.split())
            # YAML block scalar: collect its indented lines without requiring
            # PyYAML at every on-demand command invocation.
            block = []
            for child in lines[index + 1:]:
                if child.strip() == "---" or (child and not child[0].isspace()):
                    break
                block.append(child.strip())
            return " ".join(part for part in block if part)
    return ""


def _skills() -> list[tuple[str, Path, str]]:
    if not LIBRARY.is_dir():
        return []
    rows = []
    for directory in sorted(LIBRARY.iterdir()):
        skill_md = directory / "SKILL.md"
        if directory.is_dir() and NAME.fullmatch(directory.name) and skill_md.is_file():
            rows.append((directory.name, skill_md, _description(skill_md)))
    return rows


def _valid_name(name: str) -> str:
    if not NAME.fullmatch(name):
        raise ValueError("skill name must contain only lowercase letters, digits, and hyphens")
    return name


def _find(name: str) -> Path:
    name = _valid_name(name)
    path = LIBRARY / name / "SKILL.md"
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def cmd_list(_args: argparse.Namespace) -> int:
    rows = _skills()
    if not rows:
        print("No managed skills are installed. Run agent-sync guard first.", file=sys.stderr)
        return 1
    for name, _path, description in rows:
        print(f"{name}\t{description or '(no description)'}")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    query = " ".join(args.query).strip().lower()
    if not query:
        print("Provide words to search for.", file=sys.stderr)
        return 2
    terms = query.split()
    matches = [
        (name, description)
        for name, _path, description in _skills()
        if all(term in f"{name} {description}".lower() for term in terms)
    ]
    if not matches:
        print("No managed skill matches. Do not scan legacy caches; continue without a skill.")
        return 1
    for name, description in matches:
        print(f"{name}\t{description or '(no description)'}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        print(_find(args.name).read_text(encoding="utf-8", errors="replace"), end="")
    except ValueError as exc:
        print(f"Invalid skill name: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(
            f"Managed skill '{args.name}' is not installed. "
            "Run agent-sync guard, or use agent-skill find to choose one.",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    try:
        print(_find(args.name))
    except ValueError as exc:
        print(f"Invalid skill name: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(f"Managed skill '{args.name}' is not installed.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(
        description="Find and load one managed agent skill without exposing the whole library at startup."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list managed skills and compact descriptions").set_defaults(func=cmd_list)
    find = sub.add_parser("find", help="find a skill by trigger words")
    find.add_argument("query", nargs="+", help="words matched against name and description")
    find.set_defaults(func=cmd_find)
    for command, handler, help_text in (
        ("show", cmd_show, "print one SKILL.md body"),
        ("path", cmd_path, "print one SKILL.md path"),
    ):
        item = sub.add_parser(command, help=help_text)
        item.add_argument("name")
        item.set_defaults(func=handler)
    return args.func(args) if (args := parser.parse_args()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
