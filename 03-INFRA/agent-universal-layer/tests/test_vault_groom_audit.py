"""Behavioral tests for vault_groom_audit.py against a REAL git repo.

vault-groom.sh/.ps1 call this script after a write pass returns, to build
the structured audit record the 2026-07-13 review found missing entirely
(the old GROOM_LOG was raw stdout in /tmp, not a durable, structured trace).
These tests exercise the real git plumbing (log/diff/add/commit/push), not
mocks -- the whole point of this script is to report what git actually did,
not what the LLM claims it did.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REAL_UL = TESTS_DIR.parent
REAL_VAULT = REAL_UL.parent.parent
AUDIT_SCRIPT = REAL_VAULT / "03-INFRA" / "scripts" / "vault_groom_audit.py"


def _git(vault, *args):
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True, text=True, check=True,
    )


def _init_vault(tmp_path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    _git(vault, "init", "-q", "-b", "main")
    _git(vault, "config", "user.email", "nexgen-tests.invalid")
    _git(vault, "config", "user.name", "Test")
    (vault / "README.md").write_text("seed\n", encoding="utf-8")
    _git(vault, "add", "README.md")
    _git(vault, "commit", "-q", "-m", "seed")
    return vault


def _run_audit(vault, state_dir, **overrides):
    args = {
        "vault": str(vault),
        "state_dir": str(state_dir),
        "timestamp": "test-run-0001",
        "runner": "claude",
        "model": "claude-sonnet-5",
        "tranche_sha256": "a" * 64,
        "plan_record": str(state_dir / "test-run-0001-plan.txt"),
        "head_before": _git(vault, "rev-parse", "HEAD").stdout.strip(),
        "head_after": _git(vault, "rev-parse", "HEAD").stdout.strip(),
        "pushed": "false",
        "propose_log": "/tmp/propose.log",
        "write_log": "/tmp/write.log",
    }
    args.update(overrides)
    cli_args = []
    for key, value in args.items():
        cli_args += [f"--{key.replace('_', '-')}", value]
    return subprocess.run(
        ["python3", str(AUDIT_SCRIPT), *cli_args],
        capture_output=True, text=True, timeout=30,
    )


def test_record_built_from_real_git_history_between_before_and_after(tmp_path):
    vault = _init_vault(tmp_path)
    head_before = _git(vault, "rev-parse", "HEAD").stdout.strip()
    (vault / "groomed.md").write_text("archived content\n", encoding="utf-8")
    _git(vault, "add", "groomed.md")
    _git(vault, "commit", "-q", "-m", "archive: fold groomed.md")
    head_after = _git(vault, "rev-parse", "HEAD").stdout.strip()

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, state_dir, head_before=head_before, head_after=head_after)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    record_path = state_dir / "test-run-0001.json"
    assert record_path.exists()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["head_before"] == head_before
    assert record["head_after"] == head_after
    assert len(record["commits"]) == 1
    assert record["commits"][0]["subject"] == "archive: fold groomed.md"
    assert record["files_touched"] == ["groomed.md"]
    assert record["pushed"] is False


def test_record_when_head_unchanged_reports_zero_commits(tmp_path):
    vault = _init_vault(tmp_path)
    state_dir = tmp_path / "state"
    proc = _run_audit(vault, state_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    record = json.loads((state_dir / "test-run-0001.json").read_text(encoding="utf-8"))
    assert record["commits"] == []
    assert record["files_touched"] == []


def test_backlog_line_appended_and_committed(tmp_path):
    vault = _init_vault(tmp_path)
    (vault / "99-INDEX").mkdir()
    (vault / "99-INDEX" / "vault-cleanup-backlog.md").write_text("# Backlog\n", encoding="utf-8")
    _git(vault, "add", "99-INDEX/vault-cleanup-backlog.md")
    _git(vault, "commit", "-q", "-m", "seed backlog")

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, state_dir, runner="codex", tranche_sha256="b" * 64)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    backlog = (vault / "99-INDEX" / "vault-cleanup-backlog.md").read_text(encoding="utf-8")
    assert "runner=codex" in backlog
    assert "commits=0" in backlog
    assert ("b" * 12) in backlog

    log = _git(vault, "log", "-1", "--format=%s").stdout.strip()
    assert log == "chore(groom): record run test-run-0001"


def test_backlog_created_when_missing(tmp_path):
    vault = _init_vault(tmp_path)
    assert not (vault / "99-INDEX").exists()

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, state_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (vault / "99-INDEX" / "vault-cleanup-backlog.md").exists()


def test_push_true_reaches_the_real_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

    vault = _init_vault(tmp_path)
    _git(vault, "remote", "add", "origin", str(remote))
    _git(vault, "push", "-q", "-u", "origin", "main")

    (vault / "99-INDEX").mkdir()
    (vault / "99-INDEX" / "vault-cleanup-backlog.md").write_text("# Backlog\n", encoding="utf-8")
    _git(vault, "add", "99-INDEX/vault-cleanup-backlog.md")
    _git(vault, "commit", "-q", "-m", "seed backlog")
    _git(vault, "push", "-q")

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, state_dir, pushed="true")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    local_head = _git(vault, "rev-parse", "HEAD").stdout.strip()
    # A bare remote's own ref only updates locally-tracked after a fetch;
    # check the bare repo's ref directly instead of `origin/main` here.
    remote_ref = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_ref == local_head


def test_push_false_does_not_touch_the_remote(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

    vault = _init_vault(tmp_path)
    _git(vault, "remote", "add", "origin", str(remote))
    _git(vault, "push", "-q", "-u", "origin", "main")
    remote_head_before = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, state_dir, pushed="false")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    remote_head_after = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_head_after == remote_head_before


def test_rerun_with_identical_backlog_line_is_a_harmless_noop(tmp_path):
    vault = _init_vault(tmp_path)
    state_dir = tmp_path / "state"

    first = _run_audit(vault, state_dir)
    assert first.returncode == 0, first.stdout + first.stderr
    head_after_first = _git(vault, "rev-parse", "HEAD").stdout.strip()

    # Re-run with the exact same args (same timestamp/hash/runner) -- the
    # line append_backlog_line would produce is byte-identical, so nothing
    # actually changed for git to commit.
    second = _run_audit(vault, state_dir)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "nothing to commit" in second.stdout

    head_after_second = _git(vault, "rev-parse", "HEAD").stdout.strip()
    assert head_after_second == head_after_first
