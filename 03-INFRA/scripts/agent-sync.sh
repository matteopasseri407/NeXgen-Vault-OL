#!/usr/bin/env bash
# agent-sync — aligns the AI agent runtimes (Claude Code, Codex, Antigravity)
# with the KnowledgeVault's universal layer. Cloud-first via the remote hub,
# but ALWAYS functional offline: the operative source is the local copy of
# the vault; the cloud only syncs the machines together.
#
# Idempotent, but split into lanes:
#   agent-sync pull     = pull cloud + healthcheck, without touching CLI runtime
#   agent-sync guard    = pull + regenerate CLI runtime + healthcheck, no push
#   agent-sync apply    = explicit alias of guard, for manual use
#   agent-sync publish  = publish already-made local commits, no provisioning
#   agent-sync doctor   = healthcheck/alert only, no pull/provisioning
#   agent-sync full     = old full behavior, manual only
# With no arguments it stays "full" for compatibility. The timer must call "guard".
# Never auto-commits content: whoever writes commits (agents or the user).
set -u

HOME_DIR="${HOME:-$HOME}"
VAULT="${KNOWLEDGE_VAULT_PATH:-$HOME_DIR/KnowledgeVault}"
REMOTE="${KNOWLEDGE_VAULT_REMOTE:-origin}"
BRANCH="${KNOWLEDGE_VAULT_BRANCH:-main}"
# ── Vault 2.1 — Engine/Data separation (Strangler Fig) ───────────────────────
# Explicit boundary between the ENGINE (neutral code, packageable later into
# a sanitized repo/branch) and the DATA (personal or company vault). Today the
# two live together, so the DEFAULTS reproduce the historical paths EXACTLY:
# without the env vars, behavior is identical to before, zero breakage.
# Tomorrow the engine moves by only setting AGENT_ENGINE_ROOT (e.g. a
# 03-INFRA/Vibecoder-Engine folder or a dedicated repo mounted elsewhere) and
# the data with AGENT_VAULT_DATA, without touching a single line of this script.
AGENT_VAULT_DATA="${AGENT_VAULT_DATA:-$VAULT}"
AGENT_ENGINE_ROOT="${AGENT_ENGINE_ROOT:-$VAULT/03-INFRA}"
ENGINE_SCRIPTS="$AGENT_ENGINE_ROOT/scripts"
UL="$AGENT_ENGINE_ROOT/agent-universal-layer"
AG="$HOME_DIR/.agents/skills"
LOG_DIR="$HOME_DIR/.local/state"
LOG="$LOG_DIR/agent-sync.log"
mkdir -p "$LOG_DIR" "$AG"

log() { printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG"; }

MODE="${1:-full}"
case "$MODE" in
  pull)
    DO_PULL=1; DO_APPLY=0; DO_PUSH=0; DO_CREDS=0; DO_HEALTH=1 ;;
  guard|apply)
    DO_PULL=1; DO_APPLY=1; DO_PUSH=0; DO_CREDS=0; DO_HEALTH=1 ;;
  publish)
    DO_PULL=0; DO_APPLY=0; DO_PUSH=1; DO_CREDS=0; DO_HEALTH=0 ;;
  doctor)
    DO_PULL=0; DO_APPLY=0; DO_PUSH=0; DO_CREDS=0; DO_HEALTH=1 ;;
  full)
    DO_PULL=1; DO_APPLY=1; DO_PUSH=1; DO_CREDS=1; DO_HEALTH=1 ;;
  -h|--help|help)
    cat <<'EOF'
agent-sync modes:
  pull     Pull the KnowledgeVault from the remote and run healthcheck. Does not rewrite CLI runtime files.
  guard    Recurring safe propagation: pull, regenerate CLI runtime files, run healthcheck. Does not push.
  apply    Same as guard, explicit manual name for provisioning.
  publish  Push already-committed local vault changes to the remote (and mirror origin if configured).
  doctor   Run healthcheck/alerts only.
  full     Legacy full run: pull, apply runtime files, publish, creds, healthcheck.

Default without arguments: full, for backward compatibility.
The recurring timer should use: agent-sync guard
EOF
    exit 0 ;;
  *)
    printf 'agent-sync: unknown mode: %s\nUse: agent-sync --help\n' "$MODE" >&2
    exit 2 ;;
esac

log "agent-sync: start mode=$MODE"

# ── 1. Pull from the cloud (best-effort: offline blocks nothing) ────────────
if [ "$DO_PULL" = 1 ]; then
  if [ -x "$ENGINE_SCRIPTS/sync-vault-from-oracle.sh" ]; then
    # dedicated sync helper (advanced setup): used only if present
    if "$ENGINE_SCRIPTS/sync-vault-from-oracle.sh" >>"$LOG" 2>&1; then
      log "pull: ok (dedicated helper)"
    else
      log "pull: cloud unreachable or state not syncable — continuing with the local copy"
    fi
  elif git -C "$AGENT_VAULT_DATA" remote get-url "$REMOTE" >/dev/null 2>&1; then
    # standard case: git pull --ff-only from the configured remote ($REMOTE, default origin)
    if git -C "$AGENT_VAULT_DATA" pull --ff-only "$REMOTE" "$BRANCH" >>"$LOG" 2>&1; then
      log "pull: ok (git pull --ff-only from $REMOTE)"
    else
      log "pull: $REMOTE unreachable or not fast-forward — continuing with the local copy"
    fi
  else
    log "pull: no remote '$REMOTE' configured — local copy only (fine for a single machine)"
  fi
fi

if [ "$DO_APPLY" = 1 ]; then

# ── 2. Instructions: a single canonical file, Claude anti-duplication pointer ───
CANON="$UL/instructions/AGENTS.md"
write_claude_pointer() {
  target="$HOME_DIR/CLAUDE.md"
  tmp="$(mktemp)" || return 1
  cat >"$tmp" <<EOF
# Claude compatibility pointer

Canonical instructions live at:
$CANON

At session start, read and follow that file when the user-specific agent policy is needed.
Do not duplicate the full bootstrap in CLAUDE.md.
EOF
  if [ -f "$target" ] && [ ! -L "$target" ] && cmp -s "$tmp" "$target"; then
    rm -f "$tmp"
    return 0
  fi
  rm -f "$target"
  mv "$tmp" "$target" && log "instructions: wrote Claude pointer $target"
}

if [ -f "$CANON" ]; then
  write_claude_pointer
  # NB: Antigravity ACTUALLY reads ~/.gemini/config/AGENTS.md (verified with a
  # behavioral probe); ~/ANTIGRAVITY.md was dead wiring copied from the Codex
  # pattern and is no longer managed.
  for f in "$HOME_DIR/.gemini/config/AGENTS.md" "$HOME_DIR/.codex/AGENTS.md"; do
    if [ "$(readlink -f "$f" 2>/dev/null)" != "$(readlink -f "$CANON")" ]; then
      mkdir -p "$(dirname "$f")"
      ln -sfn "$CANON" "$f" && log "instructions: relinked $f"
    fi
  done
  # one-time cleanup of the dead symlink (only if it's still OUR old link)
  if [ -L "$HOME_DIR/ANTIGRAVITY.md" ] && [ "$(readlink -f "$HOME_DIR/ANTIGRAVITY.md")" = "$(readlink -f "$CANON")" ]; then
    rm -f "$HOME_DIR/ANTIGRAVITY.md" && log "instructions: removed dead symlink ~/ANTIGRAVITY.md (Antigravity doesn't read it)"
  fi
else
  log "WARNING: missing $CANON — instructions not relinked"
fi

# ── 2.5. Antigravity MCP: unify the MCP server config across the various runtimes ─
MC_SRC="$HOME_DIR/.gemini/antigravity/mcp_config.json"
if [ -f "$MC_SRC" ]; then
  for f in "$HOME_DIR/.gemini/antigravity-cli/mcp_config.json" "$HOME_DIR/.gemini/antigravity-ide/mcp_config.json" "$HOME_DIR/.gemini/config/mcp_config.json"; do
    d="$(dirname "$f")"
    mkdir -p "$d"
    if [ "$(readlink -f "$f" 2>/dev/null)" != "$(readlink -f "$MC_SRC")" ]; then
      ln -sfn "$MC_SRC" "$f" && log "mcp: relinked $f"
    fi
  done
fi

# ── 2.6. OpenCode: self-managed config on Linux (avoids the Windows wrappers in opencode.json) ──
# The local OpenCode config (~/.config/opencode/opencode.json) is not
# overwritten, to preserve the native Linux commands of the MCP connectors.

# ── 2.7. Deterministic agent utilities ─────────────────────────────────────
LOCAL_BIN="$HOME_DIR/.local/bin"
mkdir -p "$LOCAL_BIN"
NOW_SRC="$ENGINE_SCRIPTS/agent-now.sh"
if [ -f "$NOW_SRC" ]; then
  chmod +x "$NOW_SRC" 2>/dev/null || true
  if [ "$(readlink -f "$LOCAL_BIN/agent-now" 2>/dev/null)" != "$(readlink -f "$NOW_SRC" 2>/dev/null)" ]; then
    ln -sfn "$NOW_SRC" "$LOCAL_BIN/agent-now" && log "utils: relinked agent-now"
  fi
fi

# vault-push: helper for publishing infra files (commit + clean-rebase push)
VP_SRC="$ENGINE_SCRIPTS/vault-push.sh"
if [ -f "$VP_SRC" ]; then
  chmod +x "$VP_SRC" 2>/dev/null || true
  if [ "$(readlink -f "$LOCAL_BIN/vault-push" 2>/dev/null)" != "$(readlink -f "$VP_SRC" 2>/dev/null)" ]; then
    ln -sfn "$VP_SRC" "$LOCAL_BIN/vault-push" && log "utils: relinked vault-push"
  fi
fi

OCR_SRC="$ENGINE_SCRIPTS/vault-ocr-local.sh"
if [ -f "$OCR_SRC" ]; then
  chmod +x "$OCR_SRC" 2>/dev/null || true
  if [ "$(readlink -f "$LOCAL_BIN/vault-ocr-local" 2>/dev/null)" != "$(readlink -f "$OCR_SRC" 2>/dev/null)" ]; then
    ln -sfn "$OCR_SRC" "$LOCAL_BIN/vault-ocr-local" && log "utils: relinked vault-ocr-local"
  fi
fi

# ── 2.75. agent-sync timer: additive recurring guard ─
# The timer must no longer do the old full run with push/rebase/creds, but
# must still automatically propagate the manifest, skills, and instructions.
# The recurring lane is `agent-sync guard`: pull + apply + doctor, no push.
if [ "$(uname -s)" = Linux ]; then
  UNIT_DIR="$HOME_DIR/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  SVC="$UNIT_DIR/agent-sync.service"
  TMR="$UNIT_DIR/agent-sync.timer"
  changed_units=0
  tmp="$(mktemp)" || tmp=""
  if [ -n "$tmp" ]; then
    cat >"$tmp" <<'EOF'
[Unit]
Description=KnowledgeVault agent sync guard (pull + apply + healthcheck, no publish)

[Service]
Type=oneshot
ExecStart=%h/.local/bin/agent-sync guard
EOF
    if ! cmp -s "$tmp" "$SVC" 2>/dev/null; then
      [ -f "$SVC" ] && cp -f "$SVC" "$SVC.pre-pull-mode-$(date +%Y%m%d-%H%M%S).bak"
      mv "$tmp" "$SVC" && changed_units=1 && log "systemd: agent-sync.service set to pull mode"
    else
      rm -f "$tmp"
    fi
  fi
  tmp="$(mktemp)" || tmp=""
  if [ -n "$tmp" ]; then
    cat >"$tmp" <<'EOF'
[Unit]
Description=agent-sync guard every 30 minutes and shortly after login

[Timer]
OnStartupSec=3min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
EOF
    if ! cmp -s "$tmp" "$TMR" 2>/dev/null; then
      [ -f "$TMR" ] && cp -f "$TMR" "$TMR.pre-pull-mode-$(date +%Y%m%d-%H%M%S).bak"
      mv "$tmp" "$TMR" && changed_units=1 && log "systemd: agent-sync.timer updated"
    else
      rm -f "$tmp"
    fi
  fi
  if [ "$changed_units" = 1 ] && command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >>"$LOG" 2>&1 || log "systemd: user daemon-reload failed (best-effort)"
  fi
fi

# ── 2.8. Unified MCP (Vault 2.0 Phase 1): CLI configs generated from the manifest ──
# Single source: $UL/mcp/manifest.yaml → render.py generator → 4 dialects.
# The 3 static files (OpenCode/Antigravity/Codex) are realigned idempotently
# (backup + guard, no-op if already conformant). Claude self-manages
# .claude.json live: for it, ONLY --diff as a sentinel, never written to directly.
MCP_GEN="$UL/mcp/render.py"
if [ -f "$MCP_GEN" ] && command -v python3 >/dev/null 2>&1; then
  for cli in opencode antigravity codex; do
    python3 "$MCP_GEN" --write "$cli" >>"$LOG" 2>&1
    rc=$?
    case "$rc" in
      0) log "mcp-gen: $cli aligned with the manifest" ;;
      3) log "mcp-gen: $cli has no default config file yet (never launched?) — open it once, then re-run agent-sync" ;;
      *) log "mcp-gen: $cli NOT aligned (best-effort, continuing)" ;;
    esac
  done
  # Claude rewrites .claude.json live: only realign it if NO Claude session is
  # active, so as not to overwrite it underneath it. While Claude is active,
  # only the sentinel runs. write_claude is fail-safe regardless (never
  # writes when in doubt).
  if pgrep -x claude >/dev/null 2>&1; then
    log "mcp-gen: claude ACTIVE -> not touching .claude.json live (sentinel only)"
  else
    python3 "$MCP_GEN" --write claude >>"$LOG" 2>&1
    rc=$?
    case "$rc" in
      0) log "mcp-gen: claude aligned (was closed)" ;;
      3) log "mcp-gen: claude has no .claude.json yet (never launched?) — open Claude Code once, then re-run agent-sync" ;;
      *) log "mcp-gen: claude not aligned (best-effort)" ;;
    esac
  fi
  diag="$(python3 "$MCP_GEN" 2>/dev/null | tail -1)"
  drift="$(printf '%s' "$diag" | sed -n 's/.*match, \([0-9]\{1,\}\) with differences.*/\1/p')"
  extra="$(printf '%s' "$diag" | sed -n 's/.*differences, \([0-9]\{1,\}\) outside the manifest.*/\1/p')"
  [ "${drift:-0}" -gt 0 ] && log "mcp-gen: SENTINEL — $drift servers diverge from the manifest"
  [ "${extra:-0}" -gt 0 ] && log "mcp-gen: NOTE — $extra servers outside the manifest (kept as-is): register them in manifest.yaml to propagate them everywhere"
  # Drift notification: NOT here (single-megaphone consolidation). agent-sync
  # stays SILENT: it only executes and logs. The only megaphone is
  # agent-healthcheck (via agent-doctor, with debounce and human-readable
  # format), called at the end of the run. One single alert surface.
fi

# ── 3. Vault custom skills → universal root ~/.agents/skills ─────────────
if [ -d "$UL/skills" ]; then
  for d in "$UL/skills"/*/; do
    [ -d "$d" ] || continue
    s="$(basename "$d")"
    if [ "$(readlink "$AG/$s" 2>/dev/null)" != "${d%/}" ]; then
      rm -rf "$AG/$s"
      ln -sfn "${d%/}" "$AG/$s" && log "vault skill: relinked $s"
    fi
  done
fi

# ── 3.5. Universal skill catalog (lazy-loading for ALL CLIs) ──────────
# Regenerates ~/.agents/skills/INDEX.md (name + description for every skill):
# every agent, even without a skill format (Antigravity/OpenCode/local),
# consults it and opens the SKILL.md only when the task requires it (see AGENTS.md).
SKILLS_SYNC="$ENGINE_SCRIPTS/skills-sync.py"
if [ -f "$SKILLS_SYNC" ] && command -v python3 >/dev/null 2>&1; then
  # --apply (idempotent, additive) also installs the manifest's GitHub skills
  # and regenerates the INDEX: without it, a registered skill only arrives
  # wherever someone runs apply by hand.
  python3 "$SKILLS_SYNC" --apply >>"$LOG" 2>&1 || log "skills-manifest: apply failed (best-effort)"
fi

# ── 4. Universal root → agent runtimes ────────────────────────────────
# Links every universal skill inside .claude/skills and .codex/skills,
# honoring the per-provider exclusions (lazy loading: the skill stays in
# ~/.agents/skills and the agent reads it on-demand, but it is not preloaded).
# Provider-specific skills (real directories not present in .agents) are
# never touched.
for rt in "$HOME_DIR/.claude/skills" "$HOME_DIR/.codex/skills"; do
  [ -d "$rt" ] || continue
  # GUARD (self-loop bug fix, humanizer/frontend-design): if the runtime is a
  # symlink to the ENTIRE ~/.agents/skills hub, the commands below would go
  # THROUGH the symlink: `rm -rf $rt/$s` would delete the real bytes in the
  # hub and `ln -sfn` would create a self-loop. In that case, convert the
  # runtime into a real folder (the only model where lazy exclusions
  # actually work) and proceed with per-skill links.
  if [ -L "$rt" ] && [ "$(readlink -f "$rt")" = "$(readlink -f "$AG")" ]; then
    rm "$rt" && mkdir -p "$rt" && log "runtime: $rt was a symlink to the hub — converted to a real folder (per-skill links + active exclusions)"
  fi
  case "$rt" in
    */.claude/*) EXCL="$UL/skills-exclude-claude.txt" ;;
    */.codex/*)  EXCL="$UL/skills-exclude-codex.txt" ;;
    *)           EXCL="" ;;
  esac
  for d in "$AG"/*/; do
    [ -e "$d" ] || continue
    s="$(basename "$d")"
    if [ -n "$EXCL" ] && [ -f "$EXCL" ] && grep -qxF "$s" "$EXCL"; then
      if [ -L "$rt/$s" ]; then
        rm -f "$rt/$s" && log "runtime: $s excluded from $rt (lazy)"
      fi
      continue
    fi
    if [ ! -L "$rt/$s" ]; then
      rm -rf "$rt/$s"
      ln -sfn "$AG/$s" "$rt/$s" && log "runtime: relinked $s in $rt"
    fi
  done
done

# ── 4.5. Claude hooks: deploy the vault script + merge the triggers into settings.json ──
# Universal like on Windows: same hook, idempotent, preserves other hooks.
HOOK_SRC="$UL/hooks/claude-vault-checkpoint.mjs"
CLAUDE_DIR="$HOME_DIR/.claude"
HOOK_DST="$CLAUDE_DIR/claude-vault-checkpoint.mjs"
SETTINGS="$CLAUDE_DIR/settings.json"
if [ -f "$HOOK_SRC" ] && [ -d "$CLAUDE_DIR" ]; then
  if ! cmp -s "$HOOK_SRC" "$HOOK_DST" 2>/dev/null; then
    cp -f "$HOOK_SRC" "$HOOK_DST" && log "claude-hooks: deployed $HOOK_DST"
  fi
  if [ -f "$SETTINGS" ] && command -v jq >/dev/null 2>&1; then
    CMD="node \"$HOOK_DST\""
    tmp="$(mktemp)"
    if jq --arg c "$CMD" '
        .hooks = (.hooks // {})
        | .hooks.SessionStart = ((.hooks.SessionStart // []) | if any(.[]?; .hooks[]?.command == $c) then . else . + [{hooks:[{type:"command",command:$c,timeout:5}]}] end)
        | .hooks.PreCompact   = ((.hooks.PreCompact   // []) | if any(.[]?; .hooks[]?.command == $c) then . else . + [{hooks:[{type:"command",command:$c,timeout:5}]}] end)
      ' "$SETTINGS" >"$tmp" 2>/dev/null && [ -s "$tmp" ]; then
      if ! cmp -s "$tmp" "$SETTINGS"; then
        cp -f "$SETTINGS" "$SETTINGS.pre-hooks-$(date +%Y%m%d-%H%M%S).bak"
        mv "$tmp" "$SETTINGS" && log "claude-hooks: merged SessionStart/PreCompact into $SETTINGS"
      else
        rm -f "$tmp"
      fi
    else
      rm -f "$tmp"; log "claude-hooks: jq merge failed or jq missing, settings.json unchanged"
    fi
  fi
fi

fi # DO_APPLY

# ── 5. Push of already-made local commits (never auto-commits dirty files) ─────
# The workstations (Linux/Windows) share the same branch on the remote hub.
# If the other one has published in the meantime, the push gets REJECTED
# (non-fast-forward): that's different from "remote hub offline". In that
# case we do a CLEAN rebase and retry, so the benign divergence (different
# files on the two machines) resolves itself. Only a REAL conflict (same
# lines) stays manual: rebase --abort + the healthcheck flags it. Never an
# automatic merge, never lost work.
if [ "$DO_PUSH" = 1 ] && git -C "$AGENT_VAULT_DATA" rev-parse --verify "$REMOTE/$BRANCH" >/dev/null 2>&1; then
  ahead="$(git -C "$AGENT_VAULT_DATA" rev-list --count "$REMOTE/$BRANCH..$BRANCH" 2>/dev/null || echo 0)"
  if [ "${ahead:-0}" -gt 0 ]; then
    push_ok=0
    if git -C "$AGENT_VAULT_DATA" push "$REMOTE" "$BRANCH" >>"$LOG" 2>&1; then
      push_ok=1; log "push: $ahead commit(s) published to $REMOTE"
    elif git -C "$AGENT_VAULT_DATA" fetch --prune "$REMOTE" "$BRANCH" >>"$LOG" 2>&1; then
      # The remote hub is reachable (fetch ok) → the push was rejected, not offline.
      if [ -n "$(git -C "$AGENT_VAULT_DATA" status --porcelain --untracked-files=no)" ]; then
        log "push: rejected but the working tree has uncommitted tracked changes — not rebasing, resolve by hand"
      elif git -C "$AGENT_VAULT_DATA" rebase "$REMOTE/$BRANCH" >>"$LOG" 2>&1; then
        if git -C "$AGENT_VAULT_DATA" push "$REMOTE" "$BRANCH" >>"$LOG" 2>&1; then
          push_ok=1; log "push: divergence resolved via clean rebase, published to $REMOTE"
        else
          log "push: still rejected after rebase — will retry next run"
        fi
      else
        git -C "$AGENT_VAULT_DATA" rebase --abort >/dev/null 2>&1
        log "push: DIVERGENCE WITH CONFLICTS — manual 'git pull --rebase' needed (the healthcheck will flag it)"
      fi
    else
      log "push: $REMOTE unreachable (offline) — commits stay local, will retry"
    fi
    # origin = MIRROR of the remote-hub line: if it rejects (different from
    # offline), realign it with force-with-lease, never rebasing history
    # already on the remote hub.
    if [ "$push_ok" = 1 ] && ! git -C "$AGENT_VAULT_DATA" push origin "$BRANCH" >>"$LOG" 2>&1; then
      if git -C "$AGENT_VAULT_DATA" fetch --prune origin "$BRANCH" >>"$LOG" 2>&1 \
         && git -C "$AGENT_VAULT_DATA" push --force-with-lease origin "$BRANCH" >>"$LOG" 2>&1; then
        log "push: origin (mirror) realigned to the remote-hub line (force-with-lease)"
      else
        log "push: GitHub (origin) unreachable or lease expired — will retry next run"
      fi
    fi
  fi
fi

# ── 6. Auto-provisioning of alert creds (trusted machines) + grouped healthcheck ───────────
if [ "$DO_CREDS" = 1 ]; then
  [ -x "$ENGINE_SCRIPTS/ensure-alert-creds.sh" ] && "$ENGINE_SCRIPTS/ensure-alert-creds.sh" >>"$LOG" 2>&1
fi
TG_CONF="$HOME_DIR/.config/environment.d/91-telegram-alert.conf"
[ -f "$TG_CONF" ] && { set -a; . "$TG_CONF"; set +a; }
if [ "$DO_HEALTH" = 1 ] && [ -x "$ENGINE_SCRIPTS/agent-healthcheck.sh" ]; then
  "$ENGINE_SCRIPTS/agent-healthcheck.sh" >>"$LOG" 2>&1 || log "healthcheck: did not succeed (best-effort)"
fi

dirty="$(git -C "$AGENT_VAULT_DATA" status --porcelain 2>/dev/null | wc -l)"
[ "$dirty" -gt 0 ] && log "note: $dirty uncommitted file(s) in the vault (not touching them)"

log "agent-sync: completed mode=$MODE"
exit 0
