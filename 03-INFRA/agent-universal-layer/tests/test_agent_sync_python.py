"""Cross-platform smoke for the B2.5 unified provisioner.

The older POSIX regression tests still exercise `agent-sync.sh` as the public
interface on Ubuntu. This one calls `agent_sync.py` directly so Windows CI can
prove the shared implementation runs in a sandboxed USERPROFILE.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

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
