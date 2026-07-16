#!/usr/bin/env python3
"""Executable contracts for the data that drives NeXgen runtime writes.

The engine intentionally keeps user configuration in the private data root.
That makes validation a control-plane concern: malformed data must be rejected
before a renderer, a synchronizer, or a hook mutates a derived runtime file.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import yaml


MCP_SCHEMA_VERSION = 1
COUNCIL_SCHEMA_VERSION = 1
MCP_TARGETS = frozenset({"claude", "codex", "antigravity", "opencode"})
COUNCIL_CLIS = frozenset({"opencode", "agy", "codex", "claude", "ollama"})
COUNCIL_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENTRY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Exact npm package pin (e.g. "firecrawl-mcp@3.22.3" or "@org/pkg@1.2.3"): an
# npx server without this can silently resolve to whatever the registry's
# "latest" happens to be at run time. Kept identical to the constant of the
# same name in tests/test_mcp_package_pins.py (EXACT_NPM_PIN) so the two
# never drift into checking different things.
EXACT_NPM_PIN_RE = re.compile(r"^(?:@[-a-z0-9_.]+/)?[-a-z0-9_.]+@\d+(?:\.\d+){2}$", re.I)

# Heuristic for "this string looks like a literal secret, not a reference".
# Duplicated from mcp/render.py's LONGTOK (import would be circular: render.py
# imports this module) -- keep the two patterns identical if either changes.
LONGTOK_RE = re.compile(r"^[A-Za-z0-9_\-\.=+/]{40,}$")


class ConfigValidationError(ValueError):
    """A user-owned configuration does not satisfy its executable contract."""


def _error(source: str | Path, message: str) -> None:
    raise ConfigValidationError(f"{source}: {message}")


def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        _error(path, f"cannot read {label}: {exc}")
    except yaml.YAMLError as exc:
        _error(path, f"invalid {label} YAML: {exc}")
    if not isinstance(data, dict):
        _error(path, f"{label} root must be a mapping")
    return data


def _reject_unknown_keys(mapping: dict[str, Any], allowed: set[str], source: str | Path, where: str) -> None:
    unknown = sorted(str(key) for key in mapping if key not in allowed)
    if unknown:
        _error(source, f"{where} has unsupported field(s): {', '.join(unknown)}")


def _mapping(value: Any, source: str | Path, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _error(source, f"{where} must be a mapping")
    return value


def _nonempty_string(value: Any, source: str | Path, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _error(source, f"{where} must be a non-empty string")
    return value


def _env_name(value: Any, source: str | Path, where: str) -> str:
    value = _nonempty_string(value, source, where)
    if not ENV_NAME_RE.fullmatch(value):
        _error(source, f"{where} must be a valid environment variable name")
    return value


def _positive_number(value: Any, source: str | Path, where: str) -> float:
    if isinstance(value, bool):
        _error(source, f"{where} must be a finite number greater than zero")
    try:
        number = float(value)
    except (TypeError, ValueError):
        _error(source, f"{where} must be a finite number greater than zero")
    if not math.isfinite(number) or number <= 0:
        _error(source, f"{where} must be a finite number greater than zero")
    return number


def _string_list(value: Any, source: str | Path, where: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        _error(source, f"{where} must be a list of non-empty strings")
    if not allow_empty and not value:
        _error(source, f"{where} must not be empty")
    return value


def _routing_config(value: Any, source: str | Path) -> None:
    routing = _mapping(value, source, "Council routing config")
    _reject_unknown_keys(routing, {"enabled", "decision_file", "mode_defaults", "relay_roles"}, source, "Council routing config")
    if type(routing.get("enabled")) is not bool:
        _error(source, "Council routing config.enabled must be true or false")
    if routing["enabled"]:
        decision_file = _nonempty_string(routing.get("decision_file"), source, "Council routing config.decision_file")
        path = PurePosixPath(decision_file)
        windows_path = PureWindowsPath(decision_file)
        if path.is_absolute() or windows_path.is_absolute() or "\\" in decision_file or ".." in path.parts:
            _error(source, "Council routing config.decision_file must be a relative Vault path without '..'")
    elif "decision_file" in routing:
        _nonempty_string(routing["decision_file"], source, "Council routing config.decision_file")
    if "mode_defaults" in routing:
        defaults = _mapping(routing["mode_defaults"], source, "Council routing config.mode_defaults")
        allowed_modes = {"brainstorm", "challenge", "code-review"}
        _reject_unknown_keys(defaults, allowed_modes, source, "Council routing config.mode_defaults")
        for mode, role in defaults.items():
            _nonempty_string(role, source, f"Council routing config.mode_defaults.{mode}")
    if "relay_roles" in routing:
        _string_list(routing["relay_roles"], source, "Council routing config.relay_roles", allow_empty=False)


def _looks_like_env_reference(value: str) -> bool:
    """True if a stdio env value defers to another env var instead of
    embedding a value directly (render.py's own redact() uses this same
    signal to decide what is safe to print)."""
    return "${" in value or "{env:" in value


def _looks_like_secret_literal(value: str) -> bool:
    """Same heuristic as render.py's redact(): a long token-charset string
    that contains at least one digit reads as a pasted credential, not
    ordinary config data (hostnames, flags, short numbers stay well under the
    40-char floor)."""
    return bool(LONGTOK_RE.match(value)) and any(c.isdigit() for c in value)


def _env_mapping(value: Any, source: str | Path, where: str) -> dict[str, str]:
    mapping = _mapping(value, source, where)
    for key, item in mapping.items():
        _env_name(key, source, f"{where} key")
        if not isinstance(item, str):
            _error(source, f"{where}.{key} must be a string")
        if _looks_like_secret_literal(item) and not _looks_like_env_reference(item):
            _error(
                source,
                f"{where}.{key} looks like a literal secret value, not a reference -- "
                "point it at an environment variable (e.g. \"${VAR}\") instead of embedding the value",
            )
    return mapping


def _validate_auth(value: Any, source: str | Path, where: str) -> None:
    auth = _mapping(value, source, where)
    _reject_unknown_keys(auth, {"type", "env"}, source, where)
    if auth.get("type") != "bearer":
        _error(source, f"{where}.type must be 'bearer'")
    _env_name(auth.get("env"), source, f"{where}.env")


def _validate_timeouts(value: Any, source: str | Path, where: str) -> None:
    timeouts = _mapping(value, source, where)
    _reject_unknown_keys(timeouts, {"startup", "tool"}, source, where)
    if not timeouts:
        _error(source, f"{where} must contain startup and/or tool")
    for key, item in timeouts.items():
        _positive_number(item, source, f"{where}.{key}")


def _validate_npx_pin(args: Any, source: str | Path, where: str) -> None:
    """An npx stdio server without an exact version pin resolves to whatever
    "latest" is on the npm registry at process-start time -- a silent
    supply-chain door. Mirrors tests/test_mcp_package_pins.py's
    EXACT_NPM_PIN check, but as a real validation gate: that test only ever
    ran against the repo's template manifest, never against the actual
    manifest.yaml render.py loads from AGENT_VAULT_DATA at runtime."""
    values = args if isinstance(args, list) else []
    package = next((item for item in values if isinstance(item, str) and not item.startswith("-")), None)
    if package is None or not EXACT_NPM_PIN_RE.fullmatch(package):
        _error(
            source,
            f"{where}.args must pin the npx package to an exact version (e.g. 'package@1.2.3' "
            f"or '@scope/package@1.2.3'), got {package!r}",
        )


def _validate_mcp_server(
    server: Any,
    source: str | Path,
    where: str,
    *,
    allow_windows: bool,
) -> None:
    spec = _mapping(server, source, where)
    allowed = {
        "transport",
        "command",
        "args",
        "env",
        "require_env",
        "targets",
        "url",
        "url_env",
        "auth",
        "timeouts",
    }
    if allow_windows:
        allowed.add("windows")
    _reject_unknown_keys(spec, allowed, source, where)

    transport = spec.get("transport")
    if transport not in {"stdio", "http"}:
        _error(source, f"{where}.transport must be 'stdio' or 'http'")
    targets = _string_list(spec.get("targets"), source, f"{where}.targets", allow_empty=False)
    if len(set(targets)) != len(targets):
        _error(source, f"{where}.targets must not contain duplicates")
    unsupported_targets = sorted(set(targets) - MCP_TARGETS)
    if unsupported_targets:
        _error(source, f"{where}.targets contains unsupported CLI(s): {', '.join(unsupported_targets)}")

    if "require_env" in spec:
        _env_name(spec["require_env"], source, f"{where}.require_env")
    if "timeouts" in spec:
        _validate_timeouts(spec["timeouts"], source, f"{where}.timeouts")

    if transport == "stdio":
        command = _nonempty_string(spec.get("command"), source, f"{where}.command")
        args = spec.get("args")
        if "args" in spec:
            args = _string_list(spec["args"], source, f"{where}.args")
        if command == "npx":
            _validate_npx_pin(args, source, where)
        if "env" in spec:
            _env_mapping(spec["env"], source, f"{where}.env")
        for field in ("url", "url_env", "auth"):
            if field in spec:
                _error(source, f"{where}.{field} is only valid for transport 'http'")
    else:
        _nonempty_string(spec.get("url"), source, f"{where}.url")
        _validate_auth(spec.get("auth"), source, f"{where}.auth")
        if "url_env" in spec:
            _env_name(spec["url_env"], source, f"{where}.url_env")
        for field in ("command", "args", "env"):
            if field in spec:
                _error(source, f"{where}.{field} is only valid for transport 'stdio'")

    if allow_windows and "windows" in spec:
        windows = _mapping(spec["windows"], source, f"{where}.windows")
        override_fields = {
            "command",
            "args",
            "env",
            "require_env",
            "url",
            "url_env",
            "auth",
            "timeouts",
        }
        _reject_unknown_keys(windows, override_fields, source, f"{where}.windows")
        merged = {key: value for key, value in spec.items() if key != "windows"}
        merged.update(windows)
        _validate_mcp_server(merged, source, f"{where}.windows", allow_windows=False)


def validate_mcp_manifest(data: Any, source: str | Path) -> dict[str, dict[str, Any]]:
    manifest = _mapping(data, source, "MCP manifest")
    _reject_unknown_keys(manifest, {"schema_version", "servers", "retired_servers"}, source, "MCP manifest")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != MCP_SCHEMA_VERSION:
        _error(source, f"MCP manifest schema_version must be {MCP_SCHEMA_VERSION}")
    servers = _mapping(manifest.get("servers"), source, "MCP manifest.servers")
    retired = _string_list(
        manifest.get("retired_servers", []),
        source,
        "MCP manifest.retired_servers",
    )
    if len(set(retired)) != len(retired):
        _error(source, "MCP manifest.retired_servers must not contain duplicates")
    for name in retired:
        if not ENTRY_NAME_RE.fullmatch(name):
            _error(source, "every retired MCP server name must use letters, digits, '.', '_' or '-'")
        if name in servers:
            _error(source, f"MCP server '{name}' cannot be both active and retired")
    for name, server in servers.items():
        if not isinstance(name, str) or not ENTRY_NAME_RE.fullmatch(name):
            _error(source, "every MCP server name must use letters, digits, '.', '_' or '-'")
        _validate_mcp_server(server, source, f"MCP server '{name}'", allow_windows=True)
    # Codex maps hyphens to underscores in TOML table names. Two otherwise
    # valid manifest names can therefore collapse to one live key and launch
    # duplicate or ambiguous clients. Reject the collision before any writer
    # touches a runtime config.
    codex_keys: dict[str, str] = {}
    for name, server in servers.items():
        if "codex" not in server.get("targets", []):
            continue
        key = name.replace("-", "_").casefold()
        previous = codex_keys.get(key)
        if previous is not None and previous != name:
            _error(
                source,
                f"MCP servers '{previous}' and '{name}' collide as Codex key '{key}'",
            )
        codex_keys[key] = name
    for name in retired:
        key = name.replace("-", "_").casefold()
        active = codex_keys.get(key)
        if active is not None:
            _error(
                source,
                f"retired MCP server '{name}' collides with active Codex server '{active}' as key '{key}'",
            )
    return servers


def load_mcp_manifest_document(path: Path) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    data = _load_yaml_mapping(path, "MCP manifest")
    servers = validate_mcp_manifest(data, path)
    return servers, tuple(data.get("retired_servers", []))


def load_mcp_manifest(path: Path) -> dict[str, dict[str, Any]]:
    return load_mcp_manifest_document(path)[0]


def _sequence_candidates(value: Any, source: str | Path, where: str) -> list[str]:
    if isinstance(value, str):
        item = value.strip()
        if not item or "=" not in item or "," in item:
            _error(source, f"{where} string must use role=seat or role=seat|fallback")
        role, candidates = item.split("=", 1)
        if not role.strip():
            _error(source, f"{where} needs a non-empty role")
        names = [name.strip() for name in candidates.split("|") if name.strip()]
        if not names:
            _error(source, f"{where} needs at least one seat")
        return names

    stage = _mapping(value, source, where)
    _reject_unknown_keys(stage, {"role", "seat", "seats", "fallback"}, source, where)
    _nonempty_string(stage.get("role"), source, f"{where}.role")
    has_seat = "seat" in stage
    has_seats = "seats" in stage
    if has_seat == has_seats:
        _error(source, f"{where} needs exactly one of seat or seats")
    if has_seat:
        candidates = [_nonempty_string(stage["seat"], source, f"{where}.seat")]
    else:
        candidates = _string_list(stage["seats"], source, f"{where}.seats", allow_empty=False)
    if "fallback" in stage:
        fallback = stage["fallback"]
        if isinstance(fallback, str):
            candidates.append(_nonempty_string(fallback, source, f"{where}.fallback"))
        else:
            candidates.extend(_string_list(fallback, source, f"{where}.fallback", allow_empty=False))
    return candidates


def _validate_sequence(value: Any, source: str | Path, where: str) -> list[str]:
    if not isinstance(value, list) or not value:
        _error(source, f"{where} must be a non-empty list of relay stages")
    return [candidate for index, item in enumerate(value) for candidate in _sequence_candidates(item, source, f"{where}[{index}]")]


def validate_council_config(data: Any, source: str | Path) -> dict[str, Any]:
    config = _mapping(data, source, "Council seats config")
    _reject_unknown_keys(config, {"schema_version", "seats", "sequence", "sequences", "routing"}, source, "Council seats config")
    if type(config.get("schema_version")) is not int or config["schema_version"] != COUNCIL_SCHEMA_VERSION:
        _error(source, f"Council seats config schema_version must be {COUNCIL_SCHEMA_VERSION}")
    seats = _mapping(config.get("seats"), source, "Council seats config.seats")
    for name, seat in seats.items():
        if not isinstance(name, str) or not ENTRY_NAME_RE.fullmatch(name):
            _error(source, "every Council seat name must use letters, digits, '.', '_' or '-'")
        spec = _mapping(seat, source, f"Council seat '{name}'")
        _reject_unknown_keys(
            spec,
            {
                "vendor", "cli", "model", "quota_pool", "timeout_seconds", "zero_retention",
                "routing_id", "routing_label", "reasoning_effort",
            },
            source,
            f"Council seat '{name}'",
        )
        _nonempty_string(spec.get("vendor"), source, f"Council seat '{name}'.vendor")
        cli = _nonempty_string(spec.get("cli"), source, f"Council seat '{name}'.cli")
        if cli not in COUNCIL_CLIS:
            _error(source, f"Council seat '{name}'.cli must be one of: {', '.join(sorted(COUNCIL_CLIS))}")
        _nonempty_string(spec.get("model"), source, f"Council seat '{name}'.model")
        if type(spec.get("zero_retention")) is not bool:
            _error(source, f"Council seat '{name}'.zero_retention must be true or false")
        if "quota_pool" in spec:
            _nonempty_string(spec["quota_pool"], source, f"Council seat '{name}'.quota_pool")
        if "timeout_seconds" in spec:
            _positive_number(spec["timeout_seconds"], source, f"Council seat '{name}'.timeout_seconds")
        if "routing_id" in spec:
            _nonempty_string(spec["routing_id"], source, f"Council seat '{name}'.routing_id")
        if "routing_label" in spec:
            _nonempty_string(spec["routing_label"], source, f"Council seat '{name}'.routing_label")
        if "reasoning_effort" in spec:
            effort = _nonempty_string(spec["reasoning_effort"], source, f"Council seat '{name}'.reasoning_effort")
            if effort not in COUNCIL_REASONING_EFFORTS:
                _error(
                    source,
                    f"Council seat '{name}'.reasoning_effort must be one of: {', '.join(sorted(COUNCIL_REASONING_EFFORTS))}",
                )

    if "routing" in config:
        _routing_config(config["routing"], source)

    references: list[str] = []
    if "sequence" in config:
        references.extend(_validate_sequence(config["sequence"], source, "Council seats config.sequence"))
    if "sequences" in config:
        sequences = _mapping(config["sequences"], source, "Council seats config.sequences")
        for name, sequence in sequences.items():
            if not isinstance(name, str) or not name.strip():
                _error(source, "every named Council sequence needs a non-empty name")
            references.extend(_validate_sequence(sequence, source, f"Council sequence '{name}'"))
    unknown_references = sorted(set(references) - set(seats))
    if unknown_references:
        _error(source, f"Council sequence references unknown seat(s): {', '.join(unknown_references)}")
    return config


def load_council_config(path: Path) -> dict[str, Any]:
    return validate_council_config(_load_yaml_mapping(path, "Council seats config"), path)


def validate_claude_settings(path: Path) -> None:
    """Validate only the part of Claude settings that NeXgen may merge.

    Claude owns the rest of settings.json. Rejecting an unrelated future key
    would be brittle, but an invalid hooks shape would otherwise let a run copy
    the hook file and fail only halfway through its own mutation.
    """
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _error(path, f"invalid Claude settings JSON: {exc}")
    if not isinstance(data, dict):
        _error(path, "Claude settings root must be an object")
    hooks = data.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        _error(path, "Claude settings hooks must be an object")
    for event in ("SessionStart", "PreCompact"):
        if event not in hooks:
            continue
        matchers = hooks[event]
        if not isinstance(matchers, list):
            _error(path, f"Claude settings hooks.{event} must be a list")
        for matcher_index, matcher in enumerate(matchers):
            if not isinstance(matcher, dict):
                _error(path, f"Claude settings hooks.{event}[{matcher_index}] must be an object")
            entries = matcher.get("hooks", [])
            if not isinstance(entries, list):
                _error(path, f"Claude settings hooks.{event}[{matcher_index}].hooks must be a list")
            for hook_index, hook in enumerate(entries):
                if not isinstance(hook, dict):
                    _error(
                        path,
                        f"Claude settings hooks.{event}[{matcher_index}].hooks[{hook_index}] must be an object",
                    )
