"""Regression coverage for the seats.yaml resolution order: default shared
file, COUNCIL_SEATS_FILE override, and AGENT_TEAM_MEMBER naming convention
(seats.<member>.yaml). See council/seats.yaml.example for the documented
contract this file exercises."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

COUNCIL_PATH = Path(__file__).resolve().parents[1] / "council" / "council.py"


def load_council(monkeypatch, tmp_path, **env):
    vault = tmp_path / "KnowledgeVault"
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(vault))
    monkeypatch.delenv("COUNCIL_SEATS_FILE", raising=False)
    monkeypatch.delenv("AGENT_TEAM_MEMBER", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    module_name = f"council_seats_resolution_under_test_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(module_name, COUNCIL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _default_seats_path(tmp_path: Path) -> Path:
    return (
        tmp_path / "KnowledgeVault" / "03-INFRA" / "agent-universal-layer"
        / "council" / "seats.yaml"
    )


def test_default_seats_path_is_unchanged_without_any_team_override(monkeypatch, tmp_path):
    """Retrocompatibilita' totale: senza COUNCIL_SEATS_FILE ne'
    AGENT_TEAM_MEMBER la risoluzione resta il seats.yaml condiviso di
    sempre -- il caso mono-utente, la stragrande maggioranza degli installi."""
    council = load_council(monkeypatch, tmp_path)
    assert council.SEATS_PATH == _default_seats_path(tmp_path)


def test_agent_team_member_resolves_to_a_named_seats_file(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path, AGENT_TEAM_MEMBER="marco")
    expected = _default_seats_path(tmp_path).with_name("seats.marco.yaml")
    assert council.SEATS_PATH == expected


def test_different_members_resolve_to_different_seats_files(monkeypatch, tmp_path):
    council_marco = load_council(monkeypatch, tmp_path, AGENT_TEAM_MEMBER="marco")
    council_giulia = load_council(monkeypatch, tmp_path, AGENT_TEAM_MEMBER="giulia")
    assert council_marco.SEATS_PATH != council_giulia.SEATS_PATH
    assert council_marco.SEATS_PATH.name == "seats.marco.yaml"
    assert council_giulia.SEATS_PATH.name == "seats.giulia.yaml"


def test_council_seats_file_override_wins_over_agent_team_member(monkeypatch, tmp_path):
    override = tmp_path / "elsewhere" / "custom-seats.yaml"
    council = load_council(
        monkeypatch, tmp_path,
        COUNCIL_SEATS_FILE=str(override),
        AGENT_TEAM_MEMBER="marco",
    )
    assert council.SEATS_PATH == override


def test_agent_team_member_seats_file_is_actually_loaded_not_the_shared_one(monkeypatch, tmp_path):
    """Non solo il path risolto: council deve leggere DAVVERO il file col
    nome del membro, non il seats.yaml condiviso, quando entrambi esistono
    fianco a fianco (il caso reale di un piccolo team)."""
    council = load_council(monkeypatch, tmp_path, AGENT_TEAM_MEMBER="marco")
    council.SEATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    council.SEATS_PATH.write_text(
        "schema_version: 1\n"
        "seats:\n"
        "  marco-seat:\n"
        "    vendor: vendor-a\n"
        "    cli: opencode\n"
        "    model: opencode/test-one\n"
        "    zero_retention: true\n",
        encoding="utf-8",
    )
    shared = _default_seats_path(tmp_path)
    shared.write_text(
        "schema_version: 1\n"
        "seats:\n"
        "  shared-seat:\n"
        "    vendor: vendor-b\n"
        "    cli: codex\n"
        "    model: codex/other\n"
        "    zero_retention: true\n",
        encoding="utf-8",
    )

    seats = council.load_seats()
    assert "marco-seat" in seats
    assert "shared-seat" not in seats
