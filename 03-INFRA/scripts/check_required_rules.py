#!/usr/bin/env python3
"""check_required_rules.py -- guard the canonical AGENTS.md against silently
losing a non-negotiable rule (the "vault <-> public drift" failure class).

Reads a list of required rule "signatures" (stable substrings that MUST appear
in the bootstrap) and reports any missing from a target AGENTS.md. Read-only:
it never writes anything.

Usage:
  check_required_rules.py <AGENTS.md> [required-rules.txt]

Exit codes: 0 all present, 1 one or more missing, 2 usage or file error.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_RULES = HERE.parent / "agent-universal-layer" / "instructions" / "required-rules.txt"


def load_signatures(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(stripped)
    return out


def missing_signatures(target: Path, rules: Path) -> list[str]:
    text = target.read_text(encoding="utf-8")
    return [sig for sig in load_signatures(rules) if sig not in text]


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: check_required_rules.py <AGENTS.md> [required-rules.txt]")
        return 0 if argv else 2
    target = Path(argv[0])
    rules = Path(argv[1]) if len(argv) > 1 else DEFAULT_RULES
    if not target.is_file():
        print(f">>> STOP: AGENTS.md not found: {target}", file=sys.stderr)
        return 2
    if not rules.is_file():
        print(f">>> STOP: required-rules file not found: {rules}", file=sys.stderr)
        return 2
    total = len(load_signatures(rules))
    missing = missing_signatures(target, rules)
    if missing:
        print(f">>> {len(missing)}/{total} required invariant rule(s) MISSING from {target.name}:")
        for sig in missing:
            print(f"    - {sig}")
        return 1
    print(f">>> all {total} required invariant rules present in {target.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
