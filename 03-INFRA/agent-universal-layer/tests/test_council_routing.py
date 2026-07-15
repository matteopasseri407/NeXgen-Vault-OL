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
    # agy has no reasoning-effort CLI flag (verified via `agy --help`,
    # 2026-07-13): the label must say so, not show effort as if honored
    # identically to a claude/codex/ollama/opencode seat.
    assert "gemini: Gemini 3.1 Pro (High) via agy, effort high (non applicato da questa CLI)" in output
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
        # Security fix: mkstemp() must land inside the private (0700) session
        # dir, not the shared system temp dir -- otherwise the seat's raw
        # response briefly sits somewhere other local users can read.
        assert invocation.output_file.parent == tmp_path
        invocation.output_file.unlink()


def test_codex_output_file_lands_in_session_dir_not_system_tmp(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "sessions" / "council-test"
    session_dir.mkdir(parents=True)

    invocation = council._build_seat_command(
        {"cli": "codex", "model": "vendor/test"}, "brief", session_dir,
    )

    try:
        assert invocation.output_file is not None
        assert invocation.output_file.parent == session_dir
        assert invocation.output_file.exists()
    finally:
        invocation.output_file.unlink(missing_ok=True)


# --- _probe_codex_seat, exercised against a real config.toml on disk -------


def test_probe_codex_seat_matches_when_config_agrees(monkeypatch, tmp_path):
    routing = load_routing()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.6-luna"\nmodel_reasoning_effort = "high"\n', encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    capability = routing._probe_codex_seat({"model": "gpt-5.6-luna", "reasoning_effort": "high"})

    assert capability.available is True


def test_probe_codex_seat_reports_model_mismatch_with_both_models_and_path(monkeypatch, tmp_path):
    routing = load_routing()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-5.6-luna"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    capability = routing._probe_codex_seat({"model": "gpt-5.6-terra", "reasoning_effort": "none"})

    assert capability.available is False
    assert "gpt-5.6-luna" in capability.reason
    assert "gpt-5.6-terra" in capability.reason
    assert str(config_path) in capability.reason


def test_probe_codex_seat_reports_effort_mismatch_with_both_efforts_and_path(monkeypatch, tmp_path):
    routing = load_routing()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    config_path.write_text('model = "gpt-5.6-luna"\nmodel_reasoning_effort = "low"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    capability = routing._probe_codex_seat({"model": "gpt-5.6-luna", "reasoning_effort": "max"})

    assert capability.available is False
    assert "low" in capability.reason
    assert "max" in capability.reason
    assert str(config_path) in capability.reason


@pytest.mark.parametrize("setup", ["missing", "malformed"])
def test_probe_codex_seat_reports_unreadable_or_invalid_config(monkeypatch, tmp_path, setup):
    routing = load_routing()
    codex_home = tmp_path / "codex-home"
    if setup == "malformed":
        codex_home.mkdir()
        (codex_home / "config.toml").write_text("model = [unterminated", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    capability = routing._probe_codex_seat({"model": "gpt-5.6-luna"})

    assert capability.available is False
    assert "non leggibile" in capability.reason


# --- seat_capabilities: claude is excluded with a fixed, non-probed reason -


def test_seat_capabilities_excludes_claude_with_fixed_reason(monkeypatch):
    routing = load_routing()
    monkeypatch.setattr(routing.shutil, "which", lambda _cli: "/usr/bin/claude")

    capabilities = routing.seat_capabilities({"claude-seat": {"cli": "claude", "model": "opus"}})

    assert capabilities["claude-seat"].available is False
    assert capabilities["claude-seat"].reason == (
        "Claude non espone una lista locale del modello esatto, quindi non entra nella proposta automatizzata"
    )


# --- council._check_seat_allowed: the STOP path an explicit --seat can hit -


def test_check_seat_allowed_stops_without_zero_retention_or_override(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    args = argparse.Namespace(allow_training_risk=False)

    with pytest.raises(SystemExit, match="NON ha garanzia zero-retention"):
        council._check_seat_allowed("risky-seat", {"zero_retention": False}, args)


# --- resolve_seat with an explicit --seat (bypasses the routing probe) -----


def test_resolve_seat_explicit_codex_mismatch_still_resolves_and_warns(monkeypatch, tmp_path, capsys):
    council = load_council(monkeypatch, tmp_path)
    write_seats(
        council,
        """
        seats:
          codex-seat:
            vendor: openai
            cli: codex
            model: gpt-5.6-terra
            reasoning_effort: high
            zero_retention: true
        """,
    )
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.6-luna"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    args = argparse.Namespace(seat="codex-seat", allow_training_risk=False)

    seat_name, seat = council.resolve_seat(args)

    assert seat_name == "codex-seat"
    assert seat["model"] == "gpt-5.6-terra"
    output = capsys.readouterr().out
    assert "codex-seat" in output
    assert "non è il default corrente della CLI codex" in output
    assert "gpt-5.6-luna" in output
    assert "gpt-5.6-terra" in output


def test_resolve_seat_unknown_explicit_seat_exits_with_message(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    write_seats(
        council,
        """
        seats:
          known-seat:
            vendor: local
            cli: ollama
            model: gemma4:12b
            zero_retention: true
        """,
    )
    args = argparse.Namespace(seat="ghost-seat", allow_training_risk=False)

    with pytest.raises(SystemExit, match="seat sconosciuto: ghost-seat"):
        council.resolve_seat(args)


def test_resolve_seat_explicit_seat_same_vendor_as_author_stops_cross_review(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    write_seats(
        council,
        """
        seats:
          claude-seat:
            vendor: anthropic
            cli: claude
            model: opus
            zero_retention: true
        """,
    )
    args = argparse.Namespace(seat="claude-seat", allow_training_risk=False, author_vendor="Anthropic")

    with pytest.raises(SystemExit, match="STOP: il seat 'claude-seat' è dello stesso vendor"):
        council.resolve_seat(args)


# --- cmd_routing_status: a role with zero eligible candidates -------------


def test_cmd_routing_status_prints_blocked_role_with_diagnostics(monkeypatch, tmp_path, capsys):
    council = load_council(monkeypatch, tmp_path)
    write_seats(
        council,
        """
        routing:
          enabled: true
          decision_file: routing.md
        seats:
          terra:
            vendor: openai
            cli: codex
            model: gpt-5.6-terra
            routing_label: GPT-5.6 Terra
            zero_retention: false
        """,
    )
    routing_path = council._vault_data_root() / "routing.md"
    routing_path.parent.mkdir(parents=True, exist_ok=True)
    routing_path.write_text(LEGACY_ROUTING_TABLE, encoding="utf-8")
    monkeypatch.setattr(
        council,
        "seat_capabilities",
        lambda _seats: {"terra": types.SimpleNamespace(available=True, reason="rilevato")},
    )

    council.cmd_routing_status(argparse.Namespace(allow_training_risk=False))

    output = capsys.readouterr().out
    assert "L-Arch: BLOCCATO" in output
    assert "escluso dalla policy zero-retention" in output


def test_windows_probe_wraps_npm_cmd_shim(monkeypatch):
    routing = load_routing()
    monkeypatch.setattr(routing.os, "name", "nt")
    monkeypatch.setattr(routing.shutil, "which", lambda _name: r"C:\Tools\opencode.cmd")

    assert routing._windows_command_argv(["opencode", "models"]) == [
        "cmd.exe", "/d", "/s", "/c", r"C:\Tools\opencode.cmd", "models"
    ]


# --- routing.py parser error paths: every RoutingContractError branch -----


BAD_HEADER_TABLE = """### Ranking per ruoli reali

| Ruolo | Modello | Fallback 1 | Fallback 2 |
|---|---|---|---|
| L-Arch | X | Y | Z |

### Motivazioni concise
"""

INCOMPLETE_ROW_TABLE = """### Ranking per ruoli reali

| Ruolo | Primario | Fallback 1 | Fallback 2 |
|---|---|---|---|
| L-Arch | X |

### Motivazioni concise
"""


def test_legacy_table_missing_heading_raises():
    routing = load_routing()
    with pytest.raises(routing.RoutingContractError, match="non trovata"):
        routing.parse_routing_plan("# Routing decision\n\nNiente qui.\n")


def test_legacy_table_bad_header_columns_raises():
    routing = load_routing()
    with pytest.raises(routing.RoutingContractError, match="colonne della tabella"):
        routing.parse_routing_plan(BAD_HEADER_TABLE)


def test_legacy_table_incomplete_row_raises():
    routing = load_routing()
    with pytest.raises(routing.RoutingContractError, match="riga della tabella"):
        routing.parse_routing_plan(INCOMPLETE_ROW_TABLE)


def test_json_contract_incomplete_marker_raises():
    routing = load_routing()
    doc = "<!-- council-routing-contract:start -->\n{}\n"
    with pytest.raises(routing.RoutingContractError, match="marker del contratto"):
        routing.parse_routing_plan(doc)


def test_json_contract_invalid_json_raises():
    routing = load_routing()
    doc = (
        "<!-- council-routing-contract:start -->\n"
        "{not json}\n"
        "<!-- council-routing-contract:end -->\n"
    )
    with pytest.raises(routing.RoutingContractError, match="JSON del contratto Council non valido"):
        routing.parse_routing_plan(doc)


def test_json_contract_wrong_schema_version_raises():
    routing = load_routing()
    doc = (
        "<!-- council-routing-contract:start -->\n"
        '{"schema_version":2,"roles":[{"role":"L-Code","assignment":{"primary":"x"}}]}\n'
        "<!-- council-routing-contract:end -->\n"
    )
    with pytest.raises(routing.RoutingContractError, match="schema_version 1"):
        routing.parse_routing_plan(doc)


def test_json_contract_missing_roles_raises():
    routing = load_routing()
    doc = (
        "<!-- council-routing-contract:start -->\n"
        '{"schema_version":1}\n'
        "<!-- council-routing-contract:end -->\n"
    )
    with pytest.raises(routing.RoutingContractError, match="non contiene ruoli"):
        routing.parse_routing_plan(doc)


def test_json_contract_empty_roles_raises():
    routing = load_routing()
    doc = (
        "<!-- council-routing-contract:start -->\n"
        '{"schema_version":1,"roles":[]}\n'
        "<!-- council-routing-contract:end -->\n"
    )
    with pytest.raises(routing.RoutingContractError, match="non contiene ruoli"):
        routing.parse_routing_plan(doc)


def test_json_contract_role_without_candidates_raises():
    routing = load_routing()
    doc = (
        "<!-- council-routing-contract:start -->\n"
        '{"schema_version":1,"roles":[{"role":"L-Code","assignment":{},"ranked":[]}]}\n'
        "<!-- council-routing-contract:end -->\n"
    )
    with pytest.raises(routing.RoutingContractError, match="non ha candidati"):
        routing.parse_routing_plan(doc)
