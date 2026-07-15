"""Regression tests for local MCP package supply-chain pins."""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "03-INFRA" / "agent-universal-layer" / "mcp" / "manifest.yaml"
RENDER = REPO / "03-INFRA" / "agent-universal-layer" / "mcp" / "render.py"
PLAYWRIGHT_WRAPPER = REPO / "03-INFRA" / "agent-universal-layer" / "mcp" / "playwright-human-safe.mjs"
EXACT_NPM_PIN = re.compile(r"^(?:@[-a-z0-9_.]+/)?[-a-z0-9_.]+@\d+(?:\.\d+){2}$", re.I)
NPM_COLD_START_TIMEOUT = 120


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


def test_playwright_wrapper_and_manifest_share_an_exact_pin():
    wrapper = PLAYWRIGHT_WRAPPER.read_text(encoding="utf-8")
    match = re.search(r"const VERSION = '([^']+)';", wrapper)
    assert match, "Playwright wrapper must declare its reviewed upstream version"
    assert _is_exact_npm_pin(f"@playwright/mcp@{match.group(1)}")

    server = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["servers"]["playwright"]
    assert server["command"] == "npx"
    assert f"@playwright/mcp@{match.group(1)}" in server["args"]
    assert server["windows"]["command"] == "node"
    assert any("playwright-human-safe.mjs" in arg for arg in server["windows"]["args"])


def test_playwright_wrapper_can_resolve_npm_without_spawning_a_cmd_shim():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed on this test host")
    result = subprocess.run(
        [node, str(PLAYWRIGHT_WRAPPER), "--self-test"],
        capture_output=True,
        text=True,
        # A GitHub-hosted Windows runner starts with an empty npm cache. This
        # self-test deliberately prepares the exact reviewed package before
        # validating its bundle, so it needs the same cold-network budget as
        # the explicit overlong-PATH regression below.
        timeout=NPM_COLD_START_TIMEOUT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="The cmd.exe PATH ceiling is Windows-specific.")
def test_playwright_wrapper_cold_cache_survives_an_overlong_windows_path(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed on this test host")
    env = os.environ.copy()
    path_key = next((key for key in env if key.lower() == "path"), "PATH")
    env[path_key] = ("X" * 9000) + os.pathsep + env.get(path_key, "")
    env["npm_config_cache"] = str(tmp_path / "npm-cache")

    result = subprocess.run(
        [node, str(PLAYWRIGHT_WRAPPER), "--self-test"],
        capture_output=True,
        text=True,
        timeout=NPM_COLD_START_TIMEOUT,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
