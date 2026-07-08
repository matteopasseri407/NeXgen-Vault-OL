#!/usr/bin/env bash
# agent-healthcheck — grouped alert: notifies ONLY if something is wrong (FAIL).
# Run by agent-sync on every pass, but SENDS only:
#   - immediately when a problem appears/changes (FAIL), and
#   - as a once-a-day reminder if the problem persists.
# No routine "green" report.
# Content = summary from agent-doctor (includes sync drift, MCP, instructions, tokens, skills...).
# Transport (first available): direct messaging bot (env) > webhook > desktop notify-send > log.
set -u

VAULT="${KNOWLEDGE_VAULT_PATH:-$HOME/KnowledgeVault}"
DOCTOR="$VAULT/03-INFRA/scripts/agent-doctor.sh"
STATE_DIR="$HOME/.local/state"
HB_FILE="$STATE_DIR/agent-healthcheck.state"
LOG="$STATE_DIR/agent-sync.log"
INTERVAL="${AGENT_HEALTHCHECK_INTERVAL:-86400}"   # routine: once a day
HOSTN="$(hostname)"
mkdir -p "$STATE_DIR"

[ -x "$DOCTOR" ] || exit 0
now=$(date +%s)

summary="$("$DOCTOR" --summary 2>/dev/null | tail -1)"
[ -n "$summary" ] || exit 0

problem=0
printf '%s' "$summary" | grep -q 'FAIL=[1-9]' && problem=1
# stable signature (strips long numbers like timestamps), to detect a NEW problem
sig="$(printf '%s' "$summary" | tr -d ' ')"

last=0; last_sig=""
if [ -f "$HB_FILE" ]; then
  last="$(sed -n '1p' "$HB_FILE" 2>/dev/null)"
  last_sig="$(sed -n '2p' "$HB_FILE" 2>/dev/null)"
fi
case "$last" in ''|*[!0-9]*) last=0 ;; esac

# Send ONLY if something is wrong (FAIL). No routine green report.
if [ "$problem" != 1 ]; then
  printf '%s\nok\n' "$now" > "$HB_FILE"
  exit 0
fi

send=0
[ "$sig" != "$last_sig" ] && send=1                  # new or changed problem -> immediately
[ $(( now - last )) -ge "$INTERVAL" ] && send=1      # reminder if the problem persists (once a day)
[ "$send" = 1 ] || exit 0

# Plain-language message (for the user); the technical detail stays at the
# end so it can be handed to an agent as-is. The ✗ lines come from the
# doctor's full run.
failn="$(printf '%s' "$summary" | sed -n 's/.*FAIL=\([0-9]\{1,\}\).*/\1/p')"
fail_lines="$("$DOCTOR" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep '✗' | sed 's/^[[:space:]]*✗[[:space:]]*/• /' | head -6)"
[ -n "$fail_lines" ] || fail_lines="• detail not available (see log)"

msg="🔴 Automatic agent checks failed on ${HOSTN} — $(date '+%d/%m %H:%M')

Errors found:
${fail_lines}

Fix:
Hand this prompt to an agent in Claude/Antigravity as-is:
\"Run agent-doctor, explain in plain terms what broke, and fix the FAILs\"

(State: ${summary})"

# The engine's own strings are English-only, deliberately (see agent_sync.py's
# _localize_alert): translation is the user's own concern, done in DATA, never
# hardcoded here. If vault_data/03-INFRA/alert-translate.sh exists and is
# executable, it gets this English message on stdin and its stdout (if
# non-empty) replaces it; any failure falls back to the English original.
TRANSLATOR="$VAULT/03-INFRA/alert-translate.sh"
if [ -x "$TRANSLATOR" ]; then
  translated="$(printf '%s' "$msg" | "$TRANSLATOR" 2>/dev/null)"
  [ -n "$translated" ] && msg="$translated"
fi

sent=0
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  curl -s -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${msg}" >/dev/null 2>&1 && sent=1
elif [ -n "${VAULT_ALERT_WEBHOOK:-}" ]; then
  curl -s -m 10 -X POST "$VAULT_ALERT_WEBHOOK" \
    --data-urlencode "host=${HOSTN}" \
    --data-urlencode "text=${msg}" >/dev/null 2>&1 && sent=1
fi
if [ "$sent" -ne 1 ] && command -v notify-send >/dev/null 2>&1; then
  notify-send -u critical -a agent-healthcheck "Agents: something is wrong" "$msg" >/dev/null 2>&1 && sent=1
fi
[ "$sent" -ne 1 ] && printf '%s healthcheck (no transport): %s\n' "$(date -Is)" "$summary" >> "$LOG"

printf '%s\n%s\n' "$now" "$sig" > "$HB_FILE"
exit 0
