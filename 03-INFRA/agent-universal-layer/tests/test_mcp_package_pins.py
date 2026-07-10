"""Regression tests for local MCP package supply-chain pins."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "03-INFRA" / "agent-universal-layer" / "mcp" / "manifest.yaml"
RENDER = REPO / "03-INFRA" / "agent-universal-layer" / "mcp" / "render.py"
EXACT_NPM_PIN = re.compile(r"^(?:@[-a-z0-9_.]+/)?[-a-z0-9_.]+@\d+(?:\.\d+){2}$", re.I)


def _is_exact_npm_pin(package: str) -> bool:
    return bool(EXACT_NPM_PIN.fullmatch(package))


def test_manifest_pins_every_npx_package():
    servers = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["servers"]

    for name, server in servers.items():
        if server.get("command") != "npx":
            continue
        package = next(arg for arg in server["args"] if not arg.startswith("-"))
        assert _is_exact_npm_pin(package), f"{name}: npx package must use an exact version, got {package!r}"


def test_antigravity_http_bridge_is_pinned():
    tree = ast.parse(RENDER.read_text(encoding="utf-8"))
    package = next(
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "MCP_REMOTE_PACKAGE"
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    )

    assert _is_exact_npm_pin(package), f"mcp-remote must use an exact version, got {package!r}"
    bridge = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "r_antigravity"
    )
    assert any(
        isinstance(node, ast.Name) and node.id == "MCP_REMOTE_PACKAGE"
        for node in ast.walk(bridge)
    ), "r_antigravity must render the pinned mcp-remote package"
