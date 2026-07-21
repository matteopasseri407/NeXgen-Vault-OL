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

import json
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


def test_doctor_warns_when_opencode_loads_canonical_instructions_twice(sandbox_with_live_configs):
    sandbox = sandbox_with_live_configs
    config_path = sandbox.live_config_path("opencode")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    canonical = "~/KnowledgeVault/03-INFRA/agent-universal-layer/instructions/AGENTS.md"
    config["instructions"] = [canonical, canonical.replace("/", "\\")]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    result = run_agent_doctor(sandbox)

    assert "OpenCode loads the canonical AGENTS.md 2 times" in result.stdout


def test_doctor_does_not_judge_host_local_claude_permissions(sandbox):
    # Permission posture is a host-local choice the engine must not grade:
    # bypassPermissions, a suppressed dangerous-mode prompt, and persistent
    # allow rules are all legitimate depending on the machine. The doctor
    # never comments on them (0.91.3 dropped the Claude-only judgement).
    claude_dir = sandbox.home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(
        json.dumps({
            "permissions": {"defaultMode": "bypassPermissions"},
            "skipDangerousModePermissionPrompt": True,
        }),
        encoding="utf-8",
    )
    (claude_dir / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git:*)", "Bash(docker:*)"]}}),
        encoding="utf-8",
    )

    result = run_agent_doctor(sandbox)

    assert "bypassPermissions" not in result.stdout
    assert "dangerous-mode" not in result.stdout
    assert "unmanaged persistent allow rule" not in result.stdout
    assert "Claude security posture" not in result.stdout


def test_vault_library_probe_uses_mcp_protocol_headers():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")

    assert "code -X OPTIONS" in bash
    assert "--server-url vault-library" in bash
    assert "Accept: application/json, text/event-stream" in bash
    assert "httpcode $VaultLibraryUrl" in powershell
    assert "--server-url vault-library" in powershell
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


# ── --strict expected-server set now derived from the manifest, not
# hardcoded (beta-readiness review, 2026-07-13) ───────────────────────────
# The bug this closes: agent-doctor.sh (5 spots) and agent-doctor.ps1 (1
# shared literal, reused in 3 places) both hardcoded
# {firecrawl, n8n-mcp, vault-library, vault-ocr} for the --strict consumer
# checks. A manifest change (a server added/removed, a require_env gate
# flipped) silently went stale: the doctor kept checking for a set that no
# longer matched what agent-sync actually writes. Both twins now derive it
# at runtime via render.py --expected-servers, computed once near the top
# of the --strict block.

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def test_strict_block_source_no_longer_hardcodes_the_4_server_set():
    """Content assertion on the SOURCE (not a run): the literal set/loop
    that used to enumerate {firecrawl, n8n-mcp, vault-library, vault-ocr}
    for the --strict checks must be gone from both twins, replaced by a
    render.py --expected-servers call."""
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")

    assert '{"firecrawl", "n8n-mcp", "vault-library", "vault-ocr"}' not in bash
    assert "for srv in firecrawl n8n-mcp vault-library vault-ocr" not in bash
    assert 'ag_probe_best_missing="firecrawl, n8n-mcp, vault-library, vault-ocr"' not in bash
    assert '@("firecrawl", "n8n-mcp", "vault-library", "vault-ocr")' not in powershell

    assert "--expected-servers antigravity" in bash
    assert "--expected-servers opencode" in bash
    assert "--expected-servers antigravity" in powershell
    assert "--expected-servers opencode" in powershell


def test_strict_block_skips_explicitly_when_the_expected_set_cant_be_derived():
    """Empty/undeliverable expected-server set (python3/python missing,
    render.py missing, or a genuinely empty manifest result) must produce an
    explicit skip/warn in both twins, never a silent pass -- comparing
    live config content against an EMPTY expected set would otherwise
    trivially "match" and read as a false green."""
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")

    assert "python3 not found -- cannot derive the expected MCP server set" in bash
    assert "Python 3 with PyYAML not found -- cannot derive the expected MCP server set" in powershell
    assert "render.py not found" in bash
    assert "render.py not found" in powershell
    for text in (bash, powershell):
        assert "no expected Antigravity MCP servers derived from the manifest" in text
        assert "no expected OpenCode MCP servers derived from the manifest" in text


def test_ps1_has_windows_path_persistence_check():
    """Release-critical check (Task 5, external-architect review accepted
    2026-07-13): the PRIMARY signal is actually resolving 'agent-sync' as a
    bare command in a FRESH process (execution-policy/PATHEXT problems
    surface this way, a PATH string substring match does not); the
    registry value (HKCU:\\Environment) and this process's own $env:Path
    remain as fallback/diagnostic detail only, not an alternate pass path.
    The plain remediation text stays the same either way."""
    repo = Path(__file__).resolve().parents[3]
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")

    start = powershell.index("Windows PATH persistence")
    end = powershell.index("Architecture Mode", start)
    section = powershell[start:end]

    assert "Get-Command agent-sync" in section, "the primary probe must actually resolve the bare command"
    assert "-NoProfile" in section
    assert "HKCU:\\Environment" in section
    assert "Get-ItemProperty" in section
    assert ".local\\bin" in section
    assert "$env:Path" in section
    assert "run agent-sync apply, then open a NEW terminal" in section
    # the fresh-process resolution result must gate the pass/fail, not the
    # registry/process PATH detail alone.
    assert powershell.index("$freshProbeOk = ($LASTEXITCODE") < powershell.index('ok "agent-sync resolves')


def test_vault_push_remediation_text_matches_between_twins():
    """Task 6: both twins must say the same thing for the dangling-commits
    remediation, and it must stay nested under the non-Local-Only branch
    (Local-Only has no authoritative remote to be 'ahead' of)."""
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")

    assert "RUN vault-push!" in bash
    assert "RUN vault-push!" in powershell
    # sanity: the remediation line sits after the Local-Only sentinel branch
    # in both files, not before it (the ordering is what keeps Local-Only
    # from ever hitting this remediation).
    assert bash.index("Local-Only mode (") < bash.index("RUN vault-push!")
    assert powershell.index("Local-Only mode (") < powershell.index("RUN vault-push!")


def test_doctor_strict_derives_expected_servers_from_manifest_not_hardcoded(sandbox):
    """Behavioral proof (not just a source grep): the --strict OpenCode
    check must report the SANDBOX's synthetic manifest server names
    (fake-*), not a residual hardcoded {firecrawl, n8n-mcp, vault-library,
    vault-ocr} set that would happen to still look plausible."""
    opencode = sandbox.bin_stubs / "opencode"
    opencode.write_text("#!/bin/sh\necho 'nothing connected'\nexit 0\n", encoding="utf-8")
    opencode.chmod(0o755)
    # A real `agy` on the host running this test (this project's own dev
    # machine has one) would otherwise get invoked for real -- a live model
    # call with a 45s timeout, tried twice -- and blow the test's own
    # subprocess timeout. Stub it out fast and deterministic, same as
    # test_antigravity_quota_is_a_warning_not_a_false_mcp_failure above.
    agy = sandbox.bin_stubs / "agy"
    agy.write_text("#!/bin/sh\nprintf '%s\\n' 'fake-stdio-tool' 'fake-http-api' 'fake-cross-os-tool'\nexit 0\n", encoding="utf-8")
    agy.chmod(0o755)

    result = run_agent_doctor(sandbox, "--strict")
    clean = _strip_ansi(result.stdout)
    start = clean.index("CLI consumer conformance (--strict)")
    end = clean.index("Shared browser and defaults", start)
    strict_section = clean[start:end]

    assert "OpenCode mcp list does not confirm:" in strict_section, strict_section
    assert "fake-stdio-tool" in strict_section or "fake-http-api" in strict_section, strict_section
    assert "firecrawl" not in strict_section, strict_section
    assert "n8n-mcp" not in strict_section, strict_section


# ── render.py failure detail (2026-07-13 adversarial review) ─────────────
# cmd_diff() now isolates a broken CLI PER SECTION (a '>>> STOP: ...' line
# inside that CLI's own section) instead of aborting on the first one --
# agent-doctor.sh used to show only the LAST line of render.py's captured
# output, which is the trailing summary ("N CLI(s) STOPPED, see above"), not
# the actual reason. The fail message must surface the real STOP line(s).

def test_doctor_surfaces_the_actual_stop_line_not_just_the_last_output_line(sandbox_with_live_configs):
    # No agent-sync priming needed: the "MCP configured in the runtimes"
    # section runs render.py straight against whatever live config files
    # already exist under HOME, independent of an apply/guard cycle.
    sb = sandbox_with_live_configs
    claude_config = sb.live_config_path("claude")
    claude_config.write_text('{"mcpServers": ', encoding="utf-8")

    result = run_agent_doctor(sb)

    assert "render.py failed to run" in result.stdout
    assert ">>> STOP:" in result.stdout
    assert ".claude.json" in result.stdout, (
        "the fail message must carry render.py's own STOP line (which names "
        "the broken file), not just the trailing 'N CLI(s) STOPPED' summary"
    )


def test_doctor_sh_quotes_the_expected_server_argv_passing():
    """Source assertion (Task 4 follow-up): the unquoted `$expected_ag`
    word-split/glob-risk expansion into python3's argv must be gone,
    replaced by an array built with mapfile so each server name reaches
    python3 as its own literal argv element."""
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")

    assert 'python3 - "$AG_GLOBAL" $expected_ag <<' not in bash
    assert "mapfile -t expected_ag_arr" in bash
    assert '"${expected_ag_arr[@]}"' in bash


def test_doctor_ps1_strict_python_resolution_probes_python3_too():
    """The common resolver must retain the python3-first strict-runtime path.

    The resolver is deliberately outside the --strict section so every doctor
    operation shares one validated Python with PyYAML, rather than each block
    probing an inconsistent runtime independently.
    """
    repo = Path(__file__).resolve().parents[3]
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")

    resolver_start = powershell.index("function Resolve-NexgenPython")
    resolver_end = powershell.index("$NexgenPython = Resolve-NexgenPython", resolver_start)
    resolver = powershell[resolver_start:resolver_end]
    assert 'foreach ($name in @(\"python3\", \"python\"))' in resolver, resolver
    assert 'import sys, yaml' in resolver, resolver
    assert 'sys.version_info >= (3, 10)' in resolver, resolver


# --- New-version alert on the DEFAULT single-clone install (2026-07-14) -----
#
# docs/upgrade.md used to say the update warning only existed for the split
# consumer-clone topology; these lock in the single-clone variant: a doctor
# run must TELL the user a newer released tag exists, informationally (warn,
# never fail), and must stay silent on a pure data vault that doesn't track
# the engine at all.


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _make_engine_tracking_vault(sb, running_version, released):
    """Turns the sandbox vault into a single-clone engine checkout: VERSION
    committed on main, plus a local bare `origin` whose main carries the
    given (version, tag) releases."""
    vault = sb.vault
    _git(vault, "init", "-q", "-b", "main")
    _git(vault, "config", "user.name", "sb")
    _git(vault, "config", "user.email", "sb@localhost")
    (vault / "VERSION").write_text(running_version + "\n", encoding="utf-8")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-qm", f"sandbox at {running_version}")

    origin = vault.parent / "engine-origin.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(vault), str(origin)],
        check=True, capture_output=True,
    )
    work = vault.parent / "engine-work"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(work)],
        check=True, capture_output=True,
    )
    _git(work, "config", "user.name", "sb")
    _git(work, "config", "user.email", "sb@localhost")
    for version, tag in released:
        (work / "VERSION").write_text(version + "\n", encoding="utf-8")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", f"release {version}", "--allow-empty")
        _git(work, "tag", tag)
    _git(work, "push", "-q", "origin", "main", "--tags")
    _git(vault, "remote", "add", "origin", str(origin))


def test_single_clone_update_alert_warns_when_a_newer_tag_exists(sandbox):
    sb = sandbox
    _make_engine_tracking_vault(sb, "0.4.0", [("0.4.0", "v0.4.0"), ("0.5.0", "v0.5.0")])
    result = run_agent_doctor(sb)
    assert "Engine version (single-clone install)" in result.stdout
    assert "new engine version available: v0.5.0 (running: v0.4.0)" in result.stdout


def test_single_clone_update_alert_ok_at_latest(sandbox):
    sb = sandbox
    _make_engine_tracking_vault(sb, "0.5.0", [("0.5.0", "v0.5.0")])
    result = run_agent_doctor(sb)
    assert "engine at (or ahead of) the latest released version (v0.5.0" in result.stdout
    assert "new engine version available" not in result.stdout


def test_single_clone_update_alert_skips_a_pure_data_vault(sandbox):
    # No VERSION file at the vault root -> the section must not run at all.
    result = run_agent_doctor(sandbox)
    assert "Engine version (single-clone install)" not in result.stdout


def test_single_clone_update_alert_parity_with_the_ps1_twin():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")
    for content in (bash, powershell):
        assert "Engine version (single-clone install)" in content
        assert "update is always deliberate" in content
    # Informational-only contract: the alert is a warn, never a fail, in
    # both twins.
    assert 'fail "new engine version' not in bash
    assert 'bad "new engine version' not in powershell


# ── Canonical bootstrap hygiene: size budget + load-on-demand pointer
# integrity (competitor-borrow Tier 1, 2026-07-17) ────────────────────────
# Two additive, read-only, WARN-only doctor checks. They must catch a bloated
# bootstrap and a dangling load-on-demand pointer, must skip the literal
# 03-INFRA/<topic>.md placeholder in the editing-discipline prose, and must
# NEVER turn a green doctor red (informational only). Mirrored in both twins.

def _write_canon(sandbox, text: str) -> Path:
    canon = sandbox.ul / "instructions" / "AGENTS.md"
    canon.parent.mkdir(parents=True, exist_ok=True)
    canon.write_text(text, encoding="utf-8")
    return canon


def test_bootstrap_size_budget_warns_over_and_never_fails(sandbox):
    _write_canon(sandbox, "# bootstrap\n" + ("x" * 200))
    result = _run_doctor(sandbox, env_overrides={"NEXGEN_BOOTSTRAP_MAX_BYTES": "10"})
    assert _lines_with(result.stdout, "⚠", "over the 10-byte budget"), result.stdout
    # informational-only: an oversized bootstrap must never be a FAIL.
    assert not _lines_with(result.stdout, "✗", "bootstrap AGENTS.md"), result.stdout


def test_bootstrap_size_budget_ok_when_under(sandbox):
    _write_canon(sandbox, "# small bootstrap\n")
    result = _run_doctor(sandbox)
    assert _lines_with(result.stdout, "✓", "bootstrap AGENTS.md within budget"), result.stdout


def test_load_on_demand_pointer_dangling_warns_not_fails(sandbox):
    _write_canon(
        sandbox,
        "Load details:\n\n- Ghost note: `03-INFRA/ghost-note-nonexistent.md`\n",
    )
    result = _run_doctor(sandbox)
    assert _lines_with(result.stdout, "⚠", "03-INFRA/ghost-note-nonexistent.md"), result.stdout
    assert not _lines_with(result.stdout, "✗", "ghost-note-nonexistent"), result.stdout


def test_load_on_demand_pointer_resolves_ok(sandbox):
    note = sandbox.vault / "03-INFRA" / "real-note.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("real\n", encoding="utf-8")
    _write_canon(sandbox, "Load details:\n\n- Real note: `03-INFRA/real-note.md`\n")
    result = _run_doctor(sandbox)
    assert _lines_with(result.stdout, "✓", "load-on-demand pointers resolve"), result.stdout


def test_pointer_integrity_skips_the_topic_placeholder(sandbox):
    # The literal 03-INFRA/<topic>.md in the editing-discipline prose must be
    # skipped (angle brackets), never reported as a dangling pointer.
    _write_canon(sandbox, "create `03-INFRA/<topic>.md` and add a pointer.\n")
    result = _run_doctor(sandbox)
    assert not _lines_with(result.stdout, "⚠", "<topic>"), result.stdout
    assert _lines_with(result.stdout, "✓", "no vault-relative bootstrap pointers to verify"), result.stdout


def test_bootstrap_hygiene_present_and_warn_only_in_both_twins():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")
    for content in (bash, powershell):
        assert "Canonical bootstrap hygiene" in content
        assert "NEXGEN_BOOTSTRAP_MAX_BYTES" in content
        assert "NEXGEN_NOTE_MAX_BYTES" in content
    # informational-only contract: never a fail/bad on these three checks.
    assert 'fail "bootstrap AGENTS.md' not in bash
    assert 'fail "oversized detail note' not in bash
    assert 'fail "bootstrap load-on-demand' not in bash
    assert 'bad "bootstrap AGENTS.md' not in powershell
    assert 'bad "oversized detail note' not in powershell
    assert 'bad "bootstrap load-on-demand' not in powershell


# ── Required invariant-rules drift guard (competitor-borrow #4, 2026-07-17) ──
# The doctor WARNs (never fails) when the canonical AGENTS.md is missing a
# non-negotiable rule declared in required-rules.txt. It skips silently when
# the rules file isn't present in the engine tree. CI enforces the same on the
# shipped public AGENTS.md (as a hard failure). Mirrored in both twins.

def test_required_rules_guard_warns_when_a_rule_is_missing(sandbox):
    (sandbox.ul / "instructions" / "required-rules.txt").write_text(
        "Alpha Invariant\nBeta Invariant\n", encoding="utf-8")
    (sandbox.ul / "instructions" / "AGENTS.md").write_text(
        "only Alpha Invariant here\n", encoding="utf-8")
    result = _run_doctor(sandbox)
    assert _lines_with(result.stdout, "⚠", "missing required invariant rule"), result.stdout
    assert "Beta Invariant" in result.stdout
    assert not _lines_with(result.stdout, "✗", "required invariant rule"), result.stdout


def test_required_rules_guard_ok_when_all_present(sandbox):
    (sandbox.ul / "instructions" / "required-rules.txt").write_text(
        "Alpha Invariant\nBeta Invariant\n", encoding="utf-8")
    (sandbox.ul / "instructions" / "AGENTS.md").write_text(
        "Alpha Invariant and Beta Invariant both present\n", encoding="utf-8")
    result = _run_doctor(sandbox)
    assert _lines_with(result.stdout, "✓", "carries all required invariant rules"), result.stdout


def test_required_rules_guard_present_and_warn_only_in_both_twins():
    repo = Path(__file__).resolve().parents[3]
    bash = (repo / "03-INFRA/scripts/agent-doctor.sh").read_text(encoding="utf-8")
    powershell = (repo / "03-INFRA/scripts/agent-doctor.ps1").read_text(encoding="utf-8")
    for content in (bash, powershell):
        assert "check_required_rules.py" in content
        assert "required invariant rule" in content
    assert 'fail "canonical AGENTS.md is missing required invariant' not in bash
    assert 'bad "canonical AGENTS.md is missing required invariant' not in powershell
