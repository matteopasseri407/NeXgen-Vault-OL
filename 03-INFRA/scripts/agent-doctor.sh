#!/usr/bin/env bash
# agent-doctor — verifies that ALL agents are genuinely aligned and operational.
# Read-only: changes nothing. Exit 0 if no FAIL, 1 otherwise.
# Usage:
#   agent-doctor.sh             readable report (colors, sections)
#   agent-doctor.sh --summary   one-line summary (for digests/alerts)
set -u

VAULT="${KNOWLEDGE_VAULT_PATH:-$HOME/KnowledgeVault}"
UL="$VAULT/03-INFRA/agent-universal-layer"
CANON="$UL/instructions/AGENTS.md"
REMOTE="${KNOWLEDGE_VAULT_REMOTE:-origin}"
BRANCH="${KNOWLEDGE_VAULT_BRANCH:-main}"
OCJSON="$HOME/.config/opencode/opencode.json"
PASS=0; WARN=0; FAILN=0; FAILS=""

QUIET=0
STRICT=0
for arg in "$@"; do
  case "$arg" in
    --summary) QUIET=1 ;;
    --strict) STRICT=1 ;;
    -h|--help)
      cat <<'EOF'
agent-doctor.sh [--summary] [--strict]

Default: fast structural and service health checks.
--summary: one-line output for alerting.
--strict: add real CLI consumer checks, including OpenCode MCP list,
          Antigravity global MCP path, and vault-ocr stdio framing.
EOF
      exit 0 ;;
  esac
done

ok()   { PASS=$((PASS+1)); [ "$QUIET" = 1 ] || printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { WARN=$((WARN+1)); [ "$QUIET" = 1 ] || printf '  \033[33m⚠\033[0m %s\n' "$*"; }
fail() { FAILN=$((FAILN+1)); FAILS="${FAILS}${FAILS:+, }$*"; [ "$QUIET" = 1 ] || printf '  \033[31m✗\033[0m %s\n' "$*"; }
sec()  { [ "$QUIET" = 1 ] || printf '\n\033[1m%s\033[0m\n' "$*"; }
code() { curl -s -o /dev/null -m 6 -w '%{http_code}' "$@" 2>/dev/null || echo 000; }

[ "$QUIET" = 1 ] || printf '\033[1m=== agent-doctor: agent alignment check ===\033[0m\n'

sec "Host"
case "$(uname -s)" in Linux) HOST=linux;; Darwin) HOST=mac;; *) HOST=other;; esac
ok "detected: $HOST ($(hostname), $(uname -s))"

sec "Vault (memory) — git vs remote hub + GitHub mirror"
if git -C "$VAULT" rev-parse >/dev/null 2>&1; then
  git -C "$VAULT" fetch --prune "$REMOTE" "$BRANCH" >/dev/null 2>&1 || warn "fetch $REMOTE failed (offline?)"
  b=$(git -C "$VAULT" rev-list --count "$BRANCH..$REMOTE/$BRANCH" 2>/dev/null || echo '?')
  a=$(git -C "$VAULT" rev-list --count "$REMOTE/$BRANCH..$BRANCH" 2>/dev/null || echo '?')
  d=$(git -C "$VAULT" status --porcelain --untracked-files=no 2>/dev/null | wc -l | tr -d ' ')
  [ "$b" = 0 ] && ok "aligned with $REMOTE/$BRANCH (0 behind)" || fail "$b commits behind the cloud"
  [ "$a" = 0 ] && ok "no unpublished local commits" || warn "$a unpublished local commits"
  [ "$d" = 0 ] && ok "working tree clean (tracked files)" || warn "$d tracked files not committed (blocks the pull)"
else
  fail "the vault is not a git repo: $VAULT"
fi

sec "Canonical instructions (single AGENTS.md, Claude anti-duplication pointer)"
[ -f "$CANON" ] && ok "canonical file present" || fail "missing canonical file $CANON"
CLAUDE_FILE="$HOME/CLAUDE.md"
if [ -f "$CLAUDE_FILE" ] && [ ! -L "$CLAUDE_FILE" ] && grep -Fq "$CANON" "$CLAUDE_FILE" && grep -Fq "compatibility pointer" "$CLAUDE_FILE"; then
  ok "Claude pointer -> canonical AGENTS.md"
else
  fail "Claude.md must be a lightweight pointer, not a copy/symlink of the canonical file ($CLAUDE_FILE)"
fi
# NOTE (found 2026-07-01): Antigravity actually reads ~/.gemini/config/AGENTS.md,
# NOT ~/ANTIGRAVITY.md — that symlink exists (a pattern copied from Codex) but was
# never read by the app, and this check gave a false "ok" for days. Verified only
# with a real behavioral probe (agy -p), not with the symlink's mere existence:
# this loop only proves the WIRING, not that the CLI honors the file.
# If a phantom "aligned" ever shows up again, this is suspect number one.
for pair in "Codex:$HOME/.codex/AGENTS.md" "Antigravity:$HOME/.gemini/config/AGENTS.md"; do
  name="${pair%%:*}"; f="${pair#*:}"
  if [ "$(readlink -f "$f" 2>/dev/null)" = "$(readlink -f "$CANON" 2>/dev/null)" ]; then
    ok "$name → canonical AGENTS.md"
  else
    fail "$name does NOT point to the canonical file ($f)"
  fi
done
if [ -f "$OCJSON" ]; then
  grep -q "instructions/AGENTS.md" "$OCJSON" && ok "OpenCode instructions → AGENTS.md" || fail "OpenCode instructions do NOT point to AGENTS.md"
else
  fail "missing $OCJSON"
fi

sec "Deterministic agent utilities"
if command -v agent-now >/dev/null 2>&1; then
  now_payload="$(agent-now --json 2>/dev/null || true)"
  if printf '%s\n' "$now_payload" | grep -q '"source": "system_clock"' && printf '%s\n' "$now_payload" | grep -q '"local_time"'; then
    ok "agent-now available and working"
  else
    fail "agent-now present but output invalid"
  fi
else
  fail "agent-now not in PATH (run agent-sync.sh)"
fi

sec "MCP connectors — reachability"
c=$(code http://127.0.0.1:5678/healthz); [ "$c" = 200 ] && ok "n8n-mcp (5678): $c" || fail "n8n-mcp (5678): $c"
c=$(code http://127.0.0.1:33002/); { [ "$c" = 200 ] || [ "$c" = 302 ]; } && ok "firecrawl (33002): $c" || fail "firecrawl (33002): $c"
c=$(code http://127.0.0.1:33003/health); [ "$c" = 200 ] && ok "vault-ocr (33003): $c" || fail "vault-ocr (33003): $c"
if [ -n "${VAULT_LIBRARY_URL:-}" ]; then
  c=$(code -H "Authorization: Bearer ${VAULT_LIBRARY_TOKEN:-}" "$VAULT_LIBRARY_URL")
  { [ "$c" != 000 ] && [ "$c" != 401 ] && [ "$c" != 403 ]; } && ok "vault-library: $c (up)" || fail "vault-library: $c"
else
  warn "VAULT_LIBRARY_URL not in env"
fi
# Semantic RAG (optional, not part of the bundled deploy/ stack): checked through
# the MCP container's own network (same lane the agents use), so it also catches
# the footgun of "container recreated outside the expected docker network", which
# a plain curl on the exposed port would not see. Container name is overridable
# because it depends on the user's own docker-compose project name.
if [ -n "${VAULT_SEMANTIC_CONTAINER:-}" ] && [ -n "${REMOTE_ALIAS:-}" ]; then
  rag_c="$(ssh -o BatchMode=yes -o ConnectTimeout=6 "$REMOTE_ALIAS" \
    "docker exec $VAULT_SEMANTIC_CONTAINER python3 -c \"import urllib.request as u; print(u.urlopen('http://vault-semantic:8080/health', timeout=5).status)\"" 2>/dev/null || true)"
  [ "$rag_c" = "200" ] && ok "vault-semantic RAG (MCP lane): 200" || fail "vault-semantic RAG (MCP lane): ${rag_c:-KO}"
else
  warn "VAULT_SEMANTIC_CONTAINER or REMOTE_ALIAS not set, skipping semantic RAG check (optional component)"
fi
command -v npx >/dev/null 2>&1 && ok "playwright: npx available" || warn "npx not in PATH (playwright MCP)"

sec "Tokens in env"
for v in N8N_MCP_TOKEN VAULT_LIBRARY_TOKEN VAULT_LIBRARY_URL; do
  [ -n "${!v:-}" ] && ok "$v present" || fail "$v missing"
done
[ -n "${DEEPSEEK_API_KEY:-}" ] && ok "DEEPSEEK_API_KEY present" || warn "DEEPSEEK_API_KEY missing (only affects direct DeepSeek fallback/batch; OpenCode Go stays fine)"

sec "MCP configured in the runtimes (Vault 2.0 drift detection)"
if command -v python3 >/dev/null 2>&1 && [ -f "$UL/mcp/render.py" ]; then
  render_out="$(python3 "$UL/mcp/render.py" 2>&1)"
  render_rc=$?
  if [ "$render_rc" -ne 0 ]; then
    # A crash here (missing PyYAML, a broken manifest, a permission error...)
    # must never read as "no drift found": empty/error output would otherwise
    # fall through to the "100% aligned" branch below, the worst possible
    # false-green in the one check whose job is to catch misconfiguration.
    fail "render.py failed to run (exit $render_rc): $(printf '%s' "$render_out" | tail -1)"
  else
  drift_scan="$render_out"
  claude_pending=0
  if pgrep -x claude >/dev/null 2>&1; then
    claude_section="$(printf '%s\n' "$render_out" | awk '/^========== CLAUDE ==========/{p=1; next} /^========== /{p=0} p')"
    if printf '%s\n' "$claude_section" | grep -Eq '\[(DIFF|MISSING|ERROR)\]'; then
      claude_pending=1
      drift_scan="$(printf '%s\n' "$render_out" | awk '/^========== CLAUDE ==========/{p=1; next} /^========== /{p=0} !p')"
    fi
  fi
  if printf '%s\n' "$drift_scan" | grep -Eq '\[(DIFF|MISSING|ERROR)\]'; then
    fail "MCP drift detected against the canonical manifest"
    # Pull out the offending lines to show them in the report
    drift_lines="$(printf '%s\n' "$drift_scan" | grep -E '\[DIFF\]|\[MISSING\]|\[ERROR\]')"
    [ "$QUIET" = 1 ] || printf '%s\n' "$drift_lines" | while IFS= read -r line; do warn "drift detail: $line"; done
  elif [ "$claude_pending" = 1 ]; then
    warn "Claude MCP not aligned but Claude is running: will be written by the next agent-sync once Claude is closed"
  else
    ok "MCP configs 100% aligned with the canonical manifest"
  fi
  # A CLI that was never launched has no config file to patch: render.py just
  # notes "(config not present...)" for it, with no [DIFF]/[MISSING] tag, so
  # the drift scan above reads as clean even though that CLI has zero MCP
  # servers mounted. OpenCode is already caught above (missing $OCJSON is a
  # hard fail there because it holds both instructions and MCP config); check
  # the other three here so this doesn't stay a silent gap.
  for cli in claude codex antigravity; do
    section="$(printf '%s\n' "$render_out" | awk -v s="========== $(printf '%s' "$cli" | tr '[:lower:]' '[:upper:]') ==========" '$0==s{p=1; next} /^========== /{p=0} p')"
    if printf '%s\n' "$section" | grep -q "config not present"; then
      case "$cli" in
        claude) have=0; command -v claude >/dev/null 2>&1 && have=1 ;;
        codex) have=0; command -v codex >/dev/null 2>&1 && have=1 ;;
        antigravity) have=0; [ -d "$HOME/.gemini" ] && have=1 ;;
      esac
      [ "$have" = 1 ] && warn "$cli is installed but has never been launched: its MCP config doesn't exist yet, open it once and re-run agent-sync"
    fi
  done
  fi
else
  warn "python3 or render.py not found, skipping MCP drift check"
fi

if [ "$STRICT" = 1 ]; then
  sec "CLI consumer conformance (--strict)"
  AG_SRC="$HOME/.gemini/antigravity/mcp_config.json"
  AG_GLOBAL="$HOME/.gemini/config/mcp_config.json"
  if [ "$(readlink -f "$AG_GLOBAL" 2>/dev/null)" = "$(readlink -f "$AG_SRC" 2>/dev/null)" ]; then
    ok "Antigravity global MCP path -> generated source"
  else
    fail "Antigravity global MCP path does NOT point to the generated source ($AG_GLOBAL)"
  fi
  if [ -s "$AG_GLOBAL" ]; then
    ok "Antigravity global mcp_config.json not empty"
  else
    fail "Antigravity global mcp_config.json empty or missing"
  fi
  if command -v python3 >/dev/null 2>&1 && [ -s "$AG_GLOBAL" ]; then
    ag_missing="$(python3 - "$AG_GLOBAL" <<'PY' 2>/dev/null || true
import json, sys
path = sys.argv[1]
expected = {"firecrawl", "n8n-mcp", "vault-library", "vault-ocr"}
data = json.load(open(path, encoding="utf-8"))
got = set((data.get("mcpServers") or {}).keys())
print(",".join(sorted(expected - got)))
PY
)"
    [ -z "$ag_missing" ] && ok "Antigravity global contains the core MCP servers" || fail "Antigravity global is missing core MCP servers: $ag_missing"
  fi

  if command -v opencode >/dev/null 2>&1; then
    oc_tmp="$(mktemp)"
    if command -v setsid >/dev/null 2>&1; then
      setsid timeout -k 5s 25s opencode mcp list >"$oc_tmp" 2>&1
      oc_rc=$?
    else
      timeout -k 5s 25s opencode mcp list >"$oc_tmp" 2>&1
      oc_rc=$?
    fi
    oc_out="$(cat "$oc_tmp" 2>/dev/null)"
    rm -f "$oc_tmp"
    oc_missing=""
    for srv in firecrawl n8n-mcp vault-library vault-ocr; do
      printf '%s\n' "$oc_out" | grep -F "$srv" | grep -Fqi "connected" || oc_missing="${oc_missing}${oc_missing:+, }$srv"
    done
    if [ "$oc_rc" = 124 ] || [ "$oc_rc" = 137 ]; then
      fail "OpenCode mcp list timed out during strict check"
    elif [ -z "$oc_missing" ]; then
      ok "OpenCode mcp list shows the core servers connected"
    else
      fail "OpenCode mcp list does not confirm: $oc_missing"
    fi
  else
    warn "opencode not in PATH, skipping OpenCode consumer test"
  fi

  OCR_MCP="$UL/ocr/mcp/vault_ocr_mcp.py"
  if command -v python3 >/dev/null 2>&1 && [ -f "$OCR_MCP" ]; then
    if python3 - "$OCR_MCP" <<'PY' >/dev/null 2>&1; then
import json, subprocess, sys

script = sys.argv[1]
cmd = ["python3", script]
requests = [
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "doctor", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
]

proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
proc.stdin.write("".join(json.dumps(req, separators=(",", ":")) + "\n" for req in requests))
proc.stdin.close()
jsonl = [json.loads(line) for line in proc.stdout if line.strip()]
assert proc.wait(timeout=15) == 0
assert jsonl[0]["result"]["serverInfo"]["name"] == "vault-ocr"
assert any(tool["name"] == "ocr_healthcheck" for tool in jsonl[1]["result"]["tools"])

proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
for req in requests:
    data = json.dumps(req, separators=(",", ":")).encode()
    proc.stdin.write(b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data)
proc.stdin.close()
raw = proc.stdout.read()
assert proc.wait(timeout=15) == 0
responses = []
i = 0
while i < len(raw):
    end = raw.index(b"\r\n\r\n", i)
    headers = raw[i:end].decode().split("\r\n")
    length = int([h for h in headers if h.lower().startswith("content-length:")][0].split(":", 1)[1].strip())
    start = end + 4
    responses.append(json.loads(raw[start:start + length]))
    i = start + length
assert responses[0]["result"]["serverInfo"]["name"] == "vault-ocr"
assert any(tool["name"] == "ocr_healthcheck" for tool in responses[1]["result"]["tools"])
PY
      ok "vault-ocr stdio framing OK (JSONL + Content-Length)"
    else
      fail "vault-ocr stdio framing test failed"
    fi
  else
    warn "vault-ocr MCP wrapper not found, skipping framing test"
  fi
fi

sec "Shared browser and defaults"
if [ "$HOST" = linux ] && command -v xdg-settings >/dev/null 2>&1; then
  db=$(xdg-settings get default-web-browser 2>/dev/null)
  [ "$db" = "agent-chrome.desktop" ] && ok "system default browser → agent-chrome" || warn "system default browser = ${db:-?} (expected agent-chrome.desktop)"
fi

sec "Skills"
# Counting entries is not enough: a broken/self-loop symlink (humanizer bug,
# found 2026-07-01) "exists" for ls but is empty to whoever reads it.
# [ -e ] fails on ELOOP/broken links.
n=0; broken=""
for s in "$HOME/.agents/skills"/*; do
  [ -L "$s" ] || [ -d "$s" ] || continue
  if [ -e "$s" ]; then n=$((n+1)); else broken="$broken $(basename "$s")"; fi
done
[ "${n:-0}" -gt 0 ] && ok "$n readable skills in ~/.agents/skills" || fail "no readable skill in ~/.agents/skills"
[ -n "$broken" ] && fail "BROKEN skills (self-loop/dangling symlink):$broken — fix with: python3 \$VAULT/03-INFRA/scripts/skills-sync.py --apply"
# Runtimes must resolve the essential skills (from the manifest) down to a real SKILL.md.
ess_ok=1
for ess in humanizer knowledge-vault-hygiene frontend-design; do
  for rt in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
    [ -d "$rt" ] || continue
    if [ ! -f "$rt/$ess/SKILL.md" ]; then
      ess_ok=0; fail "$rt/$ess: SKILL.md NOT readable (broken link or missing skill)"
    fi
  done
done
[ "$ess_ok" = 1 ] && ok "essential skills resolve to a real SKILL.md in claude+codex"

sec "OpenCode config"
if command -v node >/dev/null 2>&1 && [ -f "$OCJSON" ]; then
  if node -e "JSON.parse(require('fs').readFileSync('$OCJSON','utf8'))" 2>/dev/null; then
    ok "opencode.json: valid JSON"
    grep -q 'opencode-go/deepseek-v4-pro' "$OCJSON" && ok "default = opencode-go/deepseek-v4-pro (Go)" || warn "default model is not opencode-go/deepseek-v4-pro"
    grep -q '"ollama"' "$OCJSON" && warn "ollama provider present in the shared file (expected DeepSeek only: local is Windows-only)" || ok "provider = DeepSeek only (no local model in the shared config)"
  else
    fail "opencode.json: invalid JSON"
  fi
fi

sec "Local model (host-aware)"
if [ "$HOST" = linux ]; then
  if ss -ltn 2>/dev/null | grep -q ':11434'; then
    ok "Ollama running on the laptop (emergency local fallback, not the routing worker)"
  else
    ok "Ollama not listening (fine: on-demand emergency fallback)"
  fi
fi

sec "Claude hooks (vault checkpoint/briefing)"
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
  if command -v jq >/dev/null 2>&1; then
    cnt=$(jq -r '[(.hooks.SessionStart[]?.hooks[]?.command), (.hooks.PreCompact[]?.hooks[]?.command)] | map(select(test("claude-vault-checkpoint"))) | length' "$SETTINGS" 2>/dev/null || echo 0)
    if [ "${cnt:-0}" -ge 2 ]; then ok "checkpoint/briefing hook on SessionStart + PreCompact"; else fail "vault-checkpoint hook missing in settings.json (run agent-sync.sh)"; fi
  else
    warn "jq missing: skipping the Claude hook check"
  fi
else
  warn "Claude settings.json missing (Claude not installed here?)"
fi

# Public engine repo — anti-leak gates (S0). Maintainer lane: these checks
# only apply where a working clone of the public engine repo exists (the
# machine that publishes engine code). On machines without one, the whole
# section is skipped silently — a consumer install has nothing to verify here.
ENGINE_REPO="${ENGINE_REPO:-$HOME/NeXgen-Vault-OL}"
if [ -d "$ENGINE_REPO/.git" ]; then
  sec "Public engine repo — anti-leak gates (S0)"
  pushurl=$(git -C "$ENGINE_REPO" config --get remote.origin.pushurl 2>/dev/null || echo "")
  case "$pushurl" in
    PUSH-DISABLED*) ok "direct push disabled on the engine clone ($ENGINE_REPO)" ;;
    *) fail "direct push NOT disabled on the engine clone: git -C $ENGINE_REPO remote set-url --push origin PUSH-DISABLED-use-engine-push" ;;
  esac
  fetchurl=$(git -C "$ENGINE_REPO" config --get remote.origin.url 2>/dev/null || echo "")
  case "$fetchurl" in
    PUSH-DISABLED*|"") fail "the engine clone's remote.origin.url is not a valid URL" ;;
    *) ok "engine clone fetch url intact" ;;
  esac
  HOOKS_SRC="$UL/sanitize/engine-hooks"
  if [ -d "$HOOKS_SRC" ]; then
    for h in pre-commit commit-msg; do
      if [ -f "$ENGINE_REPO/.git/hooks/$h" ] && [ -f "$HOOKS_SRC/$h" ]; then
        if cmp -s "$ENGINE_REPO/.git/hooks/$h" "$HOOKS_SRC/$h"; then
          ok "hook $h installed and aligned with its tracked source"
        else
          warn "hook $h installed but DIFFERENT from its tracked source (drift — reinstall from $HOOKS_SRC/$h)"
        fi
      else
        fail "hook $h missing from the engine clone (.git/hooks/$h) — reinstall from $HOOKS_SRC/$h"
      fi
    done
  else
    warn "anti-leak hook sources not found ($HOOKS_SRC) — cannot verify the engine clone's hooks"
  fi
  if command -v engine-push >/dev/null 2>&1; then
    ok "engine-push available in PATH"
  else
    fail "engine-push not found in PATH (expected in ~/.local/bin) — it is the only allowed push channel for the engine repo"
  fi
fi

# Consumer engine clone — version pin (S2). Applies only where a consumer
# clone exists (default ~/.nexgen-engine, or AGENT_ENGINE_ROOT's repo root).
# Before the cutover this machine has none, so the whole section is skipped
# silently — same pattern as the S0 section above.
CONSUMER_ENGINE_ROOT="${AGENT_ENGINE_ROOT:-$HOME/.nexgen-engine/03-INFRA}"
CONSUMER_ENGINE_REPO="$(dirname "$CONSUMER_ENGINE_ROOT")"
if [ -d "$CONSUMER_ENGINE_REPO/.git" ]; then
  sec "Consumer engine clone — version pin (S2)"
  PIN_FILE="$VAULT/99-INDEX/ENGINE-PIN.txt"
  live_sha=$(git -C "$CONSUMER_ENGINE_REPO" rev-parse HEAD 2>/dev/null || echo "")
  if [ -z "$live_sha" ]; then
    fail "cannot read the consumer engine clone's HEAD ($CONSUMER_ENGINE_REPO)"
  elif [ -f "$PIN_FILE" ]; then
    pin_sha=$(head -1 "$PIN_FILE" | tr -d '[:space:]')
    if [ "$pin_sha" = "$live_sha" ]; then
      ok "consumer engine at the pinned version (${live_sha:0:7})"
    else
      fail "consumer engine at ${live_sha:0:7}, pin expects ${pin_sha:0:7} — silent drift: pull was skipped, or the pin wasn't updated after a deliberate upgrade"
    fi
  else
    warn "no engine pin set ($PIN_FILE missing) — consumer engine version isn't tracked yet, run: git -C $CONSUMER_ENGINE_REPO rev-parse HEAD > $PIN_FILE"
  fi
  pushurl=$(git -C "$CONSUMER_ENGINE_REPO" config --get remote.origin.pushurl 2>/dev/null || echo "")
  case "$pushurl" in
    PUSH-DISABLED*) ok "direct push disabled on the consumer engine clone" ;;
    *) fail "direct push NOT disabled on the consumer engine clone: git -C $CONSUMER_ENGINE_REPO remote set-url --push origin PUSH-DISABLED-use-engine-push" ;;
  esac
fi

if [ "$QUIET" = 1 ]; then
  line="agent-doctor [$HOST] PASS=$PASS WARN=$WARN FAIL=$FAILN"
  [ "$FAILN" -gt 0 ] && line="$line | FAIL: $FAILS"
  printf '%s\n' "$line"
else
  sec "Summary"
  printf "  \033[32mPASS=%s\033[0m  \033[33mWARN=%s\033[0m  \033[31mFAIL=%s\033[0m\n" "$PASS" "$WARN" "$FAILN"
  [ "$FAILN" -eq 0 ] && printf "  → \033[32malignment VERIFIED\033[0m\n" || printf "  → \033[31mthere are FAILs to fix\033[0m\n"
fi
[ "$FAILN" -eq 0 ]
