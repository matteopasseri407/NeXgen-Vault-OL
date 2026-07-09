#!/usr/bin/env python3
"""Shared anti-leak gate (S0), used by all three layers of defense:
  - local pre-commit / commit-msg hooks (on the staged diff / commit message)
  - engine-push (locally, on every commit in the range about to be published,
    plus their messages)
  - this repo's CI (on every commit in a push or pull request)

Two pattern sources, NEVER mixed:
  --patterns FILE   PUBLIC patterns (leak_hard/leak_allow): forms only, zero
                     real values. This is the file used by CI.
  --denylist FILE    Literal real values (plain substring match), one per
                     line. Only ever used locally by a maintainer's own
                     tooling; CI never has this file and never needs it.

Scan modes (pick one):
  --tree PATH...          scan literal file/directory content (fixtures, working tree)
  --staged                scan `git diff --cached` in a repo (--repo, default cwd)
  --message FILE          scan a commit-message file (for the commit-msg hook)
  --commit-range BASE..HEAD
                          scan EVERY commit introduced in the range: diff
                          (added lines only) plus commit message, one by one.
                          This catches a value that lands in an intermediate
                          commit and disappears from the final combined diff
                          but stays in the published history.

Output: silent when clean (exit 0, no stdout). When something is found, print
one finding per line with the match ALWAYS redacted (never the full value)
and exit 1. Callers own their own success/failure UX; this script only does
detection.
"""
from __future__ import annotations
import argparse, re, subprocess, sys
from pathlib import Path
from typing import Iterable, NamedTuple

try:
    import yaml
except ImportError:
    sys.exit("[leak-scan] ABORT: PyYAML not available (pip install pyyaml)")


class Unit(NamedTuple):
    label: str      # e.g. "src/foo.py" or "a1b2c3d message"
    lineno: int
    text: str


class Finding(NamedTuple):
    label: str
    lineno: int
    kind: str       # "pattern:<name>" or "denylist"
    redacted: str
    blocking: bool  # True = leak_hard/denylist (blocks); False = leak_soft (warns only)


def load_patterns(path: Path) -> tuple[list[tuple[str, str, bool]], list[str]]:
    if not path.is_file():
        sys.exit(f"[leak-scan] ABORT: pattern file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    hard = data.get("leak_hard", [])
    if not hard:
        sys.exit("[leak-scan] ABORT: leak_hard is empty in the pattern file")
    soft = data.get("leak_soft", [])
    named = [(f"h{i}", pat, True) for i, pat in enumerate(hard)]
    named += [(f"s{i}", pat, False) for i, pat in enumerate(soft)]
    allow = data.get("leak_allow", [])
    return named, allow


def load_denylist(path: Path | None) -> list[str]:
    if path is None:
        return []
    if not path.is_file():
        sys.exit(f"[leak-scan] ABORT: denylist given but not found: {path}")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def redact(tok: str) -> str:
    if len(tok) <= 6:
        return "*" * len(tok)
    return f"{tok[:2]}…{tok[-2:]} ({len(tok)} chars)"


def scan_units(units: Iterable[Unit], patterns: list[tuple[str, str, bool]], allow: list[str],
               denylist: list[str]) -> list[Finding]:
    allow_re = [re.compile(a) for a in allow]
    pat_re = [(name, re.compile(p), blocking) for name, p, blocking in patterns]
    findings: list[Finding] = []
    for u in units:
        for name, rx, blocking in pat_re:
            for m in rx.finditer(u.text):
                tok = m.group(0)
                if any(a.search(tok) for a in allow_re):
                    continue
                findings.append(Finding(u.label, u.lineno, f"pattern:{name}", redact(tok), blocking))
        for lit in denylist:
            if lit and lit in u.text:
                findings.append(Finding(u.label, u.lineno, "denylist", redact(lit), True))
    return findings


# --- collecting "units" to scan, per mode -------------------------------------

def units_from_tree(paths: list[str]) -> list[Unit]:
    units: list[Unit] = []
    skip_dirs = {".git", "__pycache__", "node_modules"}
    files: list[Path] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            for f in pp.rglob("*"):
                if f.is_file() and not any(part in skip_dirs for part in f.parts):
                    files.append(f)
        elif pp.is_file():
            files.append(pp)
    for f in sorted(files):
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            units.append(Unit(str(f), i, line))
    return units


def parse_added_lines(diff_text: str, label_prefix: str) -> list[Unit]:
    units: list[Unit] = []
    cur_file = "?"
    ln = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            cur_file = raw[4:].split("\t")[0]
            if cur_file.startswith("b/"):
                cur_file = cur_file[2:]
            if cur_file != "/dev/null":
                label = f"{label_prefix}{cur_file}" if label_prefix else cur_file
                # the filename itself is new content too (a secret can land in
                # a name, not just in a line): scan it, not just the diff body.
                units.append(Unit(f"{label} (filename)", 0, cur_file))
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            ln = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            label = f"{label_prefix}{cur_file}" if label_prefix else cur_file
            units.append(Unit(label, ln, raw[1:]))
            ln += 1
        # "-" lines (removals) and context are not NEW content: ignored
    return units


def run_git(repo: str, *args: str) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"[leak-scan] git command failed: git {' '.join(args)}\n{r.stderr}")
    return r.stdout


def units_from_staged(repo: str) -> list[Unit]:
    diff = run_git(repo, "diff", "--cached", "--no-color", "--unified=0")
    return parse_added_lines(diff, "")


def units_from_message(path: str) -> list[Unit]:
    text = Path(path).read_text(encoding="utf-8")
    return [Unit("(commit message)", i, line) for i, line in enumerate(text.splitlines(), 1)]


def units_from_commit_range(repo: str, range_spec: str) -> list[Unit]:
    shas = [s for s in run_git(repo, "rev-list", "--reverse", range_spec).splitlines() if s]
    units: list[Unit] = []
    for sha in shas:
        short = sha[:9]
        diff = run_git(repo, "show", "--no-color", "--unified=0", "-m", "--format=", sha)
        units.extend(parse_added_lines(diff, f"commit {short}: "))
        msg = run_git(repo, "log", "-1", "--format=%B", sha)
        units.extend(
            Unit(f"commit {short} (message)", i, line)
            for i, line in enumerate(msg.splitlines(), 1)
        )
    return units


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--patterns", required=True, help="public pattern file (leak_hard/leak_allow)")
    ap.add_argument("--denylist", help="private denylist file (literal values), optional")
    ap.add_argument("--repo", default=".", help="git repo to operate on (default: cwd)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tree", nargs="+", metavar="PATH", help="scan literal file/dir content")
    mode.add_argument("--staged", action="store_true", help="scan the staged diff")
    mode.add_argument("--message", metavar="FILE", help="scan a commit-message file")
    mode.add_argument("--commit-range", metavar="BASE..HEAD", help="scan every commit in the range")
    args = ap.parse_args()

    patterns, allow = load_patterns(Path(args.patterns))
    denylist = load_denylist(Path(args.denylist)) if args.denylist else []

    if args.tree:
        units = units_from_tree(args.tree)
    elif args.staged:
        units = units_from_staged(args.repo)
    elif args.message:
        units = units_from_message(args.message)
    else:
        units = units_from_commit_range(args.repo, args.commit_range)

    findings = scan_units(units, patterns, allow, denylist)
    if not findings:
        return 0

    blocking = [f for f in findings if f.blocking]
    soft = [f for f in findings if not f.blocking]

    if soft:
        print("[leak-scan] soft warnings (not blocking):")
        for f in soft:
            print(f"  ? {f.label}:{f.lineno}  [{f.kind}]  match={f.redacted}")

    if not blocking:
        return 0

    print("[leak-scan] BLOCKING leaks found:")
    for f in blocking:
        print(f"  ! {f.label}:{f.lineno}  [{f.kind}]  match={f.redacted}")
    print(f"[leak-scan] total: {len(blocking)} finding(s) — commit/push blocked.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
