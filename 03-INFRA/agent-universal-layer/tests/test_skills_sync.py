"""Test 13 su skills-sync.py --index: genera INDEX.md con una riga per skill
e — regressione del bug "ricreava i link esclusi" — NON tocca i runtime.
--index oggi chiama SOLO write_index(apply=True) (vedi main()): lo testiamo
chiamando direttamente quella funzione, esattamente cio' che --index invoca.
"""
from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace

import pytest

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

    mod2 = load_skills_sync_module(sb)
    mod2.HUB.mkdir(parents=True, exist_ok=True)
    mod2.write_index(apply=True)
    second = (sb.hub / "INDEX.md").read_bytes()

    assert first == second


def test_github_clone_disables_interactive_credentials_and_has_timeout(sandbox, monkeypatch):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.shutil, "which", lambda command: "/usr/bin/git")
    observed = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=1, stderr="authentication required")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert not mod.install_github("remote-skill", {"repo": "example/remote-skill"}, apply=True)
    assert observed["command"][:5] == ["git", "-c", "credential.interactive=never", "clone", "--depth"]
    assert observed["kwargs"]["stdin"] is subprocess.DEVNULL
    assert observed["kwargs"]["timeout"] == mod.GIT_CLONE_TIMEOUT_SECONDS
    assert observed["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert observed["kwargs"]["env"]["GCM_INTERACTIVE"] == "Never"


def test_github_clone_timeout_is_reported_without_crashing(sandbox, monkeypatch, capsys):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.shutil, "which", lambda command: "/usr/bin/git")

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert not mod.install_github("remote-skill", {"repo": "example/remote-skill"}, apply=True)
    assert f"timed out after {mod.GIT_CLONE_TIMEOUT_SECONDS}s" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ("not-a-mapping\n", "root must be a mapping"),
        ("skills: []\n", "'skills' must be a mapping"),
        ("skills:\n  broken: nope\n", "skill 'broken' must be a mapping"),
    ],
)
def test_invalid_manifest_returns_a_readable_error_without_traceback(sandbox, monkeypatch, capsys, manifest, message):
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(manifest, encoding="utf-8")
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py"])

    assert mod.main() == 1
    output = capsys.readouterr().out
    assert message in output
    assert "AttributeError" not in output
