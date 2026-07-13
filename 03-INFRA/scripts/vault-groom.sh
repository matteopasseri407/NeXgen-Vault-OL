#!/usr/bin/env bash
# vault-groom.sh — the gardener's hand.
#
# Runner-agnostic wrapper that feeds the canonical playbook
# (03-INFRA/vault-grooming-playbook.md) to an LLM runner and lets it do one
# grooming pass. Swap GROOM_MODEL / the runner invocation to move to a
# different CLI. The judgement lives in the playbook, NOT here.
#
# Modes:
#   plan   read-only dry pass: propose a tranche, cannot edit/commit (safe)
#   run    operative: compress/merge/archive + commit (+push unless GROOM_NOPUSH=1)
#
# Env: VAULT, GROOM_MODEL, GROOM_NOPUSH=1 (run without push, for observed runs)
set -euo pipefail

VAULT="${AGENT_VAULT_DATA:-${VAULT:-$HOME/KnowledgeVault}}"
PLAYBOOK="03-INFRA/vault-grooming-playbook.md"
MODEL="${GROOM_MODEL:-claude-sonnet-5}"
# Default to the read-only lane. A first-time caller running `./vault-groom.sh`
# with no argument must never land in commit+push mode driven by unreviewed
# LLM judgement -- `run` (and its push) stays an explicit, deliberate choice.
MODE="${1:-plan}"
# mktemp, not a predictable timestamp name: a plain "tee > $LOG" onto a
# guessable /tmp path is a symlink race (CWE-59) -- anything running as this
# same user could pre-create a symlink at that name pointing at, say,
# ~/.bashrc, and tee would clobber it with permission-inherited overwrite.
LOG="${GROOM_LOG:-$(mktemp --suffix=.log "/tmp/vault-groom-$(date +%Y%m%d-%H%M%S)-XXXXXX")}"

cd "$VAULT"

# Read-only lane: no Edit/Write/git → the plan pass physically cannot mutate.
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

case "$MODE" in
  plan)
    PROMPT="Read $PLAYBOOK and execute ONLY steps 1-3 (orient, run the audit heat-map, find candidates with semantic_search). Then OUTPUT a proposed grooming tranche: the notes, the action for each (compress / merge / archive / fix-frontmatter), and one line of why. DO NOT edit, write, move, or commit anything — this is a read-only planning pass."
    claude -p "$PROMPT" --model "$MODEL" \
      --allowedTools "${READ_TOOLS[@]}" 2>&1 | tee "$LOG"
    ;;
  run)
    if [ "${GROOM_NOPUSH:-0}" = 1 ]; then
      PROMPT="Read $PLAYBOOK and execute exactly ONE grooming run following it end to end. Commit atomically per tranche with clear messages. Do NOT push — commits stay local for review."
      claude -p "$PROMPT" --model "$MODEL" \
        --allowedTools "${WRITE_TOOLS[@]}" \
        --disallowedTools "Bash(git push:*)" 2>&1 | tee "$LOG"
    else
      PROMPT="Read $PLAYBOOK and execute exactly ONE grooming run following it end to end. Commit atomically per tranche with clear messages, then push."
      claude -p "$PROMPT" --model "$MODEL" \
        --allowedTools "${WRITE_TOOLS[@]}" 2>&1 | tee "$LOG"
    fi
    ;;
  *)
    echo "usage: $0 {plan|run}   (GROOM_NOPUSH=1 run without push)" >&2
    exit 2
    ;;
esac

echo "log: $LOG"
