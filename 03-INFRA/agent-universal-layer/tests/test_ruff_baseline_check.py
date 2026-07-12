"""Regression tests for the ruff baseline gate (NX-06 / CI Pacchetto G).

`ruff check 03-INFRA` has 52 pre-existing findings that must not fail every
future PR (the bug this mechanism guards against), while a genuinely NEW or
INCREASED finding must still fail CI (the correction). These tests exercise
03-INFRA/scripts/ruff_baseline_check.py against a fake `ruff` executable on
PATH -- no real ruff binary required, since engine-tests does not install
one and this suite must stay runnable there too.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REAL_VAULT

SCRIPT = REAL_VAULT / "03-INFRA" / "scripts" / "ruff_baseline_check.py"


def _finding(abs_filename: str, code: str, row: int = 1) -> dict:
    """One ruff-JSON-shaped finding. Real ruff reports an ABSOLUTE
    `filename` resolved from the invocation cwd; the stub mirrors that."""
    return {
        "cell": None,
        "code": code,
        "end_location": {"column": 1, "row": row},
        "filename": abs_filename,
        "fix": None,
        "location": {"column": 1, "row": row},
        "message": f"stub finding {code}",
        "name": "stub-rule",
        "noqa_row": row,
        "severity": "error",
        "url": "https://example.invalid/stub",
    }


def _write_ruff_stub(bin_dir: Path, findings: list[dict]) -> None:
    """Installs a fake `ruff` on PATH that ignores argv and prints canned
    JSON, exiting 1 if findings is non-empty and 0 otherwise (mirrors real
    ruff's exit codes, which the script under test relies on)."""
    payload = json.dumps(findings)
    exit_code = 1 if findings else 0
    stub = bin_dir / "ruff"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({payload!r})\n"
        f"sys.exit({exit_code})\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)


def _make_repo(tmp_path: Path) -> Path:
    """Minimal repo skeleton with the real script copied to the same
    relative location it has in the real repo (03-INFRA/scripts/...),
    since the script locates its repo root from its own __file__ path."""
    repo = tmp_path / "repo"
    scripts_dir = repo / "03-INFRA" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts_dir / "ruff_baseline_check.py")
    return repo


def _run(repo: Path, bin_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [sys.executable, str(repo / "03-INFRA" / "scripts" / "ruff_baseline_check.py"), *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path)


@pytest.fixture
def bin_dir(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    return d


def test_generate_writes_relative_paths_grouped_with_counts(repo, bin_dir):
    abs_file = repo / "03-INFRA" / "widget.py"
    _write_ruff_stub(
        bin_dir,
        [
            _finding(str(abs_file), "E401"),
            _finding(str(abs_file), "E401"),
            _finding(str(abs_file), "F841"),
        ],
    )

    baseline_path = repo / "03-INFRA" / "ruff-baseline.json"
    proc = _run(repo, bin_dir, "--generate", "--baseline", str(baseline_path))
    assert proc.returncode == 0, proc.stderr

    rows = json.loads(baseline_path.read_text())
    by_key = {(row["file"], row["code"]): row["count"] for row in rows}
    assert by_key == {
        ("03-INFRA/widget.py", "E401"): 2,
        ("03-INFRA/widget.py", "F841"): 1,
    }


def test_passes_when_current_matches_baseline(repo, bin_dir):
    abs_file = repo / "03-INFRA" / "widget.py"
    baseline_path = repo / "03-INFRA" / "ruff-baseline.json"
    baseline_path.write_text(json.dumps([{"file": "03-INFRA/widget.py", "code": "E401", "count": 1}]))

    _write_ruff_stub(bin_dir, [_finding(str(abs_file), "E401")])
    proc = _run(repo, bin_dir, "--baseline", str(baseline_path))
    assert proc.returncode == 0, proc.stderr
    assert "no regressions" in proc.stdout


def test_fails_on_new_finding_not_in_baseline(repo, bin_dir):
    """The bug this mechanism guards against: a NEW lint violation, absent
    from the committed baseline, must fail the gate."""
    abs_file = repo / "03-INFRA" / "widget.py"
    baseline_path = repo / "03-INFRA" / "ruff-baseline.json"
    baseline_path.write_text(json.dumps([]))

    _write_ruff_stub(bin_dir, [_finding(str(abs_file), "E401")])
    proc = _run(repo, bin_dir, "--baseline", str(baseline_path))
    assert proc.returncode == 1
    assert "03-INFRA/widget.py" in proc.stdout
    assert "E401" in proc.stdout
    assert "baseline allows 0" in proc.stdout


def test_fails_when_existing_finding_count_increases(repo, bin_dir):
    abs_file = repo / "03-INFRA" / "widget.py"
    baseline_path = repo / "03-INFRA" / "ruff-baseline.json"
    baseline_path.write_text(json.dumps([{"file": "03-INFRA/widget.py", "code": "E401", "count": 1}]))

    _write_ruff_stub(bin_dir, [_finding(str(abs_file), "E401"), _finding(str(abs_file), "E401")])
    proc = _run(repo, bin_dir, "--baseline", str(baseline_path))
    assert proc.returncode == 1
    assert "baseline allows 1" in proc.stdout


def test_passes_when_existing_finding_count_decreases(repo, bin_dir):
    """Paying down pre-existing debt (fixing some, not all, occurrences of
    an already-baselined rule) must never fail the gate."""
    abs_file = repo / "03-INFRA" / "widget.py"
    baseline_path = repo / "03-INFRA" / "ruff-baseline.json"
    baseline_path.write_text(json.dumps([{"file": "03-INFRA/widget.py", "code": "E401", "count": 3}]))

    _write_ruff_stub(bin_dir, [_finding(str(abs_file), "E401")])
    proc = _run(repo, bin_dir, "--baseline", str(baseline_path))
    assert proc.returncode == 0, proc.stderr
    assert "improved" in proc.stdout


def test_missing_baseline_file_is_treated_as_empty(repo, bin_dir):
    """No baseline committed yet == baseline of nothing: any finding at all
    must fail the gate (never silently pass with an absent file)."""
    abs_file = repo / "03-INFRA" / "widget.py"
    baseline_path = repo / "03-INFRA" / "ruff-baseline.json"
    assert not baseline_path.exists()

    _write_ruff_stub(bin_dir, [_finding(str(abs_file), "E401")])
    proc = _run(repo, bin_dir, "--baseline", str(baseline_path))
    assert proc.returncode == 1
