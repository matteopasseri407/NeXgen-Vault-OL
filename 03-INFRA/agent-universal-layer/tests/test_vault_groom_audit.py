"""Behavioral tests for vault_groom_audit.py against REAL git repos.

vault-groom.sh/.ps1 call this script after a write pass returns (against a
throwaway CLONE, never the real vault -- see this script's own module
docstring and vault-groom.sh's "temp-clone gate" comment for the full
2026-07-13 architect-review rationale: "the audit must be the only
technical route a write can take to main and the remotes"). These tests
exercise the real git plumbing (clone/status/log/diff/fetch/merge), not
mocks -- the whole point of this script is to report and act on what git
actually did, not what an LLM claims it did.

Fixtures build TWO real repos per test: `vault` (the real one, never
touched except by a successful promotion) and `clone` (a real `git clone`
of `vault` with `origin` removed, exactly like the wrapper produces),
then simulate the write pass by committing directly into `clone`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REAL_UL = TESTS_DIR.parent
REAL_VAULT = REAL_UL.parent.parent
SCRIPTS_DIR = REAL_VAULT / "03-INFRA" / "scripts"
AUDIT_SCRIPT = SCRIPTS_DIR / "vault_groom_audit.py"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
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


def _clone(vault: Path, tmp_path: Path, name: str = "clone") -> Path:
    clone = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(vault), str(clone)], check=True)
    _git(clone, "remote", "remove", "origin")
    # A real `git clone` does NOT inherit the source repo's local
    # user.email/user.name (those are repo-local config, not something git
    # copies) -- the write pass (simulated here by committing straight into
    # the clone) needs its own identity, same as the wrapper's real clone
    # would pick up from the runner's ambient git config.
    _git(clone, "config", "user.email", "nexgen-tests.invalid")
    _git(clone, "config", "user.name", "Test")
    return clone


def _run_audit(vault, clone, base, state_dir, *, env_overrides=None, **overrides):
    args = {
        "vault": str(vault),
        "clone": str(clone),
        "branch": "main",
        "base": base,
        "archive_root": "99-ARCHIVE",
        "state_dir": str(state_dir),
        "timestamp": "test-run-0001",
        "runner": "claude",
        "model": "claude-sonnet-5",
        "tranche_sha256": "a" * 64,
        "plan_record": str(state_dir / "test-run-0001-plan.txt"),
        "propose_log": "/tmp/propose.log",
        "write_log": "/tmp/write.log",
        "write_exit_code": 0,
    }
    args.update(overrides)
    cli_args = []
    for key, value in args.items():
        flag = f"--{key.replace('_', '-')}"
        if isinstance(value, bool):
            # --push-if-clean: a store_true flag, only present when True --
            # its absence is what "never push this run" means to the parser.
            if value:
                cli_args.append(flag)
            continue
        cli_args += [flag, str(value)]
    env = None
    if env_overrides:
        env = dict(os.environ)
        env.update(env_overrides)
    import sys
    exe = sys.executable or "python3"
    return subprocess.run(
        [exe, str(AUDIT_SCRIPT), *cli_args],
        capture_output=True, text=True, timeout=30, env=env,
    )


def _write_plan(state_dir: Path, timestamp: str, text: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"{timestamp}-plan.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _record(state_dir: Path) -> dict:
    return json.loads((state_dir / "test-run-0001.json").read_text(encoding="utf-8"))


# --- Promotion: a clean audit fetches the clone's exact OID into the real
# vault and fast-forwards onto it. ---

def test_clean_run_promotes_exactly_the_audited_oid(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    (clone / "groomed.md").write_text("archived content\n", encoding="utf-8")
    _git(clone, "add", "groomed.md")
    _git(clone, "commit", "-q", "-m", "archive: fold groomed.md")
    audited_oid = _git(clone, "rev-parse", "HEAD").stdout.strip()

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, clone, base, state_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # Real git assertions on the vault after promotion -- not just the
    # JSON record's say-so.
    vault_head = _git(vault, "rev-parse", "HEAD").stdout.strip()
    assert vault_head != audited_oid, "the backlog commit lands ON TOP of the promoted OID"
    parent = _git(vault, "rev-parse", "HEAD~1").stdout.strip()
    assert parent == audited_oid
    merge_base = _git(vault, "merge-base", "HEAD", audited_oid).stdout.strip()
    assert merge_base == audited_oid, "the promoted OID must be a real ancestor of the new vault HEAD"
    log = _git(vault, "log", "--oneline").stdout
    assert "archive: fold groomed.md" in log

    record = _record(state_dir)
    assert record["promoted"] is True
    assert record["promoted_oid"] == audited_oid
    assert record["clone_head"] == audited_oid
    assert record["coverage_status"] == "clean"
    assert record["pushed"] is False


def test_record_when_clone_unchanged_reports_zero_commits(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    proc = _run_audit(vault, clone, base, state_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["commits"] == []
    assert record["files_touched"] == []
    assert record["promoted"] is True


def test_backlog_line_appended_and_committed_after_promotion(tmp_path):
    vault = _init_vault(tmp_path)
    (vault / "99-INDEX").mkdir()
    (vault / "99-INDEX" / "vault-cleanup-backlog.md").write_text("# Backlog\n", encoding="utf-8")
    _git(vault, "add", "99-INDEX/vault-cleanup-backlog.md")
    _git(vault, "commit", "-q", "-m", "seed backlog")
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, clone, base, state_dir, runner="codex", tranche_sha256="b" * 64)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    backlog = (vault / "99-INDEX" / "vault-cleanup-backlog.md").read_text(encoding="utf-8")
    assert "runner=codex" in backlog
    assert "commits=0" in backlog
    assert "coverage=clean" in backlog
    assert ("b" * 12) in backlog

    log = _git(vault, "log", "-1", "--format=%s").stdout.strip()
    assert log == "chore(groom): record run test-run-0001"


def test_backlog_created_when_missing(tmp_path):
    vault = _init_vault(tmp_path)
    assert not (vault / "99-INDEX").exists()
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, clone, base, state_dir)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (vault / "99-INDEX" / "vault-cleanup-backlog.md").exists()


def test_rerun_with_identical_backlog_line_is_a_harmless_noop(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone1 = _clone(vault, tmp_path, name="clone1")
    state_dir = tmp_path / "state"

    first = _run_audit(vault, clone1, base, state_dir)
    assert first.returncode == 0, first.stdout + first.stderr
    head_after_first = _git(vault, "rev-parse", "HEAD").stdout.strip()

    # Re-run with the exact same args (same timestamp/hash/runner) against a
    # FRESH clone of the now-promoted vault -- the backlog line
    # append_backlog_line would produce is byte-identical, so nothing
    # actually changes for git to commit.
    clone2 = _clone(vault, tmp_path, name="clone2")
    base2 = _git(vault, "rev-parse", "HEAD").stdout.strip()
    second = _run_audit(vault, clone2, base2, state_dir)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "nothing to commit" in second.stdout

    head_after_second = _git(vault, "rev-parse", "HEAD").stdout.strip()
    assert head_after_second == head_after_first


# --- Push: only reached after a successful promotion. KNOWLEDGE_VAULT_
# REMOTE=local makes publish() a genuine, successful no-op with no network
# needed (agent_sync.py's own Local-Only-mode branch) -- HOME is isolated to
# a per-test tmp dir so agent_sync.py's own log/lock files never touch the
# real machine. ---

def _isolated_env(tmp_path):
    home_dir = tmp_path / "isolated-home"
    home_dir.mkdir()
    return {"HOME": str(home_dir), "USERPROFILE": str(home_dir), "KNOWLEDGE_VAULT_REMOTE": "local"}, home_dir


def test_push_if_clean_with_clean_coverage_invokes_publish_and_reports_pushed(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    env_overrides, home_dir = _isolated_env(tmp_path)

    proc = _run_audit(
        vault, clone, base, state_dir,
        push_if_clean=True, engine_scripts=str(SCRIPTS_DIR),
        env_overrides=env_overrides,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["coverage_status"] == "clean"
    assert record["promoted"] is True
    assert record["pushed"] is True

    log_path = home_dir / ".local" / "state" / "agent-sync.log"
    assert log_path.is_file()
    assert "push: skipped (Local-Only mode)" in log_path.read_text(encoding="utf-8")


def test_push_if_clean_omitted_never_pushes(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    env_overrides, home_dir = _isolated_env(tmp_path)

    proc = _run_audit(vault, clone, base, state_dir, env_overrides=env_overrides)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["pushed"] is False
    assert record["promoted"] is True
    assert not (home_dir / ".local" / "state" / "agent-sync.log").exists(), (
        "agent_sync.py must never even be invoked when --push-if-clean is absent"
    )


def test_push_if_clean_reaches_a_real_configured_remote(tmp_path):
    # Not just the Local-Only no-op path: confirms agent_sync.py's publish()
    # actually pushes the promoted + backlog commits to a real (if
    # local-disk) bare remote when one is configured. "origin" is the
    # portable product default and matches what a real vault-groom user has
    # -- note this is the REAL VAULT's own origin, unrelated to the CLONE's
    # (deliberately absent) origin.
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

    vault = _init_vault(tmp_path)
    _git(vault, "remote", "add", "origin", str(remote))
    _git(vault, "push", "-q", "-u", "origin", "main")
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)

    state_dir = tmp_path / "state"
    home_dir = tmp_path / "isolated-home"
    home_dir.mkdir()
    env_overrides = {"HOME": str(home_dir), "USERPROFILE": str(home_dir)}

    proc = _run_audit(
        vault, clone, base, state_dir,
        push_if_clean=True, engine_scripts=str(SCRIPTS_DIR),
        env_overrides=env_overrides,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["pushed"] is True

    local_head = _git(vault, "rev-parse", "HEAD").stdout.strip()
    remote_ref = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert remote_ref == local_head


def test_push_if_clean_without_engine_scripts_errors(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    proc = _run_audit(vault, clone, base, state_dir, push_if_clean=True)
    assert proc.returncode == 2
    assert "--engine-scripts" in proc.stderr


# --- Coverage check: did the write pass actually do what it approved, and
# nothing else? Path-exact only now (2026-07-13 review removed the old
# basename-anywhere fallback); the one remaining exception is a genuine
# archive move under archive_root. ---

def test_coverage_flags_a_planned_file_that_was_never_touched(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    plan = _write_plan(
        state_dir, "test-run-0001",
        "| Nota | Azione | Perché |\n"
        "|---|---|---|\n"
        "| `touched.md` | **archive** | ok |\n"
        "| `forgotten.md` | **fix-frontmatter** | ok |\n",
    )
    (clone / "touched.md").write_text("x\n", encoding="utf-8")
    _git(clone, "add", "touched.md")
    _git(clone, "commit", "-q", "-m", "archive: touched.md")

    proc = _run_audit(vault, clone, base, state_dir, plan_record=str(plan))
    assert proc.returncode == 4, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["unaddressed_targets"] == ["forgotten.md"]
    assert record["coverage_status"] == "dirty"
    assert record["promoted"] is False
    assert "WARNING" in proc.stderr
    assert "forgotten.md" in proc.stderr

    # No promotion -- the real vault must be untouched.
    vault_head_after = _git(vault, "rev-parse", "HEAD").stdout.strip()
    assert vault_head_after == base
    assert not (vault / "99-INDEX").exists(), "the backlog must never be written on a blocked run"


def test_coverage_ignores_rows_flagged_nessuna_azione(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    plan = _write_plan(
        state_dir, "test-run-0001",
        "| Nota | Azione | Perché |\n"
        "|---|---|---|\n"
        "| `untouched-on-purpose.md` | **nessuna azione** | in dubbio, si lascia |\n",
    )
    proc = _run_audit(vault, clone, base, state_dir, plan_record=str(plan))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["unaddressed_targets"] == []
    assert record["coverage_status"] == "clean"
    assert "WARNING" not in proc.stderr


def test_coverage_accepts_an_archive_move_under_the_archive_root(tmp_path):
    vault = _init_vault(tmp_path)
    (vault / "old-note.md").write_text("content\n", encoding="utf-8")
    _git(vault, "add", "old-note.md")
    _git(vault, "commit", "-q", "-m", "seed old-note")
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    plan = _write_plan(
        state_dir, "test-run-0001",
        "| Nota | Azione | Perché |\n"
        "|---|---|---|\n"
        "| `old-note.md` | **archive** | chiuso |\n",
    )
    (clone / "99-ARCHIVE").mkdir()
    (clone / "99-ARCHIVE" / "old-note.md").write_text("content\n", encoding="utf-8")
    _git(clone, "rm", "-q", "old-note.md")
    _git(clone, "add", "99-ARCHIVE/old-note.md")
    _git(clone, "commit", "-q", "-m", "archive(nexgen): sposta old-note.md in 99-ARCHIVE/")

    proc = _run_audit(vault, clone, base, state_dir, plan_record=str(plan))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["unaddressed_targets"] == [], "an archive move under archive_root must not false-positive"
    assert record["matched_by_archive_move"] == ["99-ARCHIVE/old-note.md"]
    assert record["out_of_scope_targets"] == []
    assert record["coverage_status"] == "clean"
    assert record["promoted"] is True


def test_coverage_accepts_the_playbooks_own_archive_rename_pattern(tmp_path):
    # The shipped playbook's step 4 literally instructs the rename
    # "<name>-archive-<date>.md" -- exact-basename-only matching would
    # false-quarantine a by-the-book archive (found by the 2026-07-13
    # implementation review). The stem+'-' prefix rule covers it.
    vault = _init_vault(tmp_path)
    (vault / "old-note.md").write_text("content\n", encoding="utf-8")
    _git(vault, "add", "old-note.md")
    _git(vault, "commit", "-q", "-m", "seed old-note")
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    plan = _write_plan(
        state_dir, "test-run-0001",
        "| Nota | Azione | Perché |\n"
        "|---|---|---|\n"
        "| `old-note.md` | **archive** | chiuso |\n",
    )
    (clone / "99-ARCHIVE").mkdir()
    renamed = "99-ARCHIVE/old-note-archive-2026-07-13.md"
    (clone / renamed).write_text("content\n", encoding="utf-8")
    _git(clone, "rm", "-q", "old-note.md")
    _git(clone, "add", renamed)
    _git(clone, "commit", "-q", "-m", "archive(nexgen): archivia old-note.md col pattern del playbook")

    proc = _run_audit(vault, clone, base, state_dir, plan_record=str(plan))
    assert proc.returncode == 0, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["matched_by_archive_move"] == [renamed]
    assert record["out_of_scope_targets"] == []
    assert record["coverage_status"] == "clean"
    assert record["promoted"] is True


def test_coverage_rejects_an_archive_move_outside_the_archive_root(tmp_path):
    # Same shape as the accepted case above, but landing OUTSIDE the
    # configured archive root -- this is exactly what the old
    # basename-anywhere fallback would have silently accepted, and the
    # 2026-07-13 review removed that fallback for precisely this reason.
    # old-note.md's own deletion still marks the TARGET itself addressed
    # (its original path is genuinely touched); what trips coverage dirty
    # is the new file landing somewhere the archive-move exception doesn't
    # reach, which stays out_of_scope.
    vault = _init_vault(tmp_path)
    (vault / "old-note.md").write_text("content\n", encoding="utf-8")
    _git(vault, "add", "old-note.md")
    _git(vault, "commit", "-q", "-m", "seed old-note")
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    plan = _write_plan(
        state_dir, "test-run-0001",
        "| Nota | Azione | Perché |\n"
        "|---|---|---|\n"
        "| `old-note.md` | **archive** | chiuso |\n",
    )
    (clone / "somewhere-else").mkdir()
    (clone / "somewhere-else" / "old-note.md").write_text("content\n", encoding="utf-8")
    _git(clone, "rm", "-q", "old-note.md")
    _git(clone, "add", "somewhere-else/old-note.md")
    _git(clone, "commit", "-q", "-m", "moved old-note.md outside the archive root")

    proc = _run_audit(vault, clone, base, state_dir, plan_record=str(plan))
    assert proc.returncode == 4, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["unaddressed_targets"] == [], "old-note.md's own deletion still counts as touched"
    assert record["out_of_scope_targets"] == ["somewhere-else/old-note.md"]
    assert record["matched_by_archive_move"] == []
    assert record["coverage_status"] == "dirty"
    assert record["promoted"] is False


def test_coverage_archive_move_exception_never_applies_to_non_archive_actions(tmp_path):
    vault = _init_vault(tmp_path)
    (vault / "old-note.md").write_text("content\n", encoding="utf-8")
    _git(vault, "add", "old-note.md")
    _git(vault, "commit", "-q", "-m", "seed old-note")
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    plan = _write_plan(
        state_dir, "test-run-0001",
        "| Nota | Azione | Perché |\n"
        "|---|---|---|\n"
        "| `old-note.md` | **compress** | troppo lungo |\n",
    )
    (clone / "99-ARCHIVE").mkdir()
    (clone / "99-ARCHIVE" / "old-note.md").write_text("content\n", encoding="utf-8")
    _git(clone, "rm", "-q", "old-note.md")
    _git(clone, "add", "99-ARCHIVE/old-note.md")
    _git(clone, "commit", "-q", "-m", "moved old-note.md despite a compress action")

    proc = _run_audit(vault, clone, base, state_dir, plan_record=str(plan))
    assert proc.returncode == 4, proc.stdout + proc.stderr
    record = _record(state_dir)
    assert record["unaddressed_targets"] == [], "old-note.md's own deletion still counts as touched"
    assert record["out_of_scope_targets"] == ["99-ARCHIVE/old-note.md"]
    assert record["matched_by_archive_move"] == [], "the exception only applies to archive-type actions"
    assert record["coverage_status"] == "dirty"


def test_coverage_handles_a_multi_file_merge_row(tmp_path):
    vault = _init_vault(tmp_path)
    for name in ("a.md", "b.md", "c.md"):
        (vault / name).write_text("x\n", encoding="utf-8")
    _git(vault, "add", "a.md", "b.md", "c.md")
    _git(vault, "commit", "-q", "-m", "seed a/b/c")
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    plan = _write_plan(
        state_dir, "test-run-0001",
        "| Nota | Azione | Perché |\n"
        "|---|---|---|\n"
        "| `a.md` + `b.md` + `c.md` | **merge** in una nota | duplicati |\n",
    )

    _git(clone, "rm", "-q", "a.md", "b.md", "c.md")
    (clone / "merged.md").write_text("merged\n", encoding="utf-8")
    _git(clone, "add", "merged.md")
    _git(clone, "commit", "-q", "-m", "merge: a+b+c into merged.md")

    proc = _run_audit(vault, clone, base, state_dir, plan_record=str(plan))
    assert proc.returncode == 4, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["unaddressed_targets"] == []
    # merged.md was never named in the tranche -- it IS out of scope by the
    # letter of the check, same as any other unplanned file. The merge
    # action itself is still fully covered (a.md/b.md/c.md all matched by
    # exact path, since --no-renames reports deleted paths as touched too).
    assert record["out_of_scope_targets"] == ["merged.md"]


def test_coverage_missing_plan_record_file_is_harmless(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    proc = _run_audit(vault, clone, base, state_dir, plan_record=str(state_dir / "does-not-exist-plan.txt"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    record = _record(state_dir)
    assert record["unaddressed_targets"] == []
    assert record["coverage_status"] == "clean"


def test_coverage_flags_out_of_scope_file_not_named_in_the_tranche(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    plan = _write_plan(
        state_dir, "test-run-0001",
        "| Nota | Azione | Perché |\n|---|---|---|\n| `planned.md` | **archive** | ok |\n",
    )
    (clone / "planned.md").write_text("x\n", encoding="utf-8")
    (clone / "unplanned.md").write_text("y\n", encoding="utf-8")
    _git(clone, "add", "planned.md", "unplanned.md")
    _git(clone, "commit", "-q", "-m", "archive planned.md, sneak in unplanned.md")

    proc = _run_audit(vault, clone, base, state_dir, plan_record=str(plan))
    assert proc.returncode == 4, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["unaddressed_targets"] == []
    assert record["out_of_scope_targets"] == ["unplanned.md"]
    assert record["coverage_status"] == "dirty"
    assert "WARNING" in proc.stderr


def test_coverage_unparseable_when_tranche_has_content_but_zero_table_rows(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    state_dir = tmp_path / "state"
    plan = _write_plan(state_dir, "test-run-0001", "just some prose, no table at all\n")

    proc = _run_audit(vault, clone, base, state_dir, plan_record=str(plan))
    assert proc.returncode == 4, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["coverage_status"] == "unparseable"
    assert record["unaddressed_targets"] == []
    assert record["out_of_scope_targets"] == []
    assert record["promoted"] is False
    assert "WARNING" in proc.stderr
    assert "unparseable" in proc.stderr.lower() or "no rows parsed" in proc.stderr.lower()


# --- Audit gate itself: clone hygiene, linearity, freshness, write-pass
# failure -- the checks that make "the audit is the only route to main"
# actually true. ---

def test_dirty_clone_working_tree_blocks_promotion_and_quarantines(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    (clone / "uncommitted.md").write_text("oops\n", encoding="utf-8")  # never git add'ed
    state_dir = tmp_path / "state"

    proc = _run_audit(vault, clone, base, state_dir)
    assert proc.returncode == 4, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["clone_clean"] is False
    assert record["promoted"] is False
    vault_head_after = _git(vault, "rev-parse", "HEAD").stdout.strip()
    assert vault_head_after == base
    marker = clone / ".GROOM_QUARANTINE.json"
    assert marker.is_file()
    assert "not clean" in marker.read_text(encoding="utf-8")
    assert str(clone) in proc.stderr
    assert "UNTOUCHED" in proc.stderr


def test_merge_commit_in_clone_blocks_promotion_linearity(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)

    _git(clone, "checkout", "-q", "-b", "side")
    (clone / "side.md").write_text("side\n", encoding="utf-8")
    _git(clone, "add", "side.md")
    _git(clone, "commit", "-q", "-m", "side commit")
    _git(clone, "checkout", "-q", "main")
    (clone / "main.md").write_text("main\n", encoding="utf-8")
    _git(clone, "add", "main.md")
    _git(clone, "commit", "-q", "-m", "main commit")
    _git(clone, "merge", "-q", "--no-ff", "-m", "merge side into main", "side")

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, clone, base, state_dir)
    assert proc.returncode == 4, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["history_linear"] is False
    assert record["promoted"] is False
    vault_head_after = _git(vault, "rev-parse", "HEAD").stdout.strip()
    assert vault_head_after == base
    assert (clone / ".GROOM_QUARANTINE.json").is_file()


def test_vault_moved_mid_run_aborts_stale_no_promotion(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    (clone / "groomed.md").write_text("x\n", encoding="utf-8")
    _git(clone, "add", "groomed.md")
    _git(clone, "commit", "-q", "-m", "archive: groomed.md")

    # Something else wrote to the real vault while the write pass was
    # running -- BASE is now stale.
    (vault / "unrelated.md").write_text("meanwhile\n", encoding="utf-8")
    _git(vault, "add", "unrelated.md")
    _git(vault, "commit", "-q", "-m", "unrelated concurrent write")
    moved_head = _git(vault, "rev-parse", "HEAD").stdout.strip()

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, clone, base, state_dir)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "vault moved during grooming" in proc.stderr

    record = _record(state_dir)
    assert record["promoted"] is False
    vault_head_after = _git(vault, "rev-parse", "HEAD").stdout.strip()
    assert vault_head_after == moved_head, "the real vault must be completely untouched"
    assert (clone / ".GROOM_QUARANTINE.json").is_file()


def test_clone_has_no_remotes_after_wrapper_setup(tmp_path):
    # Not a vault_groom_audit.py behavior per se -- pins the temp-clone
    # gate's own invariant (`git remote remove origin` right after clone)
    # that everything else in this file's promotion tests depends on.
    vault = _init_vault(tmp_path)
    clone = _clone(vault, tmp_path)
    remotes = _git(clone, "remote").stdout.strip()
    assert remotes == ""


def test_runner_non_zero_exit_still_writes_audit_record_and_quarantines(tmp_path):
    vault = _init_vault(tmp_path)
    base = _git(vault, "rev-parse", "HEAD").stdout.strip()
    clone = _clone(vault, tmp_path)
    # The write pass CLI died mid-way -- some partial work may exist, but
    # write_exit_code != 0 must block promotion unconditionally regardless
    # of what git state the clone happens to be in.
    (clone / "partial.md").write_text("half-done\n", encoding="utf-8")
    _git(clone, "add", "partial.md")
    _git(clone, "commit", "-q", "-m", "partial: runner crashed before finishing")

    state_dir = tmp_path / "state"
    proc = _run_audit(vault, clone, base, state_dir, write_exit_code=17)
    assert proc.returncode == 4, proc.stdout + proc.stderr

    record = _record(state_dir)
    assert record["write_exit_code"] == 17
    assert record["promoted"] is False
    assert "blocked_reason" in record
    assert "exited 17" in record["blocked_reason"]

    record_path = state_dir / "test-run-0001.json"
    assert record_path.is_file(), "the audit record must be written even on a write-pass failure"
    vault_head_after = _git(vault, "rev-parse", "HEAD").stdout.strip()
    assert vault_head_after == base
    assert (clone / ".GROOM_QUARANTINE.json").is_file()
