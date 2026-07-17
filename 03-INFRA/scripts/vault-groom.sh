#!/usr/bin/env bash
# vault-groom.sh — the gardener's hand.
#
# Feeds the canonical playbook (03-INFRA/vault-grooming-playbook.md) to an
# LLM runner and lets it do one grooming pass. The judgement lives in the
# playbook, NOT here.
#
# Modes:
#   (default)  preview: read-only, runs the propose pass, prints the
#              tranche, exits. NEVER prompts, NEVER writes -- a bare
#              invocation can never modify the vault.
#   preview    explicit alias of the default above.
#   apply      the guarded flow: propose a tranche (read-only), show it to
#              you, require a typed "yes", THEN execute exactly that
#              tranche inside a throwaway clone and (only if the audit
#              below is clean) promote it into the real vault. Nothing is
#              written to the real vault without that confirmation AND a
#              clean audit -- see "the temp-clone gate" below.
#   plan/run/guarded  retired names -- rejected with a one-line migration
#              hint (exit 2), not silently remapped.
#
# The write pass never re-derives its own tranche: it is handed the exact
# text you approved (with its sha256, taken from the PLAN_RECORD file's raw
# bytes) and told to execute precisely that. A cheap TOCTOU re-hash right
# before the write pass starts catches the plan record changing underneath
# the confirmation.
#
# The temp-clone gate (2026-07-13 architect review, after an external
# REVISE verdict: "the audit must be the only technical route a write can
# take to main and the remotes"). A prompt telling the write-pass runner
# "don't push" is not enforcement -- codex and agy have no per-command
# block, and even for claude, a rejected write still left its commits as
# ancestors of the vault's next routine push. So the write pass no longer
# runs against the real vault at all: after "yes" is confirmed, this script
# clones the vault into a fresh dir and IMMEDIATELY removes that clone's
# `origin` remote, making `git push` mechanically impossible for the write
# pass to reach anywhere real, no matter what it's told or tricked into
# running. claude's --disallowedTools 'Bash(git push:*)' stays as
# belt-and-suspenders, not as the actual guarantee. vault_groom_audit.py
# then audits the CLONE (clean working tree, linear history, path-exact
# coverage) and, only if that passes AND the real vault hasn't moved since
# the clone was made, fetches the clone's exact audited commit into the
# real vault and fast-forwards onto it -- promotion, not re-execution. Any
# audit failure leaves the real vault untouched and quarantines the clone
# in place for a human to inspect.
#
# Env: VAULT, GROOM_MODEL, GROOM_RUNNER (claude|codex|agy, default claude),
#      GROOM_NOPUSH=1 (skip the auto-publish step after a clean promotion --
#      the promoted commits stay local for review, same as always passing
#      --push-if-clean=off),
#      GROOM_LOG (override the preview/propose-pass log path),
#      GROOM_STATE_DIR (override where structured audit records AND the
#      temp-clone gate's clones land, default ~/.local/state/vault-groom),
#      AGENT_ENGINE_ROOT (see resolve_engine_scripts below).
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

# Resolves a symlink chain to its final real path, one path component at a
# time (portable: no reliance on GNU-only `readlink -f`). Used both for
# this script's own location (SELF, below) and for the ~/.local/bin/
# agent-sync indirection resolve_engine_scripts reads.
resolve_symlink_chain() {
  local p="$1" target
  while [ -L "$p" ]; do
    target="$(readlink "$p")"
    case "$target" in
      /*) p="$target" ;;
      *) p="$(dirname "$p")/$target" ;;
    esac
  done
  printf '%s\n' "$p"
}

# Resolved relative to THIS script's own real location (same SELF/readlink
# pattern as vault-push.sh), not to $VAULT: vault_groom_audit.py is pure
# engine tooling that ships in the same commit as this wrapper, never a
# per-user customizable content file like the playbook above. A $VAULT-
# relative path would only work after an `agent-sync apply` had propagated
# this file into the vault -- real bug found on the very first live run
# (2026-07-13): the write pass succeeded and pushed 9 real commits, but the
# audit-record step crashed with "No such file or directory" because this
# file, added today, had never been synced into the vault.
SELF="$(resolve_symlink_chain "$0")"
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$SELF")" && pwd)"
AUDIT_SCRIPT="$SCRIPT_DIR/vault_groom_audit.py"
MODEL="${GROOM_MODEL:-claude-sonnet-5}"
RUNNER="${GROOM_RUNNER:-claude}"
STATE_DIR="${GROOM_STATE_DIR:-$HOME/.local/state/vault-groom}"
MODE="${1:-preview}"

# vault_scripts (SCRIPT_DIR, where this wrapper and vault_groom_audit.py
# live) and engine_scripts (where agent_sync.py lives, needed only for the
# post-promotion publish step) are NOT guaranteed co-located -- the
# engine/data split (see agent_sync.py's own Env class) can put them in
# different trees. Same resolution order as agent_sync.py's own
# _persisted_engine_root, so this wrapper never guesses something the rest
# of the layer wouldn't also conclude: (1) AGENT_ENGINE_ROOT wins when set;
# (2) otherwise, the live ~/.local/bin/agent-sync symlink's target dir --
# whatever engine root that command currently resolves to, not necessarily
# the default; (3) SCRIPT_DIR as the last resort (the only thing this
# script COULD assume before agent-sync has ever run once to create that
# symlink, and correct for the common single-tree layout).
resolve_engine_scripts() {
  if [ -n "${AGENT_ENGINE_ROOT:-}" ]; then
    printf '%s\n' "$AGENT_ENGINE_ROOT/scripts"
    return
  fi
  local link="$HOME/.local/bin/agent-sync"
  if [ -L "$link" ]; then
    printf '%s\n' "$(dirname -- "$(resolve_symlink_chain "$link")")"
    return
  fi
  printf '%s\n' "$SCRIPT_DIR"
}
ENGINE_SCRIPTS="$(resolve_engine_scripts)"

TS="$(date +%Y%m%d-%H%M%S)"
# mktemp, not a predictable timestamp name: a plain "tee > $LOG" onto a
# guessable /tmp path is a symlink race (CWE-59) -- anything running as this
# same user could pre-create a symlink at that name pointing at, say,
# ~/.bashrc, and tee would clobber it with permission-inherited overwrite.
mk_log() { mktemp --suffix="$1.log" "/tmp/vault-groom-$TS-XXXXXX"; }

cd "$VAULT"

case "$MODE" in
  preview | apply) ;;
  plan)
    echo "vault-groom: 'plan' is retired -- use 'preview' (or run with no argument, same thing)." >&2
    exit 2
    ;;
  run | guarded)
    echo "vault-groom: 'run'/'guarded' is retired -- use 'apply'." >&2
    exit 2
    ;;
  *)
    echo "usage: $0 [preview|apply]   (default: preview -- read-only, never prompts or writes. apply = propose, confirm, execute in a throwaway clone, promote only if the audit is clean. GROOM_RUNNER=claude|codex|agy, GROOM_NOPUSH=1 skips the auto-publish step after promotion)" >&2
    exit 2
    ;;
esac

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

# Write lane: adds file mutation + git; push is never in this list's reach
# (see invoke_write's claude branch below) -- but the REAL guarantee is the
# temp-clone gate itself (this clone's `origin` remote is removed before
# the write pass ever starts, see the apply flow below): even a `git push`
# the write pass somehow ran would have nowhere real to go.
WRITE_TOOLS=(Read Edit Write Grep Glob \
  "Bash(python3:*)" "Bash(git:*)" "Bash(mkdir:*)" "Bash(mv:*)" \
  mcp__vault-library__semantic_search mcp__vault-library__search_notes \
  mcp__vault-library__read_note mcp__vault-library__list_related \
  mcp__vault-library__update_note mcp__vault-library__create_note \
  mcp__vault-library__append_note)

PROPOSE_PROMPT="Read $PLAYBOOK and execute ONLY steps 1-3 (orient, run the audit heat-map, find candidates with semantic_search). ALSO run the structural map, python3 $SCRIPT_DIR/vault-map.py --vault $VAULT --check, and treat orphan notes and broken wikilinks it reports as first-class tranche candidates (orphan -> link-or-archive, broken link -> fix at the source). Then OUTPUT a proposed grooming tranche as a markdown table with EXACTLY these columns: | Nota | Azione | Perché | -- one row per note, action is compress / merge / archive / fix-frontmatter / fix-link / nessuna azione, last column is one line of why. DO NOT edit, write, move, or commit anything -- this is a read-only planning pass."

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
  local prompt="$1" logfile="$2" workdir="$3"
  case "$RUNNER" in
    claude)
      # --disallowedTools is unconditional now, not gated on GROOM_NOPUSH:
      # push is never the write pass's decision to make, full stop --
      # belt-and-suspenders on top of the temp-clone gate's own origin-less
      # clone, which is what actually makes a push land nowhere real.
      claude -p "$prompt" --model "$MODEL" \
        --allowedTools "${WRITE_TOOLS[@]}" \
        --disallowedTools "Bash(git push:*)" < /dev/null 2>&1 | tee "$logfile"
      ;;
    codex)
      # -C "$workdir", not $VAULT: the write pass's working directory is
      # the temp-clone gate's clone, never the real vault. No push
      # instruction here is prompt-level only (Codex has no per-command
      # block like Claude's --disallowedTools) -- weaker than claude's own
      # belt, but the clone having no `origin` remote is the actual
      # guarantee for every runner alike.
      codex exec -s workspace-write -m "$MODEL" -C "$workdir" - <<<"$prompt" 2>&1 | tee "$logfile"
      ;;
    agy)
      # Prompt-level only too, same caveat as codex above -- the clone's
      # missing origin remote is what actually stops it, not this.
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

# --- apply: propose, show, confirm, only then execute (inside a throwaway
# clone -- see the temp-clone gate comment at the top of this file). ---

PROPOSE_LOG="${GROOM_LOG:-$(mk_log .propose)}"
invoke_readonly "$PROPOSE_PROMPT" "$PROPOSE_LOG"

TRANCHE="$(cat "$PROPOSE_LOG")"
if [ -z "$(printf '%s' "$TRANCHE" | tr -d '[:space:]')" ]; then
  echo "vault-groom: empty proposal, nothing to review -- aborting." >&2
  exit 1
fi

mkdir -p "$STATE_DIR"
PLAN_RECORD="$STATE_DIR/$TS-plan.txt"
printf '%s\n' "$TRANCHE" > "$PLAN_RECORD"
# Hash the FILE's raw bytes, not the shell variable: the confirmation banner
# and the write prompt both quote this hash, and the TOCTOU re-check below
# re-hashes the same file -- all three must agree on exactly what they're
# hashing.
TRANCHE_HASH="$(sha256sum "$PLAN_RECORD" | cut -d' ' -f1)"

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

# TOCTOU guard: re-hash the plan record right before it's handed to the
# write pass. Cheap, and it closes the window between "the human approved
# this text" and "the write pass reads it" -- if anything touched the file
# in between, abort loudly instead of executing whatever it now contains.
REHASH="$(sha256sum "$PLAN_RECORD" | cut -d' ' -f1)"
if [ "$REHASH" != "$TRANCHE_HASH" ]; then
  echo "vault-groom: plan record changed after approval (expected $TRANCHE_HASH, got $REHASH) -- aborting, zero writes. Re-run and re-approve." >&2
  exit 1
fi

# --- The temp-clone gate. ---
BASE="$(git rev-parse HEAD)"
if [ -n "$(git status --porcelain)" ]; then
  echo "vault-groom: the vault's working tree is not clean (uncommitted changes present) -- commit or stash them first. Aborting: the temp-clone gate needs a clean HEAD to clone from, zero writes made." >&2
  exit 1
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

CLONE_DIR="$(mktemp -d "$STATE_DIR/$TS-clone-XXXXXX")"
git clone -q "$VAULT" "$CLONE_DIR"
# Immediately -- before the write pass ever runs a single command --
# remove the clone's `origin` remote. This is what makes `git push`
# mechanically impossible for ANY runner in ANY mode, replacing trust in
# prompt wording with an actual missing destination.
git -C "$CLONE_DIR" remote remove origin

# Resolves the archive root the write pass's "archive" actions actually
# move notes under, so vault_groom_audit.py's coverage check knows where a
# legitimate archive-move is allowed to land. Read from the vault's own
# playbook config (an `archive_root: <path>` frontmatter-style line);
# falls back to the documented default when absent or unparseable.
resolve_archive_root() {
  local value
  value="$(sed -n 's/^archive_root:[[:space:]]*//p' "$PLAYBOOK" 2>/dev/null | head -n1 | tr -d '"'"'"'')"
  printf '%s\n' "${value:-99-ARCHIVE}"
}
ARCHIVE_ROOT="$(resolve_archive_root)"

WRITE_PROMPT="Read $PLAYBOOK. The user already reviewed and approved EXACTLY the grooming tranche recorded at $PLAN_RECORD (sha256 ${TRANCHE_HASH}) -- that file is the single source of truth if anything below looks truncated or reformatted; the same text is reproduced here for convenience:

---BEGIN APPROVED TRANCHE---
${TRANCHE}
---END APPROVED TRANCHE---

Execute precisely this tranche, nothing more and nothing less -- do not re-derive or expand it. Commit atomically per action with clear messages. Do NOT push -- pushing is decided separately after this run by mechanically checking what the commits actually touched, never by you. Before finishing, re-read the approved tranche row by row and end your response with an explicit checklist, one line per note that has a real action (skip rows marked \"nessuna azione\"): DONE (with the commit it landed in) or NOT DONE (with the concrete reason). Every actioned row must appear on that list -- do not let anything go unmentioned."

WRITE_LOG="$(mk_log .execute)"
# WRITE_EXIT is captured via `||`, not left to `set -e`: a non-zero write
# pass must still reach the audit call below so it can quarantine the
# clone and write the audit record -- `set -e` would otherwise abort the
# script right here and skip that bookkeeping entirely.
WRITE_EXIT=0
( cd "$CLONE_DIR" && invoke_write "$WRITE_PROMPT" "$WRITE_LOG" "$CLONE_DIR" ) || WRITE_EXIT=$?

# --push-if-clean is omitted entirely under GROOM_NOPUSH=1: the audit
# script then never attempts to publish, even after a clean promotion.
PUSH_ARGS=()
if [ "${GROOM_NOPUSH:-0}" != 1 ]; then
  PUSH_ARGS=(--push-if-clean)
fi

echo
python3 "$AUDIT_SCRIPT" \
  --vault "$VAULT" \
  --clone "$CLONE_DIR" \
  --branch "$BRANCH" \
  --base "$BASE" \
  --archive-root "$ARCHIVE_ROOT" \
  --state-dir "$STATE_DIR" \
  --timestamp "$TS" \
  --runner "$RUNNER" \
  --model "$MODEL" \
  --tranche-sha256 "$TRANCHE_HASH" \
  --plan-record "$PLAN_RECORD" \
  --propose-log "$PROPOSE_LOG" \
  --write-log "$WRITE_LOG" \
  --write-exit-code "$WRITE_EXIT" \
  --engine-scripts "$ENGINE_SCRIPTS" \
  "${PUSH_ARGS[@]}"
