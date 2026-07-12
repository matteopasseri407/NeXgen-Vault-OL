"""Regression tests for the deploy profile's auth hardening pass
(2026-07-12, NX-audit findings A/B/C in 03-INFRA/deploy/):

  A. n8n/docker-compose.yml actually passes N8N_MCP_TOKEN through to the
     n8n container (it used to be commented out, so filling .env had no
     effect at all).
  B. Vault OCR (ocr/api/app.py) gates POST /ocr behind an optional
     VAULT_OCR_TOKEN bearer token (see ocr/api/tests/test_app.py for the
     behavioral tests; this file only checks the wiring: the compose file
     passes the var through, and .env.example documents it).
     Firecrawl's own HTTP API has no viable native auth in this
     self-hosted build without an external Supabase project -- documented
     in firecrawl/docker-compose.yml's header comment, not silently
     dropped.
  C. firecrawl-redis now requires a password (requirepass), instead of
     relying on Docker network isolation alone.

Docker itself is not available in this test environment (no daemon, no
registry access), so compose validity is checked with PyYAML rather than
`docker compose config` -- CI adds that step separately.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "03-INFRA" / "deploy"
N8N_COMPOSE = DEPLOY / "n8n" / "docker-compose.yml"
OCR_COMPOSE = DEPLOY / "ocr" / "docker-compose.yml"
FIRECRAWL_COMPOSE = DEPLOY / "firecrawl" / "docker-compose.yml"
ENV_EXAMPLE = DEPLOY / ".env.example"
BOOTSTRAP = DEPLOY / "bootstrap-vps.sh"


def _load_compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _env_list_to_dict(environment) -> dict[str, str]:
    """Normalizes a compose `environment:` block (list-of-"K=V" or
    mapping form) to a plain dict, same as docker compose itself accepts
    either style."""
    if isinstance(environment, dict):
        return {k: str(v) for k, v in environment.items()}
    out: dict[str, str] = {}
    for item in environment or []:
        key, _, value = str(item).partition("=")
        out[key] = value
    return out


# --- Finding A: n8n MCP token actually reaches the container. --------------


def test_n8n_compose_passes_mcp_token_to_the_container():
    data = _load_compose(N8N_COMPOSE)
    env = _env_list_to_dict(data["services"]["n8n"]["environment"])
    assert "N8N_MCP_TOKEN" in env, "N8N_MCP_TOKEN is missing or still commented out"
    assert "N8N_MCP_TOKEN" in env["N8N_MCP_TOKEN"], (
        f"N8N_MCP_TOKEN should read from the env var (${{N8N_MCP_TOKEN}}), "
        f"got {env['N8N_MCP_TOKEN']!r}"
    )


def test_n8n_compose_has_no_commented_out_mcp_token_line():
    """Regression for the exact original bug: a `# - N8N_MCP_TOKEN=...`
    line that LOOKS configured in the file but is inert."""
    content = N8N_COMPOSE.read_text(encoding="utf-8")
    assert not re.search(r"^\s*#\s*-\s*N8N_MCP_TOKEN=", content, re.MULTILINE), (
        "found a commented-out N8N_MCP_TOKEN line -- it must be active"
    )


# --- Finding B: Vault OCR bearer token wiring. ------------------------------


def test_ocr_compose_passes_token_through():
    data = _load_compose(OCR_COMPOSE)
    env = _env_list_to_dict(data["services"]["vault-ocr-api"]["environment"])
    assert "VAULT_OCR_TOKEN" in env
    assert "VAULT_OCR_TOKEN" in env["VAULT_OCR_TOKEN"]


def test_env_example_documents_vault_ocr_token():
    content = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"^VAULT_OCR_TOKEN=", content, re.MULTILINE)


def test_ocr_mcp_client_reads_the_same_token_var():
    """The MCP client (../ocr/mcp/vault_ocr_mcp.py) must read the same
    VAULT_OCR_TOKEN the API checks, or a deploy that sets one on the server
    side silently locks out its only real client."""
    client = (DEPLOY / "ocr" / "mcp" / "vault_ocr_mcp.py").read_text(encoding="utf-8")
    assert 'os.environ.get("VAULT_OCR_TOKEN"' in client
    assert "Authorization" in client and "Bearer" in client


def test_firecrawl_compose_documents_the_no_native_auth_finding():
    """Firecrawl's own API has no viable native token gate in this
    self-hosted build (confirmed against upstream source, see the header
    comment) -- this must stay an explicit, documented conclusion in the
    compose file, not silently dropped from the audit trail."""
    content = FIRECRAWL_COMPOSE.read_text(encoding="utf-8")
    assert "USE_DB_AUTHENTICATION" in content
    assert "Supabase" in content


# --- Finding C: firecrawl-redis requires a password. ------------------------


def test_firecrawl_redis_requires_a_password():
    data = _load_compose(FIRECRAWL_COMPOSE)
    redis_cfg = data["services"]["firecrawl-redis"]
    command = redis_cfg.get("command")
    assert command, "firecrawl-redis has no command: (no requirepass wired)"
    joined = " ".join(str(c) for c in command)
    assert "--requirepass" in joined
    assert "FIRECRAWL_REDIS_PASSWORD" in joined


def test_firecrawl_api_and_worker_connect_to_redis_with_credentials():
    data = _load_compose(FIRECRAWL_COMPOSE)
    for service in ("firecrawl-api", "firecrawl-worker"):
        env = _env_list_to_dict(data["services"][service]["environment"])
        assert "REDIS_URL" in env, f"{service}: no REDIS_URL"
        redis_url = env["REDIS_URL"]
        assert "FIRECRAWL_REDIS_PASSWORD" in redis_url, (
            f"{service}: REDIS_URL does not reference FIRECRAWL_REDIS_PASSWORD: {redis_url!r}"
        )
        # redis://:password@host:port -- a colon right after the scheme's
        # "//" marks a password-only auth URL, not just a bare host.
        assert re.search(r"redis://:", redis_url), (
            f"{service}: REDIS_URL is not in the redis://:<password>@host form: {redis_url!r}"
        )


def test_firecrawl_redis_password_interpolation_fails_fast_if_unset():
    """Uses Compose's `${VAR:?message}` form everywhere the password is
    referenced, so an operator who skips bootstrap-vps.sh (and therefore
    never gets FIRECRAWL_REDIS_PASSWORD auto-generated) gets a loud compose
    error instead of a silently unauthenticated Redis."""
    content = FIRECRAWL_COMPOSE.read_text(encoding="utf-8")
    occurrences = content.count("${FIRECRAWL_REDIS_PASSWORD:?")
    # firecrawl-redis's own command + REDISCLI_AUTH, plus REDIS_URL on both
    # firecrawl-api and firecrawl-worker = 4 references, all fail-fast.
    assert occurrences >= 4, f"expected >=4 fail-fast FIRECRAWL_REDIS_PASSWORD references, found {occurrences}"


def test_env_example_documents_firecrawl_redis_password():
    content = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"^FIRECRAWL_REDIS_PASSWORD=", content, re.MULTILINE)


def test_bootstrap_vps_auto_generates_the_redis_password():
    """Rule: never a secret Matteo has to invent or remember by hand --
    bootstrap-vps.sh must generate it programmatically the first time."""
    content = BOOTSTRAP.read_text(encoding="utf-8")
    assert "openssl rand" in content
    assert re.search(r"ensure_env_secret\s+FIRECRAWL_REDIS_PASSWORD", content)


def test_all_three_auth_touched_compose_files_still_load_with_pyyaml():
    """Sanity check for the interpolation syntax added above (${VAR:?msg}
    inside quoted YAML strings): the file must still be valid YAML. This
    does not validate compose's own variable-interpolation grammar --
    `docker compose config` does that in CI, where Docker is available."""
    for path in (N8N_COMPOSE, OCR_COMPOSE, FIRECRAWL_COMPOSE):
        data = _load_compose(path)
        assert "services" in data and data["services"]
