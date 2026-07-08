"""Test 13 su skills-sync.py --index: genera INDEX.md con una riga per skill
e — regressione del bug "ricreava i link esclusi" — NON tocca i runtime.
--index oggi chiama SOLO write_index(apply=True) (vedi main()): lo testiamo
chiamando direttamente quella funzione, esattamente cio' che --index invoca.
"""
from __future__ import annotations

import shutil

from conftest import load_skills_sync_module


def _populate_hub(sandbox):
    sandbox.hub.mkdir(parents=True, exist_ok=True)
    for name in ("fake-skill-a", "fake-skill-excluded"):
        shutil.copytree(sandbox.skills_dir / name, sandbox.hub / name)


def test_index_lists_every_skill_and_respects_exclude_state(sandbox):
    sb = sandbox
    _populate_hub(sb)

    claude_rt = sb.home / ".claude" / "skills"
    claude_rt.mkdir(parents=True, exist_ok=True)
    # stato "corretto" gia' raggiunto: fake-skill-a collegata, fake-skill-excluded NO
    (claude_rt / "fake-skill-a").symlink_to(sb.hub / "fake-skill-a", target_is_directory=True)

    mod = load_skills_sync_module(sb)
    mod.HUB.mkdir(parents=True, exist_ok=True)
    mod.write_index(apply=True)

    index_path = sb.hub / "INDEX.md"
    assert index_path.is_file(), "--index non ha generato INDEX.md"
    text = index_path.read_text(encoding="utf-8")
    assert "fake-skill-a" in text
    assert "fake-skill-excluded" in text
    assert "Skill sintetica per i test B1" in text  # descrizione dal frontmatter
    assert text.count("- **") == 2, f"attesa una riga per skill, trovato:\n{text}"

    # regressione: --index non deve aver ricreato il link della skill esclusa
    assert not (claude_rt / "fake-skill-excluded").exists(), (
        "--index ha ricreato un link runtime escluso: e' il bug storico"
    )
    # ...e non deve aver toccato quello gia' presente
    assert (claude_rt / "fake-skill-a").resolve() == (sb.hub / "fake-skill-a").resolve()


def test_index_is_idempotent_when_content_unchanged(sandbox):
    sb = sandbox
    _populate_hub(sb)
    mod = load_skills_sync_module(sb)
    mod.HUB.mkdir(parents=True, exist_ok=True)

    mod.write_index(apply=True)
    first = (sb.hub / "INDEX.md").read_bytes()
    mtime1 = (sb.hub / "INDEX.md").stat().st_mtime_ns

    mod2 = load_skills_sync_module(sb)
    mod2.HUB.mkdir(parents=True, exist_ok=True)
    mod2.write_index(apply=True)
    second = (sb.hub / "INDEX.md").read_bytes()

    assert first == second
