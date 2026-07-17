"""Behavioral tests for vault-map.py (roadmap item 21, tranche A) plus the
wiring of its two proactive surfaces.

vault-map is the deterministic structural map of a vault: broken wikilinks,
orphan notes, hub notes. Read-only, stdlib-only, always exit 0 in --check
mode (it is a WARN-only backstop, never a gate). Resolution semantics
deliberately mirror the vault-library MCP server (path, unique stem, note
title) so the map and the MCP never contradict each other; targets under
99-SECRETS are valid-but-excluded, never "broken".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "03-INFRA" / "scripts" / "vault-map.py"
DOCTOR_SH = REPO / "03-INFRA" / "scripts" / "agent-doctor.sh"
DOCTOR_PS1 = REPO / "03-INFRA" / "scripts" / "agent-doctor.ps1"
GROOM_SH = REPO / "03-INFRA" / "scripts" / "vault-groom.sh"
GROOM_PS1 = REPO / "03-INFRA" / "scripts" / "vault-groom.ps1"


@pytest.fixture()
def vault(tmp_path):
    v = tmp_path / "vault"
    for sub in ("02-PROJECTS", "01-ME/cv-artifacts", "99-INDEX", "99-SECRETS"):
        (v / sub).mkdir(parents=True)
    (v / "00-START-HERE.md").write_text("# Start\n\nvai su [[a]]\n", encoding="utf-8")
    (v / "02-PROJECTS" / "a.md").write_text(
        "# A\n\n[[b]] e [[missing-note]] e [[99-SECRETS/token-store]] "
        "e [canvas](../knowledge-map.canvas)\n",
        encoding="utf-8",
    )
    (v / "02-PROJECTS" / "b.md").write_text("# B\n\n[[A Title Note]]\n", encoding="utf-8")
    (v / "02-PROJECTS" / "c.md").write_text("# A Title Note\n\ncontenuto\n", encoding="utf-8")
    (v / "02-PROJECTS" / "orphan.md").write_text(
        "# Orphan\n\nsolo testo\n\n```\n[[fenced-link]]\n```\n", encoding="utf-8"
    )
    (v / "02-PROJECTS" / "mover.md").write_text(
        "# Mover\n\n`[[a]]` e [[02-PROJECTS/parked]]\n", encoding="utf-8"
    )
    (v / "01-ME" / "cv-artifacts" / "cv1.md").write_text("# CV\n", encoding="utf-8")
    (v / "99-INDEX" / "note-index.md").write_text(
        "# Note Index\n\n- [[02-PROJECTS/orphan]]\n- [[02-PROJECTS/a]]\n", encoding="utf-8"
    )
    (v / "99-SECRETS" / "token-store.md").write_text(
        "# secret\n\n[[nonexistent-target]]\n", encoding="utf-8"
    )
    (v / "knowledge-map.canvas").write_text("{}", encoding="utf-8")
    (v / "02-PROJECTS" / "archive").mkdir()
    (v / "02-PROJECTS" / "archive" / "parked.md").write_text(
        "# Parked\n\n[[gone-note]]\n", encoding="utf-8"
    )
    return v


def run_map(vault: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--vault", str(vault), *args],
        capture_output=True,
        text=True,
    )


def get_json(vault: Path) -> dict:
    result = run_map(vault, "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# --- analysis semantics -------------------------------------------------------


def test_broken_links_and_valid_but_excluded_targets(vault):
    data = get_json(vault)
    broken = {(entry["source"], entry["target"]) for entry in data["broken"]}
    assert broken == {
        ("02-PROJECTS/a.md", "missing-note"),
        ("02-PROJECTS/mover.md", "02-PROJECTS/parked"),
    }
    # A [[wikilink]] inside a fenced code block is quotation, never counted;
    # a backtick-wrapped inline `[[link]]` is a real edge (see mover.md,
    # covered by the hub test) because that wrapping is a rendering habit.
    assert all(entry["target"] != "fenced-link" for entry in data["broken"])
    # A path-qualified link to a note that moved gets a relocation hint.
    hints = {entry["target"]: entry.get("hint") for entry in data["broken"]}
    assert hints["02-PROJECTS/parked"] == "02-PROJECTS/archive/parked.md"
    # A target under 99-SECRETS exists on disk: valid-but-excluded, NOT broken.
    assert any("99-SECRETS" in entry["target"] for entry in data["excluded_valid"])
    # A relative link to an existing non-markdown asset is not broken either.
    assert all("canvas" not in entry["target"] for entry in data["broken"])
    # Notes inside 99-SECRETS are never scanned: their dead links don't appear.
    assert all(not entry["source"].startswith("99-SECRETS") for entry in data["broken"])
    # A dead link whose SOURCE is an archived note is frozen history:
    # reported separately, never mixed into the live broken signal.
    assert [entry["target"] for entry in data["archived_broken"]] == ["gone-note"]


def test_orphans_filter_non_notes_and_ignore_generated_index(vault):
    data = get_json(vault)
    # orphan.md has no structural inbound/outbound: the note-index link to it
    # is generated, so it must NOT rescue the note from orphan-hood.
    assert data["orphans"] == ["02-PROJECTS/orphan.md"]
    # cv-artifacts are artifacts, not notes: never reported as orphans.
    assert "01-ME/cv-artifacts/cv1.md" not in data["orphans"]
    # An archived note is deliberately parked: unlinked is its normal state.
    assert "02-PROJECTS/archive/parked.md" not in data["orphans"]


def test_title_resolution_matches_the_vault_library(vault):
    data = get_json(vault)
    # [[A Title Note]] resolves to c.md through its H1 title, like the MCP.
    assert all(entry["target"] != "A Title Note" for entry in data["broken"])
    # ...and gives c.md an inbound edge, so c.md is not an orphan.
    assert "02-PROJECTS/c.md" not in data["orphans"]


def test_hub_counts_exclude_the_generated_index(vault):
    data = get_json(vault)
    inbound = {entry["path"]: entry["inbound"] for entry in data["hubs"]}
    # a.md is linked by 00-START-HERE (structural), mover.md (structural,
    # backtick-wrapped inline `[[a]]` still counts) and note-index
    # (generated, excluded): 2 structural edges.
    assert inbound["02-PROJECTS/a.md"] == 2


def test_deterministic_output(vault):
    first = run_map(vault, "--json").stdout
    second = run_map(vault, "--json").stdout
    assert first == second


# --- check mode: WARN-only backstop contract ---------------------------------


def test_check_mode_summarizes_and_never_fails(vault):
    result = run_map(vault, "--check")
    assert result.returncode == 0, "the backstop is WARN-only: broken links never flip the exit code"
    first_line = result.stdout.splitlines()[0]
    assert " broken=2 " in f" {first_line} " and "orphans=1" in first_line
    assert "archived_broken=1" in first_line
    assert "notes=" in first_line and "links=" in first_line
    assert "missing-note" in result.stdout


def test_human_report_runs_and_names_the_findings(vault):
    result = run_map(vault)
    assert result.returncode == 0, result.stderr
    assert "missing-note" in result.stdout
    assert "orphan" in result.stdout.lower()


# --- proactive wiring: doctor backstop + groom cycle --------------------------


def test_doctor_twins_carry_the_wikilink_backstop():
    for twin in (DOCTOR_SH, DOCTOR_PS1):
        text = twin.read_text(encoding="utf-8")
        assert "vault-map.py" in text, f"{twin.name}: missing the vault-map backstop"
        assert "--check" in text


def test_groom_twins_feed_the_map_to_the_propose_pass():
    for twin in (GROOM_SH, GROOM_PS1):
        text = twin.read_text(encoding="utf-8")
        assert "vault-map" in text, (
            f"{twin.name}: the groom preview must include the structural map "
            "(orphans and broken links are first-class tranche candidates)"
        )
