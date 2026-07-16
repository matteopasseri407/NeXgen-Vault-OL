"""Test 1-8 (matrice per dialetto) su mcp/render.py.

render.py e' importato come modulo Python (non subprocess): monkeypatchiamo
HOME/MANIFEST/IS_WINDOWS direttamente sugli attributi del modulo, come deciso
nell'architettura B1. Ogni test carica una copia FRESCA del modulo (vedi
conftest.load_render_module) cosi' i monkeypatch di un test non contaminano
gli altri.
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest
import yaml

from conftest import load_render_module

DIALECTS = ["claude", "codex", "opencode", "antigravity"]

# render.py esiste in due copie che divergono nella LINGUA dei messaggi (vault
# IT, repo pubblico EN, S1 2026-07-08): i test devono valere per entrambe,
# quindi controllano il SEGNALE (marker/parola chiave), mai una frase in una
# sola lingua.
_MISSING_MARKERS = ("[MANCA]", "[MISSING]")
_OUTSIDE_MANIFEST_MARKERS = ("FUORI MANIFEST", "OUTSIDE THE MANIFEST")
_NOTHING_TO_WRITE_MARKERS = ("nothing to write", "already compliant")


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(m in text for m in markers)

WRITE_FN = {
    "claude": "write_claude",
    "codex": "write_codex",
    "opencode": "write_opencode",
    "antigravity": "write_antigravity",
}

TOP_KEY = {
    "claude": "mcpServers",
    "opencode": "mcp",
    "antigravity": "mcpServers",
}


def _manifest_servers(sandbox):
    data = yaml.safe_load((sandbox.mcp_dir / "manifest.yaml").read_text())["servers"]
    return data


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---- test 1: --diff rileva drift iniettato (per i 4 dialetti) --------------

def test_diff_reports_injected_drift(sandbox_with_live_configs, capsys):
    mod = load_render_module(sandbox_with_live_configs)
    mod.cmd_diff()
    out = capsys.readouterr().out

    sections = {}
    current = None
    for line in out.splitlines():
        if line.startswith("========== "):
            current = line.strip("= ").strip().lower()
            sections[current] = []
        elif current:
            sections[current].append(line)
    for cli in DIALECTS:
        assert cli in sections, f"sezione {cli} non trovata nell'output di --diff"
        text = "\n".join(sections[cli])
        assert "[DIFF]" in text and "fake-stdio-tool" in text, f"{cli}: drift su fake-stdio-tool non rilevato"
        assert _has_any(text, _MISSING_MARKERS) and "fake-http-api" in text, f"{cli}: server mancante fake-http-api non rilevato"
        assert _has_any(text, _MISSING_MARKERS) and "fake-cross-os-tool" in text, f"{cli}: server mancante fake-cross-os-tool non rilevato"
        extra_name = "legacy_extra_tool" if cli == "codex" else "legacy-extra-tool"
        assert "[EXTRA]" in text and extra_name in text, f"{cli}: server extra non segnalato"
    # fake-codex-only e' target SOLO codex: deve mancare li' e non comparire altrove
    assert "fake-codex-only" in "\n".join(sections["codex"])
    for cli in ("claude", "opencode", "antigravity"):
        assert "fake-codex-only" not in "\n".join(sections[cli])


# ---- test 2: --write produce l'output atteso, non-MCP intatto ---------------

@pytest.mark.parametrize("cli", DIALECTS)
def test_write_produces_expected_output(sandbox_with_live_configs, cli):
    sb = sandbox_with_live_configs
    mod = load_render_module(sb)
    path = sb.live_config_path(cli)
    before_raw = path.read_text(encoding="utf-8")

    rc = getattr(mod, WRITE_FN[cli])()
    assert rc == 0, f"{cli}: write ha restituito {rc}, atteso 0"

    after_raw = path.read_text(encoding="utf-8")
    servers = _manifest_servers(sb)
    wanted = {n: s for n, s in servers.items() if cli in s["targets"]}

    if cli == "codex":
        before = tomllib.loads(before_raw)
        after = tomllib.loads(after_raw)
        # sezioni non-mcp intatte
        for k in before:
            if k != "mcp_servers":
                assert after.get(k) == before[k], f"codex: sezione non-MCP '{k}' modificata"
        for name, spec in wanted.items():
            cname = name.replace("-", "_")
            expected_full = dict(mod.r_codex(name, mod.os_view(spec)))
            env = expected_full.pop("env", None)
            got = after["mcp_servers"][cname]
            for k, v in expected_full.items():
                assert got.get(k) == v, f"codex/{cname}: campo {k} non combacia ({got.get(k)!r} != {v!r})"
            if env is not None:
                assert got.get("env") == env, f"codex/{cname}: env non combacia"
        # overlay tools di fake_stdio_tool preservato
        assert after["mcp_servers"]["fake_stdio_tool"]["tools"]["some_tool"]["approval_mode"] == "approve"
        # server extra fuori manifest preservato
        assert after["mcp_servers"]["legacy_extra_tool"]["command"] == "legacy-cmd"
    else:
        before = _read_json(Path(path)) if False else json.loads(before_raw)
        after = json.loads(after_raw)
        key = TOP_KEY[cli]
        for k in before:
            if k != key:
                assert after.get(k) == before[k], f"{cli}: sezione non-MCP '{k}' modificata"
        render_fn = {"claude": mod.r_claude, "opencode": mod.r_opencode, "antigravity": mod.r_antigravity}[cli]
        name_fn = {"claude": lambda n: n, "opencode": lambda n: n, "antigravity": lambda n: n}[cli]
        for name, spec in wanted.items():
            expected = render_fn(name, mod.os_view(spec))
            got = after[key][name_fn(name)]
            assert got == expected, f"{cli}/{name}: reso {got!r}, atteso {expected!r}"
        # server extra fuori manifest preservato + segnalato
        assert "legacy-extra-tool" in after[key]


# ---- test 3: idempotenza (secondo --write consecutivo = nessuna modifica) --

@pytest.mark.parametrize("cli", DIALECTS)
def test_write_is_idempotent(sandbox_with_live_configs, cli, capsys):
    sb = sandbox_with_live_configs
    mod = load_render_module(sb)
    path = sb.live_config_path(cli)
    fn = getattr(mod, WRITE_FN[cli])

    rc1 = fn()
    assert rc1 == 0
    capsys.readouterr()
    hash_after_1 = path.read_bytes()
    backups_after_1 = sorted(p.name for p in path.parent.glob(path.name + ".bak-*"))

    rc2 = fn()
    out2 = capsys.readouterr().out
    assert rc2 == 0
    hash_after_2 = path.read_bytes()
    backups_after_2 = sorted(p.name for p in path.parent.glob(path.name + ".bak-*"))

    assert hash_after_2 == hash_after_1, f"{cli}: il secondo --write ha modificato il file"
    assert backups_after_2 == backups_after_1, f"{cli}: il secondo --write ha creato un nuovo backup (non era idempotente)"
    assert _has_any(out2.lower(), tuple(m.lower() for m in _NOTHING_TO_WRITE_MARKERS)), out2


# ---- test 4: guard — input manomesso -> autoblocco, file intatto -----------

@pytest.mark.parametrize("cli", ["claude", "opencode", "antigravity"])
def test_write_guard_blocks_on_missing_section(sandbox_with_live_configs, cli):
    """Manomissione: il file JSON resta VALIDO (json.loads non si rompe, quindi
    keep_extras/reorder non esplodono su un tipo inatteso) ma viene minificato
    su una riga sola. La chiave mcpServers/mcp non e' piu' a inizio riga, quindi
    la regex chirurgica di write_json_section non la trova piu' -> autoblocco
    "non trovo la sezione", non scrive, file intatto."""
    sb = sandbox_with_live_configs
    path = sb.live_config_path(cli)
    data = json.loads(path.read_text(encoding="utf-8"))
    before_raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    path.write_text(before_raw, encoding="utf-8")

    mod = load_render_module(sb)
    rc = getattr(mod, WRITE_FN[cli])()
    assert rc == 2, f"{cli}: il guard non si e' attivato su una sezione manomessa (rc={rc})"
    assert path.read_text(encoding="utf-8") == before_raw, f"{cli}: il file e' stato toccato nonostante il guard"


def test_write_codex_adds_a_newly_required_env_section(sandbox_with_live_configs):
    sb = sandbox_with_live_configs
    path = sb.live_config_path("codex")
    # Upgrade dialetto-specifico: una sezione mcp_servers.* esiste ma le manca
    # la sotto-sezione .env, mentre il manifest ora la richiede. Il renderer
    # deve aggiungerla chirurgicamente, non costringere l'utente a cancellare
    # prima la sezione live.
    before_raw = path.read_text(encoding="utf-8") + (
        "\n[mcp_servers.fake_cross_os_tool]\n"
        'command = "python3"\n'
        'args = ["/fake/vault/path/tool.py"]\n'
    )
    path.write_text(before_raw, encoding="utf-8")

    mod = load_render_module(sb)
    rc = mod.write_codex()
    assert rc == 0
    rendered = mod.toml_loads(path.read_text(encoding="utf-8"))
    assert rendered["mcp_servers"]["fake_cross_os_tool"]["env"] == {
        "FAKE_TOOL_URL": "http://127.0.0.1:19002"
    }


@pytest.mark.parametrize(
    ("manifest_text", "expected"),
    [
        ("servers: {}\n", "schema_version"),
        ("schema_version: 1\nservers: []\n", "servers must be a mapping"),
        (
            """schema_version: 1
servers:
  bad-target:
    transport: stdio
    command: node
    targets: [unknown]
""",
            "unsupported CLI",
        ),
        (
            """schema_version: 1
servers:
  missing-auth:
    transport: http
    url: https://example.invalid/mcp
    targets: [codex]
""",
            "auth must be a mapping",
        ),
        (
            """schema_version: 1
servers:
  bad-env:
    transport: stdio
    command: node
    env: {NOT-VALID: value}
    targets: [codex]
""",
            "environment variable name",
        ),
        (
            """schema_version: 1
servers:
  bad-timeout:
    transport: stdio
    command: node
    timeouts: {tool: false}
    targets: [codex]
""",
            "finite number",
        ),
        (
            """schema_version: 1
servers:
  bad-windows:
    transport: stdio
    command: node
    targets: [codex]
    windows: {transport: http}
""",
            "unsupported field",
        ),
    ],
)
def test_manifest_contract_rejects_invalid_data(sandbox, manifest_text, expected):
    (sandbox.mcp_dir / "manifest.yaml").write_text(manifest_text, encoding="utf-8")
    mod = load_render_module(sandbox)

    with pytest.raises(mod.ConfigValidationError, match=expected):
        mod.load_manifest()


def test_invalid_manifest_blocks_before_live_config_write(sandbox_with_live_configs, capsys):
    sb = sandbox_with_live_configs
    path = sb.live_config_path("codex")
    before_raw = path.read_text(encoding="utf-8")
    (sb.mcp_dir / "manifest.yaml").write_text(
        """schema_version: 1
servers:
  missing-auth:
    transport: http
    url: https://example.invalid/mcp
    targets: [codex]
""",
        encoding="utf-8",
    )

    mod = load_render_module(sb)
    assert mod.write_codex() == 2
    captured = capsys.readouterr()

    assert "invalid MCP manifest" in captured.err
    assert path.read_text(encoding="utf-8") == before_raw


# ---- test 5: server fuori manifest preservato e segnalato ------------------

@pytest.mark.parametrize("cli", DIALECTS)
def test_extra_server_preserved_and_reported(sandbox_with_live_configs, cli, capsys):
    """Regressione 2026-07-01: un server fuori manifest non va MAI cancellato.
    Per i 3 dialetti JSON (via keep_extras) la preservazione e' anche
    ANNUNCIATA a stdout ("FUORI MANIFEST"); per Codex la preservazione e'
    silenziosa (logica di patch per-sezione, nessun messaggio dedicato) — si
    verifica solo che il server resti intatto, non il messaggio."""
    sb = sandbox_with_live_configs
    mod = load_render_module(sb)
    rc = getattr(mod, WRITE_FN[cli])()
    assert rc == 0
    out = capsys.readouterr().out

    path = sb.live_config_path(cli)
    if cli == "codex":
        after = tomllib.loads(path.read_text(encoding="utf-8"))
        assert after["mcp_servers"]["legacy_extra_tool"]["command"] == "legacy-cmd"
    else:
        assert _has_any(out, _OUTSIDE_MANIFEST_MARKERS) and "legacy-extra-tool" in out
        after = json.loads(path.read_text(encoding="utf-8"))
        assert TOP_KEY[cli] and "legacy-extra-tool" in after[TOP_KEY[cli]]


@pytest.mark.parametrize("cli", DIALECTS)
def test_explicit_retirement_removes_the_stale_server_across_dialects(sandbox_with_live_configs, cli):
    sb = sandbox_with_live_configs
    manifest_path = sb.mcp_dir / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    # One canonical tombstone follows every dialect's key normalization, so
    # Codex removes the underscore form without a duplicate manifest entry.
    manifest["retired_servers"] = ["legacy-extra-tool"]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    mod = load_render_module(sb)
    assert getattr(mod, WRITE_FN[cli])() == 0

    path = sb.live_config_path(cli)
    if cli == "codex":
        after = tomllib.loads(path.read_text(encoding="utf-8"))
        assert "legacy_extra_tool" not in after["mcp_servers"]
    else:
        after = json.loads(path.read_text(encoding="utf-8"))
        assert "legacy-extra-tool" not in after[TOP_KEY[cli]]


@pytest.mark.parametrize(
    ("server_name", "table_header"),
    [
        ("legacy.tool", '[mcp_servers."legacy.tool"]'),
    ],
)
def test_codex_retirement_handles_every_valid_toml_key_shape(
    sandbox_with_live_configs, server_name, table_header
):
    sb = sandbox_with_live_configs
    manifest_path = sb.mcp_dir / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["retired_servers"] = [server_name]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    path = sb.live_config_path("codex")
    path.write_text(
        path.read_text(encoding="utf-8")
        + f'\n{table_header}\ncommand = "node"\nargs = []\n',
        encoding="utf-8",
    )

    mod = load_render_module(sb)
    assert mod.write_codex() == 0
    after = tomllib.loads(path.read_text(encoding="utf-8"))
    assert server_name not in after["mcp_servers"]


def test_toml_table_path_has_no_reserved_key_collision(sandbox):
    mod = load_render_module(sandbox)
    assert mod._toml_table_path("mcp_servers.__nexgen_table_marker__") == (
        "mcp_servers",
        "__nexgen_table_marker__",
    )


def test_codex_renders_an_active_name_containing_a_dot_as_one_quoted_key(sandbox_with_live_configs):
    sb = sandbox_with_live_configs
    manifest_path = sb.mcp_dir / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["servers"]["active.tool"] = {
        "transport": "stdio",
        "command": "node",
        "args": ["tool.mjs"],
        "targets": ["codex"],
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    mod = load_render_module(sb)
    assert mod.write_codex() == 0
    raw = sb.live_config_path("codex").read_text(encoding="utf-8")
    assert '[mcp_servers."active.tool"]' in raw
    expected = mod.r_codex("active.tool", mod.os_view(manifest["servers"]["active.tool"]))
    assert tomllib.loads(raw)["mcp_servers"]["active.tool"]["command"] == expected["command"]


# ---- test 6: backup dopo --write, pruning tiene al massimo 3 ---------------

@pytest.mark.parametrize("cli", DIALECTS)
def test_write_creates_backup(sandbox_with_live_configs, cli):
    sb = sandbox_with_live_configs
    mod = load_render_module(sb)
    path = sb.live_config_path(cli)
    rc = getattr(mod, WRITE_FN[cli])()
    assert rc == 0
    backups = list(path.parent.glob(path.name + ".bak-*"))
    assert len(backups) >= 1, f"{cli}: nessun backup creato dopo --write"


@pytest.mark.parametrize("cli", DIALECTS)
def test_write_creates_owner_only_backup_and_output(sandbox_with_live_configs, cli):
    sb = sandbox_with_live_configs
    mod = load_render_module(sb)
    path = sb.live_config_path(cli)
    path.chmod(0o644)

    rc = getattr(mod, WRITE_FN[cli])()
    assert rc == 0

    backups = list(path.parent.glob(path.name + ".bak-*"))
    assert backups, f"{cli}: nessun backup creato dopo --write"
    if os.name != "nt":
        assert all((p.stat().st_mode & 0o077) == 0 for p in backups), f"{cli}: backup leggibile da gruppo/altri"
        assert (path.stat().st_mode & 0o077) == 0, f"{cli}: output leggibile da gruppo/altri"


def test_write_rejects_invalid_json_without_touching_file(sandbox_with_live_configs, capsys):
    sb = sandbox_with_live_configs
    path = sb.live_config_path("opencode")
    bad = '{"mcp": '
    path.write_text(bad, encoding="utf-8")

    mod = load_render_module(sb)
    rc = mod.write_opencode()
    out = capsys.readouterr().out

    assert rc == 2
    assert "STOP" in out
    assert path.read_text(encoding="utf-8") == bad


def test_write_rejects_non_object_json_root_without_touching_file(sandbox_with_live_configs, capsys):
    sb = sandbox_with_live_configs
    path = sb.live_config_path("claude")
    bad = "[]"
    path.write_text(bad, encoding="utf-8")

    mod = load_render_module(sb)
    rc = mod.write_claude()
    out = capsys.readouterr().out

    assert rc == 2
    assert "STOP" in out
    assert path.read_text(encoding="utf-8") == bad


def test_write_rejects_invalid_toml_without_touching_file(sandbox_with_live_configs, capsys):
    sb = sandbox_with_live_configs
    path = sb.live_config_path("codex")
    bad = "[mcp_servers"
    path.write_text(bad, encoding="utf-8")

    mod = load_render_module(sb)
    rc = mod.write_codex()
    out = capsys.readouterr().out

    assert rc == 2
    assert "STOP" in out
    assert path.read_text(encoding="utf-8") == bad


# ── load_current() on a corrupted-but-present config (beta-readiness
# review, 2026-07-13) ───────────────────────────────────────────────────
# load_current() only caught FileNotFoundError, mapping it to None ("CLI
# not installed here"). A file that EXISTS but fails to parse (a stray
# binary write, a bad manual edit, a crash mid-save) fell through to an
# unhandled JSONDecodeError/TOMLDecodeError -- and every write_* function
# below already treats a raw crash here as unacceptable (the whole point of
# their own "STOP: not valid JSON/TOML" guards), so --diff mode having no
# equivalent guard for the SAME failure mode was the one gap.

def test_load_current_stops_loudly_on_corrupted_json_instead_of_crashing(sandbox_with_live_configs, capsys):
    sb = sandbox_with_live_configs
    path = sb.live_config_path("claude")
    path.write_text('{"mcpServers": ', encoding="utf-8")

    mod = load_render_module(sb)
    with pytest.raises(SystemExit) as exc:
        mod.load_current("claude")

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "STOP" in out
    assert ".claude.json" in out


def test_load_current_stops_loudly_on_corrupted_toml_instead_of_crashing(sandbox_with_live_configs, capsys):
    sb = sandbox_with_live_configs
    path = sb.live_config_path("codex")
    path.write_text("[mcp_servers", encoding="utf-8")

    mod = load_render_module(sb)
    with pytest.raises(SystemExit) as exc:
        mod.load_current("codex")

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "STOP" in out
    assert "config.toml" in out


def test_diff_mode_stops_loudly_on_corrupted_config_not_silently_skips_it(sandbox_with_live_configs, capsys):
    """The end-to-end contract from cmd_diff()'s side: a corrupted live
    config must never print the same "not installed here, skipped" message
    a genuinely absent config gets -- that would silently hide real drift
    on a broken file behind a message that says nothing is wrong."""
    sb = sandbox_with_live_configs
    path = sb.live_config_path("opencode")
    path.write_text("{not json at all", encoding="utf-8")

    mod = load_render_module(sb)
    rc = mod.cmd_diff()

    assert rc != 0, "a corrupted live config must make cmd_diff() exit non-zero"
    out = capsys.readouterr().out
    assert "not installed here" not in out


def test_diff_mode_isolates_a_corrupted_cli_and_keeps_scanning_the_rest(sandbox_with_live_configs, capsys):
    """The bug this closes (beta-readiness review, 2026-07-13): load_current()
    used to sys.exit(2) straight through cmd_diff()'s loop, aborting the
    whole process on the FIRST corrupted CLI and hiding drift on every other
    CLI that would otherwise still have been scanned. One broken CLI must be
    reported and isolated; the remaining CLIs must still get their own
    section, with real [DIFF]/[MISSING]/[OK] findings."""
    sb = sandbox_with_live_configs
    path = sb.live_config_path("claude")
    path.write_text('{"mcpServers": ', encoding="utf-8")

    mod = load_render_module(sb)
    rc = mod.cmd_diff()
    out = capsys.readouterr().out

    assert rc != 0
    sections = {}
    current = None
    for line in out.splitlines():
        if line.startswith("========== "):
            current = line.strip("= ").strip().lower()
            sections[current] = []
        elif current:
            sections[current].append(line)
    assert ">>> STOP:" in "\n".join(sections["claude"]), "claude's own section must carry the STOP message"
    # the other 3 CLIs must still have been scanned for real, not skipped
    # (the trailing summary line legitimately says "CLI(s) STOPPED", so
    # match the per-CLI ">>> STOP:" marker specifically, not the word alone).
    for cli in ("codex", "opencode", "antigravity"):
        text = "\n".join(sections[cli])
        assert ">>> STOP:" not in text, f"{cli}: a claude-only corruption must not bleed into other sections"
        assert "[DIFF]" in text or "[MISSING]" in text or "[OK]" in text, f"{cli}: section not scanned after claude stopped"


def test_diff_mode_isolates_a_cli_whose_config_is_unreadable(sandbox_with_live_configs, capsys, monkeypatch):
    """A locked/unreadable config file (chmod 000, held open exclusively
    elsewhere -- the realistic Windows failure mode) is a DIFFERENT
    exception class than the corrupted-JSON case above: load_current()
    only converts a PARSE failure to SystemExit, a read failure propagates
    as a plain OSError untouched. cmd_diff() must isolate it the exact
    same way as the SystemExit case: STOP that CLI's section, keep
    scanning the rest, never crash the whole diff (2026-07-13 follow-up).
    monkeypatched open, not chmod 000: portable, and immune to a test
    process running as root (which ignores file permission bits)."""
    sb = sandbox_with_live_configs
    path = sb.live_config_path("claude")
    real_read_text = Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied", str(path))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    mod = load_render_module(sb)
    rc = mod.cmd_diff()
    out = capsys.readouterr().out

    assert rc != 0, "an unreadable live config must make cmd_diff() exit non-zero"
    sections = {}
    current = None
    for line in out.splitlines():
        if line.startswith("========== "):
            current = line.strip("= ").strip().lower()
            sections[current] = []
        elif current:
            sections[current].append(line)
    assert ">>> STOP:" in "\n".join(sections["claude"]), "claude's own section must carry the STOP message"
    # the trailing summary line legitimately says "CLI(s) STOPPED", so match
    # the per-CLI ">>> STOP:" marker specifically, not the word alone (same
    # distinction the corrupted-JSON isolation test above relies on).
    for cli in ("codex", "opencode", "antigravity"):
        text = "\n".join(sections[cli])
        assert ">>> STOP:" not in text, f"{cli}: claude's read failure must not bleed into other sections"
        assert "[DIFF]" in text or "[MISSING]" in text or "[OK]" in text, f"{cli}: section not scanned after claude stopped"


def test_prune_backups_keeps_at_most_three(tmp_path):
    mod_spec_path = None
    # import diretto della sola funzione via file reale (non serve una sandbox
    # completa: _prune_backups non tocca HOME/MANIFEST)
    import importlib.util
    real_render = Path(__file__).resolve().parent.parent / "mcp" / "render.py"
    spec = importlib.util.spec_from_file_location("render_prune_test", real_render)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    target = tmp_path / "fake-config.json"
    target.write_text("{}")
    # timestamp fittizio costruito a runtime (non come literal): un digit-run
    # di 8+ cifre nel SORGENTE farebbe scattare il leak-check (pattern
    # "id numerico lungo"), anche se e' solo una data finta di test.
    year, month, day = 2026, 1, 1
    date_part = f"{year:04d}{month:02d}{day:02d}"
    stamps = [f"{date_part}-{i:06d}" for i in range(1, 6)]
    for s in stamps:
        (tmp_path / f"fake-config.json.bak-{s}").write_text("backup")

    mod._prune_backups(target, keep=3)
    remaining = sorted(p.name for p in tmp_path.glob("fake-config.json.bak-*"))
    assert remaining == [f"fake-config.json.bak-{s}" for s in stamps[-3:]], remaining


# ---- test 7: os_view applica l'override windows: solo su IS_WINDOWS=True ---

def test_os_view_windows_override(sandbox, monkeypatch):
    mod = load_render_module(sandbox)
    monkeypatch.setattr(mod.shutil, "which", lambda _command: None)
    servers = _manifest_servers(sandbox)
    server = servers["fake-cross-os-tool"]

    mod.IS_WINDOWS = False
    linux_view = mod.os_view(server)
    assert linux_view["command"] == "python3"
    assert linux_view["args"] == ["/fake/vault/path/tool.py"]
    assert "windows" not in linux_view

    mod.IS_WINDOWS = True
    win_view = mod.os_view(server)
    assert win_view["command"] == "python"
    assert win_view["args"] == ["C:\\fake\\vault\\path\\tool.py"]
    assert "windows" not in win_view

    # un server SENZA blocco windows: non deve rompersi sotto IS_WINDOWS=True
    plain = servers["fake-stdio-tool"]
    mod.IS_WINDOWS = True
    plain_view = mod.os_view(plain)
    assert plain_view["command"] == "fake-cmd"


def test_os_view_windows_normalizes_common_mcp_wrappers(sandbox, monkeypatch):
    mod = load_render_module(sandbox)
    mod.IS_WINDOWS = True
    monkeypatch.setattr(mod.shutil, "which", lambda _command: None)
    base = {"transport": "stdio", "args": ["server.js"], "targets": ["codex"]}

    assert mod.os_view({**base, "command": "npx"})["command"] == "npx.cmd"
    assert mod.os_view({**base, "command": "node"})["command"] == "node.exe"
    assert mod.os_view({**base, "command": "python3"})["command"] == "python"
    http = {"transport": "http", "url": "http://127.0.0.1:1", "auth": {"env": "TOKEN"}}
    rendered_http = mod.r_antigravity("http", http)
    assert rendered_http["command"] == "node.exe"
    assert rendered_http["args"][-2:] == ["TOKEN", mod.MCP_REMOTE_PACKAGE]
    assert rendered_http["args"][0].endswith("mcp-http-bridge.mjs")
    # An explicit Windows override wins first, then receives the same safe
    # normalization as a portable manifest entry.
    overridden = {**base, "command": "python3", "windows": {"command": "npx"}}
    assert mod.os_view(overridden)["command"] == "npx.cmd"


def test_os_view_windows_resolves_npx_absolutely_and_bounds_child_path(sandbox, monkeypatch, tmp_path):
    mod = load_render_module(sandbox)
    mod.IS_WINDOWS = True
    npx_dir = tmp_path / "npm-shims"
    node_dir = tmp_path / "nodejs"
    npx_dir.mkdir()
    node_dir.mkdir()
    npx = npx_dir / "npx.cmd"
    node = node_dir / "node.exe"
    npx.write_text("@echo off\r\n", encoding="utf-8")
    node.write_bytes(b"")
    monkeypatch.setattr(
        mod.shutil,
        "which",
        lambda command: str(npx) if command == "npx.cmd" else (str(node) if command == "node.exe" else None),
    )
    monkeypatch.setenv("PATH", ("X" * 5000) + mod.os.pathsep + ("Y" * 5000))

    rendered = mod.os_view({"transport": "stdio", "command": "npx", "args": ["pkg"], "targets": ["codex"]})

    assert rendered["command"] == str(npx)
    assert str(npx_dir) in rendered["env"]["PATH"]
    assert str(node_dir) in rendered["env"]["PATH"]
    assert len(rendered["env"]["PATH"]) <= mod.WINDOWS_CMD_ENV_LIMIT


def test_os_view_expands_only_portable_local_path_placeholders(sandbox, monkeypatch):
    engine = sandbox.home / "engine-root"
    data = sandbox.home / "vault-data"
    monkeypatch.setenv("AGENT_ENGINE_ROOT", str(engine))
    monkeypatch.setenv("AGENT_VAULT_DATA", str(data))
    mod = load_render_module(sandbox)
    mod.IS_WINDOWS = True
    monkeypatch.setattr(mod.shutil, "which", lambda command: command)

    rendered = mod.os_view(
        {
            "transport": "stdio",
            "command": "node",
            "args": [
                r"${AGENT_ENGINE_ROOT}\agent-universal-layer\mcp\server.mjs",
                "${AGENT_VAULT_DATA}",
                "${SECRET_TOKEN}",
            ],
            "targets": ["codex"],
        }
    )

    assert rendered["args"][0].startswith(str(engine))
    assert rendered["args"][1] == str(data)
    assert rendered["args"][2] == "${SECRET_TOKEN}"


def test_render_honors_knowledge_vault_path_when_agent_vault_data_is_unset(sandbox, monkeypatch):
    custom_vault = sandbox.home / "custom-vault"
    monkeypatch.delenv("AGENT_VAULT_DATA", raising=False)
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(custom_vault))

    mod = load_render_module(sandbox)

    assert mod.VAULT_DATA == custom_vault


def test_manifest_rejects_codex_alias_collisions(sandbox):
    (sandbox.mcp_dir / "manifest.yaml").write_text(
        """schema_version: 1
servers:
  desktop-commander:
    transport: stdio
    command: fake
    targets: [codex]
  desktop_commander:
    transport: stdio
    command: fake
    targets: [codex]
""",
        encoding="utf-8",
    )
    mod = load_render_module(sandbox)

    with pytest.raises(mod.ConfigValidationError, match="collide as Codex key"):
        mod.load_manifest()


def test_codex_live_alias_collisions_are_reported_without_auto_deletion(sandbox):
    mod = load_render_module(sandbox)

    assert mod._codex_alias_collisions(["desktop-commander", "desktop_commander", "other"]) == [
        ["desktop-commander", "desktop_commander"]
    ]


def test_matching_live_server_fields_are_preserved_additively(sandbox):
    mod = load_render_module(sandbox)
    generated = {"playwright": {"command": "npx.cmd", "args": ["server"]}}
    live = {"playwright": {"command": "npx", "args": ["server"], "env": {"RUNTIME": "1"}}}

    merged = mod.preserve_server_fields(generated, live)
    assert merged["playwright"]["command"] == "npx.cmd"
    assert merged["playwright"]["args"] == ["server"]
    assert merged["playwright"]["env"] == {"RUNTIME": "1"}


# ---- test 8: nessun env-ref espanso in chiaro nell'output ------------------

def test_redact_masks_secrets_and_env_refs(sandbox):
    mod = load_render_module(sandbox)
    assert mod.redact("${SOME_VAR}") == "<AUTH>"
    assert mod.redact("Bearer sometoken123", key="headers") in ("<AUTH>",) or "sometoken123" not in mod.redact("Bearer sometoken123")
    assert mod.redact("plain-value", key="command") == "plain-value"
    assert mod.redact("abc123", key="token") == "<AUTH>"
    assert mod.redact({"env": {"CUSTOM_NAME": "sensitive-value"}})["env"]["CUSTOM_NAME"] == "<AUTH>"
    assert mod.redact_for_log({"args": ["--api-key", "short-sensitive"]}) == {
        "args": ["<REDACTED>", "<REDACTED>"]
    }


def test_cmd_diff_detects_path_drift_without_printing_live_values(sandbox, monkeypatch, capsys):
    mod = load_render_module(sandbox)
    manifest = mod.load_manifest(quiet=True)
    codex = dict(mod.CLI["codex"])
    base_render = codex["render"]

    def render_with_expected_path(name, spec):
        rendered = base_render(name, spec)
        if name == "fake-codex-only":
            rendered.setdefault("env", {})["PATH"] = "fixture-bounded-path"
        return rendered

    codex["render"] = render_with_expected_path
    current = {
        codex["name"](name): codex["render"](name, spec)
        for name, spec in manifest.items()
        if "codex" in spec["targets"]
    }
    drifted = current["fake_codex_only"]
    drifted["env"]["PATH"] = "fixture-broken-path"
    drifted["env"]["API_TOKEN"] = "fixture-sensitive-value"
    monkeypatch.setattr(mod, "CLI", {"codex": codex})
    monkeypatch.setattr(mod, "load_current", lambda cli: current)

    assert mod.cmd_diff() == 0
    output = capsys.readouterr().out
    assert "[DIFF]   fake-codex-only" in output
    assert "env.PATH" in output
    assert "fixture-broken-path" not in output
    assert "fixture-sensitive-value" not in output


def test_antigravity_retirement_never_prints_inline_env_secrets(sandbox_with_live_configs, capsys):
    sb = sandbox_with_live_configs
    manifest_path = sb.mcp_dir / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["retired_servers"] = ["retired-secret"]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    path = sb.live_config_path("antigravity")
    live = json.loads(path.read_text(encoding="utf-8"))
    live["mcpServers"]["retired-secret"] = {
        "command": "node",
        "args": ["--api-key", "positional-sensitive-value"],
        "env": {"API_TOKEN": "fixture-sensitive-value", "CUSTOM_NAME": "another-sensitive-value"},
    }
    path.write_text(json.dumps(live, indent=2), encoding="utf-8")

    mod = load_render_module(sb)
    assert mod.write_antigravity() == 0
    output = capsys.readouterr().out
    assert "fixture-sensitive-value" not in output
    assert "another-sensitive-value" not in output
    assert "positional-sensitive-value" not in output
    assert "<REDACTED>" in output


def test_codex_retirement_never_prints_inline_env_secrets(sandbox_with_live_configs, capsys):
    sb = sandbox_with_live_configs
    manifest_path = sb.mcp_dir / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["retired_servers"] = ["retired-secret"]
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    path = sb.live_config_path("codex")
    path.write_text(
        path.read_text(encoding="utf-8")
        + '\n[mcp_servers.retired_secret]\ncommand = "node"\n'
          'args = ["--password", "positional-sensitive-value"]\n'
          '[mcp_servers.retired_secret.env]\n'
          'API_TOKEN = "fixture-sensitive-value"\n'
          'CUSTOM_NAME = "another-sensitive-value"\n',
        encoding="utf-8",
    )

    mod = load_render_module(sb)
    assert mod.write_codex() == 0
    output = capsys.readouterr().out
    assert "fixture-sensitive-value" not in output
    assert "another-sensitive-value" not in output
    assert "positional-sensitive-value" not in output
    assert "<REDACTED>" in output


# ---- --expected-servers: machine-consumable listing for agent-doctor -------
# Feeds agent-doctor.sh/.ps1's --strict block (which used to hardcode the
# {firecrawl, n8n-mcp, vault-library, vault-ocr} set): the expected set must
# now be DERIVED from the manifest at runtime, including require_env
# filtering, so a Local-Only install expects only what agent-sync actually
# writes -- never the full manifest.

_GATED_MANIFEST = """schema_version: 1
servers:
  fake-open-tool:
    transport: stdio
    command: fake-cmd
    targets: [codex, claude]
  fake-gated-tool:
    transport: stdio
    command: fake-cmd2
    targets: [codex]
    require_env: FAKE_GATE_VAR
"""


def _write_gated_manifest(sandbox):
    (sandbox.mcp_dir / "manifest.yaml").write_text(_GATED_MANIFEST, encoding="utf-8")


def test_expected_servers_excludes_gated_server_when_env_unset(sandbox, monkeypatch, capsys):
    _write_gated_manifest(sandbox)
    monkeypatch.delenv("FAKE_GATE_VAR", raising=False)
    mod = load_render_module(sandbox)

    rc = mod.cmd_expected_servers("codex")
    out = capsys.readouterr().out

    assert rc == 0
    assert out.splitlines() == ["fake-open-tool"], f"il server con require_env non soddisfatto non deve comparire: {out!r}"


def test_expected_servers_includes_gated_server_when_env_set(sandbox, monkeypatch, capsys):
    _write_gated_manifest(sandbox)
    monkeypatch.setenv("FAKE_GATE_VAR", "1")
    mod = load_render_module(sandbox)

    rc = mod.cmd_expected_servers("codex")
    out = capsys.readouterr().out

    assert rc == 0
    assert set(out.splitlines()) == {"fake-open-tool", "fake-gated-tool"}


def test_expected_servers_filters_by_cli_target(sandbox, monkeypatch, capsys):
    _write_gated_manifest(sandbox)
    monkeypatch.setenv("FAKE_GATE_VAR", "1")
    mod = load_render_module(sandbox)

    rc = mod.cmd_expected_servers("claude")
    out = capsys.readouterr().out

    assert rc == 0
    # fake-gated-tool targets only codex: must not leak into claude's list
    # even though its require_env is satisfied.
    assert out.splitlines() == ["fake-open-tool"]


def test_expected_servers_output_is_pure_no_skip_chatter(sandbox, monkeypatch, capsys):
    """Machine-consumable contract: stdout must be names only, one per line,
    no '>>> skip [...]' line for the gated-out server, no blank lines."""
    _write_gated_manifest(sandbox)
    monkeypatch.delenv("FAKE_GATE_VAR", raising=False)
    mod = load_render_module(sandbox)

    mod.cmd_expected_servers("codex")
    out = capsys.readouterr().out

    lines = out.splitlines()
    assert lines, "expected at least one server name"
    assert all(line.strip() == line and not line.startswith(">>>") for line in lines), out
    assert "" not in lines


def test_expected_servers_exit_2_on_invalid_manifest(sandbox, capsys):
    (sandbox.mcp_dir / "manifest.yaml").write_text(
        """schema_version: 1
servers:
  missing-auth:
    transport: http
    url: https://example.invalid/mcp
    targets: [codex]
""",
        encoding="utf-8",
    )
    mod = load_render_module(sandbox)

    rc = mod.cmd_expected_servers("codex")
    err = capsys.readouterr().err

    assert rc == 2
    assert "invalid MCP manifest" in err


def test_expected_servers_rejects_unknown_cli_via_argparse(sandbox, monkeypatch, capsys):
    _write_gated_manifest(sandbox)
    mod = load_render_module(sandbox)
    monkeypatch.setattr("sys.argv", ["render.py", "--expected-servers", "not-a-real-cli"])

    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("cli", DIALECTS)
def test_written_file_never_contains_expanded_token(sandbox_with_live_configs, cli):
    sb = sandbox_with_live_configs
    mod = load_render_module(sb)
    getattr(mod, WRITE_FN[cli])()
    raw = sb.live_config_path(cli).read_text(encoding="utf-8")

    expected_ref = {
        "claude": "${FAKE_TOKEN}",
        "antigravity": '"FAKE_TOKEN"',
        "opencode": "Bearer {env:FAKE_TOKEN}",
        "codex": '"FAKE_TOKEN"',
    }[cli]
    assert expected_ref in raw, f"{cli}: riferimento env atteso {expected_ref!r} non trovato"
    # nessun token "vero" (il manifest di test non ne contiene: verifichiamo
    # solo che non compaia un valore che SEMBRI un secret espanso, es. un
    # blob esadecimale lungo che non sia gia' uno dei nostri fake-*)
    assert "sk-" not in raw and "AKIA" not in raw


def test_windows_opencode_config_uses_appdata_layout(sandbox, monkeypatch):
    mod = load_render_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    expected = sandbox.home / "AppData" / "Roaming" / "opencode" / "opencode.json"
    assert mod._opencode_config_path() == expected


def test_atomic_write_retries_windows_sharing_violation(sandbox, monkeypatch, tmp_path):
    mod = load_render_module(sandbox)
    target = tmp_path / "config.json"
    target.write_text("old", encoding="utf-8")
    real_replace = mod.os.replace
    attempts = []

    def flaky_replace(source, destination):
        attempts.append((source, destination))
        if len(attempts) < 3:
            raise PermissionError("sharing violation")
        return real_replace(source, destination)

    monkeypatch.setattr(mod.os, "replace", flaky_replace)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)
    mod._atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert len(attempts) == 3
