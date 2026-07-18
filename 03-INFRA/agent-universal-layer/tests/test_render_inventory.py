"""Onboarding inventory slice (render.py --inventory): read-only MCP scan
across every CLI, canonical vs out-of-manifest. Foundation of the adopt/reset
flow. Uses the same fresh-module + live-config sandbox as test_render.py."""
from __future__ import annotations

from conftest import load_render_module

DIALECTS = ["claude", "codex", "opencode", "antigravity"]


def test_inventory_reports_out_of_manifest_extras(sandbox_with_live_configs, capsys):
    sb = sandbox_with_live_configs
    mod = load_render_module(sb)

    rc = mod.cmd_inventory()

    assert rc == 0  # pure report, never fails on stray servers
    out = capsys.readouterr().out
    assert "Onboarding inventory" in out
    # the sandbox seeds a legacy extra on each dialect; the scan must flag it
    assert "out-of-manifest" in out
    assert "legacy-extra-tool" in out or "legacy_extra_tool" in out
    # every supported CLI is named in the report
    for cli in DIALECTS:
        assert cli in out
