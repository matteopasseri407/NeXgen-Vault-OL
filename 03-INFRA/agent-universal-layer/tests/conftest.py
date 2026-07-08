"""Infrastruttura condivisa dei test B1 (fixtures del motore).

Ogni test gira dentro una HOME sandbox temporanea (mktemp), MAI contro la HOME
reale. Il runner si rifiuta di procedere se manca il file sentinella: è la
garanzia che nessun assert di questi test possa mai leggere/scrivere fuori
dalla sandbox, anche se un test futuro dimentica di passare l'ambiente giusto.
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
REAL_UL = TESTS_DIR.parent                      # .../agent-universal-layer (sorgente vera)
REAL_VAULT = REAL_UL.parent.parent               # KnowledgeVault (root vero)
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
    def hub(self) -> Path:
        return self.home / ".agents" / "skills"

    @property
    def bin_stubs(self) -> Path:
        return self.home / "bin-stubs"

    def live_config_path(self, cli: str) -> Path:
        return {
            "claude": self.home / ".claude.json",
            "codex": self.home / ".codex" / "config.toml",
            "opencode": self.home / ".config" / "opencode" / "opencode.json",
            "antigravity": self.home / ".gemini" / "antigravity" / "mcp_config.json",
        }[cli]

    def assert_is_sandbox(self) -> None:
        sentinel = self.home / SENTINEL_NAME
        if not sentinel.is_file():
            raise RuntimeError(
                f"RIFIUTO: {self.home} non e' una sandbox di test (manca {SENTINEL_NAME}). "
                "Non eseguo MAI script reali contro una HOME che non sia sicuramente sandbox."
            )

    def env(self, **extra) -> dict:
        self.assert_is_sandbox()
        e = dict(os.environ)
        e["HOME"] = str(self.home)
        e["USERPROFILE"] = str(self.home)
        e["KNOWLEDGE_VAULT_PATH"] = str(self.vault)
        e["KNOWLEDGE_VAULT_REMOTE"] = "origin-non-esiste-in-sandbox"
        e["PATH"] = f"{self.bin_stubs}:{e.get('PATH', '')}"
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
        """Hash di ogni file/symlink sotto HOME, per confronti di idempotenza.
        Esclude per nome (es. il file di log, che cambia sempre) non per path,
        cosi' l'esclusione resta valida a prescindere dalla sub-directory."""
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


def _make_systemctl_stub(sandbox: Sandbox) -> None:
    """Neutra systemctl per i test: agent-sync.sh chiama 'systemctl --user
    daemon-reload' se aggiorna le unit. Le unit finiscono comunque dentro la
    sandbox (HOME finto), ma daemon-reload parlerebbe al systemd --user VERO
    della macchina: lo intercettiamo con uno stub no-op, cosi' zero effetti
    fuori sandbox, lettera E spirito del criterio di accettazione B1."""
    sandbox.bin_stubs.mkdir(parents=True, exist_ok=True)
    stub = sandbox.bin_stubs / "systemctl"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)


def _copy_engine_scripts(sandbox: Sandbox) -> None:
    sandbox.mcp_dir.mkdir(parents=True, exist_ok=True)
    sandbox.scripts_dir.mkdir(parents=True, exist_ok=True)
    sandbox.skills_dir.mkdir(parents=True, exist_ok=True)
    (sandbox.ul / "instructions").mkdir(parents=True, exist_ok=True)
    (sandbox.ul / "hooks").mkdir(parents=True, exist_ok=True)

    shutil.copy2(REAL_UL / "mcp" / "render.py", sandbox.mcp_dir / "render.py")
    for name in ("agent-sync.sh", "agent_sync.py", "skills-sync.py", "agent-doctor.sh"):
        dst = sandbox.scripts_dir / name
        shutil.copy2(REAL_SCRIPTS / name, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IEXEC)

    shutil.copy2(FIXTURES / "manifest.yaml", sandbox.mcp_dir / "manifest.yaml")
    shutil.copy2(FIXTURES / "AGENTS.md", sandbox.ul / "instructions" / "AGENTS.md")
    shutil.copy2(FIXTURES / "skills-exclude-claude.txt", sandbox.ul / "skills-exclude-claude.txt")
    shutil.copy2(FIXTURES / "skills-exclude-codex.txt", sandbox.ul / "skills-exclude-codex.txt")
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
    """Sandbox 'nuda': motore + fixture copiati, NESSUna live-config, NESSUN
    runtime skill pre-creato. I singoli test aggiungono solo cio' che serve."""
    home = tmp_path / "home"
    home.mkdir()
    (home / SENTINEL_NAME).write_text(
        "Sandbox di test B1 — se vedi questo file in una HOME vera, qualcosa e' andato storto.\n"
    )
    sb = Sandbox(home)
    _copy_engine_scripts(sb)
    _make_systemctl_stub(sb)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sb.vault))
    return sb


@pytest.fixture
def sandbox_with_live_configs(sandbox) -> Sandbox:
    """Sandbox + le 4 config live sintetiche nei 4 stili reali, con drift
    deliberato (server mancante, arg diverso, env superflua, server extra fuori
    manifest) — usata dai test di render.py e dall'integrazione agent-sync."""
    _install_live_configs(sandbox)
    return sandbox


def load_render_module(sandbox: Sandbox):
    """Importa render.py come modulo Python fresco (una copia di modulo per
    test, cosi' i monkeypatch di uno non contaminano gli altri) e lo punta
    alla sandbox: HOME e MANIFEST diventano quelli della sandbox."""
    spec = importlib.util.spec_from_file_location(
        f"render_under_test_{id(sandbox)}", sandbox.mcp_dir / "render.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HOME = sandbox.home
    mod.MANIFEST = sandbox.mcp_dir / "manifest.yaml"
    return mod


def load_skills_sync_module(sandbox: Sandbox):
    """Idem per skills-sync.py: HOME, HUB, RUNTIME, UL, MANIFEST puntati alla
    sandbox (skills-sync deriva questi path da HOME e da __file__, non da env)."""
    spec = importlib.util.spec_from_file_location(
        f"skills_sync_under_test_{id(sandbox)}", sandbox.scripts_dir / "skills-sync.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.HOME = sandbox.home
    mod.UL = sandbox.ul
    mod.MANIFEST = sandbox.skills_dir / "skills.manifest.yaml"
    mod.HUB = sandbox.hub
    mod.RUNTIME = {
        "claude": sandbox.home / ".claude" / "skills",
        "codex": sandbox.home / ".codex" / "skills",
    }
    return mod


def load_agent_sync_module(sandbox: Sandbox):
    spec = importlib.util.spec_from_file_location(
        f"agent_sync_under_test_{id(sandbox)}", sandbox.scripts_dir / "agent_sync.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_agent_sync(sandbox: Sandbox, mode: str = "apply", timeout: int = 60) -> subprocess.CompletedProcess:
    sandbox.assert_is_sandbox()
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
    return subprocess.run(
        ["bash", str(sandbox.scripts_dir / "agent-doctor.sh"), *args],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
