#!/usr/bin/env python3
"""Read-only lifecycle audit for the KnowledgeVault.

Flags notes that are likely stale, oversized, missing metadata, or historical.
It never edits files and it never reads decrypted secrets.
"""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


# Not derived from __file__: this script ships in the ENGINE tree, which is
# a separate checkout from the vault DATA it's meant to audit. __file__-based
# anchoring would silently audit the engine repo instead and report a
# vault that looks empty/pristine. AGENT_VAULT_DATA (falling back to
# KNOWLEDGE_VAULT_PATH, same cascade as the rest of the layer) points at the
# actual data root regardless of where this file is deployed.
ROOT = Path(os.environ.get("AGENT_VAULT_DATA") or os.environ.get("KNOWLEDGE_VAULT_PATH") or Path.home() / "KnowledgeVault").resolve()
SKIP_DIRS = {".git", ".obsidian"}
SKIP_REL_DIRS = {
    "99-SECRETS/plaintext",
    "99-SECRETS/tmp",
}
GENERATED_DIRS = [
    "03-INFRA/n8n-backup",
    "01-ME/cv-artifacts",
]
KNOWN_NO_FRONTMATTER_PREFIXES = (
    "01-ME/cv-artifacts/",
    "03-INFRA/agent-universal-layer/instructions/",
)
HISTORICAL_HINTS = re.compile(
    r"(historical version|historical note|historical log|superseded|deprecated|"
    r"outdated|not canonical|do not use|legacy|retired|obsolete)",
    re.IGNORECASE,
)


@dataclass
class Note:
    path: Path
    rel: str
    lines: list[str]
    frontmatter: dict[str, str]
    has_frontmatter: bool

    @property
    def line_count(self) -> int:
        return len(self.lines)


def parse_note(path: Path) -> Note:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    frontmatter: dict[str, str] = {}
    has_frontmatter = False
    if lines and lines[0].strip() == "---":
        try:
            end = lines[1:].index("---") + 1
            has_frontmatter = True
        except ValueError:
            end = 0
        if has_frontmatter:
            for line in lines[1:end]:
                match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
                if match:
                    frontmatter[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    rel = path.relative_to(ROOT).as_posix()
    return Note(path=path, rel=rel, lines=lines, frontmatter=frontmatter, has_frontmatter=has_frontmatter)


def iter_notes() -> list[Note]:
    notes: list[Note] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = Path(dirpath).relative_to(ROOT).as_posix()
        dirnames[:] = [
            d
            for d in dirnames
            if d not in SKIP_DIRS
            and (f"{rel_dir}/{d}".lstrip("./") not in SKIP_REL_DIRS)
        ]
        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            notes.append(parse_note(Path(dirpath) / filename))
    return sorted(notes, key=lambda n: n.rel)


def parse_note_date(note: Note) -> date | None:
    value = note.frontmatter.get("last_reviewed") or note.frontmatter.get("last_updated")
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


def human_size(size: int) -> str:
    units = ["B", "K", "M", "G"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def print_section(title: str, rows: list[str], limit: int) -> None:
    print(f"\n## {title}")
    if not rows:
        print("OK")
        return
    for row in rows[:limit]:
        print(row)
    remaining = len(rows) - limit
    if remaining > 0:
        print(f"... +{remaining} more")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only KnowledgeVault lifecycle audit.")
    parser.add_argument("--today", default=date.today().isoformat(), help="YYYY-MM-DD, defaults to today.")
    parser.add_argument("--stale-days", type=int, default=60)
    parser.add_argument("--large-lines", type=int, default=300)
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()

    today = datetime.strptime(args.today, "%Y-%m-%d").date()
    notes = iter_notes()

    no_frontmatter: list[str] = []
    missing_status: list[str] = []
    missing_type: list[str] = []
    missing_date: list[str] = []
    status_other: list[str] = []
    stale: list[tuple[int, str]] = []
    large: list[tuple[int, str]] = []
    historical: list[str] = []

    status_counts: dict[str, int] = {}
    for note in notes:
        if not note.has_frontmatter:
            if note.line_count >= args.large_lines:
                large.append((note.line_count, note.rel))
            if not note.rel.startswith(KNOWN_NO_FRONTMATTER_PREFIXES):
                no_frontmatter.append(note.rel)
            continue

        status = note.frontmatter.get("status")
        if note.line_count >= args.large_lines and status != "archive":
            large.append((note.line_count, note.rel))

        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
            if status not in {"active", "archive", "draft"} and not note.rel.startswith(("04-NOW/applied/", "04-NOW/colloqui/")):
                status_other.append(f"{status}\t{note.rel}")
        else:
            missing_status.append(note.rel)

        if "type" not in note.frontmatter and not note.rel.startswith(("04-NOW/applied/", "04-NOW/colloqui/")):
            missing_type.append(note.rel)

        note_date = parse_note_date(note)
        if note_date is None:
            if not note.rel.startswith(("04-NOW/applied/", "04-NOW/colloqui/")):
                missing_date.append(note.rel)
        else:
            age = (today - note_date).days
            if age >= args.stale_days and status != "archive":
                stale.append((age, f"{age}d\t{note_date.isoformat()}\t{note.rel}"))

        head = "\n".join(note.lines[:60])
        if status != "archive" and HISTORICAL_HINTS.search(head):
            historical.append(note.rel)

    print("KnowledgeVault lifecycle audit")
    print(f"Date: {today.isoformat()}")
    print(f"Markdown notes: {len(notes)}")
    print("Status counts: " + ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())) if status_counts else "Status counts: none")

    payload_rows = []
    for rel in GENERATED_DIRS:
        payload_rows.append(f"{human_size(dir_size(ROOT / rel))}\t{rel}")
    print_section("Generated or bulky payloads", payload_rows, args.limit)
    print_section("No frontmatter outside accepted generated paths", no_frontmatter, args.limit)
    print_section("Missing status", missing_status, args.limit)
    print_section("Missing type outside CRM", missing_type, args.limit)
    print_section("Missing review/update date outside CRM", missing_date, args.limit)
    print_section("Non-standard status outside CRM", status_other, args.limit)
    print_section("Stale by review date", [row for _, row in sorted(stale, reverse=True)], args.limit)
    print_section("Large notes", [f"{lines} lines\t{rel}" for lines, rel in sorted(large, reverse=True)], args.limit)
    print_section("Historical or superseded hints near top", historical, args.limit)

    print("\nThis is an audit list, not an automatic deletion list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
