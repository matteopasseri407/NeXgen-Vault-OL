"""Regression tests — NX-10: MCP filesystem/memory trust-plane scope.

Before this fix, the distributed manifest mounted the filesystem MCP server
on the user's entire home (a bare ``${HOME}`` argument) and mounted a
``memory`` MCP server unconditionally, creating a second, non-authoritative
memory channel outside the KnowledgeVault (single source of truth for memory
in this layer). These tests pin:

  - the distributed filesystem server ships explicit, non-``${HOME}`` roots
  - the manifest schema structurally accepts multiple explicit roots
  - the memory server is opt-in (``require_env``): absent by default, present
    only once the user sets the opt-in variable, all the way through to a
    rendered CLI dialect (Codex).
"""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import yaml

from conftest import load_render_module

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "03-INFRA" / "agent-universal-layer" / "mcp" / "manifest.yaml"
REAL_CONFIG_SCHEMA = REPO / "03-INFRA" / "scripts" / "config_schema.py"


def _load_real_config_schema():
    spec = importlib.util.spec_from_file_location("real_config_schema_under_test", REAL_CONFIG_SCHEMA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _real_servers() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["servers"]


# ---- the distributed template no longer mounts the whole home -------------

def test_filesystem_server_has_no_bare_home_root():
    servers = _real_servers()
    assert "filesystem" in servers
    args = servers["filesystem"]["args"]
    assert "${HOME}" not in args, "filesystem server still mounts the whole home (${HOME})"


def test_filesystem_server_ships_multiple_explicit_roots():
    servers = _real_servers()
    args = servers["filesystem"]["args"]
    roots = [a for a in args if not a.startswith("-") and "server-filesystem" not in a]
    assert len(roots) >= 2, f"expected at least 2 explicit filesystem roots, got {roots!r}"
    for root in roots:
        assert root.strip(), "a filesystem root must not be a blank arg"
        assert root != "${HOME}", "a filesystem root must not silently fall back to the whole home"


def test_real_manifest_validates_against_schema():
    """The edited manifest.yaml (explicit filesystem roots, opt-in memory)
    still satisfies config_schema.py's contract end to end."""
    schema = _load_real_config_schema()
    servers = schema.load_mcp_manifest(MANIFEST)
    assert "filesystem" in servers
    assert "memory" in servers


def test_vault_library_keeps_its_complete_endpoint_in_user_configuration():
    """A shared template must not bake one tunnel port or MCP route in.

    The endpoint can include a non-default port and a custom MCP path.  The
    renderer resolves it for Node clients, while OpenCode keeps its native
    environment reference.
    """
    vault = _real_servers()["vault-library"]
    assert vault["url"] == "${VAULT_LIBRARY_URL}"
    assert vault["url_env"] == "VAULT_LIBRARY_URL"
    assert vault["require_env"] == "VAULT_LIBRARY_URL"


# ---- memory is opt-in, not mounted by default ------------------------------

def test_memory_server_requires_explicit_opt_in():
    servers = _real_servers()
    assert "memory" in servers
    assert servers["memory"].get("require_env"), (
        "memory server must declare require_env so it stays opt-in, not mounted by default"
    )


# ---- schema level: explicit multi-root filesystem args are structurally ---
# ---- valid (not a special case the validator has to special-case) ---------

def test_manifest_contract_accepts_explicit_filesystem_roots(sandbox):
    (sandbox.mcp_dir / "manifest.yaml").write_text(
        """schema_version: 1
servers:
  filesystem:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem@2026.7.10", "/opt/engine", "/opt/data"]
    targets: [codex]
""",
        encoding="utf-8",
    )
    mod = load_render_module(sandbox)
    manifest = mod.load_manifest()
    assert manifest["filesystem"]["args"][-2:] == ["/opt/engine", "/opt/data"]


# ---- behavioral: require_env gating actually skips/admits memory ----------

_SCOPED_MANIFEST = """schema_version: 1
servers:
  filesystem:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem@2026.7.10", "/opt/engine", "/opt/data"]
    targets: [codex]
  memory:
    transport: stdio
    command: npx
    args: ["-y", "@modelcontextprotocol/server-memory@2026.7.4"]
    require_env: MCP_MEMORY_OPT_IN
    targets: [codex]
"""


def test_memory_absent_by_default(sandbox, monkeypatch):
    (sandbox.mcp_dir / "manifest.yaml").write_text(_SCOPED_MANIFEST, encoding="utf-8")
    monkeypatch.delenv("MCP_MEMORY_OPT_IN", raising=False)
    mod = load_render_module(sandbox)

    manifest = mod.load_manifest()

    assert "memory" not in manifest
    assert "filesystem" in manifest


def test_memory_present_once_opted_in(sandbox, monkeypatch):
    (sandbox.mcp_dir / "manifest.yaml").write_text(_SCOPED_MANIFEST, encoding="utf-8")
    monkeypatch.setenv("MCP_MEMORY_OPT_IN", "1")
    mod = load_render_module(sandbox)

    manifest = mod.load_manifest()

    assert "memory" in manifest


def test_memory_opt_in_reaches_codex_dialect(sandbox, monkeypatch):
    """End to end through the Codex renderer: opted out -> no [mcp_servers.memory]
    section written; opted in -> the section appears with the pinned package."""
    (sandbox.mcp_dir / "manifest.yaml").write_text(_SCOPED_MANIFEST, encoding="utf-8")
    codex_config = sandbox.home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True, exist_ok=True)
    codex_config.write_text('model = "fake-model"\n', encoding="utf-8")

    monkeypatch.delenv("MCP_MEMORY_OPT_IN", raising=False)
    mod = load_render_module(sandbox)
    assert mod.write_codex() == 0
    written = tomllib.loads(codex_config.read_text(encoding="utf-8"))
    assert "memory" not in written.get("mcp_servers", {})
    assert "filesystem" in written.get("mcp_servers", {})
    assert written["mcp_servers"]["filesystem"]["args"][-2:] == ["/opt/engine", "/opt/data"]

    monkeypatch.setenv("MCP_MEMORY_OPT_IN", "1")
    mod2 = load_render_module(sandbox)
    assert mod2.write_codex() == 0
    written2 = tomllib.loads(codex_config.read_text(encoding="utf-8"))
    assert written2["mcp_servers"]["memory"]["args"] == ["-y", "@modelcontextprotocol/server-memory@2026.7.4"]
