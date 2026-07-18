"""Onboarding inventory slice on agent_sync.py: the pure helpers behind the
`inventory` mode (skill split + manifest name parsing). The MCP part is already
covered by test_render_inventory.py; the bootstrap part is trivial existence."""
from __future__ import annotations

from conftest import load_agent_sync_module


def test_skill_inventory_split(sandbox):
    mod = load_agent_sync_module(sandbox)
    canonical, extras, missing = mod._skill_inventory({"a", "b"}, ["a", "c"])
    assert canonical == ["a"]        # materialized AND in manifest
    assert extras == ["c"]           # materialized, out-of-manifest
    assert missing == ["b"]          # in manifest, not materialized


def test_skill_manifest_names_reads_mapping_keys(sandbox, tmp_path):
    mod = load_agent_sync_module(sandbox)
    manifest = tmp_path / "skills.manifest.yaml"
    manifest.write_text(
        "skills:\n"
        "  vault-doctor:\n    origin: vault\n"
        "  vault-map:\n    origin: vault\n",
        encoding="utf-8",
    )
    assert mod._skill_manifest_names(manifest) == {"vault-doctor", "vault-map"}
    # absent manifest -> None (caller skips the skill section, no false 'all stray')
    assert mod._skill_manifest_names(tmp_path / "nope.yaml") is None
