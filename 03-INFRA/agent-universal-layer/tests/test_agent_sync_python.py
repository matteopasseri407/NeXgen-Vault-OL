"""Cross-platform smoke for the B2.5 unified provisioner.

The older POSIX regression tests still exercise `agent-sync.sh` as the public
interface on Ubuntu. This one calls `agent_sync.py` directly so Windows CI can
prove the shared implementation runs in a sandboxed USERPROFILE.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import REAL_SCRIPTS, load_agent_sync_module, run_agent_sync_python


def _patch_apply_phases(monkeypatch, mod, called: list[str]) -> None:
    for name in (
        "preflight",
        "data_migrations",
        "instructions",
        "antigravity_mcp",
        "utils",
        "local_model_runtime",
        "install_scheduler",
        "mcp_render",
        "vault_skills",
        "runtimes",
        "skills_index",
        "claude_hooks",
    ):
        monkeypatch.setattr(mod, name, lambda _env, phase=name: called.append(phase))
    monkeypatch.setattr(mod, "creds_health", lambda *_args, **_kwargs: None)


def _git(repo: Path, *args: str, capture_output: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=capture_output,
        text=True,
    )


def _init_git_vault(sandbox, *remote_names: str) -> dict[str, Path]:
    subprocess.run(
        ["git", "init", "-b", "main", str(sandbox.vault)],
        check=True,
        capture_output=True,
    )
    _git(sandbox.vault, "config", "user.email", "nexgen-tests.invalid")
    _git(sandbox.vault, "config", "user.name", "NeXgen tests")
    _git(sandbox.vault, "add", ".")
    _git(sandbox.vault, "commit", "-m", "fixture")
    remotes = {}
    for name in remote_names:
        path = sandbox.home / f"{name}.git"
        subprocess.run(["git", "init", "--bare", str(path)], check=True, capture_output=True)
        _git(sandbox.vault, "remote", "add", name, str(path))
        remotes[name] = path
    if remote_names:
        _git(sandbox.vault, "push", "-u", remote_names[0], "main")
    return remotes


def test_agent_sync_python_guard_smoke(sandbox):
    sb = sandbox
    for rt in (".claude/skills", ".codex/skills"):
        (sb.home / rt).mkdir(parents=True, exist_ok=True)

    proc = run_agent_sync_python(sb, "guard")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = (sb.home / ".local" / "state" / "agent-sync.log").read_text(encoding="utf-8")
    assert "agent-sync: start mode=guard" in log
    assert "agent-sync: completed mode=guard" in log


def test_agent_sync_python_accepts_legacy_powershell_mode_flag(sandbox):
    for rt in (".claude/skills", ".codex/skills"):
        (sandbox.home / rt).mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), "-Mode", "guard"],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = (sandbox.home / ".local" / "state" / "agent-sync.log").read_text(encoding="utf-8")
    assert "agent-sync: start mode=guard" in log
    assert "unknown mode" not in proc.stderr


def test_no_arguments_only_prints_help_and_does_not_mutate_home(sandbox):
    before = sandbox.tree_snapshot()

    proc = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py")],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "agent_sync modes:" in proc.stdout
    assert sandbox.tree_snapshot() == before


def test_unexpected_arguments_fail_before_mutating_home(sandbox):
    before = sandbox.tree_snapshot()

    proc = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), "guard", "surprise"],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 2
    assert "unexpected arguments" in proc.stderr
    assert sandbox.tree_snapshot() == before


def test_remote_config_is_loaded_from_vault_data_and_env_can_override(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    config = sandbox.ul / "sync" / "remotes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "schema_version: 1\nauthoritative_remote: oracle\nmirrors: [origin]\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("KNOWLEDGE_VAULT_REMOTE", raising=False)
    monkeypatch.delenv("KNOWLEDGE_VAULT_MIRRORS", raising=False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    configured = mod.Env()
    assert configured.remote == "oracle"
    assert configured.mirrors == ("origin",)

    monkeypatch.setenv("KNOWLEDGE_VAULT_REMOTE", "emergency")
    monkeypatch.setenv("KNOWLEDGE_VAULT_MIRRORS", "backup-a,backup-b")
    overridden = mod.Env()
    assert overridden.remote == "emergency"
    assert overridden.mirrors == ("backup-a", "backup-b")


def test_invalid_remote_config_fails_before_mutating_home(sandbox):
    config = sandbox.ul / "sync" / "remotes.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "schema_version: 1\nauthoritative_remote: [not, a, string]\n",
        encoding="utf-8",
    )
    before = sandbox.tree_snapshot()
    env = sandbox.env()
    env.pop("KNOWLEDGE_VAULT_REMOTE", None)

    proc = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), "guard"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 2
    assert "remote config" in proc.stderr.lower()
    assert sandbox.tree_snapshot() == before


def test_invalid_environment_remote_name_fails_before_mutating_home(sandbox):
    before = sandbox.tree_snapshot()

    proc = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), "guard"],
        env=sandbox.env(KNOWLEDGE_VAULT_REMOTE="--upload-pack=surprise"),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 2
    assert "invalid Git remote name" in proc.stderr
    assert sandbox.tree_snapshot() == before


def test_guard_blocks_apply_when_pull_state_is_dirty(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    monkeypatch.setenv("KNOWLEDGE_VAULT_REMOTE", "origin")
    called: list[str] = []
    _patch_apply_phases(monkeypatch, mod, called)
    monkeypatch.setattr(
        mod,
        "pull",
        lambda _env: mod.PullOutcome(mod.PullState.DIRTY, "tracked changes"),
    )

    rc = mod.main(["guard"])

    assert rc == 1
    assert called == []


def test_offline_apply_requires_explicit_override(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    monkeypatch.setenv("KNOWLEDGE_VAULT_REMOTE", "origin")
    called: list[str] = []
    _patch_apply_phases(monkeypatch, mod, called)
    monkeypatch.setattr(
        mod,
        "pull",
        lambda _env: mod.PullOutcome(mod.PullState.FETCH_FAILED, "network unavailable"),
    )

    assert mod.main(["apply"]) == 1
    assert called == []

    assert mod.main(["apply", "--allow-offline"]) == 0
    assert "mcp_render" in called
    assert "skills_index" in called


def test_apply_renders_antigravity_source_before_propagating_it(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    monkeypatch.setenv("KNOWLEDGE_VAULT_REMOTE", "local")
    called: list[str] = []
    _patch_apply_phases(monkeypatch, mod, called)

    assert mod.main(["apply"]) == 0
    assert called.index("mcp_render") < called.index("antigravity_mcp")


def test_apply_returns_nonzero_when_a_declared_phase_fails(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    monkeypatch.setenv("KNOWLEDGE_VAULT_REMOTE", "local")
    called: list[str] = []
    _patch_apply_phases(monkeypatch, mod, called)
    monkeypatch.setattr(mod, "mcp_render", lambda _env: False)

    rc = mod.main(["apply"])

    assert rc == 1
    assert "skills_index" in called
    log = (sandbox.home / ".local" / "state" / "agent-sync.log").read_text(encoding="utf-8")
    assert "phase mcp_render: ERROR" in log


def test_real_dirty_git_tree_blocks_guard_before_runtime_mutation(sandbox):
    _init_git_vault(sandbox, "oracle")
    agents = sandbox.ul / "instructions" / "AGENTS.md"
    agents.write_text(agents.read_text(encoding="utf-8") + "local edit\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), "guard"],
        env=sandbox.env(KNOWLEDGE_VAULT_REMOTE="oracle"),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert not (sandbox.home / "CLAUDE.md").exists()
    assert not (sandbox.home / ".local" / "bin" / "agent-skill").exists()
    log = (sandbox.home / ".local" / "state" / "agent-sync.log").read_text(encoding="utf-8")
    assert "pull: blocked (the vault has uncommitted tracked changes" in log
    assert "apply: BLOCKED" in log


def test_real_wrong_branch_blocks_guard_before_runtime_mutation(sandbox):
    _init_git_vault(sandbox, "oracle")
    _git(sandbox.vault, "switch", "-c", "offline-work")

    proc = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), "guard"],
        env=sandbox.env(KNOWLEDGE_VAULT_REMOTE="oracle"),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert not (sandbox.home / "CLAUDE.md").exists()
    assert not (sandbox.home / ".local" / "bin" / "agent-skill").exists()
    log = (sandbox.home / ".local" / "state" / "agent-sync.log").read_text(encoding="utf-8")
    assert "pull: blocked (current branch is offline-work, expected main)" in log


# ── Remaining PullState coverage on real git (beta-readiness review,
# 2026-07-13) ───────────────────────────────────────────────────────────
# FRESH/LOCAL_ONLY/WRONG_BRANCH/DIRTY/FETCH_FAILED already had real-git
# coverage above; AHEAD, DIVERGED, REMOTE_MISSING and ERROR (the most
# dangerous one -- it blocks an automatic merge on ambiguous history) had
# none, only mocked pull() returns for DIRTY/FETCH_FAILED elsewhere. These
# call pull() directly (not through guard/apply) since triggering AHEAD/
# DIVERGED/ERROR needs precise git history shaping that a full CLI run
# would otherwise obscure behind unrelated phase output.

def _env_for(sandbox, monkeypatch, mod, **overrides):
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    return mod.Env()


def test_pull_reports_ahead_when_local_has_unpushed_commits(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    _init_git_vault(sandbox, "oracle")
    (sandbox.vault / "local-only-commit.txt").write_text("ahead\n", encoding="utf-8")
    _git(sandbox.vault, "add", "local-only-commit.txt")
    _git(sandbox.vault, "commit", "-m", "local commit never pushed")
    env = _env_for(sandbox, monkeypatch, mod, KNOWLEDGE_VAULT_REMOTE="oracle")

    outcome = mod.pull(env)

    assert outcome.state == mod.PullState.AHEAD
    assert not outcome.allows_apply


def test_pull_reports_diverged_on_real_conflicting_history(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    remotes = _init_git_vault(sandbox, "oracle")
    # Diverge: a second clone pushes a commit the local checkout never sees,
    # while the local checkout ALSO commits something of its own on top of
    # the same shared ancestor -- neither is a fast-forward of the other.
    other_clone = sandbox.home / "other-clone"
    # --branch main explicitly: the bare remote's own HEAD symref (set by
    # `git init --bare` before any push ever named "main") is not
    # guaranteed to point at "main", so a plain clone can fail to check out
    # any branch at all ("remote HEAD refers to nonexistent ref").
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remotes["oracle"]), str(other_clone)],
        check=True, capture_output=True,
    )
    _git(other_clone, "config", "user.email", "nexgen-tests.invalid")
    _git(other_clone, "config", "user.name", "NeXgen tests")
    (other_clone / "remote-side-commit.txt").write_text("remote diverges\n", encoding="utf-8")
    _git(other_clone, "add", "remote-side-commit.txt")
    _git(other_clone, "commit", "-m", "remote-side commit")
    _git(other_clone, "push", "origin", "main")  # `git clone` names it origin, not oracle
    (sandbox.vault / "local-side-commit.txt").write_text("local diverges\n", encoding="utf-8")
    _git(sandbox.vault, "add", "local-side-commit.txt")
    _git(sandbox.vault, "commit", "-m", "local-side commit")
    env = _env_for(sandbox, monkeypatch, mod, KNOWLEDGE_VAULT_REMOTE="oracle")

    outcome = mod.pull(env)

    assert outcome.state == mod.PullState.DIVERGED
    assert not outcome.allows_apply


def test_pull_reports_remote_missing_when_configured_remote_was_never_added(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    _init_git_vault(sandbox)  # no remotes at all
    env = _env_for(sandbox, monkeypatch, mod, KNOWLEDGE_VAULT_REMOTE="oracle")

    outcome = mod.pull(env)

    assert outcome.state == mod.PullState.REMOTE_MISSING
    assert not outcome.allows_apply


def test_pull_reports_error_on_unrelated_histories(sandbox, monkeypatch):
    """Local `main` and `oracle/main` both exist and both fetch/rev-parse
    fine (so neither FETCH_FAILED nor a rev-parse failure fires first) --
    but they share no common ancestor, so `git merge-base` itself fails.
    The one ERROR path this suite had never exercised for real: local and
    remote history that genuinely cannot be compared, not merely blocked."""
    mod = load_agent_sync_module(sandbox)
    subprocess.run(["git", "init", "-b", "main", str(sandbox.vault)], check=True, capture_output=True)
    _git(sandbox.vault, "config", "user.email", "nexgen-tests.invalid")
    _git(sandbox.vault, "config", "user.name", "NeXgen tests")
    _git(sandbox.vault, "add", ".")
    _git(sandbox.vault, "commit", "-m", "local, unrelated history")

    unrelated_remote = sandbox.home / "oracle.git"
    unrelated_seed = sandbox.home / "unrelated-seed"
    subprocess.run(["git", "init", "-b", "main", str(unrelated_seed)], check=True, capture_output=True)
    _git(unrelated_seed, "config", "user.email", "nexgen-tests.invalid")
    _git(unrelated_seed, "config", "user.name", "NeXgen tests")
    (unrelated_seed / "seed.txt").write_text("completely separate repo\n", encoding="utf-8")
    _git(unrelated_seed, "add", "seed.txt")
    _git(unrelated_seed, "commit", "-m", "remote, unrelated history")
    subprocess.run(["git", "init", "--bare", str(unrelated_remote)], check=True, capture_output=True)
    _git(unrelated_seed, "remote", "add", "origin", str(unrelated_remote))
    _git(unrelated_seed, "push", "origin", "main")

    _git(sandbox.vault, "remote", "add", "oracle", str(unrelated_remote))
    env = _env_for(sandbox, monkeypatch, mod, KNOWLEDGE_VAULT_REMOTE="oracle")

    outcome = mod.pull(env)

    assert outcome.state == mod.PullState.ERROR
    assert not outcome.allows_apply


def test_publish_blocks_when_local_branch_is_behind_authoritative_remote(sandbox):
    remotes = _init_git_vault(sandbox, "oracle")
    writer = sandbox.home / "other-writer"
    subprocess.run(
        ["git", "clone", "-b", "main", str(remotes["oracle"]), str(writer)],
        check=True,
        capture_output=True,
    )
    _git(writer, "config", "user.email", "nexgen-writer.invalid")
    _git(writer, "config", "user.name", "Other writer")
    (writer / "remote-change.txt").write_text("new authoritative data\n", encoding="utf-8")
    _git(writer, "add", "remote-change.txt")
    _git(writer, "commit", "-m", "remote change")
    _git(writer, "push", "origin", "main")

    proc = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), "publish"],
        env=sandbox.env(KNOWLEDGE_VAULT_REMOTE="oracle"),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 1, proc.stdout + proc.stderr
    log = (sandbox.home / ".local" / "state" / "agent-sync.log").read_text(encoding="utf-8")
    assert "push: BLOCKED because local main is behind authoritative oracle/main" in log


def test_publish_aligns_mirror_even_when_authoritative_is_already_current(sandbox):
    remotes = _init_git_vault(sandbox, "oracle", "origin")

    proc = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), "publish"],
        env=sandbox.env(
            KNOWLEDGE_VAULT_REMOTE="oracle",
            KNOWLEDGE_VAULT_MIRRORS="origin",
        ),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    local_head = _git(sandbox.vault, "rev-parse", "main").stdout.strip()
    mirror_head = subprocess.run(
        ["git", "--git-dir", str(remotes["origin"]), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert mirror_head == local_head


@pytest.mark.parametrize("relative_manifest", ["mcp/manifest.yaml", "skills/skills.manifest.yaml"])
def test_invalid_manifest_blocks_in_preflight_before_apply(sandbox, relative_manifest):
    (sandbox.ul / relative_manifest).write_text("- invalid-root\n", encoding="utf-8")

    proc = run_agent_sync_python(sandbox, "apply")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    log = (sandbox.home / ".local" / "state" / "agent-sync.log").read_text(encoding="utf-8")
    assert "phase preflight: ERROR" in log
    assert not (sandbox.vault / "99-INDEX" / "DATA-SCHEMA-VERSION.txt").exists()


def test_preflight_command_validates_without_generating_runtime_files(sandbox):
    proc = run_agent_sync_python(sandbox, "preflight")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (sandbox.vault / "99-INDEX" / "DATA-SCHEMA-VERSION.txt").exists()
    assert not (sandbox.home / ".local" / "bin").exists()


def test_preflight_blocks_invalid_claude_hook_shape_before_copying_hook(sandbox):
    claude_dir = sandbox.home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text('{"hooks": []}\n', encoding="utf-8")

    proc = run_agent_sync_python(sandbox, "apply")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert not (claude_dir / "claude-vault-checkpoint.mjs").exists()
    assert not (sandbox.vault / "99-INDEX" / "DATA-SCHEMA-VERSION.txt").exists()


def test_preflight_blocks_invalid_optional_council_data_before_apply(sandbox):
    seats = sandbox.ul / "council" / "seats.yaml"
    seats.parent.mkdir(parents=True, exist_ok=True)
    seats.write_text(
        """schema_version: 1
seats:
  unsafe:
    vendor: example
    cli: opencode
    model: example/model
    zero_retention: "true"
""",
        encoding="utf-8",
    )

    proc = run_agent_sync_python(sandbox, "apply")

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert not (sandbox.vault / "99-INDEX" / "DATA-SCHEMA-VERSION.txt").exists()


# ── Timeout on Python-helper subprocess calls inside the sync lock
# (beta-readiness review, 2026-07-13) ──────────────────────────────────────
# mcp_render()/skills_index()/preflight() called render.py/skills-sync.py
# with no timeout=, all three from inside `with SyncRunLock(...)`: a hang in
# any of them held the host-wide lock forever, silently, with no logged
# error (the guard timer would just never complete). _run_python_script()
# centralizes the fix.

def test_run_python_script_times_out_instead_of_hanging(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    hang_script = sandbox.home / "hang.py"
    hang_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    result = mod._run_python_script([sys.executable, str(hang_script)], timeout=1)

    assert result.returncode != 0
    assert "timed out after 1s" in result.stderr


def test_run_python_script_returns_real_output_on_success(sandbox):
    mod = load_agent_sync_module(sandbox)
    ok_script = sandbox.home / "ok.py"
    ok_script.write_text("print('hello')\n", encoding="utf-8")

    result = mod._run_python_script([sys.executable, str(ok_script)], timeout=10)

    assert result.returncode == 0
    assert "hello" in result.stdout


# ── OpenCode instructions pointer (beta-readiness review, 2026-07-13) ─────
# The bug this closes: instructions() relinked Claude/Gemini/Codex/Antigravity
# but never touched OpenCode at all -- opencode.json's own "instructions"
# array (its equivalent of a bootstrap pointer, confirmed against a real
# working config) was left permanently unset by any code path, so
# agent-doctor's "OpenCode instructions -> AGENTS.md" check failed forever
# on a fresh install, for one of the 4 officially supported CLIs.

def test_windows_opencode_path_prefers_current_xdg_location(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(sandbox.home / "AppData" / "Roaming"))

    assert mod._opencode_config_path(sandbox.home) == (
        sandbox.home / ".config" / "opencode" / "opencode.json"
    )


def test_windows_opencode_path_keeps_appdata_only_compatibility(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    appdata_root = sandbox.home / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata_root))
    xdg = sandbox.home / ".config" / "opencode" / "opencode.json"
    xdg.unlink(missing_ok=True)
    appdata = appdata_root / "opencode" / "opencode.json"
    appdata.parent.mkdir(parents=True, exist_ok=True)
    appdata.write_text("{}\n", encoding="utf-8")

    assert mod._opencode_config_path(sandbox.home) == appdata

def test_instructions_adds_opencode_pointer_to_existing_config(sandbox_with_live_configs, monkeypatch):
    sandbox = sandbox_with_live_configs
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    env = mod.Env()

    assert mod.instructions(env) is True

    oc_path = sandbox.live_config_path("opencode")
    config = json.loads(oc_path.read_text(encoding="utf-8"))
    canon = env.instance_ul / "instructions" / "AGENTS.md"
    expected_entry = "~/" + canon.relative_to(sandbox.home).as_posix()
    assert expected_entry in config["instructions"]
    # Additive: pre-existing MCP section and other keys must survive untouched.
    assert config["model"] == "fake-provider/fake-model"
    assert "fake-stdio-tool" in config["mcp"]


def test_instructions_opencode_missing_config_is_a_noop(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    env = mod.Env()

    assert mod.instructions(env) is True

    assert not (sandbox.home / ".config" / "opencode" / "opencode.json").exists()


def test_instructions_opencode_pointer_is_idempotent(sandbox_with_live_configs, monkeypatch):
    sandbox = sandbox_with_live_configs
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    env = mod.Env()

    mod.instructions(env)
    oc_path = sandbox.live_config_path("opencode")
    first_pass = json.loads(oc_path.read_text(encoding="utf-8"))

    mod.instructions(env)
    second_pass = json.loads(oc_path.read_text(encoding="utf-8"))

    assert first_pass["instructions"] == second_pass["instructions"]
    assert second_pass["instructions"].count(second_pass["instructions"][0]) == 1
    # Exactly one backup, from the first (real) write -- the second, no-op
    # call must not detect a "change" and back up again.
    assert len(list(oc_path.parent.glob("opencode.json.pre-instructions-*.bak"))) == 1


def test_instructions_opencode_deduplicates_windows_and_posix_spellings(
    sandbox_with_live_configs, monkeypatch
):
    sandbox = sandbox_with_live_configs
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    env = mod.Env()
    oc_path = sandbox.live_config_path("opencode")
    config = json.loads(oc_path.read_text(encoding="utf-8"))
    canonical = "~/KnowledgeVault/03-INFRA/agent-universal-layer/instructions/AGENTS.md"
    config["instructions"] = [
        canonical,
        canonical.replace("/", "\\"),
        "~/KnowledgeVault/03-INFRA/another-instruction.md",
    ]
    oc_path.write_text(json.dumps(config), encoding="utf-8")

    assert mod.instructions(env) is True

    updated = json.loads(oc_path.read_text(encoding="utf-8"))
    assert updated["instructions"] == [
        canonical,
        "~/KnowledgeVault/03-INFRA/another-instruction.md",
    ]


def test_instructions_opencode_malformed_json_does_not_crash(sandbox_with_live_configs, monkeypatch):
    sandbox = sandbox_with_live_configs
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    oc_path = sandbox.live_config_path("opencode")
    oc_path.write_text("{not valid json", encoding="utf-8")
    env = mod.Env()

    # instructions() must still relink Claude/Gemini/Codex and return True --
    # one CLI's broken config must not abort the rest of the fan-out.
    assert mod.instructions(env) is True
    assert oc_path.read_text(encoding="utf-8") == "{not valid json"


def test_host_wide_lock_rejects_second_manual_run(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    env = mod.Env()

    with mod.SyncRunLock(env.lock_path, timeout=0) as first:
        assert first.acquired
        with mod.SyncRunLock(env.lock_path, timeout=0) as second:
            assert not second.acquired


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink launcher behavior is covered on Linux and macOS.")
def test_posix_utils_links_agent_sync_and_agent_doctor_launchers(sandbox, monkeypatch):
    # Real gap found in a 2026-07-13 follow-up: agent-sync/agent-doctor were
    # documented everywhere (README, INIT.md, both concept maps) as bare
    # commands, but utils() -- the only code that links anything onto PATH
    # -- never linked either one. The systemd guard timer's own ExecStart
    # depends on the agent-sync symlink existing; _persisted_engine_root()
    # reads it too. Same bug class already fixed for vault-groom/firecrawl-
    # local, just missed in that pass.
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.utils(env)

    agent_sync_link = sandbox.home / ".local" / "bin" / "agent-sync"
    agent_doctor_link = sandbox.home / ".local" / "bin" / "agent-doctor"
    assert agent_sync_link.is_symlink()
    assert agent_sync_link.resolve() == (sandbox.scripts_dir / "agent-sync.sh").resolve()
    assert agent_doctor_link.is_symlink()
    assert agent_doctor_link.resolve() == (sandbox.scripts_dir / "agent-doctor.sh").resolve()


def test_windows_utils_installs_agent_sync_and_agent_doctor_command_wrappers(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.utils(env)

    for name in ("agent-sync", "agent-doctor"):
        launcher = sandbox.home / ".local" / "bin" / f"{name}.ps1"
        wrapper = sandbox.home / ".local" / "bin" / f"{name}.cmd"
        assert launcher.exists(), f"{name}.ps1 launcher missing"
        assert not launcher.is_symlink()
        launcher_text = launcher.read_text(encoding="utf-8")
        assert str(sandbox.scripts_dir / f"{name}.ps1") in launcher_text
        assert "& $Target @args" in launcher_text
        assert f"{name}.ps1" in wrapper.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher execution requires PowerShell.")
def test_windows_agent_sync_launcher_executes_the_engine_script_not_the_bin_directory(sandbox, monkeypatch):
    """A file symlink makes $PSScriptRoot resolve to ~/.local/bin instead of
    the engine scripts directory, so agent-sync cannot find agent_sync.py.
    The generated shim must invoke the real target and preserve its sibling
    lookup in a physical Windows PowerShell process."""
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    assert mod.utils(env)
    launcher = sandbox.home / ".local" / "bin" / "agent-sync.ps1"
    result = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(launcher), "--help",
        ],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "agent_sync modes:" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="systemd is a Linux-only recurring trigger; Windows uses schtasks.exe instead.")
def test_systemd_install_warns_loudly_if_agent_sync_link_is_somehow_missing(sandbox, monkeypatch):
    # utils() always runs before install_scheduler() in the same apply/guard
    # pass, so this should never fire in practice -- but the phase loop does
    # not abort on an unrelated phase failure, so this is the fallback that
    # keeps a missing link from failing completely silently.
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    monkeypatch.setattr(mod, "resolve_cmd", lambda name: None)  # no real systemctl in the sandbox

    env = mod.Env()
    # Deliberately skip utils() -- agent-sync was never linked this pass.
    mod._install_systemd_units(env)

    assert "agent-sync does not exist yet" in env.log_path.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="systemd is a Linux-only recurring trigger; Windows uses schtasks.exe instead.")
def test_systemd_install_does_not_warn_once_agent_sync_is_linked(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    monkeypatch.setattr(mod, "resolve_cmd", lambda name: None)

    env = mod.Env()
    mod.utils(env)
    mod._install_systemd_units(env)

    assert "agent-sync does not exist yet" not in env.log_path.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink launcher behavior is covered on Linux and macOS.")
def test_posix_utils_links_council_launcher(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.utils(env)

    launcher = sandbox.home / ".local" / "bin" / "council"
    assert launcher.is_symlink()
    assert launcher.resolve() == (sandbox.scripts_dir / "council.sh").resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink launcher behavior is covered on Linux and macOS.")
def test_posix_utils_links_vault_groom_launcher(sandbox, monkeypatch):
    # Real gap found on the gardener's first live run (2026-07-13): the
    # README/n8n reminder/playbook all say "run `vault-groom`" as a bare
    # command, but nothing ever actually linked it onto PATH -- it was
    # never invokable without the full script path.
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.utils(env)

    launcher = sandbox.home / ".local" / "bin" / "vault-groom"
    assert launcher.is_symlink()
    assert launcher.resolve() == (sandbox.scripts_dir / "vault-groom.sh").resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink launcher behavior is covered on Linux and macOS.")
def test_posix_utils_links_firecrawl_local_launcher(sandbox, monkeypatch):
    # Same bug class as vault-groom, found by the same cascading check
    # (2026-07-13): documented everywhere as a bare command, never linked.
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.utils(env)

    launcher = sandbox.home / ".local" / "bin" / "firecrawl-local"
    assert launcher.is_symlink()
    assert launcher.resolve() == (sandbox.scripts_dir / "firecrawl-local.sh").resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits are not the Windows permission model.")
def test_posix_utils_does_not_change_the_engine_source_mode(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    source = sandbox.scripts_dir / "council.sh"
    source.chmod(0o644)

    env = mod.Env()
    mod.utils(env)

    assert source.stat().st_mode & 0o777 == 0o644
    assert not (sandbox.home / ".local" / "bin" / "council").exists()


def test_windows_utils_installs_council_command_wrapper(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.utils(env)

    launcher = sandbox.home / ".local" / "bin" / "council.ps1"
    wrapper = sandbox.home / ".local" / "bin" / "council.cmd"
    assert launcher.exists()
    assert not launcher.is_symlink()
    assert str(sandbox.scripts_dir / "council.ps1") in launcher.read_text(encoding="utf-8")
    assert 'council.ps1' in wrapper.read_text(encoding="utf-8")
    skill_wrapper = sandbox.home / ".local" / "bin" / "agent-skill.cmd"
    assert skill_wrapper.exists()
    assert "agent-skill.py" in skill_wrapper.read_text(encoding="utf-8")


def test_windows_utils_installs_vault_groom_command_wrapper(sandbox, monkeypatch):
    # Same real gap as the POSIX test above, Windows side: vault-groom.ps1
    # existed but was never linked, so `vault-groom` was not a real command
    # on Windows either.
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.utils(env)

    launcher = sandbox.home / ".local" / "bin" / "vault-groom.ps1"
    wrapper = sandbox.home / ".local" / "bin" / "vault-groom.cmd"
    assert launcher.exists()
    assert not launcher.is_symlink()
    assert str(sandbox.scripts_dir / "vault-groom.ps1") in launcher.read_text(encoding="utf-8")
    assert "vault-groom.ps1" in wrapper.read_text(encoding="utf-8")


def test_windows_utils_installs_firecrawl_command_wrapper(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.utils(env)

    launcher = sandbox.home / ".local" / "bin" / "firecrawl-local.ps1"
    wrapper = sandbox.home / ".local" / "bin" / "firecrawl-local.cmd"
    assert launcher.exists()
    assert not launcher.is_symlink()
    assert str(sandbox.scripts_dir / "firecrawl-local.ps1") in launcher.read_text(encoding="utf-8")
    assert "firecrawl-local.ps1" in wrapper.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher behavior is covered on Linux and macOS.")
def test_posix_utils_installs_agent_skill_command(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.utils(env)

    wrapper = sandbox.home / ".local" / "bin" / "agent-skill"
    assert wrapper.exists()
    assert wrapper.stat().st_mode & 0o111
    assert "agent-skill.py" in wrapper.read_text(encoding="utf-8")


def test_windows_file_copy_fallback_is_idempotent(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)

    def fail_symlink(self, target, target_is_directory=False):
        raise OSError("symlink privilege unavailable")

    monkeypatch.setattr(Path, "symlink_to", fail_symlink)
    src = sandbox.home / "src.txt"
    dst = sandbox.home / "dst.txt"
    src.write_text("same bytes\n", encoding="utf-8")

    assert mod.make_link(src, dst, is_dir=False) is True
    first = dst.read_bytes()
    assert first == src.read_bytes()
    assert mod.make_link(src, dst, is_dir=False) is False
    assert dst.read_bytes() == first


def test_windows_local_worker_runtime_is_preserved(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    instructions = sandbox.ul / "instructions"
    (instructions / "GEMMA.md").write_text("Gemma bootstrap\n", encoding="utf-8")
    (instructions / "LOCAL-WORKER.md").write_text("Local worker bootstrap\n", encoding="utf-8")
    private_scripts = sandbox.vault / "03-INFRA" / "scripts"
    private_scripts.mkdir(parents=True, exist_ok=True)
    (private_scripts / "local-model-agent.ps1").write_text("param()\n", encoding="utf-8")
    legacy = sandbox.home / ".local" / "bin"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "gemma-worker.ps1").write_text(
        "$ScriptPath = Join-Path $PSScriptRoot 'local-model-agent.ps1'\r\n"
        "& $ScriptPath -Mode worker @args\r\n",
        encoding="utf-8",
    )
    (legacy / "gemma-agent.ps1").write_text(
        "$ScriptPath = Join-Path $PSScriptRoot 'local-model-agent.ps1'\r\n"
        "& $ScriptPath -Mode agent @args\r\n",
        encoding="utf-8",
    )

    env = mod.Env()
    mod.instructions(env)
    mod.local_model_runtime(env)

    assert (sandbox.home / "GEMMA.md").exists()
    assert (sandbox.home / "LOCAL-WORKER.md").exists()
    assert (sandbox.home / ".local" / "bin" / "local-model-agent.ps1").exists()
    for name in ("local-worker.ps1", "local-agent.ps1"):
        text = (sandbox.home / ".local" / "bin" / name).read_text(encoding="utf-8")
        assert "local-model-agent.ps1" in text
    assert not (sandbox.home / ".local" / "bin" / "gemma-worker.ps1").exists()
    assert not (sandbox.home / ".local" / "bin" / "gemma-agent.ps1").exists()


def test_windows_local_worker_does_not_delete_user_owned_legacy_alias(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    private_scripts = sandbox.vault / "03-INFRA" / "scripts"
    private_scripts.mkdir(parents=True, exist_ok=True)
    (private_scripts / "local-model-agent.ps1").write_text("param()\n", encoding="utf-8")
    alias = sandbox.home / ".local" / "bin" / "gemma-worker.ps1"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.write_text("Write-Output 'user owned'\n", encoding="utf-8")

    assert mod.local_model_runtime(mod.Env()) is True

    assert alias.read_text(encoding="utf-8") == "Write-Output 'user owned'\n"


def test_windows_runtime_skill_dirs_are_created(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.runtimes(env)

    assert (sandbox.home / ".claude" / "skills").is_dir()
    assert (sandbox.home / ".codex" / "skills").is_dir()


def test_windows_reparse_point_is_detected_without_pathlib_junction_support(sandbox, monkeypatch):
    """Older supported Python builds lack Path.is_junction(). The Windows
    adapter must still recognize a directory reparse point as link-like."""
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)

    class ReparsePoint:
        def is_symlink(self):
            return False

    monkeypatch.setattr(
        mod.os,
        "lstat",
        lambda _path: SimpleNamespace(st_file_attributes=mod._REPARSE_POINT),
    )

    assert mod._is_link_like(ReparsePoint())


def test_windows_junction_command_quotes_cmd_metacharacters(sandbox, monkeypatch, tmp_path):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    source = tmp_path / "source&folder"
    destination = tmp_path / "destination&folder"
    source.mkdir()
    captured = {}

    def fake_external(argv, **_kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(mod, "_run_external", fake_external)
    assert mod.make_link(source, destination, is_dir=True)
    assert captured["argv"][:5] == ["cmd.exe", "/d", "/c", "mklink", "/J"]
    assert captured["argv"][5] == str(destination).replace("&", "^&")
    assert captured["argv"][6] == str(source).replace("&", "^&")


def test_windows_process_probe_detects_node_wrapped_claude(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    calls = []

    def fake_external(argv, **_kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "node.exe claude.js --print", "")

    monkeypatch.setattr(mod, "_run_external", fake_external)
    assert mod._process_running("claude") is True
    assert calls[0][0] == "powershell.exe"


def test_windows_atomic_write_retries_locked_replace(sandbox, monkeypatch, tmp_path):
    mod = load_agent_sync_module(sandbox)
    target = tmp_path / "locked.json"
    target.write_text("old", encoding="utf-8")
    real_replace = mod.os.replace
    attempts = []

    def flaky_replace(source, destination):
        attempts.append((source, destination))
        if len(attempts) < 3:
            raise PermissionError("sharing violation")
        return real_replace(source, destination)

    monkeypatch.setattr(mod.os, "replace", flaky_replace)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)
    mod._atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert len(attempts) == 3


def test_windows_alert_translation_prefers_powershell_twin(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    translator = sandbox.vault / "03-INFRA" / "alert-translate.ps1"
    translator.parent.mkdir(parents=True, exist_ok=True)
    translator.write_text("Write-Output translated", encoding="utf-8")
    captured = {}

    def fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "tradotto", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod._localize_alert(mod.Env(), "English alert") == "tradotto"
    assert captured["argv"][0] == "powershell.exe"


def test_windows_codex_eager_junction_is_converted_without_touching_active_view(sandbox, monkeypatch):
    """Codex must not point at an entire eager discovery root."""
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    source = env.active_skills / "fake-skill-a"
    source.mkdir(parents=True)
    source_file = source / "SKILL.md"
    source_file.write_text("canonical source\n", encoding="utf-8")
    source_bytes = source_file.read_bytes()

    runtime = sandbox.home / ".codex" / "skills"
    runtime.mkdir(parents=True)

    real_resolve = Path.resolve

    def simulate_junction_resolve(self, *args, **kwargs):
        if self == runtime:
            return real_resolve(env.active_skills)
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_junction", lambda self: self == runtime, raising=False)
    monkeypatch.setattr(Path, "resolve", simulate_junction_resolve)

    removed = []

    def remove_simulated_junction(path):
        removed.append(path)
        shutil.rmtree(path)

    monkeypatch.setattr(mod, "_remove_path", remove_simulated_junction)

    mod.runtimes(env)

    assert removed == [runtime]
    assert source_file.read_bytes() == source_bytes
    assert runtime.is_dir() and not runtime.is_symlink()
    assert not (runtime / source.name).exists()


def test_windows_claude_library_junction_is_preserved(sandbox, monkeypatch):
    """Claude may retain its native lazy whole-library view."""
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    source = env.skill_library / "fake-skill-a"
    source.mkdir(parents=True)
    source_file = source / "SKILL.md"
    source_file.write_text("canonical source\n", encoding="utf-8")
    source_bytes = source_file.read_bytes()

    runtime = sandbox.home / ".claude" / "skills"
    runtime.mkdir(parents=True)
    real_resolve = Path.resolve

    def simulate_junction_resolve(self, *args, **kwargs):
        if self == runtime:
            return real_resolve(env.skill_library)
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_junction", lambda self: self == runtime, raising=False)
    monkeypatch.setattr(Path, "resolve", simulate_junction_resolve)
    removed = []
    monkeypatch.setattr(mod, "_remove_path", lambda path: removed.append(path))

    mod.runtimes(env)

    assert source_file.read_bytes() == source_bytes
    assert removed == []
    assert runtime.is_dir()


def test_windows_runtimes_leave_manual_child_copies_for_explicit_migration(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    library_skill = sandbox.skill_library / "fake-skill-excluded"
    library_skill.mkdir(parents=True, exist_ok=True)
    (library_skill / "SKILL.md").write_text("canonical manual skill\n", encoding="utf-8")
    for runtime in (sandbox.home / ".claude" / "skills", sandbox.home / ".codex" / "skills"):
        copied_skill = runtime / "fake-skill-excluded"
        copied_skill.mkdir(parents=True, exist_ok=True)
        (copied_skill / "SKILL.md").write_text("stale copy\n", encoding="utf-8")

    env = mod.Env()
    mod.runtimes(env)

    assert (sandbox.home / ".claude" / "skills" / "fake-skill-excluded").is_dir()
    assert (sandbox.home / ".codex" / "skills" / "fake-skill-excluded").is_dir()
    assert (sandbox.skill_library / "fake-skill-excluded" / "SKILL.md").is_file()


def test_windows_backup_failure_does_not_delete_local_edit(sandbox, monkeypatch):
    """A local edit differs from src on Windows (no link privilege, fell back
    to a real copy) and the backup-before-overwrite fails (locked file, full
    disk, ...): make_link must not fall through to deleting dst without a
    confirmed backup. Regression for a full-codebase audit finding,
    2026-07-09."""
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)

    src = sandbox.home / "src.txt"
    dst = sandbox.home / "dst.txt"
    src.write_text("canonical\n", encoding="utf-8")
    dst.write_text("local edit, different from src\n", encoding="utf-8")

    def fail_copy2(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod.shutil, "copy2", fail_copy2)

    result = mod.make_link(src, dst, is_dir=False)

    assert result is False
    assert dst.read_text(encoding="utf-8") == "local edit, different from src\n"


def test_claude_hooks_skips_non_dict_settings_root(sandbox, monkeypatch):
    """settings.json can be syntactically valid JSON with a non-object root
    (e.g. "[]"); claude_hooks must skip cleanly instead of crashing with
    AttributeError on settings.setdefault(...), which would abort the rest
    of the agent-sync run (publish/creds/health run after it in main()).
    Regression for a full-codebase audit finding, 2026-07-09."""
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    hooks_dir = sandbox.ul / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "claude-vault-checkpoint.mjs").write_text("// hook\n", encoding="utf-8")
    claude_dir = sandbox.home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"
    settings_path.write_text("[]", encoding="utf-8")

    env = mod.Env()
    mod.claude_hooks(env)  # must not raise

    assert settings_path.read_text(encoding="utf-8") == "[]"


def test_alert_creds_credential_id_is_not_interpolated_into_remote_script(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    monkeypatch.setenv("KNOWLEDGE_VAULT_REMOTE", "origin")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    dangerous_cred_id = 'dummy"); require("child_process").execSync("touch /tmp/pwned"); //'
    monkeypatch.setenv("N8N_TELEGRAM_CRED_ID", dangerous_cred_id)
    monkeypatch.setenv("REMOTE_ALIAS", "oracle")
    monkeypatch.setenv("N8N_CONTAINER", "n8n-n8n-1")

    calls = []

    def fake_run(args, *, input, capture_output, text, timeout):
        calls.append((args, input, capture_output, text, timeout))
        return subprocess.CompletedProcess(args, 0, stdout="retrieved-token\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    env = mod.Env()
    mod._ensure_alert_creds(env)

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "retrieved-token"
    args, remote_script, capture_output, text, timeout = calls[0]
    assert args[:4] == ["ssh", "-o", "ConnectTimeout=12", "-o"]
    assert args[-2] == "oracle"
    remote_command = args[-1]
    assert remote_command.endswith(f" sh -s -- {shlex.quote(dangerous_cred_id)}")
    assert dangerous_cred_id not in remote_script
    assert 'x.id==="' not in remote_script
    assert "process.env.N8N_TELEGRAM_CRED_ID" in remote_script
    assert "mktemp /tmp/agent-sync-n8n-creds.XXXXXX" in remote_script
    assert 'chmod 600 "$tmpfile"' in remote_script
    assert 'trap \'rm -f "$tmpfile"\'' in remote_script
    assert '--output="$tmpfile"' in remote_script
    assert capture_output is True
    assert text is True
    assert timeout == 20


def test_systemd_env_line_quotes_values_with_spaces(sandbox):
    """Regression for finding 13: an unquoted 'Environment=KEY=value with
    spaces' splits on whitespace in systemd, so the unit silently sees a
    truncated path instead of the real one. The whole assignment must be
    quoted, per systemd.syntax(7)."""
    mod = load_agent_sync_module(sandbox)

    quoted = mod._systemd_env_line("AGENT_ENGINE_ROOT", "/opt/agents/nexgen engine")
    assert quoted == 'Environment="AGENT_ENGINE_ROOT=/opt/agents/nexgen engine"'

    # Embedded double-quote and backslash must be C-escaped inside the quotes,
    # not just wrapped, or the unit file itself becomes malformed.
    escaped = mod._systemd_env_line("AGENT_VAULT_DATA", 'C:\\weird"path')
    assert escaped == 'Environment="AGENT_VAULT_DATA=C:\\\\weird\\"path"'


def test_systemd_service_content_emits_quoted_environment_lines(sandbox, monkeypatch):
    """End-to-end: _systemd_service_content must route both overrides through
    the quoting helper, not string-format them directly."""
    mod = load_agent_sync_module(sandbox)
    engine_root_with_space = sandbox.home / "engine root"
    engine_root_with_space.mkdir()
    vault_data_with_space = sandbox.home / "vault data"
    vault_data_with_space.mkdir()
    env = SimpleNamespace(
        vault=sandbox.vault,
        engine_root=engine_root_with_space,
        vault_data=vault_data_with_space,
    )

    content = mod._systemd_service_content(env)

    # Built through the same helper under test, not a raw f-string: on
    # Windows CI, engine_root_with_space.resolve() contains backslashes,
    # which _systemd_env_line C-escapes -- a literal expected string would
    # mismatch there even though the production code is correct.
    assert mod._systemd_env_line("AGENT_ENGINE_ROOT", str(engine_root_with_space.resolve())) in content
    assert mod._systemd_env_line("AGENT_VAULT_DATA", str(vault_data_with_space.resolve())) in content
    # No unquoted Environment= line should slip through for these two keys.
    assert "Environment=AGENT_ENGINE_ROOT=" not in content
    assert "Environment=AGENT_VAULT_DATA=" not in content


# ── creds_health() resilience to a malformed alert conf (beta-readiness
# review, 2026-07-13) ────────────────────────────────────────────────────
# _ensure_alert_creds() and _send_healthcheck() were both individually
# wrapped in try/except inside creds_health(), but _load_env_conf() sat
# bare between them: a non-UTF-8 91-telegram-alert.conf (a stray binary
# write, a bad manual edit) raised UnicodeDecodeError uncaught, which
# skipped _send_healthcheck entirely -- the one step in this function whose
# whole job is telling the user something is wrong.

def test_creds_health_survives_a_non_utf8_alert_conf_and_still_runs_healthcheck(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    conf_dir = sandbox.home / ".config" / "environment.d"
    conf_dir.mkdir(parents=True, exist_ok=True)
    (conf_dir / "91-telegram-alert.conf").write_bytes(b"\xff\xfe\x00garbage-not-utf8")

    healthcheck_called = []
    monkeypatch.setattr(mod, "_send_healthcheck", lambda _env: healthcheck_called.append(True))
    env = mod.Env()

    mod.creds_health(env, do_creds=False, do_health=True)  # must not raise

    assert healthcheck_called, "_load_env_conf failing must not skip _send_healthcheck"


# ── LINKED_COMMANDS single source (2026-07-13 follow-up) ──────────────────
# utils()'s POSIX and Windows branches used to carry their own hardcoded
# link lists. vault-push moved from "POSIX only, hand-listed" to "cross-
# platform, driven by the same LINKED_COMMANDS dict both branches read" --
# these two tests are the direct regression net for that move.

@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink launcher behavior is covered on Linux and macOS.")
def test_posix_utils_links_vault_push_launcher(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    mod.utils(env)

    launcher = sandbox.home / ".local" / "bin" / "vault-push"
    assert launcher.is_symlink()
    assert launcher.resolve() == (sandbox.scripts_dir / "vault-push.sh").resolve()


def test_windows_utils_installs_vault_push_command_wrapper(sandbox, monkeypatch):
    # vault-push used to have no .ps1 twin at all on Windows -- LINKED_COMMANDS
    # now declares it windows=True and utils()'s Windows branch, driven by
    # that same dict, links it exactly like council/vault-groom.
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    shutil.copy2(REAL_SCRIPTS / "vault-push.ps1", sandbox.scripts_dir / "vault-push.ps1")
    monkeypatch.setitem(sys.modules, "winreg", _make_fake_winreg())

    env = mod.Env()
    mod.utils(env)

    launcher = sandbox.home / ".local" / "bin" / "vault-push.ps1"
    wrapper = sandbox.home / ".local" / "bin" / "vault-push.cmd"
    assert launcher.exists()
    assert not launcher.is_symlink()
    assert str(sandbox.scripts_dir / "vault-push.ps1") in launcher.read_text(encoding="utf-8")
    assert "vault-push.ps1" in wrapper.read_text(encoding="utf-8")


def test_windows_utils_sources_engine_owned_vault_commands_after_split(sandbox, monkeypatch, tmp_path):
    """After the engine/data cutover the Vault no longer owns runtime code.
    Launchers aimed at stale Vault copies either lose their sibling engine or
    silently execute a pre-cutover implementation instead of the pinned one."""
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    monkeypatch.setitem(sys.modules, "winreg", _make_fake_winreg())

    engine_root = tmp_path / "consumer-engine" / "03-INFRA"
    engine_scripts = engine_root / "scripts"
    engine_scripts.mkdir(parents=True)
    for name in ("vault-push.ps1", "vault-groom.ps1", "vault_groom_audit.py", "agent_sync.py"):
        shutil.copy2(REAL_SCRIPTS / name, engine_scripts / name)
    monkeypatch.setenv("AGENT_ENGINE_ROOT", str(engine_root))

    env = mod.Env()
    mod.utils(env)

    for command in ("vault-push", "vault-groom"):
        launcher = sandbox.home / ".local" / "bin" / f"{command}.ps1"
        launcher_text = launcher.read_text(encoding="utf-8")
        assert str(engine_scripts / f"{command}.ps1") in launcher_text
        assert str(sandbox.scripts_dir / f"{command}.ps1") not in launcher_text


# ── _run_external timeout primitive (2026-07-13 follow-up) ────────────────
# Mirrors _run_python_script's own TimeoutExpired-swallowing test above:
# mklink/pgrep/tasklist/systemctl/schtasks.exe/notify-send now all route
# through this, so a single test proves the shared behavior instead of one
# per call site.

def test_run_external_times_out_instead_of_hanging(sandbox):
    mod = load_agent_sync_module(sandbox)
    hang_script = sandbox.home / "hang.py"
    hang_script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    result = mod._run_external([sys.executable, str(hang_script)], timeout=1, capture_output=True, text=True)

    assert result.returncode != 0
    assert "timed out after 1s" in result.stderr


def test_run_external_returns_real_output_on_success(sandbox):
    mod = load_agent_sync_module(sandbox)
    ok_script = sandbox.home / "ok.py"
    ok_script.write_text("print('hello')\n", encoding="utf-8")

    result = mod._run_external([sys.executable, str(ok_script)], timeout=10, capture_output=True, text=True)

    assert result.returncode == 0
    assert "hello" in result.stdout


# ── Windows User PATH (release-critical, 2026-07-13 follow-up) ────────────
# utils() writes command wrappers into env.local_bin, but nothing ever put
# that folder on the Windows User PATH: every wrapper was reachable only by
# full path forever, on every fresh install, until a human added it by
# hand. winreg is monkeypatched into sys.modules (a fake, in-memory
# HKCU\Environment) so this is POSIX-runnable; real winreg only exists on
# Windows, and _ensure_user_path_entry's own `import winreg` picks up
# whatever is in sys.modules under that name.

def _make_fake_winreg(initial_path: str = ""):
    """Minimal in-memory stand-in for the winreg module surface
    _ensure_user_path_entry actually calls: HKEY_CURRENT_USER is an opaque
    sentinel (never dereferenced), OpenKey returns a context-manager key
    object, QueryValueEx/SetValueEx read/write a single in-memory dict --
    enough to exercise the real append/no-op/preserve logic under test
    without touching a real registry (which does not exist on this
    machine)."""
    state = {}
    if initial_path:
        state["Path"] = (initial_path, 2)  # 2 == REG_EXPAND_SZ, matches below

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        KEY_READ=1,
        KEY_WRITE=2,
        REG_SZ=1,
        REG_EXPAND_SZ=2,
        OpenKey=lambda hive, subkey, reserved=0, access=0: _Key(),
        QueryValueEx=lambda key, name: state[name] if name in state else (_ for _ in ()).throw(FileNotFoundError(name)),
        SetValueEx=lambda key, name, reserved, kind, value: state.__setitem__(name, (value, kind)),
        _state=state,
    )
    return fake


def _enable_host_mutations(monkeypatch):
    """Registry unit tests opt in explicitly; the sandbox default is no-op."""
    monkeypatch.delenv("NEXGEN_DISABLE_HOST_MUTATIONS", raising=False)
    monkeypatch.setenv("PATH", r"C:\Windows;C:\Windows\System32")


def test_windows_test_boundary_blocks_registry_and_scheduler(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("NEXGEN_DISABLE_HOST_MUTATIONS", "1")
    fake_winreg = _make_fake_winreg(r"C:\Windows")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(
        mod,
        "_run_external",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("host command must not run")),
    )
    env = mod.Env()

    mod._ensure_user_path_entry(env)
    assert mod._install_scheduled_task(env) is True

    assert fake_winreg._state["Path"][0] == r"C:\Windows"
    assert not (env.log_dir / "start-agent-sync-hidden.vbs").exists()
    log = env.log_path.read_text(encoding="utf-8")
    assert "user PATH registry update skipped" in log
    assert "Task Scheduler update skipped" in log


def test_windows_user_path_appends_when_missing(sandbox, monkeypatch):
    _enable_host_mutations(monkeypatch)
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    fake_winreg = _make_fake_winreg(r"C:\Windows;C:\Windows\System32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    env = mod.Env()

    mod._ensure_user_path_entry(env)

    value, _kind = fake_winreg._state["Path"]
    entries = value.split(";")
    assert str(env.local_bin) in entries
    assert r"C:\Windows" in entries
    assert r"C:\Windows\System32" in entries
    assert f"added {env.local_bin}" in env.log_path.read_text(encoding="utf-8")


def test_windows_user_path_noop_when_already_present_case_insensitive(sandbox, monkeypatch):
    _enable_host_mutations(monkeypatch)
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    env_probe = mod.Env()
    # Same folder, different case AND a trailing backslash -- both must be
    # tolerated by the idempotent check, not just an exact string match.
    existing = f"C:\\Windows;{str(env_probe.local_bin).upper()}\\"
    fake_winreg = _make_fake_winreg(existing)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    env = mod.Env()

    mod._ensure_user_path_entry(env)

    value, _kind = fake_winreg._state["Path"]
    assert value == existing, "an already-present entry must not be rewritten"
    assert f"{env.local_bin} already on user PATH" in env.log_path.read_text(encoding="utf-8")


def test_windows_user_path_preserves_existing_entries(sandbox, monkeypatch):
    _enable_host_mutations(monkeypatch)
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    # C:\tools\bin, deliberately not a Windows-home-shaped path: the
    # leak-scan gate blocks that shape in the public tree, synthetic or not.
    fake_winreg = _make_fake_winreg(r"C:\Windows;C:\Program Files\Git\cmd;C:\tools\bin")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    env = mod.Env()

    mod._ensure_user_path_entry(env)

    entries = fake_winreg._state["Path"][0].split(";")
    assert r"C:\Windows" in entries
    assert r"C:\Program Files\Git\cmd" in entries
    assert r"C:\tools\bin" in entries
    assert str(env.local_bin) in entries


def test_windows_user_path_creates_value_when_entirely_absent(sandbox, monkeypatch):
    """Plausible first-run state on a fresh Windows account: the
    HKCU\\Environment key exists (it always does) but has never had a
    "Path" value written to it at all -- distinct from the "already
    present"/"append to existing entries" cases above, and distinct from
    the OpenKey-itself-fails case below. QueryValueEx raising
    FileNotFoundError is _make_fake_winreg's own default (initial_path=""
    means no "Path" key seeded into its in-memory state at all)."""
    _enable_host_mutations(monkeypatch)
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    fake_winreg = _make_fake_winreg()
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    env = mod.Env()

    mod._ensure_user_path_entry(env)

    value, kind = fake_winreg._state["Path"]
    assert value == str(env.local_bin), "with no prior value, the new Path value must be exactly local_bin, nothing else"
    assert kind == fake_winreg.REG_EXPAND_SZ
    assert f"added {env.local_bin}" in env.log_path.read_text(encoding="utf-8")


def test_windows_user_path_registry_failure_is_logged_not_raised(sandbox, monkeypatch):
    """A registry failure must not crash utils() or flip the phase to
    failed -- see utils()'s own call-site comment. A future doctor check
    surfaces a still-missing PATH entry; this phase's job (writing the
    wrappers) is already done by the time this runs."""
    _enable_host_mutations(monkeypatch)
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    def raise_open_key(*_args, **_kwargs):
        raise OSError("registry unavailable in this sandbox")

    fake_winreg = _make_fake_winreg()
    fake_winreg.OpenKey = raise_open_key
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    env = mod.Env()

    mod._ensure_user_path_entry(env)  # must not raise

    assert "WARNING" in env.log_path.read_text(encoding="utf-8")


def test_windows_user_path_refuses_to_cross_cmd_limit(sandbox, monkeypatch):
    _enable_host_mutations(monkeypatch)
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    fake_winreg = _make_fake_winreg("X" * mod.WINDOWS_CMD_ENV_LIMIT)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    env = mod.Env()

    mod._ensure_user_path_entry(env)

    assert fake_winreg._state["Path"][0] == "X" * mod.WINDOWS_CMD_ENV_LIMIT
    log = env.log_path.read_text(encoding="utf-8")
    assert "refusing to append" in log
    assert str(mod.WINDOWS_CMD_ENV_LIMIT) in log


def test_windows_user_path_accounts_for_the_combined_process_path(sandbox, monkeypatch):
    _enable_host_mutations(monkeypatch)
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("PATH", "X" * (mod.WINDOWS_CMD_ENV_LIMIT - 4))
    fake_winreg = _make_fake_winreg(r"C:\tools")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    env = mod.Env()

    mod._ensure_user_path_entry(env)

    assert fake_winreg._state["Path"][0] == r"C:\tools"
    log = env.log_path.read_text(encoding="utf-8")
    assert "projected process PATH" in log
    assert "refusing to append" in log


def test_windows_scheduler_wrapper_lives_in_runtime_state_and_reenters_split_topology(sandbox, monkeypatch):
    _enable_host_mutations(monkeypatch)
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mod, "_run_external", fake_run)
    env = mod.Env()

    assert mod._install_scheduled_task(env) is True

    wrapper = env.log_dir / "start-agent-sync-hidden.vbs"
    assert wrapper.is_file()
    assert not (env.engine_scripts / "start-agent-sync-hidden.vbs").exists()
    content = wrapper.read_text(encoding="utf-8")
    assert str(env.engine_root) in content
    assert str(env.vault_data) in content
    assert str(env.vault) in content
    assert env.branch in content
    assert calls and all(str(wrapper) in " ".join(call) for call in calls)


@pytest.mark.skipif(os.name != "nt", reason="Real HKCU invariant is Windows-only.")
def test_windows_guard_sandbox_leaves_real_user_path_unchanged(sandbox):
    import winreg

    def task_state(name):
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", name, "/XML"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.returncode, result.stdout

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        before = winreg.QueryValueEx(key, "Path")
    task_names = ("KnowledgeVault Agent Sync", "KnowledgeVault Agent Sync Logon")
    tasks_before = {name: task_state(name) for name in task_names}
    result = run_agent_sync_python(sandbox, "guard")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        after = winreg.QueryValueEx(key, "Path")
    tasks_after = {name: task_state(name) for name in task_names}

    assert result.returncode == 0, result.stdout + result.stderr
    assert after == before
    assert tasks_after == tasks_before


# ── vault-push subcommand wiring (2026-07-13) ──────────────────────────────
# Full behavioral coverage (commit/push/lock/local-only/usage-error paths)
# lives in test_vault_push_python.py; this is just proof that main() itself
# actually dispatches to it and documents it, the same class of gap the
# LINKED_COMMANDS tests above guard against for utils().

def test_help_text_documents_vault_push_subcommand(sandbox):
    proc = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), "--help"],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "vault-push" in proc.stdout


def test_main_dispatches_vault_push_before_mode_validation(sandbox, monkeypatch):
    """vault-push is not a MODES entry -- it must be special-cased in main()
    the same way 'config' already is, dispatched before the mode/extras
    validation that would otherwise reject it as an unknown mode."""
    mod = load_agent_sync_module(sandbox)
    called = []
    monkeypatch.setattr(mod, "_vault_push_cli", lambda argv: called.append(argv) or 0)

    rc = mod.main(["vault-push", "-m", "msg", "file.txt"])

    assert rc == 0
    assert called == [["-m", "msg", "file.txt"]]
