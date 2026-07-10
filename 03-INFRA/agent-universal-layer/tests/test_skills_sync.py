"""Test 13 su skills-sync.py --index: genera INDEX.md con una riga per skill
e — regressione del bug "ricreava i link esclusi" — NON tocca i runtime.
--index oggi chiama SOLO write_index(apply=True) (vedi main()): lo testiamo
chiamando direttamente quella funzione, esattamente cio' che --index invoca.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
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


PINNED_COMMIT = hashlib.sha1(b"skills-sync test revision").hexdigest()


def test_github_fetch_disables_interactive_credentials_and_has_timeout(sandbox, monkeypatch):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.shutil, "which", lambda command: "/usr/bin/git")
    observed = {}

    def fake_run(command, **kwargs):
        observed.setdefault("commands", []).append(command)
        observed["kwargs"] = kwargs
        if "fetch" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="authentication required")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert not mod.install_github(
        "remote-skill", {"repo": "example/remote-skill", "commit": PINNED_COMMIT}, apply=True
    )
    fetch = next(command for command in observed["commands"] if "fetch" in command)
    assert fetch[-2:] == ["origin", PINNED_COMMIT]
    assert "credential.interactive=never" in fetch
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

    assert not mod.install_github(
        "remote-skill", {"repo": "example/remote-skill", "commit": PINNED_COMMIT}, apply=True
    )
    assert f"timed out after {mod.GIT_CLONE_TIMEOUT_SECONDS}s" in capsys.readouterr().out


def test_github_skill_fetches_and_records_the_pinned_commit(sandbox, monkeypatch):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.shutil, "which", lambda command: "/usr/bin/git")
    observed = []

    def fake_run(command, **kwargs):
        observed.append(command)
        if command[1] == "init":
            Path(command[-1]).mkdir(parents=True)
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout=f"{PINNED_COMMIT}\n", stderr="")
        if "checkout" in command:
            repo_dir = Path(command[command.index("-C") + 1])
            (repo_dir / "SKILL.md").write_text("# pinned skill\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    spec = {"repo": "example/remote-skill", "commit": PINNED_COMMIT}

    assert mod.install_github("remote-skill", spec, apply=True)
    installed = sandbox.hub / "remote-skill"
    source = (installed / ".source").read_text(encoding="utf-8")
    assert f"commit: {PINNED_COMMIT}" in source
    fetch = next(command for command in observed if "fetch" in command)
    assert fetch[-1] == PINNED_COMMIT

    def fail_if_called(*args, **kwargs):
        raise AssertionError("a verified pinned skill must not be fetched again")

    monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
    assert mod.install_github("remote-skill", spec, apply=True)


def test_github_skill_rejects_a_different_fetched_commit(sandbox, monkeypatch, capsys):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.shutil, "which", lambda command: "/usr/bin/git")

    def fake_run(command, **kwargs):
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert not mod.install_github(
        "remote-skill", {"repo": "example/remote-skill", "commit": PINNED_COMMIT}, apply=True
    )
    assert "does not match" in capsys.readouterr().out
    assert not (sandbox.hub / "remote-skill").exists()


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ("not-a-mapping\n", "root must be a mapping"),
        ("skills: []\n", "'skills' must be a mapping"),
        ("skills:\n  broken: nope\n", "skill 'broken' must be a mapping"),
        (
            "skills:\n  remote:\n    origin: github\n    repo: example/remote\n    targets: []\n",
            "needs a full 40-character commit SHA",
        ),
        (
            "skills:\n  remote:\n    origin: github\n    repo: example/remote\n    commit: short\n    targets: []\n",
            "needs a full 40-character commit SHA",
        ),
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
