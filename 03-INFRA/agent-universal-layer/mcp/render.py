#!/usr/bin/env python3
"""MCP generator — Vault 2.0 Phase 1.
Reads manifest.yaml and, for each CLI, builds the MCP config in the right
dialect.
  - default (--diff): compares against the live file, does NOT write. Secrets
    are reduced to <AUTH> on both sides: the structure is compared without
    ever touching the tokens.
  - --write CLI: regenerates ONLY that CLI's MCP section from the manifest,
    with a surgical substitution (the rest of the file stays intact), in the
    file's own style. Makes a backup, validates, and AUTO-BLOCKS if a non-MCP
    section would end up modified.
  - Servers OUTSIDE THE MANIFEST in the live file: never deleted. They are
    KEPT as-is and flagged (additive rule: something new installed by an
    agent is the new standard to register in the manifest and propagate).
  - Exit codes for --write: 0 = written or already compliant, 2 = blocked by
    a safety guard (see the STOP message), 3 = the CLI's default config file
    does not exist yet (it has never been launched once) — nothing to patch
    until it has been opened at least once."""
from __future__ import annotations
import argparse, difflib, json, os, platform, re, sys, time
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None
TOMLDecodeError = tomllib.TOMLDecodeError if tomllib is not None else ValueError
try:
    import yaml
except ModuleNotFoundError:
    print("render.py needs PyYAML: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

HOME = Path.home()
HERE = Path(__file__).parent
# The manifest is DATA (the user's real server list, concrete values), never
# something the engine repo should serve — read it from vault_data, not from
# HERE (this script may be running from a cloned engine, where the sibling
# manifest.yaml is only the generic product template).
VAULT_DATA = Path(os.environ.get("AGENT_VAULT_DATA") or str(HOME / "KnowledgeVault"))
MANIFEST = VAULT_DATA / "03-INFRA" / "agent-universal-layer" / "mcp" / "manifest.yaml"
IS_WINDOWS = platform.system() == "Windows"

# Antigravity reaches HTTP MCP servers through this local bridge.  It is
# intentionally exact: an implicit npx update would run new code as the user.
MCP_REMOTE_PACKAGE = "mcp-remote@0.1.38"

SECRET_KEY = re.compile(r"(token|secret|password|authorization|bearer|api[_-]?key|cookie)", re.I)
LONGTOK = re.compile(r"^[A-Za-z0-9_\-\.=+/]{40,}$")

def toml_loads(text):
    if tomllib is None:
        sys.exit("render.py needs Python 3.11+ or tomli for TOML: pip install tomli")
    return tomllib.loads(text)

def redact(obj, key=None):
    if isinstance(obj, dict):
        return {k: redact(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x, key) for x in obj]
    if isinstance(obj, str):
        if key and SECRET_KEY.search(str(key)):
            return "<AUTH>"
        if "${" in obj or "{env:" in obj:
            return "<AUTH>"
        if obj.lower().startswith("authorization:") or "bearer " in obj.lower():
            return "<AUTH>"
        if LONGTOK.match(obj) and any(c.isdigit() for c in obj):
            return "<AUTH>"
    return obj

# ---- per-dialect rendering (REAL values: env-ref where needed) ------------------

def r_claude(name, s):
    if s["transport"] == "stdio":
        return {"type": "stdio", "command": s["command"], "args": s.get("args", []), "env": s.get("env", {})}
    # header via env-ref: Claude Code expands ${VAR} in headers at startup,
    # so the token never stays in plaintext in .claude.json.
    return {"type": "http", "url": s["url"],
            "headers": {"Authorization": f"Bearer ${{{s['auth']['env']}}}"}}

def r_codex(name, s):
    if s["transport"] == "stdio":
        d = {"command": s["command"], "args": s.get("args", [])}
        if s.get("env"):
            d["env"] = s["env"]
        return d
    t = s.get("timeouts", {})
    return {"url": s["url"], "bearer_token_env_var": s["auth"]["env"],
            "startup_timeout_sec": float(t.get("startup", 120)),
            "tool_timeout_sec": float(t.get("tool", 120))}

def r_antigravity(name, s):
    if s["transport"] == "stdio":
        return {"command": s["command"], "args": s.get("args", []), "env": s.get("env", {})}
    hdr = f"Authorization: Bearer ${{{s['auth']['env']}}}"
    return {"command": "npx", "args": ["-y", MCP_REMOTE_PACKAGE, s["url"], "--header", hdr], "env": {}}

def r_opencode(name, s):
    if s["transport"] == "stdio":
        d = {"type": "local", "command": [s["command"], *s.get("args", [])], "enabled": True}
        if s.get("timeouts", {}).get("tool"):
            d["timeout"] = int(float(s["timeouts"]["tool"]) * 1000)
        if s.get("env"):
            d["environment"] = s["env"]
        return d
    url = "{env:%s}" % s["url_env"] if s.get("url_env") else s["url"]
    auth = "Bearer {env:%s}" % s["auth"]["env"]
    return {"type": "remote", "url": url, "headers": {"Authorization": auth}, "enabled": True, "oauth": False}

CLI = {
    "claude":      dict(render=r_claude,      name=lambda n: n),
    "codex":       dict(render=r_codex,       name=lambda n: n.replace("-", "_")),
    "antigravity": dict(render=r_antigravity, name=lambda n: n),
    "opencode":    dict(render=r_opencode,    name=lambda n: n),
}

def os_view(s):
    """View of the server for the current OS: if we're on Windows and the
    server has a 'windows:' block (command/args/env/... override), apply it;
    otherwise discard the 'windows' key. This way the single manifest serves
    both OSes — Windows values get populated by running render.py on the
    Windows machine, not guessed."""
    if IS_WINDOWS and isinstance(s.get("windows"), dict):
        merged = {**s, **s["windows"]}
        merged.pop("windows", None)
        return merged
    return {k: v for k, v in s.items() if k != "windows"}

def _env_present(var):
    """True if the env var is defined and non-empty."""
    v = os.environ.get(var, "").strip()
    return bool(v)

def _required_ok(s):
    """Respect manifest `require_env`: skip a server if its gating env var
    is unset. Lets a Local-Only install omit Cloud-Only MCP (firecrawl, n8n,
    vault-library, vault-ocr) instead of pointing them at dead ports."""
    req = s.get("require_env")
    if not req:
        return True
    return _env_present(req)

def load_manifest():
    raw = yaml.safe_load(MANIFEST.read_text("utf-8"))["servers"]
    out = {}
    for n, s in raw.items():
        s = os_view(s)
        if not _required_ok(s):
            print(f">>> skip [{n}]: require_env not satisfied (Local-Only?)")
            continue
        out[n] = s
    return out

def keep_extras(gen, live, label):
    """A server in the live file but NOT in the manifest is not drift to be
    deleted: it's something new installed by an agent (vault rule: 'it's not
    drift, it's the new standard everyone should get'). It is KEPT as-is and
    flagged until it gets registered in the manifest. Codex already does this
    by design (per-section patch); here the JSON writers get aligned to it."""
    out = dict(gen)
    for k in live:
        if k not in out:
            out[k] = live[k]
            print(f">>> OUTSIDE THE MANIFEST [{label}]: server '{k}' KEPT. Register it in manifest.yaml to propagate it everywhere.")
    return out

# ---- loading live configs (MCP section only) ---------------------------

def load_current(cli):
    try:
        if cli == "claude":
            return json.loads((HOME / ".claude.json").read_text("utf-8")).get("mcpServers", {})
        if cli == "codex":
            d = toml_loads((HOME / ".codex/config.toml").read_text("utf-8"))
            return {k: {kk: vv for kk, vv in v.items() if kk != "tools"} for k, v in d.get("mcp_servers", {}).items()}
        if cli == "antigravity":
            d = json.loads((HOME / ".gemini/antigravity/mcp_config.json").read_text("utf-8"))
            return {k: {kk: vv for kk, vv in v.items() if kk != "$typeName"} for k, v in d.get("mcpServers", {}).items()}
        if cli == "opencode":
            return json.loads((HOME / ".config/opencode/opencode.json").read_text("utf-8")).get("mcp", {})
    except FileNotFoundError:
        return None     # CLI not installed on this machine
    return {}

# ---- structural diff (--diff mode) -------------------------------------

def diff_struct(path, cur, exp, out):
    if isinstance(exp, dict) and isinstance(cur, dict):
        for k in sorted(set(exp) | set(cur)):
            if k not in cur:
                out.append(f"    - {path}{k}: MISSING in the live file (expected: {json.dumps(exp[k], ensure_ascii=False)})")
            elif k not in exp:
                out.append(f"    + {path}{k}: extra in the live file (value: {json.dumps(cur[k], ensure_ascii=False)})")
            else:
                diff_struct(f"{path}{k}.", cur[k], exp[k], out)
    elif cur != exp:
        out.append(f"    ~ {path[:-1]}: live={json.dumps(cur, ensure_ascii=False)}  expected={json.dumps(exp, ensure_ascii=False)}")

def cmd_diff():
    man = load_manifest()
    ok = bad = extra = 0
    for cli, spec in CLI.items():
        current = load_current(cli)
        print(f"\n========== {cli.upper()} ==========")
        if current is None:
            print("  (config not present: CLI not installed here, or installed but never launched yet, skipped)"); continue
        wanted = {n: s for n, s in man.items() if cli in s["targets"]}
        seen = set()
        for name, s in wanted.items():
            key = spec["name"](name); seen.add(key)
            exp = redact(spec["render"](name, s))
            if key not in current:
                print(f"  [MISSING]  {name} -> the live file has no '{key}'"); bad += 1; continue
            out = []
            diff_struct("", redact(current[key]), exp, out)
            if out:
                print(f"  [DIFF]   {name}"); print("\n".join(out)); bad += 1
            else:
                print(f"  [OK]     {name}"); ok += 1
        for k in sorted(set(current) - seen):
            print(f"  [EXTRA]  '{k}' in the live file but not in the manifest (kept by --write: register it to propagate it)"); extra += 1
    print(f"\n---- summary: {ok} servers match, {bad} with differences, {extra} outside the manifest ----")

# ---- per-style serializers ------------------------------------------------

def s_inline(obj, ind=0):
    """OpenCode: inline array of scalars, expanded objects."""
    pad, pad2 = "  " * ind, "  " * (ind + 1)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        body = ",\n".join(f'{pad2}{json.dumps(k, ensure_ascii=False)}: {s_inline(v, ind + 1)}' for k, v in obj.items())
        return "{\n" + body + "\n" + pad + "}"
    if isinstance(obj, list):
        if all(not isinstance(x, (dict, list)) for x in obj):
            return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in obj) + "]"
        body = ",\n".join(f'{pad2}{s_inline(x, ind + 1)}' for x in obj)
        return "[\n" + body + "\n" + pad + "]"
    return json.dumps(obj, ensure_ascii=False)

def s_standard(obj, ind=0):
    """Antigravity: standard json.dump indent=2 (expanded arrays)."""
    return json.dumps(obj, indent=2, ensure_ascii=False)

def reorder(gen, live):
    """Aligns gen's key order to live's (values still come from gen)."""
    if isinstance(gen, dict) and isinstance(live, dict):
        out = {k: reorder(gen[k], live[k]) for k in live if k in gen}
        for k in gen:
            out.setdefault(k, gen[k])
        return out
    return gen

def _value_span(text, brace_idx):
    """Index right after the '}' that closes the object started at brace_idx,
    skipping strings (and braces inside strings, e.g. {env:...})."""
    depth = 0; i = brace_idx; in_str = esc = False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
        elif c == '"': in_str = True
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("closing brace not found")

def _insert_new_top_level_key(raw, key, indent_exact):
    """Inserts `"key": {}` as a new top-level entry in a JSON object's text,
    right after the opening brace, preserving everything else untouched.
    Only used when the key is entirely absent (fresh install, e.g. a CLI
    launched for the first time with zero MCP servers configured): never to
    replace an existing key sitting at an unexpected indent, which stays a
    STOP (found in a cross-vendor audit, 2026-07-09: without this, a brand
    new config file with no "mcpServers" section at all blocked sync forever
    with no way out except a manual edit)."""
    open_idx = raw.index("{")
    close_idx = _value_span(raw, open_idx) - 1
    body = raw[open_idx + 1:close_idx]
    m = re.search(r'\n(\s+)\S', body)
    indent = indent_exact if indent_exact is not None else (m.group(1) if m else "  ")
    entry = f'{indent}{json.dumps(key)}: {{}}'
    new_body = ("\n" + entry + "\n") if not body.strip() else ("\n" + entry + "," + body)
    return raw[:open_idx + 1] + new_body + raw[close_idx:]

# ---- generic surgical writer for JSON files -----------------------------

def _prune_backups(path, keep=3):
    """Keeps only the last `keep` .bak-* backups of a file, deletes older ones."""
    backs = sorted(path.parent.glob(path.name + ".bak-*"))
    for old in backs[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass

def _owner_only_mode(path):
    try:
        return (path.stat().st_mode & 0o700) or 0o600
    except OSError:
        return 0o600

def _secure_create_text(path, text, encoding="utf-8", mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            fd = None
            f.write(text)
    finally:
        if fd is not None:
            os.close(fd)

def _secure_backup(path, text, encoding="utf-8"):
    stem = path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    for attempt in range(100):
        suffix = "" if attempt == 0 else f"-{attempt}"
        bak = path.with_name(stem + suffix)
        try:
            _secure_create_text(bak, text, encoding=encoding, mode=_owner_only_mode(path))
            return bak
        except FileExistsError:
            continue
    raise FileExistsError(f"could not create a unique backup for {path}")

def _atomic_write_text(path, text, encoding="utf-8"):
    """write_text() truncates then writes: a CLI re-reading the file (this
    runs on a 30-minute recurring timer, config files are live) can catch it
    mid-write and see a truncated/empty JSON. Write to a same-directory temp
    file and os.replace() it in, which POSIX/Windows both guarantee atomic
    for a rename onto an existing path. Create the temp file with owner-only
    permissions from the start: these files can contain MCP auth material, so
    creating first and chmodding later would expose a small but real window."""
    mode = _owner_only_mode(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    if tmp.exists():
        tmp = path.with_name(f"{path.name}.{os.getpid()}-{time.monotonic_ns()}.tmp")
    _secure_create_text(tmp, text, encoding=encoding, mode=mode)
    os.replace(tmp, path)

def write_json_section(path, key, new_section, live_section, serialize, indent_exact=None):
    if not path.exists():
        print(f">>> {path.name} not present: CLI never launched yet (no default config file), skipping."); return 3
    raw = path.read_text("utf-8")
    try:
        live = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f">>> STOP: {path.name} is not valid JSON ({e}). Fix it or restore a .bak-* backup before rerunning."); return 2
    if not isinstance(live, dict):
        print(f">>> STOP: {path.name} JSON root is not an object; refusing to patch it."); return 2
    # indent_exact: if given, only match the key at that exact indentation
    # (for .claude.json, where "mcpServers" also appears nested in projects).
    ind_pat = re.escape(indent_exact) if indent_exact is not None else r'[ \t]*'
    m = re.search(rf'(?m)^({ind_pat}){re.escape(json.dumps(key))}[ \t]*:[ \t]*', raw)
    if not m or raw[m.end():m.end() + 1] != "{":
        if key in live:
            print(f">>> STOP: can't find the {json.dumps(key)} section at the expected indent."); return 2
        # Fresh install: the key is entirely absent, not just at an
        # unexpected indent. Insert an empty placeholder and let the normal
        # surgical-replace logic below fill it in.
        try:
            raw = _insert_new_top_level_key(raw, key, indent_exact)
            live = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as e:
            print(f">>> STOP: cannot insert the {json.dumps(key)} placeholder ({e})."); return 2
        m = re.search(rf'(?m)^({ind_pat}){re.escape(json.dumps(key))}[ \t]*:[ \t]*', raw)
        if not m or raw[m.end():m.end() + 1] != "{":
            print(f">>> STOP: inserted the {json.dumps(key)} placeholder but can't find it again (unexpected file shape)."); return 2
        print(f">>> {json.dumps(key)} section was missing entirely (fresh install): inserted an empty placeholder.")
    indent = m.group(1)
    end = _value_span(raw, m.end())
    inner = serialize(new_section)
    lines = inner.split("\n")
    block_val = lines[0] + "\n" + "\n".join(indent + l for l in lines[1:])
    block = f'{indent}{json.dumps(key)}: ' + block_val
    new_text = raw[:m.start()] + block + raw[end:]

    if new_text[:m.start()] != raw[:m.start()] or not new_text.endswith(raw[end:]):
        print(">>> STOP: the replacement touches text outside the section."); return 2
    try:
        new_parsed = json.loads(new_text)
    except json.JSONDecodeError as e:
        print(f">>> STOP: result is not valid JSON ({e})."); return 2
    for k in live:
        if k != key and new_parsed.get(k) != live[k]:
            print(f">>> STOP: the non-MCP section '{k}' would end up modified."); return 2

    def _mask(line):   # never a plaintext token in ANY generator output
        return re.sub(r'(Bearer )[^"\s]+', r'\1<MASK>', line)
    d = list(difflib.unified_diff(raw.splitlines(), new_text.splitlines(),
                                  f"{path.name} (live)", f"{path.name} (generated)", lineterm=""))
    print("\n".join(_mask(l) for l in d) if d else "(no textual difference: already compliant)")
    sem = []
    diff_struct("", live_section, new_section, sem)
    print("\nSEMANTIC differences in the MCP section (order-independent):")
    print("\n".join(_mask(l) for l in sem) if sem else "    (none: already compliant with the manifest)")
    if not d:
        print("\n>>> Nothing to write."); return 0

    bak = _secure_backup(path, raw, "utf-8")
    _prune_backups(path)
    _atomic_write_text(path, new_text, "utf-8")
    json.loads(path.read_text("utf-8"))
    print(f"\n>>> WRITTEN and validated (JSON ok). Backup: {bak}")
    return 0

def write_opencode():
    path = HOME / ".config/opencode/opencode.json"
    if not path.exists():
        print(">>> opencode.json not present: OpenCode never launched yet (no default config file), skipping."); return 3
    try:
        live = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        print(f">>> STOP: {path.name} is not valid JSON ({e}). Fix it or restore a .bak-* backup before rerunning."); return 2
    if not isinstance(live, dict):
        print(f">>> STOP: {path.name} JSON root is not an object; refusing to patch it."); return 2
    man = load_manifest()
    gen = {n: r_opencode(n, s) for n, s in man.items() if "opencode" in s["targets"]}
    gen = keep_extras(gen, live.get("mcp", {}), "opencode")
    new_mcp = reorder(gen, live.get("mcp", {}))
    return write_json_section(path, "mcp", new_mcp, live.get("mcp", {}), s_inline)

def write_antigravity():
    path = HOME / ".gemini/antigravity/mcp_config.json"
    if not path.exists():
        print(">>> mcp_config.json not present: Antigravity never launched yet (no default config file), skipping."); return 3
    try:
        live = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        print(f">>> STOP: {path.name} is not valid JSON ({e}). Fix it or restore a .bak-* backup before rerunning."); return 2
    if not isinstance(live, dict):
        print(f">>> STOP: {path.name} JSON root is not an object; refusing to patch it."); return 2
    live_servers = live.get("mcpServers", {})
    man = load_manifest()
    gen = {}
    for n, s in man.items():
        if "antigravity" not in s["targets"]:
            continue
        d = r_antigravity(n, s)
        for k, v in live_servers.get(n, {}).items():   # preserve internal extras (e.g. $typeName)
            d.setdefault(k, v)
        gen[n] = d
    gen = keep_extras(gen, live_servers, "antigravity")
    new_servers = reorder(gen, live_servers)
    return write_json_section(path, "mcpServers", new_servers, live_servers, s_standard)

# ---- surgical writer for Codex (TOML, targeted per-section patch) ---------

def _toml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)            # 120.0, 600.0
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False)

def _codex_body(d):
    return [f"{k} = {_toml_scalar(v)}" for k, v in d.items()]

def _section_headers(lines):
    out = {}
    for i, l in enumerate(lines):
        m = re.match(r'^\[(.+?)\]\s*$', l)
        if m:
            out[m.group(1)] = i
    return out

def _content_range(lines, header_idx):
    """Content lines right after a section header: up to a blank line, the
    next header, or EOF. Never touches headers or separating blank lines."""
    s = header_idx + 1
    e = s
    while e < len(lines) and lines[e].strip() != "" and not lines[e].startswith("["):
        e += 1
    return s, e

def write_codex(path=None):
    path = path or HOME / ".codex/config.toml"
    if not path.exists():
        print(f">>> {path.name} not present: Codex never launched yet (no default config file), skipping."); return 3
    raw = path.read_text("utf-8")
    lines = raw.split("\n")
    try:
        live = toml_loads(raw)
    except TOMLDecodeError as e:
        print(f">>> STOP: {path.name} is not valid TOML ({e}). Fix it or restore a .bak-* backup before rerunning."); return 2
    live_srv = live.get("mcp_servers", {})
    man = load_manifest()

    targets = {}   # cname -> (direct_fields, env_or_None)
    for n, s in man.items():
        if "codex" not in s["targets"]:
            continue
        full = dict(r_codex(n, s))
        env = full.pop("env", None)
        targets[n.replace("-", "_")] = (full, env)

    headers = _section_headers(lines)
    # insertion point for NEW servers = end of the last mcp_servers.* block
    mcp_ends = [_content_range(lines, idx)[1] for h, idx in headers.items()
                if h == "mcp_servers" or h.startswith("mcp_servers.")]
    insert_pos = max(mcp_ends) if mcp_ends else len(lines)

    edits = []        # (start, end, new_lines) — in-place patch of existing sections
    add_block = []    # lines for NEW servers to append to the mcp_servers block
    for cname, (direct, env) in targets.items():
        if f"mcp_servers.{cname}" in headers:
            s, e = _content_range(lines, headers[f"mcp_servers.{cname}"])
            edits.append((s, e, _codex_body(direct)))
            if env is not None:
                if f"mcp_servers.{cname}.env" in headers:
                    s2, e2 = _content_range(lines, headers[f"mcp_servers.{cname}.env"])
                    edits.append((s2, e2, _codex_body(env)))
                else:
                    print(f">>> STOP: [mcp_servers.{cname}] exists but [.env] is missing — rare case, not patching."); return 2
        else:
            add_block += ["", f"[mcp_servers.{cname}]"] + _codex_body(direct)
            if env is not None:
                add_block += ["", f"[mcp_servers.{cname}.env]"] + _codex_body(env)
    if add_block:
        edits.append((insert_pos, insert_pos, add_block))

    new_lines = lines[:]
    for s, e, nl in sorted(edits, key=lambda x: x[0], reverse=True):
        new_lines[s:e] = nl
    new_text = "\n".join(new_lines)

    # --- guard: parsing + non-MCP untouched + tools preserved + fields == manifest
    try:
        np_ = toml_loads(new_text)
    except Exception as ex:
        print(f">>> STOP: result is not valid TOML ({ex})."); return 2
    for k in live:
        if k != "mcp_servers" and np_.get(k) != live[k]:
            print(f">>> STOP: the non-MCP section '{k}' would end up modified."); return 2
    for cname, (direct, env) in targets.items():
        ns = np_["mcp_servers"][cname]
        ls = live_srv.get(cname)              # None if it's a new server
        if ls is not None and ns.get("tools") != ls.get("tools"):
            print(f">>> STOP: the 'tools' overlay of {cname} would end up modified."); return 2
        for kk, vv in direct.items():
            if ns.get(kk) != vv:
                print(f">>> STOP: field {cname}.{kk} does not match the manifest."); return 2
        if env is not None and ns.get("env") != env:
            print(f">>> STOP: env of {cname} does not match."); return 2
    for cname in live_srv:                          # no other MCP server touched
        if cname not in targets and np_["mcp_servers"][cname] != live_srv[cname]:
            print(f">>> STOP: the non-manifest server '{cname}' would end up modified."); return 2

    d = list(difflib.unified_diff(raw.splitlines(), new_text.splitlines(),
                                  f"{path.name} (live)", f"{path.name} (generated)", lineterm=""))
    print("\n".join(d) if d else "(no textual difference: Codex already compliant)")
    if not d:
        print("\n>>> Nothing to write."); return 0

    bak = _secure_backup(path, raw, "utf-8")
    _prune_backups(path)
    _atomic_write_text(path, new_text, "utf-8")
    toml_loads(path.read_text("utf-8"))
    print(f"\n>>> WRITTEN and validated (TOML ok). Backup: {bak}")
    return 0

def write_claude(path=None):
    """Surgical patch of ONLY the top-level mcpServers (indent 2) of
    .claude.json. Preserves the live file's literal tokens (the manifest
    doesn't contain them) and ignores the mcpServers nested inside projects.
    Fail-safe: write_json_section's guards block it if anything else would be
    touched. Meant to run while Claude is CLOSED (Claude rewrites .claude.json
    live)."""
    path = path or HOME / ".claude.json"
    if not path.exists():
        print(">>> .claude.json not present: Claude never launched yet (no default config file), skipping."); return 3
    try:
        live = json.loads(path.read_text("utf-8"))
    except json.JSONDecodeError as e:
        print(f">>> STOP: {path.name} is not valid JSON ({e}). Fix it or restore a .bak-* backup before rerunning."); return 2
    if not isinstance(live, dict):
        print(f">>> STOP: {path.name} JSON root is not an object; refusing to patch it."); return 2
    live_mcp = live.get("mcpServers", {})
    man = load_manifest()
    gen = {}
    for n, s in man.items():
        if "claude" not in s["targets"]:
            continue
        d = r_claude(n, s)   # http header already as ${VAR}: no literal token in .claude.json
        gen[n] = d
    gen = keep_extras(gen, live_mcp, "claude")
    new_mcp = reorder(gen, live_mcp)
    return write_json_section(path, "mcpServers", new_mcp, live_mcp, s_standard, indent_exact="  ")

def cmd_write(cli):
    if cli == "opencode":
        return write_opencode()
    if cli == "antigravity":
        return write_antigravity()
    if cli == "codex":
        return write_codex()
    if cli == "claude":
        return write_claude()
    print(f"--write for '{cli}' not implemented yet.")
    return 1

def main():
    ap = argparse.ArgumentParser(description="MCP generator from a single manifest (Vault 2.0 Phase 1).")
    ap.add_argument("--write", metavar="CLI", choices=list(CLI), help="regenerate a CLI's MCP config (default: diff only).")
    args = ap.parse_args()
    if args.write:
        return cmd_write(args.write)
    cmd_diff()
    return 0

if __name__ == "__main__":
    sys.exit(main())
