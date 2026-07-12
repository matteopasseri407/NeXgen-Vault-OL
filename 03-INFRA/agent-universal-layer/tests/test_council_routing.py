from __future__ import annotations

import argparse
import importlib.util
import sys
import textwrap
import types
from pathlib import Path

import pytest


COUNCIL_PATH = Path(__file__).resolve().parents[1] / "council" / "council.py"
ROUTING_PATH = COUNCIL_PATH.parent / "routing.py"


def load_routing():
    module_name = f"council_routing_under_test_{id(object())}"
    spec = importlib.util.spec_from_file_location(module_name, ROUTING_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_council(monkeypatch, tmp_path):
    vault = tmp_path / "KnowledgeVault"
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(vault))
    module_name = f"council_routing_integration_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(module_name, COUNCIL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    mod.SESSIONS_DIR = tmp_path / "sessions"
    mod.SEATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    return mod


def write_seats(council, text: str) -> None:
    council.SEATS_PATH.write_text(
        "schema_version: 1\n" + textwrap.dedent(text).strip() + "\n",
        encoding="utf-8",
    )


LEGACY_ROUTING_TABLE = """# Routing decision

<!-- routing-document:start -->
### Ranking per ruoli reali

| Ruolo | Primario | Fallback 1 | Fallback 2 | Metodo |
|---|---|---|---|---|
| L-Arch | Gemini 3.1 Pro (Antigravity, cloud condiviso) | GPT-5.6 Terra (Codex Plus, cloud condiviso) | DeepSeek V4 Pro (OpenCode Go, cloud condiviso) | deterministico |
| L-Code | DeepSeek V4 Pro (OpenCode Go, cloud condiviso) | GPT-5.6 Terra (Codex Plus, cloud condiviso) | Claude Opus 4.8 (Antigravity, cloud condiviso) | deterministico |

### Motivazioni concise
<!-- routing-document:end -->
"""


def test_legacy_routing_table_is_parsed_strictly():
    routing = load_routing()

    plan = routing.parse_routing_plan(LEGACY_ROUTING_TABLE)

    assert plan.source == "legacy-routing-table"
    assert [candidate.value for candidate in plan.roles["L-Code"]] == [
        "DeepSeek V4 Pro",
        "GPT-5.6 Terra",
        "Claude Opus 4.8",
    ]


def test_machine_contract_takes_precedence_and_keeps_ranked_fallbacks():
    routing = load_routing()
    routing_document = """<!-- council-routing-contract:start -->
```json
{"schema_version":1,"roles":[{"role":"L-Code","assignment":{"primary":"deepseek-v4-pro","fallback_1":"gpt-5-6-terra","fallback_2":"claude-opus-4-8"},"ranked":[{"id":"deepseek-v4-pro"},{"id":"glm-5-2"}]}]}
```
<!-- council-routing-contract:end -->
"""

    plan = routing.parse_routing_plan(routing_document)

    assert plan.source == "contract-v1"
    assert [candidate.value for candidate in plan.roles["L-Code"]] == [
        "deepseek-v4-pro",
        "gpt-5-6-terra",
        "claude-opus-4-8",
        "glm-5-2",
    ]


def test_resolver_keeps_document_order_and_skips_unavailable_or_unsafe_seats():
    routing = load_routing()
    plan = routing.parse_routing_plan(LEGACY_ROUTING_TABLE)
    seats = {
        "deepseek": {
            "routing_label": "DeepSeek V4 Pro",
            "zero_retention": True,
            "cli": "opencode",
            "model": "opencode-go/deepseek-v4-pro",
        },
        "terra": {
            "routing_label": "GPT-5.6 Terra",
            "zero_retention": False,
            "cli": "codex",
            "model": "gpt-5.6-terra",
        },
        "claude": {
            "routing_label": "Claude Opus 4.8",
            "zero_retention": True,
            "cli": "claude",
            "model": "opus",
        },
    }
    capabilities = {
        "deepseek": routing.SeatCapability(True, "rilevato"),
        "terra": routing.SeatCapability(True, "rilevato"),
        "claude": routing.SeatCapability(False, "modello non verificabile"),
    }

    candidates, diagnostics = routing.resolve_role_candidates(
        plan, seats, capabilities, "L-Code", allow_training_risk=False,
    )

    assert candidates == ["deepseek"]
    assert any("zero-retention" in item for item in diagnostics)
    assert any("non verificabile" in item for item in diagnostics)


def test_private_config_accepts_routing_metadata_and_new_cli_adapters(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    write_seats(
        council,
        """
        routing:
          enabled: true
          decision_file: routing.md
          relay_roles: [L-Code]
        seats:
          claude-seat:
            vendor: anthropic
            cli: claude
            model: opus
            reasoning_effort: high
            zero_retention: false
          local-seat:
            vendor: local
            cli: ollama
            model: gemma4:12b
            reasoning_effort: none
            zero_retention: true
        """,
    )

    config = council.load_config()

    assert config["routing"]["enabled"] is True
    assert config["seats"]["claude-seat"]["reasoning_effort"] == "high"


@pytest.mark.parametrize("decision_file", ["../outside.md", "C:\\\\Users\\\\example\\\\routing.md"])
def test_private_config_rejects_routing_path_escape(monkeypatch, tmp_path, decision_file):
    council = load_council(monkeypatch, tmp_path)
    write_seats(
        council,
        f"""
        routing:
          enabled: true
          decision_file: '{decision_file}'
        seats:
          one:
            vendor: vendor
            cli: opencode
            model: opencode/test
            zero_retention: true
        """,
    )

    with pytest.raises(SystemExit, match="relative Vault path"):
        council.load_config()


def test_routing_proposal_requires_an_explicit_human_selected_seat(monkeypatch, tmp_path, capsys):
    council = load_council(monkeypatch, tmp_path)
    write_seats(
        council,
        """
        routing:
          enabled: true
          decision_file: routing.md
          mode_defaults:
            challenge: L-Code
          relay_roles: [L-Arch, L-Code]
        seats:
          unsafe-gemini:
            vendor: google
            cli: agy
            model: Gemini 3.1 Pro (High)
            routing_label: Gemini 3.1 Pro
            reasoning_effort: high
            zero_retention: false
          deepseek:
            vendor: deepseek
            cli: opencode
            model: opencode-go/deepseek-v4-pro
            routing_label: DeepSeek V4 Pro
            zero_retention: true
          terra:
            vendor: openai
            cli: codex
            model: gpt-5.6-terra
            routing_label: GPT-5.6 Terra
            reasoning_effort: max
            zero_retention: false
        """,
    )
    routing_path = council._vault_data_root() / "routing.md"
    routing_path.parent.mkdir(parents=True, exist_ok=True)
    routing_path.write_text(LEGACY_ROUTING_TABLE, encoding="utf-8")
    monkeypatch.setattr(
        council,
        "seat_capabilities",
        lambda _seats: {
            "unsafe-gemini": types.SimpleNamespace(available=True, reason="rilevato"),
            "deepseek": types.SimpleNamespace(available=True, reason="rilevato"),
            "terra": types.SimpleNamespace(available=True, reason="rilevato"),
        },
    )

    args = argparse.Namespace(seat=None, routing_role=None, allow_training_risk=False, mode="challenge")

    with pytest.raises(SystemExit, match="scelta umana richiesta"):
        council.resolve_seat(args, default_routing_role="L-Arch")

    output = capsys.readouterr().out
    assert "Nessuna chiamata a modelli è stata effettuata" in output
    assert "deepseek: opencode-go/deepseek-v4-pro via opencode" in output
    assert "rischio training consentito" not in output


def test_relay_without_sequence_requires_human_selection(monkeypatch, tmp_path, capsys):
    council = load_council(monkeypatch, tmp_path)
    write_seats(
        council,
        """
        routing:
          enabled: true
          decision_file: routing.md
          relay_roles: [L-Arch, L-Code]
        seats:
          gemini:
            vendor: google
            cli: agy
            model: Gemini 3.1 Pro (High)
            routing_label: Gemini 3.1 Pro
            reasoning_effort: high
            zero_retention: true
          deepseek:
            vendor: deepseek
            cli: opencode
            model: opencode-go/deepseek-v4-pro
            routing_label: DeepSeek V4 Pro
            zero_retention: true
        """,
    )
    routing_path = council._vault_data_root() / "routing.md"
    routing_path.parent.mkdir(parents=True, exist_ok=True)
    routing_path.write_text(LEGACY_ROUTING_TABLE, encoding="utf-8")
    monkeypatch.setattr(
        council,
        "seat_capabilities",
        lambda _seats: {
            "gemini": types.SimpleNamespace(available=True, reason="rilevato"),
            "deepseek": types.SimpleNamespace(available=True, reason="rilevato"),
        },
    )

    args = argparse.Namespace(
        sequence=None,
        max_seats=5,
        allow_training_risk=False,
        no_stats_precheck=True,
    )
    config = council.load_config()
    with pytest.raises(SystemExit, match="scelta umana richiesta"):
        council._load_relay_sequence(args, config, config["seats"])

    output = capsys.readouterr().out
    assert "proposta per relay" in output
    assert "gemini: Gemini 3.1 Pro (High) via agy, effort high" in output
    assert "deepseek: opencode-go/deepseek-v4-pro via opencode" in output


def test_propose_lists_candidates_but_never_runs_a_seat(monkeypatch, tmp_path, capsys):
    council = load_council(monkeypatch, tmp_path)
    write_seats(
        council,
        """
        routing:
          enabled: true
          decision_file: routing.md
          mode_defaults:
            challenge: L-Code
        seats:
          deepseek:
            vendor: deepseek
            cli: opencode
            model: opencode-go/deepseek-v4-pro
            routing_label: DeepSeek V4 Pro
            zero_retention: true
        """,
    )
    routing_path = council._vault_data_root() / "routing.md"
    routing_path.parent.mkdir(parents=True, exist_ok=True)
    routing_path.write_text(LEGACY_ROUTING_TABLE, encoding="utf-8")
    monkeypatch.setattr(
        council,
        "seat_capabilities",
        lambda _seats: {"deepseek": types.SimpleNamespace(available=True, reason="rilevato")},
    )
    invoked = []
    monkeypatch.setattr(council, "run_seat", lambda *_args, **_kwargs: invoked.append(True))

    council.cmd_propose(
        argparse.Namespace(proposal_mode="challenge", routing_role=None, allow_training_risk=False)
    )

    assert invoked == []
    output = capsys.readouterr().out
    assert "Nessuna chiamata a modelli è stata effettuata" in output
    assert "scegli tu un candidato" in output


def test_opencode_usage_hint_never_reorders_other_cli_positions(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    seats = {
        "codex": {"cli": "codex", "model": "gpt-5.6-terra"},
        "expensive-open": {"cli": "opencode", "model": "opencode-go/expensive"},
        "cheap-open": {"cli": "opencode", "model": "opencode-go/cheap"},
        "gemini": {"cli": "agy", "model": "Gemini 3.1 Pro (High)"},
    }

    ordered = council._sort_candidates_by_usage(
        ["codex", "expensive-open", "cheap-open", "gemini"],
        seats,
        {"opencode-go/expensive": 9.0, "opencode-go/cheap": 0.0},
    )

    assert ordered == ["codex", "cheap-open", "expensive-open", "gemini"]


@pytest.mark.parametrize(
    ("cli", "effort", "expected"),
    [
        ("codex", "max", "model_reasoning_effort=\"max\""),
        ("claude", "high", "--effort"),
    ],
)
def test_vendor_adapter_passes_declared_effort(monkeypatch, tmp_path, cli, effort, expected):
    council = load_council(monkeypatch, tmp_path)
    invocation = council._build_seat_command(
        {"cli": cli, "model": "vendor/model", "reasoning_effort": effort},
        "brief", tmp_path,
    )

    assert expected in invocation.argv
    assert invocation.stdin_text == "brief"
    if cli == "codex":
        assert invocation.output_file is not None
        invocation.output_file.unlink()
