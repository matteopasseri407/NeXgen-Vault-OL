"""Regression tests -- P3: validate_mcp_manifest() must enforce npm pins and
reject literal secrets in stdio env, not just the template-only pytest check.

Before this fix, config_schema.py's _validate_mcp_server() only checked that
a stdio server's `args` was a list of non-empty strings: it never verified
that an `npx` package carried an exact version pin, and _env_mapping() never
verified that an env value wasn't a literal secret. The only pin check lived
in test_mcp_package_pins.py, run against the repo's *template* manifest --
never against validate_mcp_manifest()/load_mcp_manifest(), which is what
render.py and agent_sync.py actually call against the REAL manifest.yaml a
user or agent can edit in AGENT_VAULT_DATA. An edit dropping a pin (or
pasting a literal token into `env:`) passed validation silently.

These tests call validate_mcp_manifest() directly -- the same function the
runtime path uses -- so a regression here means the real load path is once
again unguarded, not just a template fixture.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
REAL_CONFIG_SCHEMA = REPO / "03-INFRA" / "scripts" / "config_schema.py"
REAL_MANIFEST = REPO / "03-INFRA" / "agent-universal-layer" / "mcp" / "manifest.yaml"


def _load_real_config_schema():
    """Fresh module copy of the real config_schema.py (same pattern as
    test_mcp_manifest_scope.py): exercises the actual runtime validator, not
    a stand-in, without leaking a shared import across test modules."""
    spec = importlib.util.spec_from_file_location("config_schema_under_test", REAL_CONFIG_SCHEMA)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_schema = _load_real_config_schema()
ConfigValidationError = _schema.ConfigValidationError
validate_mcp_manifest = _schema.validate_mcp_manifest

SOURCE = "test-manifest"


def _manifest(server_yaml_fragment: dict) -> dict:
    return {
        "schema_version": 1,
        "servers": {"under-test": server_yaml_fragment},
    }


# ---- Finding A: npx package must carry an exact version pin ---------------


def test_npx_without_pin_is_rejected():
    manifest = _manifest(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "firecrawl-mcp@latest"],
            "targets": ["claude"],
        }
    )
    with pytest.raises(ConfigValidationError, match="exact version"):
        validate_mcp_manifest(manifest, SOURCE)


def test_retired_server_names_are_validated_and_cannot_remain_active():
    manifest = {
        "schema_version": 1,
        "retired_servers": ["under-test"],
        "servers": {
            "under-test": {
                "transport": "stdio",
                "command": "node",
                "targets": ["claude"],
            }
        },
    }
    with pytest.raises(ConfigValidationError, match="both active and retired"):
        validate_mcp_manifest(manifest, SOURCE)


def test_invalid_retired_server_name_is_rejected():
    manifest = {"schema_version": 1, "retired_servers": ["../escape"], "servers": {}}
    with pytest.raises(ConfigValidationError, match="retired MCP server name"):
        validate_mcp_manifest(manifest, SOURCE)


def test_retired_server_cannot_collide_with_active_codex_normalized_name():
    manifest = {
        "schema_version": 1,
        "retired_servers": ["active-tool"],
        "servers": {
            "active_tool": {
                "transport": "stdio",
                "command": "node",
                "targets": ["codex"],
            }
        },
    }
    with pytest.raises(ConfigValidationError, match="collides with active Codex server"):
        validate_mcp_manifest(manifest, SOURCE)


def test_npx_with_no_package_arg_at_all_is_rejected():
    manifest = _manifest(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y"],
            "targets": ["claude"],
        }
    )
    with pytest.raises(ConfigValidationError, match="exact version"):
        validate_mcp_manifest(manifest, SOURCE)


def test_npx_with_exact_pin_passes():
    manifest = _manifest(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "firecrawl-mcp@3.22.3"],
            "targets": ["claude"],
        }
    )
    validate_mcp_manifest(manifest, SOURCE)  # must not raise


def test_npx_with_scoped_exact_pin_passes():
    manifest = _manifest(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem@2026.7.10", "/opt/root"],
            "targets": ["codex"],
        }
    )
    validate_mcp_manifest(manifest, SOURCE)  # must not raise


def test_non_npx_stdio_command_is_not_pin_checked():
    manifest = _manifest(
        {
            "transport": "stdio",
            "command": "python3",
            "args": ["/opt/tool.py"],
            "targets": ["claude"],
        }
    )
    validate_mcp_manifest(manifest, SOURCE)  # must not raise: pin rule is npx-specific


def test_npx_pin_enforced_inside_windows_override():
    """The windows: override block re-runs full stdio validation (see
    _validate_mcp_server's recursive call): an unpinned npx package hidden
    behind a Windows-only override must be caught too."""
    manifest = _manifest(
        {
            "transport": "stdio",
            "command": "python3",
            "args": ["/opt/tool.py"],
            "targets": ["claude"],
            "windows": {
                "command": "npx",
                "args": ["-y", "some-tool"],
            },
        }
    )
    with pytest.raises(ConfigValidationError, match="exact version"):
        validate_mcp_manifest(manifest, SOURCE)


# ---- Finding B: stdio env values must not be literal secrets --------------


def test_high_entropy_env_value_is_rejected():
    manifest = _manifest(
        {
            "transport": "stdio",
            "command": "node",
            "args": ["server.js"],
            # Synthetic, non-hex, no digit run >=7: shaped to satisfy the
            # LONGTOK heuristic (40+ chars, has a digit) without tripping the
            # repo's own leak-scan hard patterns on a fixture file.
            "env": {"API_TOKEN": "totally-fake-placeholder-token-value-nq9xz"},
            "targets": ["claude"],
        }
    )
    with pytest.raises(ConfigValidationError, match="literal secret"):
        validate_mcp_manifest(manifest, SOURCE)


def test_env_reference_is_not_flagged_even_if_long():
    manifest = _manifest(
        {
            "transport": "stdio",
            "command": "node",
            "args": ["server.js"],
            "env": {
                "SERVICE_URL": "http://127.0.0.1:${SOME_TUNNEL_PORT:-33002}/a/b/c/d/e/f/g/h"
            },
            "targets": ["claude"],
        }
    )
    validate_mcp_manifest(manifest, SOURCE)  # must not raise: contains "${" => reference


def test_short_env_value_is_not_flagged():
    manifest = _manifest(
        {
            "transport": "stdio",
            "command": "node",
            "args": ["server.js"],
            "env": {"PORT": "33002"},
            "targets": ["claude"],
        }
    )
    validate_mcp_manifest(manifest, SOURCE)  # must not raise: far under the 40-char floor


# ---- Combined valid case: pin + env reference, exactly what the template --
# ---- ships, must keep passing --------------------------------------------


def test_valid_pinned_npx_server_with_env_reference_passes():
    manifest = _manifest(
        {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "firecrawl-mcp@3.22.3"],
            "env": {"FIRECRAWL_API_URL": "http://127.0.0.1:${FIRECRAWL_TUNNEL_PORT:-33002}"},
            "require_env": "FIRECRAWL_TUNNEL_PORT",
            "targets": ["claude", "codex"],
        }
    )
    validate_mcp_manifest(manifest, SOURCE)  # must not raise


# ---- No regression on the real, distributed template -----------------------


def test_real_template_manifest_still_validates():
    """The public repo's own mcp/manifest.yaml (pinned npx packages, env
    values that are all ${VAR} references) must keep passing end to end
    after adding the pin and secret-literal checks."""
    import yaml

    data = yaml.safe_load(REAL_MANIFEST.read_text(encoding="utf-8"))
    servers = validate_mcp_manifest(data, REAL_MANIFEST)
    assert "firecrawl" in servers
    assert "playwright" in servers
