"""Cross-platform coverage for agent_sync.py's `vault-push` subcommand.

test_vault_push.py (unchanged, the acceptance harness) exercises the same
contract through the POSIX vault-push.sh wrapper and is skipped on Windows
because bash isn't guaranteed there. This file invokes `python agent_sync.py
vault-push` directly -- no bash, no skipif(nt) -- so the same contract is
proven on windows-latest CI too, where vault-push.sh cannot run at all.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import REAL_SCRIPTS, Sandbox

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(sandbox: Sandbox) -> tuple[Path, Path]:
    subprocess.run(["git", "init", "-b", "main", str(sandbox.vault)], check=True, capture_output=True)
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


def _run_vault_push(sandbox: Sandbox, *args: str, env: dict | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), "vault-push", *args],
        env=sandbox.env() if env is None else env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_vault_push_python_commits_and_pushes_to_authoritative_remote(sandbox):
    oracle, _mirror = _init_repo(sandbox)
    config = sandbox.ul / "sync" / "remotes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "schema_version: 1\nauthoritative_remote: oracle\nmirrors: [origin]\n",
        encoding="utf-8",
    )
    env = sandbox.env()
    env.pop("KNOWLEDGE_VAULT_REMOTE", None)

    proc = _run_vault_push(sandbox, "-m", "configure remotes", str(config.relative_to(sandbox.vault)), env=env)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "push oracle OK" in proc.stdout
    local_head = _git(sandbox.vault, "rev-parse", "main").stdout.strip()
    oracle_head = subprocess.run(
        ["git", "--git-dir", str(oracle), "rev-parse", "main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert local_head == oracle_head


def test_vault_push_python_local_only_commits_and_skips_publication(sandbox):
    _init_repo(sandbox)
    before = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    target = sandbox.vault / "note.txt"
    target.write_text("local-only change\n", encoding="utf-8")
    env = sandbox.env()
    env["KNOWLEDGE_VAULT_REMOTE"] = "local"

    proc = _run_vault_push(sandbox, "-m", "local-only infra change", str(target.relative_to(sandbox.vault)), env=env)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "push skipped" in proc.stdout
    after = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    assert after != before, "the local commit must still happen in Local-Only mode"


def test_vault_push_python_blocked_while_sync_lock_is_held(sandbox):
    _init_repo(sandbox)
    before = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    target = sandbox.vault / "note.txt"
    target.write_text("should not be committed\n", encoding="utf-8")

    lock_file = sandbox.home / "agent-sync.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_file.open("a+b")
    if os.name == "nt":
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    try:
        env = sandbox.env()
        env["KNOWLEDGE_VAULT_REMOTE"] = "local"
        env["AGENT_SYNC_LOCK_FILE"] = str(lock_file)
        env["AGENT_SYNC_LOCK_TIMEOUT_SECONDS"] = "0.3"

        proc = _run_vault_push(sandbox, "-m", "must not commit", str(target.relative_to(sandbox.vault)), env=env, timeout=30)
    finally:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()

    assert proc.returncode == 75, proc.stdout + proc.stderr
    assert "sync lock busy" in proc.stderr
    assert _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip() == before


def test_vault_push_python_nothing_staged_is_a_clean_noop(sandbox):
    _init_repo(sandbox)
    before = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    env = sandbox.env()
    env["KNOWLEDGE_VAULT_REMOTE"] = "local"

    # No files given and nothing pre-staged: "-- nothing to commit" path,
    # never reaches git commit at all.
    proc = _run_vault_push(sandbox, "-m", "empty push", env=env)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "nothing staged" in proc.stdout
    assert _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip() == before


def test_vault_push_python_realigns_diverged_mirror_with_force_with_lease(sandbox):
    oracle, mirror = _init_repo(sandbox)
    config = sandbox.ul / "sync" / "remotes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "schema_version: 1\nauthoritative_remote: oracle\nmirrors: [origin]\n",
        encoding="utf-8",
    )
    env = sandbox.env()
    env.pop("KNOWLEDGE_VAULT_REMOTE", None)

    # _init_repo only pushes the fixture commit to oracle: the mirror
    # (origin.git) starts out completely empty (no main branch at all), so
    # a first vault-push run is needed just to give it a HEAD -- otherwise
    # the "diverge the mirror" clone below would have no branch to check
    # out or push against.
    seed_target = sandbox.vault / "seed.txt"
    seed_target.write_text("seed the mirror\n", encoding="utf-8")
    seed_proc = _run_vault_push(sandbox, "-m", "seed the mirror", str(seed_target.relative_to(sandbox.vault)), env=env)
    assert seed_proc.returncode == 0, seed_proc.stdout + seed_proc.stderr

    # Diverge the mirror independently of the authoritative remote: a
    # separate clone pushes a commit to the mirror ONLY, so a plain `git
    # push origin main` from the sandbox vault is rejected (non-fast-
    # forward) and the subcommand must fall through to its fetch +
    # force-with-lease realignment branch, not the plain-push happy path
    # test_vault_push_python_commits_and_pushes_to_authoritative_remote
    # already covers.
    outside = sandbox.home / "outside-clone"
    subprocess.run(["git", "clone", "-q", str(mirror), str(outside)], check=True, capture_output=True)
    _git(outside, "config", "user.email", "nexgen-tests.invalid")
    _git(outside, "config", "user.name", "NeXgen tests")
    # The bare mirror's own HEAD symref was never repointed to "main" (only
    # its refs/heads/main got created by the seed push above), so a plain
    # clone leaves no branch checked out -- check out the branch explicitly.
    _git(outside, "checkout", "-B", "main", "origin/main")
    (outside / "mirror-only.txt").write_text("mirror diverged\n", encoding="utf-8")
    _git(outside, "add", "mirror-only.txt")
    _git(outside, "commit", "-m", "mirror-only divergence")
    _git(outside, "push", "origin", "main")

    target = sandbox.vault / "note.txt"
    target.write_text("authoritative change\n", encoding="utf-8")

    proc = _run_vault_push(sandbox, "-m", "authoritative change", str(target.relative_to(sandbox.vault)), env=env)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mirror origin aligned to authoritative oracle" in proc.stdout
    local_head = _git(sandbox.vault, "rev-parse", "main").stdout.strip()
    oracle_head = subprocess.run(
        ["git", "--git-dir", str(oracle), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    mirror_head = subprocess.run(
        ["git", "--git-dir", str(mirror), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert local_head == oracle_head == mirror_head, "force-with-lease must realign the mirror, never rewrite oracle"


def test_vault_push_python_skips_one_malformed_env_mirror_without_failing_the_push(sandbox):
    # KNOWLEDGE_VAULT_MIRRORS is the emergency/bootstrap override (same env
    # var the old pre-port vault-push.sh read directly): one typo'd entry
    # in a value someone typed by hand during an actual emergency must
    # never brick the authoritative push -- it should skip just that
    # mirror, with a warning, same as the old script's behavior.
    oracle, mirror = _init_repo(sandbox)
    env = sandbox.env()
    env["KNOWLEDGE_VAULT_REMOTE"] = "oracle"
    env["KNOWLEDGE_VAULT_MIRRORS"] = "origin,bad remote name!"
    target = sandbox.vault / "note.txt"
    target.write_text("env-mirror override change\n", encoding="utf-8")

    proc = _run_vault_push(sandbox, "-m", "env mirror override", str(target.relative_to(sandbox.vault)), env=env)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "push oracle OK" in proc.stdout
    assert "push mirror origin OK" in proc.stdout
    assert "skipping this mirror" in proc.stderr
    local_head = _git(sandbox.vault, "rev-parse", "main").stdout.strip()
    oracle_head = subprocess.run(
        ["git", "--git-dir", str(oracle), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    mirror_head = subprocess.run(
        ["git", "--git-dir", str(mirror), "rev-parse", "main"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert local_head == oracle_head == mirror_head


def test_vault_push_python_usage_error_without_message_exits_2(sandbox):
    proc = _run_vault_push(sandbox, "--")

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "needs -m" in proc.stderr


def test_vault_push_python_missing_dash_m_argument_exits_2(sandbox):
    proc = _run_vault_push(sandbox, "-m")

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "argument missing for -m" in proc.stderr


# ── vault-push.sh's degraded emergency lane (2026-07-13 adversarial review)
# ───────────────────────────────────────────────────────────────────────
# These two exercise vault-push.sh itself (not agent_sync.py's vault-push
# subcommand), so -- unlike the rest of this file -- they are POSIX-only:
# the degraded lane is bash+git, no python involved at all, by design (it
# exists precisely for when python3/agent_sync.py aren't usable). Windows'
# own degraded lane lives in vault-push.ps1, exercised separately (pwsh is
# not available in this sandbox's CI matrix for direct testing).

@pytest.mark.skipif(
    os.name == "nt",
    reason="vault-push.sh's degraded emergency lane is POSIX-only (bash+git, no python)",
)
def test_vault_push_sh_degraded_lane_commits_with_warning_when_engine_unreachable(sandbox):
    _init_repo(sandbox)
    (sandbox.scripts_dir / "agent_sync.py").unlink()
    before = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    target = sandbox.vault / "note.txt"
    target.write_text("degraded lane commit\n", encoding="utf-8")
    env = sandbox.env()
    env["KNOWLEDGE_VAULT_REMOTE"] = "local"

    proc = subprocess.run(
        ["bash", str(sandbox.scripts_dir / "vault-push.sh"), "-m", "degraded commit",
         str(target.relative_to(sandbox.vault))],
        env=env, capture_output=True, text=True, timeout=30,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "degraded emergency lane" in proc.stderr
    assert "push skipped" in proc.stdout
    after = _git(sandbox.vault, "rev-parse", "HEAD").stdout.strip()
    assert after != before, "the degraded lane must still commit locally"


@pytest.mark.skipif(
    os.name == "nt",
    reason="vault-push.sh's degraded emergency lane is POSIX-only (bash+git, no python)",
)
def test_vault_push_sh_degraded_lane_requires_explicit_remote_opt_in(sandbox):
    (sandbox.scripts_dir / "agent_sync.py").unlink()
    env = sandbox.env()
    env.pop("KNOWLEDGE_VAULT_REMOTE", None)

    proc = subprocess.run(
        ["bash", str(sandbox.scripts_dir / "vault-push.sh"), "-m", "must not commit"],
        env=env, capture_output=True, text=True, timeout=30,
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "engine checkout is incomplete" in proc.stderr


# ── Real multi-machine contention, windows-latest-safe (2026-07-13
# follow-up) ────────────────────────────────────────────────────────────
# test_vault_push.py already proves this exact contract through the bash
# wrapper (POSIX-only, skipped on Windows). These two invoke `python
# agent_sync.py vault-push` directly against REAL git remotes and REAL
# independent OS processes -- no bash, no skipif(nt) -- so the same
# rebase-on-divergence and true-concurrency guarantees are proven on
# windows-latest CI, where vault-push.sh cannot run at all.

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


def test_vault_push_python_recovers_from_real_push_rejection_via_clean_rebase(tmp_path):
    remote = tmp_path / "remote.git"
    _seed_bare_remote(remote)

    vault_a = _init_machine(tmp_path / "machine-a", remote)
    vault_b = _init_machine(tmp_path / "machine-b", remote)
    (vault_a / "from-a.md").write_text("machine a\n", encoding="utf-8")
    (vault_b / "from-b.md").write_text("machine b\n", encoding="utf-8")

    agent_sync = str(REAL_SCRIPTS / "agent_sync.py")

    # Machine A pushes for real first -- a genuine side effect of its own
    # vault-push run, not history the test shaped by hand.
    proc_a = subprocess.run(
        [sys.executable, agent_sync, "vault-push", "-m", "from machine A", "from-a.md"],
        env=_machine_env(tmp_path / "machine-a", vault_a),
        capture_output=True, text=True, timeout=30,
    )
    assert proc_a.returncode == 0, proc_a.stdout + proc_a.stderr
    assert "push origin OK" in proc_a.stdout

    # Machine B is still based on the pre-A remote HEAD: its push is
    # rejected for REAL (non-fast-forward against what A just pushed),
    # forcing the subcommand's real fetch -> compare -> clean-rebase ->
    # retry path, produced by an actual second process, not fabricated by
    # the test.
    proc_b = subprocess.run(
        [sys.executable, agent_sync, "vault-push", "-m", "from machine B", "from-b.md"],
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


def test_vault_push_python_two_concurrent_real_processes_never_lose_a_commit(tmp_path):
    # Best-effort true-concurrency variant: both machines' vault-push
    # started as independent OS processes with no ordering imposed by the
    # test. This doesn't assert which one the OS schedules first -- only
    # the property that actually matters: both eventually succeed and
    # nothing the remote already accepted is ever lost.
    remote = tmp_path / "remote-race.git"
    _seed_bare_remote(remote)

    vault_a = _init_machine(tmp_path / "race-a", remote)
    vault_b = _init_machine(tmp_path / "race-b", remote)
    (vault_a / "race-a.md").write_text("race a\n", encoding="utf-8")
    (vault_b / "race-b.md").write_text("race b\n", encoding="utf-8")

    agent_sync = str(REAL_SCRIPTS / "agent_sync.py")
    proc_a = subprocess.Popen(
        [sys.executable, agent_sync, "vault-push", "-m", "race from A", "race-a.md"],
        env=_machine_env(tmp_path / "race-a", vault_a),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    proc_b = subprocess.Popen(
        [sys.executable, agent_sync, "vault-push", "-m", "race from B", "race-b.md"],
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
