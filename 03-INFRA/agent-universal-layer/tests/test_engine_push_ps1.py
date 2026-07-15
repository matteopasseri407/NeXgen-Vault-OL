"""Static contract for the Windows public-engine publication gate."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "03-INFRA" / "scripts" / "engine-push.ps1"


def test_windows_engine_push_keeps_the_release_gates():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "--commit-range" in source
    assert "--denylist" in source
    assert "commit.gpgsign" in source
    assert "user.signingkey" in source
    assert "verify-commit" in source
    assert "verify-tag" in source
    assert "merge-base" in source
    assert "never force a public tag" in source
    assert "--force" not in source


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell parser check.")
def test_windows_engine_push_parses_in_windows_powershell():
    command = "[void][scriptblock]::Create([IO.File]::ReadAllText(" + repr(str(SCRIPT)) + "))"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )


def _unsigned_publish_fixture(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "engine"
    vault = tmp_path / "vault"
    subprocess.run(
        ["git", "init", "--bare", str(origin)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    _git(repo, "config", "user.name", "NeXgen Test")
    synthetic_email = "nexgen-test" + chr(64) + "example.invalid"
    _git(repo, "config", "user.email", synthetic_email)

    scanner = repo / "03-INFRA" / "agent-universal-layer" / "leak-scan"
    scanner.mkdir(parents=True)
    (scanner / "leak_scan.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (scanner / "leak_patterns.yaml").write_text("leak_hard: []\n", encoding="utf-8")
    denylist = vault / "03-INFRA" / "agent-universal-layer" / "sanitize" / "deny.txt"
    denylist.parent.mkdir(parents=True)
    denylist.write_text("# test denylist\n", encoding="utf-8")

    _git(repo, "add", ".")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "test: unsigned baseline")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "config", "commit.gpgsign", "true")
    _git(repo, "config", "user.signingkey", "DEADBEEF")
    return repo, vault


def _run_engine_push(repo: Path, vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ENGINE_REPO"] = str(repo)
    env["KNOWLEDGE_VAULT_PATH"] = str(vault)
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell publication behavior.")
def test_windows_engine_push_rejects_unsigned_introduced_commit(tmp_path):
    repo, vault = _unsigned_publish_fixture(tmp_path)
    _git(repo, "switch", "-c", "fix/unsigned")
    (repo / "change.txt").write_text("unsigned change\n", encoding="utf-8")
    _git(repo, "add", "change.txt")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "test: unsigned change")

    result = _run_engine_push(repo, vault, "fix/unsigned")

    assert result.returncode != 0
    assert "commit signature verification failed" in (result.stdout + result.stderr)
    remote_branch = subprocess.run(
        ["git", "--git-dir", str(tmp_path / "origin.git"), "rev-parse", "--verify", "refs/heads/fix/unsigned"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert remote_branch.returncode != 0


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell publication behavior.")
def test_windows_engine_push_rejects_unsigned_tag(tmp_path):
    repo, vault = _unsigned_publish_fixture(tmp_path)
    _git(repo, "tag", "-a", "v-test-unsigned", "-m", "unsigned test tag")

    result = _run_engine_push(repo, vault, "tag", "v-test-unsigned")

    assert result.returncode != 0
    assert "tag signature verification failed" in (result.stdout + result.stderr)
    remote_tag = subprocess.run(
        ["git", "--git-dir", str(tmp_path / "origin.git"), "rev-parse", "--verify", "refs/tags/v-test-unsigned"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert remote_tag.returncode != 0
