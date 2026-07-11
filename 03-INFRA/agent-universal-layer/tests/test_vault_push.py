from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="vault-push.sh is the POSIX infra publication helper; Windows uses its native agent-sync path.",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(sandbox) -> tuple[Path, Path]:
    subprocess.run(
        ["git", "init", "-b", "main", str(sandbox.vault)],
        check=True,
        capture_output=True,
    )
    _git(sandbox.vault, "config", "user.email", "nexgen-tests.invalid")
    _git(sandbox.vault, "config", "user.name", "NeXgen tests")
    _git(sandbox.vault, "add", ".")
    _git(sandbox.vault, "commit", "-m", "fixture")

    oracle = sandbox.home / "oracle.git"
    mirror = sandbox.home / "origin.git"
    for path in (oracle, mirror):
        subprocess.run(["git", "init", "--bare", str(path)], check=True, capture_output=True)
    _git(sandbox.vault, "remote", "add", "oracle", str(oracle))
    _git(sandbox.vault, "remote", "add", "origin", str(mirror))
    _git(sandbox.vault, "push", "-u", "oracle", "main")
    return oracle, mirror


def test_vault_push_uses_authoritative_remote_then_aligns_mirror(sandbox):
    oracle, mirror = _init_repo(sandbox)
    config = sandbox.ul / "sync" / "remotes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "schema_version: 1\nauthoritative_remote: oracle\nmirrors: [origin]\n",
        encoding="utf-8",
    )
    env = sandbox.env()
    env.pop("KNOWLEDGE_VAULT_REMOTE", None)

    proc = subprocess.run(
        [
            "bash",
            str(sandbox.scripts_dir / "vault-push.sh"),
            "-m",
            "configure remotes",
            str(config.relative_to(sandbox.vault)),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    local_head = _git(sandbox.vault, "rev-parse", "main").stdout.strip()
    oracle_head = subprocess.run(
        ["git", "--git-dir", str(oracle), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    mirror_head = subprocess.run(
        ["git", "--git-dir", str(mirror), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert local_head == oracle_head == mirror_head


def test_vault_push_resolves_engine_sibling_when_invoked_through_symlink(sandbox):
    oracle, mirror = _init_repo(sandbox)
    config = sandbox.ul / "sync" / "remotes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "schema_version: 1\nauthoritative_remote: oracle\nmirrors: [origin]\n",
        encoding="utf-8",
    )
    launcher = sandbox.home / ".local" / "bin" / "vault-push"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.symlink_to(sandbox.scripts_dir / "vault-push.sh")
    env = sandbox.env()
    env.pop("KNOWLEDGE_VAULT_REMOTE", None)

    proc = subprocess.run(
        [
            "bash",
            str(launcher),
            "-m",
            "configure remotes via symlink",
            str(config.relative_to(sandbox.vault)),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    local_head = _git(sandbox.vault, "rev-parse", "main").stdout.strip()
    oracle_head = subprocess.run(
        ["git", "--git-dir", str(oracle), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    mirror_head = subprocess.run(
        ["git", "--git-dir", str(mirror), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert local_head == oracle_head == mirror_head


def test_vault_push_rejects_invalid_remote_policy_before_commit(sandbox):
    _init_repo(sandbox)
    before = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    config = sandbox.ul / "sync" / "remotes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("schema_version: wrong\n", encoding="utf-8")
    env = sandbox.env()
    env.pop("KNOWLEDGE_VAULT_REMOTE", None)

    proc = subprocess.run(
        [
            "bash",
            str(sandbox.scripts_dir / "vault-push.sh"),
            "-m",
            "must not commit",
            str(config.relative_to(sandbox.vault)),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 2
    assert _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip() == before
    assert _git(sandbox.vault, "diff", "--cached", "--name-only").stdout == ""
