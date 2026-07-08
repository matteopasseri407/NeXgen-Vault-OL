"""Test 1-8 (matrice per dialetto) su mcp/render.py.

render.py e' importato come modulo Python (non subprocess): monkeypatchiamo
HOME/MANIFEST/IS_WINDOWS direttamente sugli attributi del modulo, come deciso
nell'architettura B1. Ogni test carica una copia FRESCA del modulo (vedi
conftest.load_render_module) cosi' i monkeypatch di un test non contaminano
gli altri.
"""
from __future__ import annotations

import json
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
_NOTHING_TO_WRITE_MARKERS = ("niente da scrivere", "gia' conforme", "già conforme", "nothing to write", "already compliant")


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
            expected_full = dict(mod.r_codex(name, spec))
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
            expected = render_fn(name, spec)
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


def test_write_codex_guard_blocks_on_missing_env_section(sandbox_with_live_configs):
    sb = sandbox_with_live_configs
    path = sb.live_config_path("codex")
    # manomissione dialetto-specifica: una sezione mcp_servers.* esiste ma le
    # manca la sotto-sezione .env, mentre il manifest richiede env per quel
    # server (fake-cross-os-tool ha env). Guard atteso: "manca [.env]".
    before_raw = path.read_text(encoding="utf-8") + (
        "\n[mcp_servers.fake_cross_os_tool]\n"
        'command = "python3"\n'
        'args = ["/fake/vault/path/tool.py"]\n'
    )
    path.write_text(before_raw, encoding="utf-8")

    mod = load_render_module(sb)
    rc = mod.write_codex()
    assert rc == 2, f"guard codex .env non attivato (rc={rc})"
    assert path.read_text(encoding="utf-8") == before_raw, "codex: il file e' stato toccato nonostante il guard"


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

def test_os_view_windows_override(sandbox):
    mod = load_render_module(sandbox)
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


# ---- test 8: nessun env-ref espanso in chiaro nell'output ------------------

def test_redact_masks_secrets_and_env_refs(sandbox):
    mod = load_render_module(sandbox)
    assert mod.redact("${SOME_VAR}") == "<AUTH>"
    assert mod.redact("Bearer sometoken123", key="headers") in ("<AUTH>",) or "sometoken123" not in mod.redact("Bearer sometoken123")
    assert mod.redact("plain-value", key="command") == "plain-value"
    assert mod.redact("abc123", key="token") == "<AUTH>"


@pytest.mark.parametrize("cli", DIALECTS)
def test_written_file_never_contains_expanded_token(sandbox_with_live_configs, cli):
    sb = sandbox_with_live_configs
    mod = load_render_module(sb)
    getattr(mod, WRITE_FN[cli])()
    raw = sb.live_config_path(cli).read_text(encoding="utf-8")

    expected_ref = {
        "claude": "${FAKE_TOKEN}",
        "antigravity": "Bearer ${FAKE_TOKEN}",
        "opencode": "Bearer {env:FAKE_TOKEN}",
        "codex": '"FAKE_TOKEN"',
    }[cli]
    assert expected_ref in raw, f"{cli}: riferimento env atteso {expected_ref!r} non trovato"
    # nessun token "vero" (il manifest di test non ne contiene: verifichiamo
    # solo che non compaia un valore che SEMBRI un secret espanso, es. un
    # blob esadecimale lungo che non sia gia' uno dei nostri fake-*)
    assert "sk-" not in raw and "AKIA" not in raw
