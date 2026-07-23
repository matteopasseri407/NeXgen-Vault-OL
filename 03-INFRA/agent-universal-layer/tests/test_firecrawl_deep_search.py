"""Regression coverage for the optional Firecrawl deep-search lane."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "03-INFRA" / "deploy"
FIRECRAWL = DEPLOY / "firecrawl"
BASE_COMPOSE = FIRECRAWL / "docker-compose.yml"
SEARCH_COMPOSE = FIRECRAWL / "docker-compose.search.yml"
RENDERER = FIRECRAWL / "searxng" / "render-settings.py"
ENTRYPOINT = FIRECRAWL / "searxng" / "entrypoint.sh"
BOOTSTRAP = DEPLOY / "bootstrap-vps.sh"
SCRIPTS = REPO / "03-INFRA" / "scripts"
HEALTH = SCRIPTS / "firecrawl-search-health.py"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def environment_map(service: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in service.get("environment", []):
        key, _, value = str(entry).partition("=")
        result[key] = value
    return result


def test_search_overlay_is_pinned_private_and_brave_only():
    overlay = load_yaml(SEARCH_COMPOSE)
    api_env = environment_map(overlay["services"]["firecrawl-api"])
    assert api_env["SEARXNG_ENDPOINT"] == "http://firecrawl-searxng:8080"
    assert api_env["SEARXNG_ENGINES"] == "braveapi"
    assert "SEARXNG_CATEGORIES" not in api_env

    searxng = overlay["services"]["firecrawl-searxng"]
    image = searxng["image"]
    digest = image.split("@sha256:", 1)[1].rstrip("}")
    assert len(digest) == 64
    assert all(character in "0123456789abcdef" for character in digest)
    assert "ports" not in searxng
    assert "BRAVE_SEARCH_API_KEY=${BRAVE_SEARCH_API_KEY:?" in "\n".join(searxng["environment"])
    assert "FIRECRAWL_SEARXNG_SECRET=${FIRECRAWL_SEARXNG_SECRET:?" in "\n".join(
        searxng["environment"]
    )


def test_base_stack_does_not_reintroduce_categories_union():
    base = load_yaml(BASE_COMPOSE)
    api_env = environment_map(base["services"]["firecrawl-api"])
    assert "SEARXNG_CATEGORIES" not in api_env


def test_settings_renderer_writes_only_the_expected_engine(tmp_path):
    destination = tmp_path / "settings.yml"
    env = dict(os.environ)
    env.update(
        {
            "BRAVE_SEARCH_API_KEY": "test-brave-key",
            "FIRECRAWL_SEARXNG_SECRET": "test-searxng-secret",
        }
    )
    result = subprocess.run(
        [sys.executable, str(RENDERER), str(destination)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""

    settings = load_yaml(destination)
    assert settings["server"]["secret_key"] == "test-searxng-secret"
    assert settings["server"]["limiter"] is False
    assert settings["search"]["formats"] == ["html", "json"]
    assert settings["engines"] == [
        {
            "name": "braveapi",
            "engine": "braveapi",
            "inactive": False,
            "api_key": "test-brave-key",
            "results_per_page": 20,
        }
    ]
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize("missing", ["BRAVE_SEARCH_API_KEY", "FIRECRAWL_SEARXNG_SECRET"])
def test_settings_renderer_fails_closed_when_a_secret_is_missing(tmp_path, missing):
    env = dict(os.environ)
    env.update(
        {
            "BRAVE_SEARCH_API_KEY": "test-brave-key",
            "FIRECRAWL_SEARXNG_SECRET": "test-searxng-secret",
        }
    )
    env.pop(missing)
    result = subprocess.run(
        [sys.executable, str(RENDERER), str(tmp_path / "settings.yml")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert missing in result.stderr


def test_bootstrap_enables_search_only_for_a_configured_brave_key():
    content = BOOTSTRAP.read_text(encoding="utf-8")
    assert "brave_search_api_key=" in content
    assert "ensure_env_secret FIRECRAWL_SEARXNG_SECRET" in content
    assert "firecrawl/docker-compose.search.yml" in content
    assert 'if [ -n "$brave_search_api_key" ]' in content
    assert "scrape/crawl/map only" in content


def test_cross_platform_wrappers_default_to_twenty_results():
    bash = (SCRIPTS / "firecrawl-local.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "firecrawl-local.ps1").read_text(encoding="utf-8")
    assert "limit=20" in bash
    assert "$limit = 20" in powershell
    assert "search --limit=20" in bash
    assert "search --limit=20" in powershell


def test_public_policy_requires_sources_beyond_search_snippets():
    runbook = (REPO / "03-INFRA" / "firecrawl.md").read_text(encoding="utf-8")
    agents = (
        REPO / "03-INFRA" / "agent-universal-layer" / "instructions" / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert "up to 20 results in the first query" in runbook
    assert "at least 5 useful sources" in runbook
    assert "never stops at the first two snippets" in agents
    assert 'categories=["research"]' in runbook
    assert "post-filter returned URLs" in runbook


class SuccessfulSearchHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        assert self.path == "/v2/search"
        assert payload["limit"] == 1
        assert "site:example.com" in payload["query"]
        assert self.headers["Authorization"] == "Bearer local-self-hosted"
        body = json.dumps(
            {
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "Example Domain",
                            "url": "https://example.com/",
                        }
                    ]
                },
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_end_to_end_health_probe_uses_a_24_hour_success_cache(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), SuccessfulSearchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cache = tmp_path / "firecrawl-search-health.json"
    url = f"http://127.0.0.1:{server.server_port}"

    first = subprocess.run(
        [sys.executable, str(HEALTH), "--url", url, "--cache", str(cache)],
        capture_output=True,
        text=True,
        check=False,
    )
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

    assert first.returncode == 0, first.stdout + first.stderr
    assert json.loads(first.stdout)["status"] == "live"
    assert cache.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600

    second = subprocess.run(
        [
            sys.executable,
            str(HEALTH),
            "--url",
            "http://127.0.0.1:1",
            "--cache",
            str(cache),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(second.stdout)["status"] == "cached"


def test_both_doctors_wire_the_strict_functional_probe():
    bash = (SCRIPTS / "agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (SCRIPTS / "agent-doctor.ps1").read_text(encoding="utf-8")
    for content in (bash, powershell):
        assert "firecrawl-search-health.py" in content
        assert "end-to-end" in content
        assert "24h" in content


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits do not apply on Windows.")
def test_new_posix_entrypoints_are_executable():
    for path in (RENDERER, ENTRYPOINT, HEALTH):
        assert path.stat().st_mode & stat.S_IXUSR, f"{path} must be executable"
