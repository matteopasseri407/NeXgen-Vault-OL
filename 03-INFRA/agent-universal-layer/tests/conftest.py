"""Shared infrastructure for the B1 tests (engine fixtures).

Every test runs inside a temporary sandbox HOME (mktemp), NEVER against the
real HOME. The runner refuses to proceed if the sentinel file is missing:
that's the guarantee that no assertion in these tests can ever read/write
outside the sandbox, even if a future test forgets to pass the right env.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR / "fixtures"
REAL_UL = TESTS_DIR.parent                      # .../agent-universal-layer (real source)
REAL_VAULT = REAL_UL.parent.parent               # KnowledgeVault (real root)
REAL_SCRIPTS = REAL_VAULT / "03-INFRA" / "scripts"

SENTINEL_NAME = ".b1-sandbox-sentinel"


@dataclass
class Sandbox:
    home: Path

    @property
    def vault(self) -> Path:
        return self.home / "KnowledgeVault"

    @property
    def ul(self) -> Path:
        return self.vault / "03-INFRA" / "agent-universal-layer"

    @property
    def scripts_dir(self) -> Path:
        return self.vault / "03-INFRA" / "scripts"

    @property
    def mcp_dir(self) -> Path:
        return self.ul / "mcp"

    @property
    def skills_dir(self) -> Path:
        return self.ul / "skills"

    @property
    def active_skills(self) -> Path:
        return self.home / ".agents" / "skills"

    @property
    def skill_library(self) -> Path:
        return self.home / ".agents" / "skill-library"

    @property
    def bin_stubs(self) -> Path:
        return self.home / "bin-stubs"

    def live_config_path(self, cli: str) -> Path:
        opencode = self.home / ".config" / "opencode" / "opencode.json"
        return {
            "claude": self.home / ".claude.json",
            "codex": self.home / ".codex" / "config.toml",
            "opencode": opencode,
            "antigravity": self.home / ".gemini" / "antigravity" / "mcp_config.json",
        }[cli]

    def assert_is_sandbox(self) -> None:
        sentinel = self.home / SENTINEL_NAME
        if not sentinel.is_file():
            raise RuntimeError(
                f"REFUSED: {self.home} is not a test sandbox (missing {SENTINEL_NAME}). "
                "NEVER run real scripts against a HOME that isn't verifiably a sandbox."
            )

    def env(self, **extra) -> dict:
        self.assert_is_sandbox()
        e = dict(os.environ)
        e["HOME"] = str(self.home)
        e["USERPROFILE"] = str(self.home)
        e["KNOWLEDGE_VAULT_PATH"] = str(self.vault)
        if os.name == "nt":
            e["APPDATA"] = str(self.home / "AppData" / "Roaming")
            e["LOCALAPPDATA"] = str(self.home / "AppData" / "Local")
        # Most provisioning tests exercise local mutations, not Git transport.
        # Keep their pull state explicitly healthy and offline from real remotes.
        # Tests for remote failures override this value deliberately.
        e["KNOWLEDGE_VAULT_REMOTE"] = "local"
        # HOME/USERPROFILE redirects files but not HKCU or Task Scheduler.
        # Full guard/apply subprocess tests must never mutate the host that is
        # running pytest, especially a maintainer's physical Windows machine.
        e["NEXGEN_DISABLE_HOST_MUTATIONS"] = "1"
        e["PATH"] = f"{self.bin_stubs}{os.pathsep}{e.get('PATH', '')}"
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "VAULT_ALERT_WEBHOOK",
            "N8N_TELEGRAM_CRED_ID",
            "REMOTE_ALIAS",
        ):
            e.pop(key, None)
        e.update(extra)
        return e

    def tree_snapshot(self, *, exclude_names: frozenset[str] = frozenset()) -> dict:
        """Hash of every file/symlink under HOME, for idempotence comparisons.
        Excludes by name (e.g. the log file, which always changes) not by
        path, so the exclusion stays valid regardless of the sub-directory."""
        out = {}
        for p in sorted(self.home.rglob("*")):
            if p.name in exclude_names:
                continue
            rel = str(p.relative_to(self.home))
            if p.is_symlink():
                out[rel] = ("symlink", os.readlink(p))
            elif p.is_file():
                out[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())
            elif p.is_dir():
                out[rel] = ("dir", None)
        return out


def _make_bin_stubs(sandbox: Sandbox) -> None:
    """Neutralizes systemctl and notify-send for the tests:
    - systemctl: to avoid daemon-reload hitting the REAL systemd
    - notify-send: to avoid real desktop notifications when the
      _send_healthcheck step (agent_sync.py) runs in the sandbox, fails
      (expected) and tries to alert."""
    sandbox.bin_stubs.mkdir(parents=True, exist_ok=True)
    for cmd in ("systemctl", "notify-send"):
        stub = sandbox.bin_stubs / cmd
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)


def _copy_engine_scripts(sandbox: Sandbox) -> None:
    sandbox.mcp_dir.mkdir(parents=True, exist_ok=True)
    sandbox.scripts_dir.mkdir(parents=True, exist_ok=True)
    sandbox.skills_dir.mkdir(parents=True, exist_ok=True)
    (sandbox.ul / "instructions").mkdir(parents=True, exist_ok=True)
    (sandbox.ul / "hooks").mkdir(parents=True, exist_ok=True)

    shutil.copy2(REAL_UL / "mcp" / "render.py", sandbox.mcp_dir / "render.py")
    for name in (
        "agent-sync.sh", "agent-sync.ps1", "agent_sync.py", "agent-skill.py", "skills-sync.py", "config_schema.py",
        "check_required_rules.py",
        "agent-doctor.sh", "agent-doctor.ps1",
        "council.sh", "council.ps1", "vault-push.sh", "vault-push.ps1", "vault-groom.sh", "vault-groom.ps1",
        "vault_groom_audit.py", "agent-now.sh", "agent-now.ps1", "firecrawl-local.sh", "firecrawl-local.ps1",
    ):
        dst = sandbox.scripts_dir / name
        shutil.copy2(REAL_SCRIPTS / name, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IEXEC)

    shutil.copy2(FIXTURES / "manifest.yaml", sandbox.mcp_dir / "manifest.yaml")
    shutil.copy2(FIXTURES / "AGENTS.md", sandbox.ul / "instructions" / "AGENTS.md")
    shutil.copy2(FIXTURES / "claude-vault-checkpoint.mjs", sandbox.ul / "hooks" / "claude-vault-checkpoint.mjs")
    shutil.copy2(FIXTURES / "skills.manifest.yaml", sandbox.skills_dir / "skills.manifest.yaml")
    for skill_dir in (FIXTURES / "skills").iterdir():
        if skill_dir.is_dir():
            shutil.copytree(skill_dir, sandbox.skills_dir / skill_dir.name)


def _install_live_configs(sandbox: Sandbox) -> None:
    dests = {
        "claude": FIXTURES / "live-configs" / "claude.json",
        "codex": FIXTURES / "live-configs" / "codex-config.toml",
        "opencode": FIXTURES / "live-configs" / "opencode.json",
        "antigravity": FIXTURES / "live-configs" / "antigravity-mcp_config.json",
    }
    for cli, src in dests.items():
        dst = sandbox.live_config_path(cli)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


@pytest.fixture
def sandbox(tmp_path, monkeypatch) -> Sandbox:
    """'Bare' sandbox: engine + fixtures copied, NO live-config, NO
    pre-created runtime skill. Individual tests add only what they need."""
    home = tmp_path / "home"
    home.mkdir()
    (home / SENTINEL_NAME).write_text(
        "B1 test sandbox — if you're seeing this file in a real HOME, something went wrong.\n"
    )
    sb = Sandbox(home)
    _copy_engine_scripts(sb)
    _make_bin_stubs(sb)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
        monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sb.vault))
    monkeypatch.setenv("NEXGEN_DISABLE_HOST_MUTATIONS", "1")
    return sb


@pytest.fixture
def sandbox_with_live_configs(sandbox) -> Sandbox:
    """Sandbox + the 4 synthetic live configs in the 4 real styles, with
    deliberate drift (missing server, different arg, extra env, extra server
    outside the manifest) — used by render.py's tests and the agent-sync
    integration tests."""
    _install_live_configs(sandbox)
    return sandbox


def load_render_module(sandbox: Sandbox):
    """Imports render.py as a fresh Python module (one module copy per
    test, so one test's monkeypatches don't contaminate another's) and
    points it at the sandbox: HOME and MANIFEST become the sandbox's own."""
    spec = importlib.util.spec_from_file_location(
        f"render_under_test_{id(sandbox)}", sandbox.mcp_dir / "render.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HOME = sandbox.home
    mod.MANIFEST = sandbox.mcp_dir / "manifest.yaml"
    return mod


def load_skills_sync_module(sandbox: Sandbox):
    """Same for skills-sync.py: HOME, LIBRARY, RUNTIME, UL, MANIFEST pointed at
    the sandbox (skills-sync derives these paths from HOME and __file__,
    not from env)."""
    spec = importlib.util.spec_from_file_location(
        f"skills_sync_under_test_{id(sandbox)}", sandbox.scripts_dir / "skills-sync.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HOME = sandbox.home
    mod.UL = sandbox.ul
    mod.MANIFEST = sandbox.skills_dir / "skills.manifest.yaml"
    mod.LIBRARY = sandbox.skill_library
    mod.ACTIVE = sandbox.active_skills
    mod.LEGACY = sandbox.skill_library / "legacy"
    mod.RUNTIME = {
        "claude": sandbox.home / ".claude" / "skills",
        "codex": sandbox.home / ".codex" / "skills",
        "antigravity": sandbox.home / ".gemini" / "antigravity-cli" / "skills",
    }
    return mod


def load_agent_sync_module(sandbox: Sandbox):
    spec = importlib.util.spec_from_file_location(
        f"agent_sync_under_test_{id(sandbox)}", sandbox.scripts_dir / "agent_sync.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # Dataclasses with postponed annotations resolve their module through
    # sys.modules while the class decorator runs. A normal import registers
    # this automatically; the fixture's manual import must do the same.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def run_agent_sync(sandbox: Sandbox, mode: str = "apply", timeout: int = 60) -> subprocess.CompletedProcess:
    sandbox.assert_is_sandbox()
    if os.name == "nt":
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(sandbox.scripts_dir / "agent-sync.ps1"), mode],
            env=sandbox.env(), capture_output=True, text=True, timeout=timeout,
        )
    return subprocess.run(
        ["bash", str(sandbox.scripts_dir / "agent-sync.sh"), mode],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_agent_sync_python(sandbox: Sandbox, mode: str = "apply", timeout: int = 60) -> subprocess.CompletedProcess:
    sandbox.assert_is_sandbox()
    return subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent_sync.py"), mode],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def run_agent_doctor(sandbox: Sandbox, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    sandbox.assert_is_sandbox()
    if os.name == "nt":
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(sandbox.scripts_dir / "agent-doctor.ps1"), *args],
            env=sandbox.env(), capture_output=True, text=True, timeout=timeout,
        )
    return subprocess.run(
        ["bash", str(sandbox.scripts_dir / "agent-doctor.sh"), *args],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
