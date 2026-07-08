"""Test 9-12 su agent-sync.sh, eseguito per davvero (subprocess bash) dentro
la sandbox. MAI sulla HOME reale: ogni chiamata passa da run_agent_sync(),
che si rifiuta di partire se manca il sentinel di sandbox.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import run_agent_sync

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX launcher/symlink regression tests run on Ubuntu; Windows runs agent_sync.py smoke.",
)

RUNTIME_DIRS = (".claude/skills", ".codex/skills")


def _make_hub_symlink_runtime(sandbox) -> None:
    """Riproduce lo stato del bug 2026-07-01: il runtime e' un symlink
    all'INTERA hub, non ancora convertito in cartella reale."""
    (sandbox.home / ".agents").mkdir(parents=True, exist_ok=True)
    for rt in RUNTIME_DIRS:
        p = sandbox.home / rt
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() or p.is_symlink():
            p.unlink()
        os.symlink(sandbox.hub, p, target_is_directory=True)


def _make_real_runtime_dirs(sandbox) -> None:
    for rt in RUNTIME_DIRS:
        (sandbox.home / rt).mkdir(parents=True, exist_ok=True)


# ---- test 9: regressione self-loop (bug 2026-07-01) ------------------------

def test_self_loop_regression_hub_bytes_survive(sandbox):
    sb = sandbox
    _make_hub_symlink_runtime(sb)
    vault_skill_md = sb.skills_dir / "fake-skill-a" / "SKILL.md"
    original_bytes = vault_skill_md.read_bytes()

    proc = run_agent_sync(sb, "apply")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # i byte veri nella sorgente vault non sono mai stati toccati
    assert vault_skill_md.read_bytes() == original_bytes

    # l'hub deve contenere la skill, leggibile fino al SKILL.md vero
    hub_skill_md = sb.hub / "fake-skill-a" / "SKILL.md"
    assert hub_skill_md.is_file(), "hub: fake-skill-a non leggibile dopo il run"
    assert hub_skill_md.read_bytes() == original_bytes

    for rt in RUNTIME_DIRS:
        rt_path = sb.home / rt
        assert rt_path.is_dir() and not rt_path.is_symlink(), (
            f"{rt}: doveva diventare cartella reale (era symlink all'hub), trovato altro"
        )
        per_skill_link = rt_path / "fake-skill-a"
        assert per_skill_link.is_symlink(), f"{rt}/fake-skill-a: atteso link per-skill"
        resolved = per_skill_link.resolve()
        assert resolved == (sb.hub / "fake-skill-a").resolve(), (
            f"{rt}/fake-skill-a: non punta all'hub ({resolved})"
        )
        # niente self-loop: il link risolto non deve mai ricadere dentro rt_path stesso
        assert rt_path.resolve() not in resolved.parents and resolved != rt_path.resolve(), (
            f"{rt}/fake-skill-a: self-loop rilevato (risolve dentro se stesso: {resolved})"
        )
        assert (per_skill_link / "SKILL.md").read_bytes() == original_bytes


# ---- test 10: self-healing symlink -----------------------------------------

def test_self_healing_symlink_restored_after_deletion(sandbox):
    sb = sandbox
    _make_real_runtime_dirs(sb)

    proc1 = run_agent_sync(sb, "apply")
    assert proc1.returncode == 0, proc1.stdout + proc1.stderr

    codex_agents = sb.home / ".codex" / "AGENTS.md"
    assert codex_agents.is_symlink(), "pointer AGENTS.md per Codex non creato al primo giro"
    canonical = sb.ul / "instructions" / "AGENTS.md"
    assert codex_agents.resolve() == canonical.resolve()

    codex_agents.unlink()
    assert not codex_agents.exists()

    proc2 = run_agent_sync(sb, "apply")
    assert proc2.returncode == 0, proc2.stdout + proc2.stderr
    assert codex_agents.is_symlink(), "il pointer cancellato non e' stato ripristinato"
    assert codex_agents.resolve() == canonical.resolve()


# ---- test 11: exclude-list rispettata --------------------------------------

def test_exclude_list_respected(sandbox):
    sb = sandbox
    _make_real_runtime_dirs(sb)

    proc = run_agent_sync(sb, "apply")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    # esclusa dal precarico nei runtime...
    assert not (sb.home / ".claude" / "skills" / "fake-skill-excluded").exists()
    assert not (sb.home / ".codex" / "skills" / "fake-skill-excluded").exists()
    # ...ma presente nell'hub (lazy, on-demand)
    assert (sb.hub / "fake-skill-excluded" / "SKILL.md").is_file()
    # la skill NON esclusa invece e' collegata in entrambi i runtime
    assert (sb.home / ".claude" / "skills" / "fake-skill-a").is_symlink()
    assert (sb.home / ".codex" / "skills" / "fake-skill-a").is_symlink()


# ---- test 12: idempotenza (doppio giro = zero modifiche al filesystem) ----

def test_apply_is_idempotent(sandbox):
    sb = sandbox
    _make_real_runtime_dirs(sb)

    # priming run: porta la sandbox a regime (la prima esecuzione fa sempre
    # qualche scrittura iniziale: pointer, hub, backup di render.py, ecc.)
    proc0 = run_agent_sync(sb, "apply")
    assert proc0.returncode == 0, proc0.stdout + proc0.stderr

    exclude = frozenset({"agent-sync.log"})
    snap_before = sb.tree_snapshot(exclude_names=exclude)

    proc1 = run_agent_sync(sb, "apply")
    assert proc1.returncode == 0, proc1.stdout + proc1.stderr
    snap_after = sb.tree_snapshot(exclude_names=exclude)

    if snap_after != snap_before:
        only_before = {k: v for k, v in snap_before.items() if snap_after.get(k) != v}
        only_after = {k: v for k, v in snap_after.items() if snap_before.get(k) != v}
        raise AssertionError(
            "il secondo giro ha modificato il filesystem sandbox.\n"
            f"cambiati/rimossi: {only_before}\naggiunti/cambiati: {only_after}"
        )
