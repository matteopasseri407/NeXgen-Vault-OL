"""Cross-platform smoke for the B2.5 unified provisioner.

The older POSIX regression tests still exercise `agent-sync.sh` as the public
interface on Ubuntu. This one calls `agent_sync.py` directly so Windows CI can
prove the shared implementation runs in a sandboxed USERPROFILE.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import load_agent_sync_module, run_agent_sync_python


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
    assert launcher.resolve() == (sandbox.scripts_dir / "council.ps1").resolve()
    assert 'council.ps1' in wrapper.read_text(encoding="utf-8")
    skill_wrapper = sandbox.home / ".local" / "bin" / "agent-skill.cmd"
    assert skill_wrapper.exists()
    assert "agent-skill.py" in skill_wrapper.read_text(encoding="utf-8")


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
    (sandbox.scripts_dir / "local-model-agent.ps1").write_text("param()\n", encoding="utf-8")

    env = mod.Env()
    mod.instructions(env)
    mod.local_model_runtime(env)

    assert (sandbox.home / "GEMMA.md").exists()
    assert (sandbox.home / "LOCAL-WORKER.md").exists()
    assert (sandbox.home / ".local" / "bin" / "local-model-agent.ps1").exists()
    for name in ("local-worker.ps1", "local-agent.ps1", "gemma-worker.ps1", "gemma-agent.ps1"):
        text = (sandbox.home / ".local" / "bin" / name).read_text(encoding="utf-8")
        assert "local-model-agent.ps1" in text


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
