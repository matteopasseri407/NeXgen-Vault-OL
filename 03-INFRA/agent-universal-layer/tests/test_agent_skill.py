"""Tests for the universal on-demand `agent-skill` command."""
from __future__ import annotations

import shutil
import subprocess
import sys


def _populate_library(sandbox):
    sandbox.skill_library.mkdir(parents=True, exist_ok=True)
    for name in ("fake-skill-a", "fake-skill-excluded"):
        shutil.copytree(sandbox.skills_dir / name, sandbox.skill_library / name)


def _run(sandbox, *args):
    return subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent-skill.py"), *args],
        env=sandbox.env(),
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_list_find_and_show_load_only_the_requested_skill(sandbox):
    _populate_library(sandbox)

    listed = _run(sandbox, "list")
    assert listed.returncode == 0
    assert "fake-skill-a" in listed.stdout
    assert "fake-skill-excluded" in listed.stdout

    found = _run(sandbox, "find", "sintetica")
    assert found.returncode == 0
    assert "fake-skill-a" in found.stdout

    shown = _run(sandbox, "show", "fake-skill-a")
    assert shown.returncode == 0
    assert "Skill sintetica per i test B1" in shown.stdout
    assert "fake-skill-excluded" not in shown.stdout


def test_show_rejects_path_traversal_and_unknown_skills(sandbox):
    _populate_library(sandbox)

    traversal = _run(sandbox, "show", "../outside")
    assert traversal.returncode == 2
    assert "Invalid skill name" in traversal.stderr

    missing = _run(sandbox, "show", "does-not-exist")
    assert missing.returncode == 1
    assert "not installed" in missing.stderr


def test_list_and_find_use_multiline_frontmatter_description(sandbox):
    skill = sandbox.skill_library / "multiline-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: multiline-skill\n"
        "description: |\n"
        "  Finds a specific capability without loading every playbook.\n"
        "  Use for a focused manual lookup.\n"
        "---\n",
        encoding="utf-8",
    )

    listed = _run(sandbox, "list")
    assert "Finds a specific capability without loading every playbook." in listed.stdout

    found = _run(sandbox, "find", "focused", "lookup")
    assert found.returncode == 0
    assert "multiline-skill" in found.stdout


def test_show_forces_utf8_even_when_windows_code_page_is_not_utf8(sandbox):
    skill = sandbox.skill_library / "unicode-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Perché: sorgente → destinazione.\n", encoding="utf-8")
    env = sandbox.env()
    env["PYTHONIOENCODING"] = "cp1252"

    shown = subprocess.run(
        [sys.executable, str(sandbox.scripts_dir / "agent-skill.py"), "show", "unicode-skill"],
        env=env,
        capture_output=True,
        timeout=20,
    )

    assert shown.returncode == 0, shown.stderr.decode("utf-8", errors="replace")
    assert shown.stdout.decode("utf-8").splitlines() == ["Perché: sorgente → destinazione."]
