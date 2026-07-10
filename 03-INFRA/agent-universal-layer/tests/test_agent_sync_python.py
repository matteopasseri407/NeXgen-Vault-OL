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

from conftest import load_agent_sync_module, run_agent_sync_python


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


def test_windows_junction_runtime_is_converted_without_touching_hub(sandbox, monkeypatch):
    """A runtime directory can be a Junction to the complete skill hub.
    It must become a real runtime directory before per-skill links are made,
    without deleting any source data in the hub."""
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    source = env.agents_hub / "fake-skill-a"
    source.mkdir(parents=True)
    source_file = source / "SKILL.md"
    source_file.write_text("canonical source\n", encoding="utf-8")
    source_bytes = source_file.read_bytes()

    runtime = sandbox.home / ".claude" / "skills"
    runtime.mkdir(parents=True)

    real_resolve = Path.resolve

    def simulate_junction_resolve(self, *args, **kwargs):
        if self == runtime:
            return real_resolve(env.agents_hub)
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_junction", lambda self: self == runtime, raising=False)
    monkeypatch.setattr(Path, "resolve", simulate_junction_resolve)

    removed = []

    def remove_simulated_junction(path):
        removed.append(path)
        shutil.rmtree(path)

    created = []

    def make_test_link(src, dst, *, is_dir):
        created.append((src, dst, is_dir))
        dst.symlink_to(src, target_is_directory=is_dir)
        return True

    monkeypatch.setattr(mod, "_remove_path", remove_simulated_junction)
    monkeypatch.setattr(mod, "make_link", make_test_link)

    mod.runtimes(env)

    assert removed == [runtime]
    assert source_file.read_bytes() == source_bytes
    assert runtime.is_dir() and not runtime.is_symlink()
    assert (runtime / source.name).is_symlink()
    assert any(dst == runtime / source.name for _src, dst, _is_dir in created)


def test_windows_junction_skill_is_not_relinked(sandbox, monkeypatch):
    """A per-skill Junction already targeting its hub source is correct.
    Treating it as a normal directory calls rmtree on a Junction and aborts
    the Windows sync, so it must be left in place."""
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    env = mod.Env()
    source = env.agents_hub / "fake-skill-a"
    source.mkdir(parents=True)
    source_file = source / "SKILL.md"
    source_file.write_text("canonical source\n", encoding="utf-8")
    source_bytes = source_file.read_bytes()

    runtime_links = []
    for rel in (".claude/skills", ".codex/skills"):
        link = sandbox.home / rel / source.name
        link.mkdir(parents=True)
        (link / "SKILL.md").write_text("simulated junction target\n", encoding="utf-8")
        runtime_links.append(link)

    original_points_to = getattr(mod, "_points_to", None)

    def simulate_junction_target(path, target):
        if path in runtime_links and target == source:
            return True
        return original_points_to(path, target) if original_points_to else False

    created = []

    def make_test_link(src, dst, *, is_dir):
        created.append((src, dst, is_dir))
        return True

    monkeypatch.setattr(mod, "_points_to", simulate_junction_target, raising=False)
    monkeypatch.setattr(mod, "make_link", make_test_link)

    mod.runtimes(env)

    assert source_file.read_bytes() == source_bytes
    assert not any(dst in runtime_links for _src, dst, _is_dir in created)


def test_windows_excluded_copy_fallback_skill_is_removed(sandbox, monkeypatch):
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))

    hub_skill = sandbox.hub / "fake-skill-excluded"
    hub_skill.mkdir(parents=True, exist_ok=True)
    (hub_skill / "SKILL.md").write_text("canonical excluded skill\n", encoding="utf-8")
    for runtime in (sandbox.home / ".claude" / "skills", sandbox.home / ".codex" / "skills"):
        copied_skill = runtime / "fake-skill-excluded"
        copied_skill.mkdir(parents=True, exist_ok=True)
        (copied_skill / "SKILL.md").write_text("stale copy\n", encoding="utf-8")

    env = mod.Env()
    mod.runtimes(env)

    assert not (sandbox.home / ".claude" / "skills" / "fake-skill-excluded").exists()
    assert not (sandbox.home / ".codex" / "skills" / "fake-skill-excluded").exists()
    assert (sandbox.hub / "fake-skill-excluded" / "SKILL.md").is_file()


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
