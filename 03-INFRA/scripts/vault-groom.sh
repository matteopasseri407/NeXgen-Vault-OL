#!/usr/bin/env bash
# vault-groom.sh — the gardener's hand.
#
# Feeds the canonical playbook (03-INFRA/vault-grooming-playbook.md) to an
# LLM runner and lets it do one grooming pass. The judgement lives in the
# playbook, NOT here.
#
# Modes:
#   (default)  guarded run: propose a tranche (read-only), show it to you,
#              require a typed "yes", THEN execute exactly that tranche and
#              commit (+push unless GROOM_NOPUSH=1). Nothing is written or
#              committed without that confirmation.
#   preview    read-only only: propose a tranche and stop. Never executes.
#
# The write pass never re-derives its own tranche: it is handed the exact
# text you approved (with its sha256) and told to execute precisely that,
# closing the old plan/run gap where `run` acted on whatever the LLM felt
# like re-proposing, not on what you actually reviewed.
#
# Env: VAULT, GROOM_MODEL, GROOM_RUNNER (claude|codex|agy, default claude),
#      GROOM_NOPUSH=1 (run without push, for observed runs),
#      GROOM_LOG (override the preview/propose-pass log path),
#      GROOM_STATE_DIR (override where structured audit records land,
#      default ~/.local/state/vault-groom)
#
# Runner support is real, not cosmetic: each runner below uses that CLI's
# OWN verified read-only/write-scoping mechanism, not a shared flag set.
# `opencode` has no per-invocation permission-scoping CLI flag today (its
# permission model lives in opencode.json's own config, not something this
# script can safely toggle per run without risking either a silent
# full-access run or a broken invocation) -- selecting it fails loudly with
# that explanation instead of guessing.
set -euo pipefail

VAULT="${AGENT_VAULT_DATA:-${VAULT:-$HOME/KnowledgeVault}}"
PLAYBOOK="03-INFRA/vault-grooming-playbook.md"
AUDIT_SCRIPT="03-INFRA/scripts/vault_groom_audit.py"
MODEL="${GROOM_MODEL:-claude-sonnet-5}"
RUNNER="${GROOM_RUNNER:-claude}"
STATE_DIR="${GROOM_STATE_DIR:-$HOME/.local/state/vault-groom}"
MODE="${1:-guarded}"

TS="$(date +%Y%m%d-%H%M%S)"
# mktemp, not a predictable timestamp name: a plain "tee > $LOG" onto a
# guessable /tmp path is a symlink race (CWE-59) -- anything running as this
# same user could pre-create a symlink at that name pointing at, say,
# ~/.bashrc, and tee would clobber it with permission-inherited overwrite.
mk_log() { mktemp --suffix="$1.log" "/tmp/vault-groom-$TS-XXXXXX"; }

cd "$VAULT"

if [ "$MODE" != "guarded" ] && [ "$MODE" != "preview" ]; then
  echo "usage: $0 [preview]   (default: guarded run -- propose, confirm, execute. GROOM_RUNNER=claude|codex|agy, GROOM_NOPUSH=1 run without push)" >&2
  exit 2
fi

case "$RUNNER" in
  claude|codex|agy) ;;
  opencode)
    echo "vault-groom: GROOM_RUNNER=opencode is not supported today." >&2
    echo "  opencode has no per-invocation permission-scoping flag (its permission" >&2
    echo "  model lives in opencode.json's own config, checked once per project," >&2
    echo "  not something this script can safely toggle per run): there is no way" >&2
    echo "  to guarantee the read-only pass is actually read-only, or that the" >&2
    echo "  write pass doesn't silently inherit broader access than intended." >&2
    echo "  Use claude, codex, or agy, or define a dedicated restricted opencode" >&2
    echo "  agent profile yourself and extend this script's opencode branch." >&2
    exit 2
    ;;
  *)
    echo "vault-groom: unknown GROOM_RUNNER '$RUNNER' (supported: claude, codex, agy)" >&2
    exit 2
    ;;
esac

# Read-only lane: no Edit/Write/git -> the propose pass physically cannot mutate.
READ_TOOLS=(Read Grep Glob "Bash(python3:*)" \
  mcp__vault-library__semantic_search mcp__vault-library__search_notes \
  mcp__vault-library__read_note mcp__vault-library__recent_activity \
  mcp__vault-library__list_related mcp__vault-library__get_start_here)

# Write lane: adds file mutation + git; push is gated separately below.
WRITE_TOOLS=(Read Edit Write Grep Glob \
  "Bash(python3:*)" "Bash(git:*)" "Bash(mkdir:*)" "Bash(mv:*)" \
  mcp__vault-library__semantic_search mcp__vault-library__search_notes \
  mcp__vault-library__read_note mcp__vault-library__list_related \
  mcp__vault-library__update_note mcp__vault-library__create_note \
  mcp__vault-library__append_note)

PROPOSE_PROMPT="Read $PLAYBOOK and execute ONLY steps 1-3 (orient, run the audit heat-map, find candidates with semantic_search). Then OUTPUT a proposed grooming tranche: the notes, the action for each (compress / merge / archive / fix-frontmatter), and one line of why. DO NOT edit, write, move, or commit anything -- this is a read-only planning pass."

invoke_readonly() {
  local prompt="$1" logfile="$2"
  case "$RUNNER" in
    claude)
      # < /dev/null: claude has no reason to read stdin in -p mode, but
      # nothing stops it from trying -- and this process inherits the
      # wrapper's own stdin (the confirmation gate's future `read -r` source)
      # unless explicitly cut off here. Without this, a curious/greedy stdin
      # read on the LLM's side could silently consume the answer meant for
      # the human's "yes" prompt before the wrapper ever gets to ask.
      claude -p "$prompt" --model "$MODEL" \
        --allowedTools "${READ_TOOLS[@]}" < /dev/null 2>&1 | tee "$logfile"
      ;;
    codex)
      # -s read-only is a real Codex sandbox policy (verified via
      # `codex exec --help`), not a guess: makes the "no mutation" promise a
      # runtime guarantee, same strength as Claude's empty tool list. The
      # here-string already gives this command its own isolated stdin, same
      # effect as claude's explicit </dev/null above.
      codex exec -s read-only -m "$MODEL" -C "$VAULT" - <<<"$prompt" 2>&1 | tee "$logfile"
      ;;
    agy)
      # < /dev/null: same stdin-isolation reasoning as claude above -- agy
      # has no heredoc/here-string to isolate it implicitly.
      agy --print --model "$MODEL" --mode plan --sandbox --prompt "$prompt" < /dev/null 2>&1 | tee "$logfile"
      ;;
  esac
}

invoke_write() {
  local prompt="$1" logfile="$2"
  case "$RUNNER" in
    claude)
      if [ "${GROOM_NOPUSH:-0}" = 1 ]; then
        # The one runner where NOPUSH is a hard runtime block, not just a
        # prompt instruction: --disallowedTools makes `git push` uncallable.
        claude -p "$prompt" --model "$MODEL" \
          --allowedTools "${WRITE_TOOLS[@]}" \
          --disallowedTools "Bash(git push:*)" < /dev/null 2>&1 | tee "$logfile"
      else
        claude -p "$prompt" --model "$MODEL" \
          --allowedTools "${WRITE_TOOLS[@]}" < /dev/null 2>&1 | tee "$logfile"
      fi
      ;;
    codex)
      # NOPUSH on this runner is prompt-level only (Codex has no per-command
      # block like Claude's --disallowedTools) -- weaker than claude, said
      # plainly rather than implied.
      codex exec -s workspace-write -m "$MODEL" -C "$VAULT" - <<<"$prompt" 2>&1 | tee "$logfile"
      ;;
    agy)
      # NOPUSH here is prompt-level only too, same caveat as codex above.
      agy --print --model "$MODEL" --mode accept-edits --prompt "$prompt" < /dev/null 2>&1 | tee "$logfile"
      ;;
  esac
}

if [ "$MODE" = preview ]; then
  LOG="${GROOM_LOG:-$(mk_log .preview)}"
  invoke_readonly "$PROPOSE_PROMPT" "$LOG"
  echo
  echo "log: $LOG"
  exit 0
fi

# --- Guarded run: propose, show, confirm, only then execute. ---

PROPOSE_LOG="${GROOM_LOG:-$(mk_log .propose)}"
invoke_readonly "$PROPOSE_PROMPT" "$PROPOSE_LOG"

TRANCHE="$(cat "$PROPOSE_LOG")"
if [ -z "$(printf '%s' "$TRANCHE" | tr -d '[:space:]')" ]; then
  echo "vault-groom: empty proposal, nothing to review -- aborting." >&2
  exit 1
fi
TRANCHE_HASH="$(printf '%s' "$TRANCHE" | sha256sum | cut -d' ' -f1)"

echo
echo "======================================================================"
echo " Tranche proposta (sha256 ${TRANCHE_HASH:0:12}...) -- leggila prima di confermare"
echo "======================================================================"
printf '%s\n' "$TRANCHE"
echo "======================================================================"
echo "Digita esattamente 'yes' per eseguire QUESTA tranche cosi' com'e'."
echo "Qualunque altra risposta annulla: nessuna modifica al vault."
printf 'Procedere? > '
read -r ANSWER || ANSWER=""

if [ "$ANSWER" != "yes" ]; then
  echo "vault-groom: annullato, nessuna modifica al vault." >&2
  exit 0
fi

mkdir -p "$STATE_DIR"
PLAN_RECORD="$STATE_DIR/$TS-plan.txt"
printf '%s\n' "$TRANCHE" > "$PLAN_RECORD"

if [ "${GROOM_NOPUSH:-0}" = 1 ]; then
  PUSH_CLAUSE="Do NOT push -- commits stay local for review."
  PUSHED="false"
else
  PUSH_CLAUSE="then push."
  PUSHED="true"
fi

WRITE_PROMPT="Read $PLAYBOOK. The user already reviewed and approved EXACTLY the following grooming tranche (sha256 ${TRANCHE_HASH}):

---BEGIN APPROVED TRANCHE---
${TRANCHE}
---END APPROVED TRANCHE---

Execute precisely this tranche, nothing more and nothing less -- do not re-derive or expand it. Commit atomically per action with clear messages. $PUSH_CLAUSE"

WRITE_LOG="$(mk_log .execute)"
HEAD_BEFORE="$(git rev-parse HEAD)"
invoke_write "$WRITE_PROMPT" "$WRITE_LOG"
HEAD_AFTER="$(git rev-parse HEAD)"

echo
python3 "$AUDIT_SCRIPT" \
  --vault "$VAULT" \
  --state-dir "$STATE_DIR" \
  --timestamp "$TS" \
  --runner "$RUNNER" \
  --model "$MODEL" \
  --tranche-sha256 "$TRANCHE_HASH" \
  --plan-record "$PLAN_RECORD" \
  --head-before "$HEAD_BEFORE" \
  --head-after "$HEAD_AFTER" \
  --pushed "$PUSHED" \
  --propose-log "$PROPOSE_LOG" \
  --write-log "$WRITE_LOG"
