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


def test_claude_memory_stats_counts_facts_per_project(sandbox, tmp_path):
    mod = load_agent_sync_module(sandbox)
    projects = tmp_path / "projects"
    (projects / "projA" / "memory").mkdir(parents=True)
    (projects / "projA" / "memory" / "fact1.md").write_text("x", encoding="utf-8")
    (projects / "projA" / "memory" / "fact2.md").write_text("x", encoding="utf-8")
    (projects / "projB" / "memory").mkdir(parents=True)   # memory dir, no facts
    (projects / "projC").mkdir()                          # no memory dir -> excluded

    stats = dict(mod._claude_memory_stats(projects))
    assert stats["projA"] == 2
    assert stats["projB"] == 0
    assert "projC" not in stats
    # absent projects dir -> empty, never crashes
    assert mod._claude_memory_stats(tmp_path / "absent") == []
