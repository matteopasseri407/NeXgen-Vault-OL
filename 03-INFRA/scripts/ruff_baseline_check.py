#!/usr/bin/env python3
"""Ruff baseline gate for 03-INFRA.

`ruff check 03-INFRA` currently reports pre-existing findings that nobody
has cleaned up yet. Rather than either (a) failing CI on every one of them
(noisy, blocks unrelated PRs) or (b) ignoring ruff entirely (no gate at
all), this script compares the CURRENT findings against a committed
baseline (03-INFRA/ruff-baseline.json) grouped by (file, rule code) with a
count each. The check FAILS only when:

  - a (file, code) pair appears that is not in the baseline at all, or
  - a (file, code) pair already in the baseline has a HIGHER count now.

Findings that stay at or below their baseline count (including ones fixed
entirely) do not fail the build. That means new code is held to "don't add
new lint debt", while the pre-existing debt can be paid down over time
without a giant one-shot cleanup PR.

Usage:
    python3 03-INFRA/scripts/ruff_baseline_check.py
        Check current findings against the baseline. Exits 1 on regression.

    python3 03-INFRA/scripts/ruff_baseline_check.py --generate
        Regenerate 03-INFRA/ruff-baseline.json from the current findings.
        Run this after intentionally fixing findings (to shrink the
        baseline) or after confirming a new finding is expected/accepted.
        Always review and commit the diff of the regenerated file.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent          # .../03-INFRA/scripts
TARGET_DIR = SCRIPT_DIR.parent                         # .../03-INFRA
REPO_ROOT = TARGET_DIR.parent                          # repo root
TARGET_NAME = TARGET_DIR.name                           # "03-INFRA"
DEFAULT_BASELINE = TARGET_DIR / "ruff-baseline.json"


def run_ruff() -> list[dict]:
    # RUFF_CMD (a JSON array, not a shell string -- no quoting ambiguity
    # across POSIX/Windows path separators) lets tests point this at a fake
    # ruff, e.g. [sys.executable, "/path/to/stub.py"], without touching
    # PATH: a bare "ruff" name on Windows resolves only to "ruff.exe" via
    # CreateProcess, so a POSIX shebang stub named plain "ruff" is invisible
    # there even when it's on PATH and executable. Real usage (CI, local)
    # never sets this, and gets the real `ruff` from PATH as before.
    ruff_cmd = json.loads(os.environ["RUFF_CMD"]) if "RUFF_CMD" in os.environ else ["ruff"]
    proc = subprocess.run(
        [*ruff_cmd, "check", TARGET_NAME, "--output-format=json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    # ruff exits 0 when clean, 1 when it found lint violations. Anything
    # else (2, a crash, "command not found" surfaced as a huge negative
    # code, ...) means the invocation itself is broken and must not be
    # silently treated as "zero findings".
    if proc.returncode not in (0, 1):
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"ruff invocation failed unexpectedly (exit {proc.returncode})")
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"could not parse ruff JSON output: {exc}")


def to_counts(findings: list[dict]) -> dict[tuple[str, str], int]:
    """Groups findings by (repo-relative file, rule code) -> count.

    ruff's JSON output always reports absolute filenames (resolved from
    whatever cwd it was invoked with), which would make the baseline file
    unusable across machines/CI checkouts. Relativize against REPO_ROOT so
    the baseline is portable.
    """
    counts: dict[tuple[str, str], int] = {}
    for item in findings:
        filename = Path(item["filename"]).resolve()
        try:
            rel = filename.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = filename.as_posix()
        code = item.get("code") or "?"
        key = (rel, code)
        counts[key] = counts.get(key, 0) + 1
    return counts


def load_baseline(path: Path) -> dict[tuple[str, str], int]:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {(row["file"], row["code"]): row["count"] for row in data}


def save_baseline(path: Path, counts: dict[tuple[str, str], int]) -> None:
    rows = [{"file": f, "code": c, "count": n} for (f, c), n in sorted(counts.items())]
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--generate",
        action="store_true",
        help="write current findings as the new baseline instead of checking against it",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=f"baseline file path (default: {DEFAULT_BASELINE.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args(argv)

    current = to_counts(run_ruff())

    if args.generate:
        save_baseline(args.baseline, current)
        print(f"Wrote {len(current)} (file, code) entries to {args.baseline}")
        return 0

    baseline = load_baseline(args.baseline)

    regressions = [
        (key, baseline.get(key, 0), count)
        for key, count in sorted(current.items())
        if count > baseline.get(key, 0)
    ]

    if regressions:
        print("Ruff baseline gate FAILED: new or increased findings vs baseline:")
        for (file_, code), base_count, count in regressions:
            print(f"  {file_}  {code}: {count} found (baseline allows {base_count})")
        print()
        print(
            "Fix the new lint violations, or if they're deliberate/pre-existing debt "
            "you're knowingly accepting, regenerate the baseline with:"
        )
        rel_script = Path(__file__).resolve().relative_to(REPO_ROOT)
        print(f"  python3 {rel_script} --generate")
        print(f"and commit the updated {args.baseline.relative_to(REPO_ROOT)}.")
        return 1

    improved = [key for key in baseline if baseline[key] > current.get(key, 0)]
    print(f"Ruff baseline gate OK: {len(current)} known (file, code) finding group(s), no regressions.")
    if improved:
        print(
            f"Note: {len(improved)} baseline entrie(s) improved (count decreased or fully "
            "fixed) - consider regenerating the baseline to lock in the improvement."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
