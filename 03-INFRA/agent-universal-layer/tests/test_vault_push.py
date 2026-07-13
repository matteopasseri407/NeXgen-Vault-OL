from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from conftest import REAL_SCRIPTS


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


def test_vault_push_commits_locally_and_skips_publication_in_local_only_mode(sandbox):
    _init_repo(sandbox)
    before = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    target = sandbox.vault / "note.txt"
    target.write_text("local-only change\n", encoding="utf-8")
    env = sandbox.env()
    env["KNOWLEDGE_VAULT_REMOTE"] = "local"

    proc = subprocess.run(
        [
            "bash",
            str(sandbox.scripts_dir / "vault-push.sh"),
            "-m",
            "local-only infra change",
            str(target.relative_to(sandbox.vault)),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "push skipped" in proc.stdout
    after = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    assert after != before, "the local commit must still happen in Local-Only mode"


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


# ── Shared sync lock with agent_sync.py (beta-readiness review, 2026-07-13)
# ───────────────────────────────────────────────────────────────────────
# vault-push.sh used no lock at all: it could run concurrently with an
# agent-sync guard cycle (SyncRunLock, same fcntl.flock mechanism) and
# interleave a commit with a mid-apply working tree. It now flocks the
# SAME lock file agent_sync.py uses (fcntl.flock on the same path is a
# cooperative, cross-process lock -- the C library call underneath both
# Python's fcntl module and the flock(1) CLI is identical).

def test_vault_push_blocked_while_sync_lock_is_held(sandbox):
    import fcntl

    _init_repo(sandbox)
    before = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    target = sandbox.vault / "note.txt"
    target.write_text("should not be committed\n", encoding="utf-8")

    lock_file = sandbox.home / "agent-sync.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_file.open("a+b")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        env = sandbox.env()
        env["KNOWLEDGE_VAULT_REMOTE"] = "local"
        env["AGENT_SYNC_LOCK_FILE"] = str(lock_file)
        env["AGENT_SYNC_LOCK_TIMEOUT_SECONDS"] = "0.3"

        proc = subprocess.run(
            ["bash", str(sandbox.scripts_dir / "vault-push.sh"), "-m", "must not commit",
             str(target.relative_to(sandbox.vault))],
            env=env, capture_output=True, text=True, timeout=30,
        )
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    assert proc.returncode == 75, proc.stdout + proc.stderr
    assert "sync lock busy" in proc.stderr
    assert _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip() == before


def test_vault_push_proceeds_once_sync_lock_is_free(sandbox):
    _init_repo(sandbox)
    before = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    target = sandbox.vault / "note.txt"
    target.write_text("free to commit\n", encoding="utf-8")

    lock_file = sandbox.home / "agent-sync.lock"
    env = sandbox.env()
    env["KNOWLEDGE_VAULT_REMOTE"] = "local"
    env["AGENT_SYNC_LOCK_FILE"] = str(lock_file)

    proc = subprocess.run(
        ["bash", str(sandbox.scripts_dir / "vault-push.sh"), "-m", "free commit",
         str(target.relative_to(sandbox.vault))],
        env=env, capture_output=True, text=True, timeout=30,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert lock_file.exists(), "vault-push should create/use the shared lock file even on success"
    assert _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip() != before


# --- Real multi-machine contention -------------------------------------
#
# The 2026-07-13 follow-up review found every prior PullState/lock test
# shaped a git history sequentially inside ONE test process before calling
# into the code under test -- never two independent OS processes actually
# racing each other. These two tests fix that: each "machine" is its own
# HOME, its own clone, and its own lock file (the lock is per-machine by
# design, see docs/sync-contract.md), and the rejection one machine hits is
# a genuine side effect of the OTHER machine's real vault-push.sh run, not
# git plumbing the test performed directly.

def _init_machine(home: Path, remote: Path) -> Path:
    vault = home / "KnowledgeVault"
    subprocess.run(["git", "clone", "-q", str(remote), str(vault)], check=True, capture_output=True)
    _git(vault, "config", "user.email", "nexgen-tests.invalid")
    _git(vault, "config", "user.name", "NeXgen tests")
    return vault


def _machine_env(home: Path, vault: Path) -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["AGENT_VAULT_DATA"] = str(vault)
    env["KNOWLEDGE_VAULT_PATH"] = str(vault)
    env["KNOWLEDGE_VAULT_REMOTE"] = "origin"
    env["KNOWLEDGE_VAULT_MIRRORS"] = ""
    # Own lock file per machine, matching the real per-machine lock path --
    # sharing one here would test single-machine serialization instead.
    env["AGENT_SYNC_LOCK_FILE"] = str(home / "agent-sync.lock")
    return env


def _seed_bare_remote(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
    seed = path.parent / f"{path.name}-seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _git(seed, "config", "user.email", "nexgen-tests.invalid")
    _git(seed, "config", "user.name", "NeXgen tests")
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(path))
    _git(seed, "push", "-q", "-u", "origin", "main")


def test_two_real_machines_pushing_the_same_vault_do_not_lose_either_commit(tmp_path):
    remote = tmp_path / "remote.git"
    _seed_bare_remote(remote)

    vault_a = _init_machine(tmp_path / "machine-a", remote)
    vault_b = _init_machine(tmp_path / "machine-b", remote)
    (vault_a / "from-a.md").write_text("machine a\n", encoding="utf-8")
    (vault_b / "from-b.md").write_text("machine b\n", encoding="utf-8")

    script = str(REAL_SCRIPTS / "vault-push.sh")

    # Machine A pushes for real first -- a genuine side effect of its own
    # vault-push.sh run, not history the test shaped by hand.
    proc_a = subprocess.run(
        ["bash", script, "-m", "from machine A", "from-a.md"],
        env=_machine_env(tmp_path / "machine-a", vault_a),
        capture_output=True, text=True, timeout=30,
    )
    assert proc_a.returncode == 0, proc_a.stdout + proc_a.stderr
    assert "push origin OK" in proc_a.stdout

    # Machine B is still based on the pre-A remote HEAD: its push is
    # rejected for REAL (non-fast-forward against what A just pushed),
    # forcing the script's real fetch -> compare -> clean-rebase -> retry
    # path, produced by an actual second process, not fabricated by the test.
    proc_b = subprocess.run(
        ["bash", script, "-m", "from machine B", "from-b.md"],
        env=_machine_env(tmp_path / "machine-b", vault_b),
        capture_output=True, text=True, timeout=30,
    )
    assert proc_b.returncode == 0, proc_b.stdout + proc_b.stderr
    assert "after a clean rebase" in proc_b.stdout

    # Nothing was lost: the remote carries both machines' commits.
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", str(remote), str(check)], check=True, capture_output=True)
    log = _git(check, "log", "--format=%s").stdout
    assert "from machine A" in log
    assert "from machine B" in log
    assert (check / "from-a.md").exists()
    assert (check / "from-b.md").exists()


def test_two_real_processes_started_concurrently_never_corrupt_the_remote(tmp_path):
    # Best-effort true-concurrency variant: both machines' vault-push.sh
    # started as independent OS processes with no ordering imposed by the
    # test (unlike the deterministic test above). This doesn't assert which
    # one the OS schedules first -- only the property that actually matters
    # for "6 machines on at once": both eventually succeed and nothing the
    # remote already accepted is ever lost.
    remote = tmp_path / "remote-race.git"
    _seed_bare_remote(remote)

    vault_a = _init_machine(tmp_path / "race-a", remote)
    vault_b = _init_machine(tmp_path / "race-b", remote)
    (vault_a / "race-a.md").write_text("race a\n", encoding="utf-8")
    (vault_b / "race-b.md").write_text("race b\n", encoding="utf-8")

    script = str(REAL_SCRIPTS / "vault-push.sh")
    proc_a = subprocess.Popen(
        ["bash", script, "-m", "race from A", "race-a.md"],
        env=_machine_env(tmp_path / "race-a", vault_a),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    proc_b = subprocess.Popen(
        ["bash", script, "-m", "race from B", "race-b.md"],
        env=_machine_env(tmp_path / "race-b", vault_b),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    out_a = proc_a.communicate(timeout=30)[0]
    out_b = proc_b.communicate(timeout=30)[0]

    assert proc_a.returncode == 0, out_a
    assert proc_b.returncode == 0, out_b

    check = tmp_path / "check-race"
    subprocess.run(["git", "clone", "-q", str(remote), str(check)], check=True, capture_output=True)
    log = _git(check, "log", "--format=%s").stdout
    assert "race from A" in log
    assert "race from B" in log
