"""Regression tests for audit FINDING A (2026-07-12, package P4):

The Popen that launches a Council seat used to omit ``env=`` entirely for
every CLI, so codex/agy/opencode inherited the operator's full os.environ --
including applicative bearer tokens (``N8N_MCP_TOKEN``, ``VAULT_LIBRARY_TOKEN``,
``FIRECRAWL_*``) that a code-review seat has no legitimate reason to hold,
given ``-s read-only``/``--sandbox`` only scope the shell tool, never MCP
servers (see the long note above ``_build_seat_command`` in council.py).

These tests pin:
- claude/ollama are untouched (``invocation.env is None``, Popen inherits
  os.environ exactly as before this fix).
- codex/agy/opencode get an explicit, minimal env: no application token
  survives, even one the allowlist has never heard of before.
- codex/opencode additionally get an isolated config directory under the
  private session dir (no on-disk MCP manifest for the CLI to load at all),
  while auth-relevant material (codex's auth.json) is preserved.
- run_seat actually threads ``invocation.env`` into the real ``Popen`` call.
"""
from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path

import pytest

COUNCIL_PATH = Path(__file__).resolve().parents[1] / "council" / "council.py"

APP_TOKEN_ENV = {
    "N8N_MCP_TOKEN": "n8n-secret-token",
    "VAULT_LIBRARY_TOKEN": "vault-secret-token",
    "VAULT_LIBRARY_URL": "https://vault.example/mcp-secret-path",
    "FIRECRAWL_API_URL": "http://127.0.0.1:33002",
    "FIRECRAWL_API_KEY": "fc-secret-key",
    # A token for a service that does not exist yet anywhere in this
    # codebase: proves the allowlist, not a list of known-bad names, is
    # what keeps it out.
    "SOME_FUTURE_MCP_TOKEN": "future-secret",
}


def load_council(monkeypatch, tmp_path):
    vault = tmp_path / "KnowledgeVault"
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(vault))
    for key, value in APP_TOKEN_ENV.items():
        monkeypatch.setenv(key, value)
    module_name = f"council_env_isolation_under_test_{id(tmp_path)}"
    spec = importlib.util.spec_from_file_location(module_name, COUNCIL_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    mod.SESSIONS_DIR = tmp_path / "sessions"
    mod.SEATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    return mod


@pytest.mark.parametrize("cli", ["codex", "agy", "opencode"])
def test_isolated_seat_env_excludes_every_application_token(monkeypatch, tmp_path, cli):
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    invocation = council._build_seat_command({"cli": cli, "model": "vendor/test"}, "prompt", session_dir)

    assert invocation.env is not None, f"{cli} must run with an explicit, non-inherited env"
    for key in APP_TOKEN_ENV:
        assert key not in invocation.env, f"{key} leaked into the {cli} seat env"


@pytest.mark.parametrize("cli", ["codex", "agy", "opencode"])
def test_isolated_seat_env_still_carries_what_the_binary_needs_to_run(monkeypatch, tmp_path, cli):
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    invocation = council._build_seat_command({"cli": cli, "model": "vendor/test"}, "prompt", session_dir)

    assert invocation.env["PATH"]  # otherwise Popen can't even locate the binary
    # POSIX carries HOME; Windows has no HOME by default and carries
    # USERPROFILE instead (both are on the allowlist -- see council.py).
    home_var = "USERPROFILE" if sys.platform == "win32" else "HOME"
    assert invocation.env[home_var]


@pytest.mark.parametrize("cli", ["claude", "ollama"])
def test_claude_and_ollama_are_not_env_isolated(monkeypatch, tmp_path, cli):
    """Explicitly out of scope for FINDING A: claude's --tools "" already
    makes every tool (MCP included) uninvocable by construction, and ollama
    never gets the --experimental flag that would give it a tool-calling
    surface. Popen must keep inheriting the full environment for these two,
    unchanged from before this fix."""
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    invocation = council._build_seat_command({"cli": cli, "model": "vendor/test"}, "prompt", session_dir)

    assert invocation.env is None


def test_codex_isolated_home_has_only_auth_copied_never_config(monkeypatch, tmp_path):
    """No config.toml means no [mcp_servers.*] table is ever read; auth.json
    is copied so the seat stays logged in."""
    real_codex_home = tmp_path / "real-codex-home"
    real_codex_home.mkdir()
    (real_codex_home / "auth.json").write_text('{"tokens": "real-chatgpt-session"}', encoding="utf-8")
    (real_codex_home / "config.toml").write_text(
        '[mcp_servers.n8n_mcp]\nurl = "http://127.0.0.1:5678/mcp-server/http"\n', encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))

    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    invocation = council._build_seat_command({"cli": "codex", "model": "vendor/test"}, "prompt", session_dir)

    isolated_home = Path(invocation.env["CODEX_HOME"])
    assert str(session_dir) in str(isolated_home), "the isolated CODEX_HOME must live under the private session dir"
    assert isolated_home != real_codex_home
    assert (isolated_home / "auth.json").read_text(encoding="utf-8") == '{"tokens": "real-chatgpt-session"}'
    assert not (isolated_home / "config.toml").exists(), "config.toml (and its [mcp_servers.*] table) must not be copied"


def test_codex_isolated_home_survives_a_missing_real_auth_file(monkeypatch, tmp_path):
    """No auth.json at the real CODEX_HOME must not crash seat construction
    (the seat will simply fail its own auth check downstream, same as an
    unconfigured install would today)."""
    real_codex_home = tmp_path / "real-codex-home-empty"
    real_codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(real_codex_home))

    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    invocation = council._build_seat_command({"cli": "codex", "model": "vendor/test"}, "prompt", session_dir)

    isolated_home = Path(invocation.env["CODEX_HOME"])
    assert not (isolated_home / "auth.json").exists()


def test_opencode_isolated_config_dir_is_empty_and_private(monkeypatch, tmp_path):
    """No opencode.json means no "mcp" key is ever read. Auth stays reachable
    because XDG_DATA_HOME is untouched -- only XDG_CONFIG_HOME is redirected."""
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    invocation = council._build_seat_command({"cli": "opencode", "model": "vendor/test"}, "prompt", session_dir)

    config_home = Path(invocation.env["XDG_CONFIG_HOME"])
    assert str(session_dir) in str(config_home)
    assert list(config_home.iterdir()) == []
    assert "XDG_DATA_HOME" not in invocation.env or invocation.env.get("XDG_DATA_HOME")
    if hasattr(__import__("os"), "name") and __import__("os").name != "nt":
        assert stat.S_IMODE(config_home.stat().st_mode) == 0o700


def test_agy_gets_no_directory_isolation_only_the_allowlist(monkeypatch, tmp_path):
    """Documented residual gap: no verified config-isolation mechanism for
    agy exists, so only the env allowlist is applied (still denies the
    ${N8N_MCP_TOKEN}-style substitution inside agy's own MCP manifest)."""
    council = load_council(monkeypatch, tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    invocation = council._build_seat_command({"cli": "agy", "model": "vendor/test"}, "prompt", session_dir)

    assert "CODEX_HOME" not in invocation.env
    assert "XDG_CONFIG_HOME" not in invocation.env
    # real HOME (or USERPROFILE on Windows) preserved: agy's config is
    # scattered under it, not overridable -- see council.py's allowlist.
    home_var = "USERPROFILE" if sys.platform == "win32" else "HOME"
    assert invocation.env[home_var]


def test_run_seat_passes_the_isolated_env_through_to_popen(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    captured = {}

    class FakeProcess:
        def __init__(self):
            import io
            self.stdout = io.StringIO('{"type":"text","part":{"text":"Risposta\\nVERDICT: APPROVE\\n"}}\n')
            self.stderr = io.StringIO()

        def kill(self):
            return None

        def wait(self):
            return 0

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return FakeProcess()

    monkeypatch.setattr(council.subprocess, "Popen", fake_popen)

    council.run_seat({"cli": "opencode", "model": "vendor/test"}, "prompt", tmp_path)

    assert captured["env"] is not None
    for key in APP_TOKEN_ENV:
        assert key not in captured["env"]


def test_run_seat_leaves_claude_env_as_none_meaning_full_inherit(monkeypatch, tmp_path):
    council = load_council(monkeypatch, tmp_path)
    captured = {}

    class FakeStdin:
        def __init__(self):
            self.text = ""

        def write(self, text):
            self.text += text

        def flush(self):
            return None

        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            import io
            self.stdin = FakeStdin()
            self.stdout = io.StringIO("Risposta\nVERDICT: APPROVE\n")
            self.stderr = io.StringIO()

        def kill(self):
            return None

        def wait(self):
            return 0

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return FakeProcess()

    monkeypatch.setattr(council.subprocess, "Popen", fake_popen)

    council.run_seat({"cli": "claude", "model": "vendor/test"}, "prompt", tmp_path)

    assert captured["env"] is None


def test_run_seat_forces_utf8_text_pipes_for_non_ascii_codex_prompt(monkeypatch, tmp_path):
    """Windows defaults subprocess text pipes to the active ANSI code page.
    Codex requires UTF-8 on ``codex exec -`` stdin, so an Italian prompt must
    never be encoded through cp1252 (or any other locale-dependent codec)."""
    council = load_council(monkeypatch, tmp_path)
    captured = {}

    class FakeStdin:
        def __init__(self):
            self.text = ""

        def write(self, text):
            self.text += text

        def flush(self):
            return None

        def close(self):
            return None

    class FakeProcess:
        def __init__(self, argv):
            import io

            self.stdin = FakeStdin()
            captured["stdin"] = self.stdin
            self.stdout = io.StringIO("codex started\n")
            self.stderr = io.StringIO()
            output_path = Path(argv[argv.index("-o") + 1])
            output_path.write_text("Risposta valida\n", encoding="utf-8")

        def kill(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **kwargs):
        captured["popen_kwargs"] = kwargs
        return FakeProcess(argv)

    monkeypatch.setattr(council.subprocess, "Popen", fake_popen)

    response, _usage = council.run_seat(
        {"cli": "codex", "model": "vendor/test"},
        "Perché è importante?",
        tmp_path,
    )

    assert response == "Risposta valida\n"
    assert captured["stdin"].text == "Perché è importante?"
    assert captured["popen_kwargs"]["encoding"] == "utf-8"
