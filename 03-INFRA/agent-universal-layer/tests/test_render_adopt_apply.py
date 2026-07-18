"""Adopt --apply (v0.92 slice 4): promote out-of-manifest MCP servers into the
vault manifest, guarded (backup + re-validate + restore-on-failure)."""
from __future__ import annotations

import yaml

from conftest import load_render_module


def test_insert_under_servers_appends_and_parses(sandbox_with_live_configs):
    mod = load_render_module(sandbox_with_live_configs)
    text = "schema_version: 1\nretired_servers:\nservers:\n  a:\n    command: x\n"
    out = mod._insert_under_servers(text, ["  b:\n    command: y"])
    data = yaml.safe_load(out)
    assert set(data["servers"]) == {"a", "b"}


def test_insert_under_servers_returns_none_without_servers_key(sandbox_with_live_configs):
    mod = load_render_module(sandbox_with_live_configs)
    assert mod._insert_under_servers("schema_version: 1\n", ["  b:\n    command: y"]) is None


def test_adopt_apply_promotes_extra_into_manifest(sandbox_with_live_configs):
    sb = sandbox_with_live_configs
    mod = load_render_module(sb)

    # the sandbox seeds an out-of-manifest 'legacy-extra-tool' on each dialect
    rc = mod.cmd_adopt("claude", apply=True)
    assert rc == 0

    data = yaml.safe_load(mod.MANIFEST.read_text(encoding="utf-8"))
    assert "legacy-extra-tool" in data["servers"]
    assert data["servers"]["legacy-extra-tool"]["targets"] == ["claude"]

    # a backup of the pre-adopt manifest was written (reversibility)
    assert list(mod.MANIFEST.parent.glob(f"{mod.MANIFEST.name}.bak-*"))

    # idempotent: now that it's canonical, there is nothing left to adopt
    assert mod.cmd_adopt("claude", apply=True) == 0
