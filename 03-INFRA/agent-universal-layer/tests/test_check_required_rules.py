"""Tests for check_required_rules.py — the AGENTS.md invariant-rules drift guard.

Read-only guard: a non-negotiable security/behaviour rule must never silently
drop out of the canonical bootstrap. The signatures live in required-rules.txt.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_required_rules.py"


def _run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *[str(a) for a in args]],
        capture_output=True,
        text=True,
    )


def test_passes_when_all_signatures_present(tmp_path):
    rules = tmp_path / "rules.txt"
    rules.write_text("# header comment\nRule One\nRule Two\n", encoding="utf-8")
    agents = tmp_path / "AGENTS.md"
    agents.write_text("... Rule One ...\nand also Rule Two here\n", encoding="utf-8")
    r = _run(agents, rules)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all 2 required invariant rules present" in r.stdout


def test_fails_and_names_the_missing_signature(tmp_path):
    rules = tmp_path / "rules.txt"
    rules.write_text("Rule One\nRule Two\n", encoding="utf-8")
    agents = tmp_path / "AGENTS.md"
    agents.write_text("only Rule One here\n", encoding="utf-8")
    r = _run(agents, rules)
    assert r.returncode == 1
    assert "1/2 required invariant rule(s) MISSING" in r.stdout
    assert "Rule Two" in r.stdout


def test_exit_2_when_target_absent(tmp_path):
    rules = tmp_path / "rules.txt"
    rules.write_text("Rule One\n", encoding="utf-8")
    r = _run(tmp_path / "does-not-exist.md", rules)
    assert r.returncode == 2


def test_exit_2_when_rules_file_absent(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("whatever\n", encoding="utf-8")
    r = _run(agents, tmp_path / "no-rules.txt")
    assert r.returncode == 2


def test_comment_and_blank_lines_are_not_signatures(tmp_path):
    rules = tmp_path / "rules.txt"
    rules.write_text("# only comments\n\n#  and blanks\n", encoding="utf-8")
    agents = tmp_path / "AGENTS.md"
    agents.write_text("anything\n", encoding="utf-8")
    r = _run(agents, rules)
    assert r.returncode == 0
    assert "all 0 required invariant rules present" in r.stdout


def test_shipped_public_agents_md_satisfies_the_committed_rules():
    # The public AGENTS.md must contain every committed required signature — the
    # same thing the CI 'Required invariant rules' step enforces. Asserting it
    # here fails fast locally if an edit drops a non-negotiable rule.
    repo = Path(__file__).resolve().parents[3]
    agents = repo / "03-INFRA/agent-universal-layer/instructions/AGENTS.md"
    rules = repo / "03-INFRA/agent-universal-layer/instructions/required-rules.txt"
    r = _run(agents, rules)
    assert r.returncode == 0, r.stdout + r.stderr
