"""Test 14 su agent-doctor.sh: smoke in sandbox.

Nota di adattamento (dichiarata, non nascosta): agent-doctor.sh fa MOLTI check
contro infrastruttura reale hardcoded (porte di servizi locali, un backend
remoto raggiunto via SSH, variabili d'ambiente con URL/token, nomi di skill
specifiche dell'installazione) che NON possono mai passare in una
sandbox sintetica, a prescindere da quanto sia "sana" — non e' questo che il
test #14 deve provare. Il comportamento davvero testabile e specifico della
sandbox e' il meccanismo di drift-detection: iniettare un drift controllabile
(qui: un symlink rotto sotto ~/.agents/skills, esattamente l'esempio del
design) deve far AUMENTARE il numero di FAIL rispetto a una baseline nella
STESSA sandbox. Confrontiamo baseline vs drift, non l'exit code assoluto.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from conftest import run_agent_doctor, run_agent_sync

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="agent-doctor.sh is POSIX-only; B2.5 Windows coverage is agent_sync.py smoke.",
)


def _parse_summary(stdout: str) -> tuple[int, int, int]:
    m = re.search(r"PASS=(\d+)\s+WARN=(\d+)\s+FAIL=(\d+)", stdout)
    assert m, f"riga di riepilogo --summary non trovata:\n{stdout}"
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def test_doctor_smoke_detects_injected_broken_symlink(sandbox):
    sb = sandbox
    for rt in (".claude/skills", ".codex/skills"):
        (sb.home / rt).mkdir(parents=True, exist_ok=True)
    priming = run_agent_sync(sb, "apply")
    assert priming.returncode == 0, priming.stdout + priming.stderr

    baseline = run_agent_doctor(sb, "--summary")
    base_pass, base_warn, base_fail = _parse_summary(baseline.stdout)

    # drift iniettato: un symlink rotto nella library non scoperta.
    library_link = sb.skill_library / "fake-skill-a"
    assert library_link.is_symlink(), "precondizione: la library deve gia' avere il link creato da agent-sync"
    library_link.unlink()
    library_link.symlink_to(sb.home / "questo-target-non-esiste-affatto")

    drifted = run_agent_doctor(sb, "--summary")
    drift_pass, drift_warn, drift_fail = _parse_summary(drifted.stdout)

    assert drift_fail > base_fail, (
        f"il drift iniettato non ha aumentato i FAIL (baseline={base_fail}, dopo drift={drift_fail})\n"
        f"baseline: {baseline.stdout}\ndrift: {drifted.stdout}"
    )
    assert "FAIL:" in drifted.stdout
    assert "fake-skill-a" in drifted.stdout or "ROTTE" in drifted.stdout, drifted.stdout


def test_vault_library_probe_uses_mcp_protocol_headers():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")

    assert "code -X OPTIONS" in bash
    assert "Accept: application/json, text/event-stream" in bash
    assert "httpcode $env:VAULT_LIBRARY_URL" in powershell
    assert "Accept = \"application/json, text/event-stream\"" in powershell
    assert '"Options"' in powershell


def test_doctor_resolves_the_authoritative_remote_from_agent_sync():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")

    assert "config authoritative_remote" in bash
    assert "config authoritative_remote" in powershell
    assert 'KNOWLEDGE_VAULT_REMOTE:-origin' not in bash
    assert 'else { "origin" }' not in powershell


def test_antigravity_quota_is_a_warning_not_a_false_mcp_failure(sandbox):
    agy = sandbox.bin_stubs / "agy"
    agy.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'Error: Individual quota reached. Please upgrade your subscription.'\nexit 1\n",
        encoding="utf-8",
    )
    agy.chmod(0o755)

    result = run_agent_doctor(sandbox, "--strict")

    assert "Antigravity behavioral probe skipped: the selected model quota is unavailable" in result.stdout
    assert "Antigravity behavioral probe does not confirm" not in result.stdout


# ── Local-Only vault-remote sentinel ("local"/"none") ────────────────────
# Architectural-review finding: agent_sync.py's pull()/publish() already
# special-case `env.remote in ("local", "none")` (the Local-Only sentinel
# from USER-PROFILE.md's "Environment variable: KNOWLEDGE_VAULT_REMOTE=local").
# agent-doctor.sh/.ps1 did NOT: the vault section tried a real `git fetch`
# against a remote literally named "local"/"none" (which never exists), and
# the resulting "?" ahead/behind comparison hard-FAILed a correctly
# configured Local-Only install every single run.

def _init_vault_git_repo(sandbox) -> None:
    subprocess.run(["git", "init", "-b", "main", str(sandbox.vault)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(sandbox.vault), "config", "user.email", "nexgen-tests.invalid"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(sandbox.vault), "config", "user.name", "NeXgen tests"],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(sandbox.vault), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(sandbox.vault), "commit", "-m", "fixture"], check=True, capture_output=True)


# These are exactly the connector-gating vars agent-doctor.sh's Mode-gating
# reads (N8N_MCP_TOKEN, and vault-library's own two, kept isolated too for
# the same reason). A dev machine actually running this project's own
# Cloud-Server stack can have these set for real in the ambient shell
# environment that sandbox.env() inherits via dict(os.environ); the
# Mode-gating tests below need deterministic control over them regardless
# of what the host running the suite happens to have configured.
_CONNECTOR_ENV_VARS = ("N8N_MCP_TOKEN", "VAULT_LIBRARY_TOKEN", "VAULT_LIBRARY_URL",
                       "FIRECRAWL_TUNNEL_PORT", "OCR_TUNNEL_PORT")


def _run_doctor(sandbox, *args: str, env_overrides: dict | None = None, timeout: int = 60):
    """Like conftest.run_agent_doctor, but lets a test override env vars
    (KNOWLEDGE_VAULT_REMOTE, connector tokens, ...) -- run_agent_doctor()
    itself calls sandbox.env() with no extra kwargs, and sandbox.env()
    hardcodes KNOWLEDGE_VAULT_REMOTE=local, so overriding it (e.g. to
    "none", or to a real remote name for the Mode-gating tests) requires
    going through sandbox.env() directly. Always starts from a clean slate
    for _CONNECTOR_ENV_VARS (see above), then applies env_overrides on top."""
    sandbox.assert_is_sandbox()
    env = sandbox.env()
    for var in _CONNECTOR_ENV_VARS:
        env.pop(var, None)
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(sandbox.scripts_dir / "agent-doctor.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.parametrize("sentinel", ["local", "none"])
def test_local_only_remote_sentinel_skips_fetch_and_never_fails(sandbox, sentinel):
    _init_vault_git_repo(sandbox)

    result = _run_doctor(sandbox, env_overrides={"KNOWLEDGE_VAULT_REMOTE": sentinel})

    assert "commits behind the cloud" not in result.stdout, result.stdout
    assert "the vault is not a git repo" not in result.stdout, result.stdout
    assert f"Local-Only mode ({sentinel})" in result.stdout, result.stdout


def test_non_local_remote_still_runs_the_real_fetch_ahead_behind_checks(sandbox):
    """Guard against over-fixing: a REAL remote name must still go through
    the fetch/ahead/behind path (and, since no such remote is configured in
    this sandbox, still legitimately FAIL) -- the Local-Only branch must not
    swallow genuine misconfiguration for non-local/non-none remotes."""
    _init_vault_git_repo(sandbox)

    result = _run_doctor(sandbox, env_overrides={"KNOWLEDGE_VAULT_REMOTE": "oracle"})

    assert "Local-Only mode" not in result.stdout, result.stdout
    assert "commits behind the cloud" in result.stdout, result.stdout


# ── Mode-based gating of MCP connector checks ─────────────────────────────
# Architectural-review finding: the "MCP connectors — reachability" and
# "Tokens in env" sections hard-FAILed unconditionally on n8n/firecrawl/
# vault-ocr, with no gating on the Mode declared in USER-PROFILE.md or on
# what the user actually configured -- unlike vault-library, which already
# WARNs (not FAILs) when VAULT_LIBRARY_URL is absent. A Cloud-Server user
# with a real missing connector and a Local-Only user who never needed one
# got the identical FAIL either way.

def _lines_with(stdout: str, marker: str, needle: str) -> list[str]:
    return [line for line in stdout.splitlines() if marker in line and needle in line]


def _stub_curl_always_unreachable(sandbox) -> None:
    """Deterministic http_code stub for agent-doctor's `code()` helper (which
    shells out to curl against hardcoded 127.0.0.1 ports). Some dev machines
    legitimately run real n8n/firecrawl/vault-ocr services on those exact
    ports (5678/33002/33003), which would otherwise make these Mode-gating
    tests flaky depending on what happens to be running locally. Forces
    every reachability probe to read as unreachable (000) regardless of the
    host's real local services, so the tests exercise only the gating LOGIC
    (Mode / env-var presence), never real network state."""
    curl_stub = sandbox.bin_stubs / "curl"
    curl_stub.write_text("#!/bin/sh\nprintf '000'\nexit 0\n", encoding="utf-8")
    curl_stub.chmod(0o755)


def _write_user_profile_mode(sandbox, mode_value: str) -> Path:
    profile = sandbox.vault / "99-INDEX" / "USER-PROFILE.md"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        "# User Profile and Host Awareness\n\n"
        "## Architecture: Local-Only or Cloud-Server\n\n"
        f"- **Mode**: `{mode_value}`\n",
        encoding="utf-8",
    )
    return profile


def test_no_user_profile_treats_missing_connectors_as_ok_not_fail(sandbox):
    """Fresh sandbox has no USER-PROFILE.md at all: Mode must be treated as
    unknown (WARN, never a crash), and n8n/firecrawl/vault-ocr -- genuinely
    unreachable in this sandbox, with no token ever set -- must read as
    correct-by-design (OK), not FAIL."""
    _stub_curl_always_unreachable(sandbox)

    result = _run_doctor(sandbox)

    assert "Mode not found or not parseable" in result.stdout, result.stdout
    assert not _lines_with(result.stdout, "✗", "n8n-mcp (5678)"), result.stdout
    assert not _lines_with(result.stdout, "✗", "firecrawl (33002)"), result.stdout
    assert not _lines_with(result.stdout, "✗", "vault-ocr (33003)"), result.stdout
    assert not _lines_with(result.stdout, "✗", "N8N_MCP_TOKEN"), result.stdout
    assert _lines_with(result.stdout, "✓", "not expected in current Mode"), result.stdout


def test_mode_cloud_server_makes_a_really_missing_connector_fail(sandbox):
    """The bug this closes: a Cloud-Server install with a genuinely missing
    connector (nothing running/configured in this sandbox) must now produce
    a real FAIL instead of the same silent pass a Local-Only install gets."""
    _stub_curl_always_unreachable(sandbox)
    _write_user_profile_mode(sandbox, "CLOUD-SERVER")

    result = _run_doctor(sandbox)

    assert "USER-PROFILE.md declares Mode: CLOUD-SERVER" in result.stdout, result.stdout
    assert _lines_with(result.stdout, "✗", "n8n-mcp (5678)"), result.stdout
    assert _lines_with(result.stdout, "✗", "N8N_MCP_TOKEN missing"), result.stdout


def test_configured_env_var_is_checked_even_when_mode_says_local_only(sandbox):
    """Hard product requirement: Mode is a floor, never a ceiling. A user who
    declares LOCAL-ONLY but has already set N8N_MCP_TOKEN (mid-upgrade, or a
    single cloud connector added while staying local for the rest) must have
    that connector actually checked, not silently waved through just
    because Mode says Local-Only."""
    _stub_curl_always_unreachable(sandbox)
    _write_user_profile_mode(sandbox, "LOCAL-ONLY")

    result = _run_doctor(sandbox, env_overrides={"N8N_MCP_TOKEN": "fake-token-value"})

    assert "USER-PROFILE.md declares Mode: LOCAL-ONLY" in result.stdout, result.stdout
    assert "N8N_MCP_TOKEN present" in result.stdout, result.stdout
    # n8n itself is still unreachable in the sandbox: now expected (the var is
    # set), so it must FAIL for real instead of being waved through by Mode.
    assert _lines_with(result.stdout, "✗", "n8n-mcp (5678)"), result.stdout


# ── vault-library Mode-gating in "Tokens in env" (beta-readiness review,
# 2026-07-13) ───────────────────────────────────────────────────────────
# The bug this closes: unlike N8N_MCP_TOKEN two lines above it, the
# VAULT_LIBRARY_TOKEN/VAULT_LIBRARY_URL loop checked presence
# unconditionally, with no connector_expected() gating at all. A Local-Only
# install configured exactly as vault-write-architecture.md prescribes ("no
# VPS, no vault-library MCP container") got 2 permanent, unfixable FAILs on
# a component the architecture itself declares absent for that Mode.

def test_local_only_vault_library_tokens_not_expected(sandbox):
    _stub_curl_always_unreachable(sandbox)
    _write_user_profile_mode(sandbox, "LOCAL-ONLY")

    result = _run_doctor(sandbox)

    assert "USER-PROFILE.md declares Mode: LOCAL-ONLY" in result.stdout, result.stdout
    assert not _lines_with(result.stdout, "✗", "VAULT_LIBRARY_TOKEN missing"), result.stdout
    assert not _lines_with(result.stdout, "✗", "VAULT_LIBRARY_URL missing"), result.stdout
    assert _lines_with(result.stdout, "✓", "VAULT_LIBRARY_TOKEN not set"), result.stdout
    assert _lines_with(result.stdout, "✓", "VAULT_LIBRARY_URL not set"), result.stdout


def test_cloud_server_missing_vault_library_tokens_really_fail(sandbox):
    """Guard against over-fixing: a genuine Cloud-Server misconfiguration
    (tokens unset, Mode declares CLOUD-SERVER) must still FAIL for real,
    not be silently waved through by the same fix that frees Local-Only."""
    _stub_curl_always_unreachable(sandbox)
    _write_user_profile_mode(sandbox, "CLOUD-SERVER")

    result = _run_doctor(sandbox)

    assert "USER-PROFILE.md declares Mode: CLOUD-SERVER" in result.stdout, result.stdout
    assert _lines_with(result.stdout, "✗", "VAULT_LIBRARY_TOKEN missing"), result.stdout
    assert _lines_with(result.stdout, "✗", "VAULT_LIBRARY_URL missing"), result.stdout


# ── Bearer token off curl's argv (security audit finding, LOW) ───────────
# The vault-library reachability probe used to pass
# `-H "Authorization: Bearer $VAULT_LIBRARY_TOKEN"` straight on curl's argv,
# which any other local user can read via `ps` or /proc/<pid>/cmdline. The
# fix routes it through a curl config file (bearer_cfg(), mode 600) instead.
# This stub curl records every invocation's argv (to prove no argv element
# ever contains "Bearer ...") and, when it sees -K, copies that file's
# content out (to prove the header still actually reaches curl).

_CURL_STUB_PY = """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]

argv_log = os.environ.get("CURL_STUB_ARGV_LOG")
if argv_log:
    with open(argv_log, "a", encoding="utf-8") as f:
        f.write(repr(args) + "\\n")

cfg_log = os.environ.get("CURL_STUB_CFG_LOG")
if cfg_log and "-K" in args:
    cfg_path = args[args.index("-K") + 1]
    try:
        with open(cfg_path, encoding="utf-8") as cf:
            content = cf.read()
    except OSError as exc:
        content = f"<unreadable: {exc}>"
    with open(cfg_log, "a", encoding="utf-8") as f:
        f.write(content)

sys.stdout.write("200" if ("-X" in args and "OPTIONS" in args) else "000")
"""


def _stub_curl_capture_bearer_cfg(sandbox) -> tuple[Path, Path]:
    curl_stub = sandbox.bin_stubs / "curl"
    curl_stub.write_text(_CURL_STUB_PY, encoding="utf-8")
    curl_stub.chmod(0o755)
    return sandbox.home / "curl-argv.log", sandbox.home / "curl-cfg.log"


def test_vault_library_bearer_token_never_appears_in_curl_argv(sandbox):
    argv_log, cfg_log = _stub_curl_capture_bearer_cfg(sandbox)

    result = _run_doctor(
        sandbox,
        env_overrides={
            "VAULT_LIBRARY_URL": "https://vault.example.invalid/mcp",
            "VAULT_LIBRARY_TOKEN": "super-secret-argv-must-not-see-this",
            "CURL_STUB_ARGV_LOG": str(argv_log),
            "CURL_STUB_CFG_LOG": str(cfg_log),
        },
    )

    assert "vault-library: 200 (up)" in result.stdout, result.stdout

    all_argv = argv_log.read_text(encoding="utf-8") if argv_log.exists() else ""
    assert "super-secret-argv-must-not-see-this" not in all_argv, (
        f"bearer token leaked into curl argv:\n{all_argv}"
    )
    assert "-K" in all_argv, f"expected the vault-library probe to use curl's -K/--config:\n{all_argv}"

    cfg_content = cfg_log.read_text(encoding="utf-8") if cfg_log.exists() else ""
    assert "Authorization: Bearer super-secret-argv-must-not-see-this" in cfg_content, (
        f"the -K config file did not carry the expected Authorization header:\n{cfg_content}"
    )
