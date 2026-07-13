#!/usr/bin/env bash
# vault-push — commits + publishes the KnowledgeVault's INFRA FILES to the
# configured authoritative remote, then its optional mirrors. It uses a CLEAN
# rebase on benign authoritative divergence and stops on real conflicts.
#
# Thin wrapper only: the actual logic lives in agent_sync.py's `vault-push`
# subcommand (single cross-platform implementation, shared with
# vault-push.ps1 on Windows -- see agent_sync.py's own module docstring and
# docs/sync-contract.md). This script's only job is resolving where
# agent_sync.py lives and execing into it with the same arguments.
#
# DEGRADED EMERGENCY LANE: if agent_sync.py/python3 cannot be resolved AND
# KNOWLEDGE_VAULT_REMOTE is explicitly set (the emergency/bootstrap opt-in,
# same env var agent_sync.py itself treats as a complete override), this
# script falls back to a minimal pure-bash+git commit/push/rebase-retry lane
# instead of hard-failing -- the original pre-port purpose of this file:
# working with nothing but bash+git when the engine checkout is missing or
# python3 isn't installed. No mirrors in this lane (see degraded_lane below).
#
# Scope: the vault's code/config files (scripts, manifests, hooks...).
# NOTES (markdown knowledge) do NOT go through here: they are written via MCP
# (vault-library), which serializes with a lock and commits to the repo.
# One door per kind of thing.
#
# Usage:
#   vault-push -m "commit message" [file ...]
#     - with files:    git add those files, then commit
#     - without files: commits whatever is already staged (the caller stages it)
set -u

VAULT="${AGENT_VAULT_DATA:-${KNOWLEDGE_VAULT_PATH:-$HOME/KnowledgeVault}}"

# readlink -f (not plain BASH_SOURCE/dirname): this launcher is normally
# reached through a ~/.local/bin/vault-push symlink, and BASH_SOURCE does
# NOT follow symlinks -- dirname on the raw value would resolve to the
# symlink's own directory, not this script's, and miss agent_sync.py
# entirely. Same resolution agent-sync.sh itself uses to find its own
# co-located agent_sync.py.
SELF="$0"
while [ -L "$SELF" ]; do
  TARGET="$(readlink "$SELF")"
  case "$TARGET" in
    /*) SELF="$TARGET" ;;
    *) SELF="$(dirname "$SELF")/$TARGET" ;;
  esac
done
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$SELF")" && pwd)"

# Resolution chain for agent_sync.py, same order agent_sync.py's own
# Env.engine_root resolution uses:
#   1. AGENT_ENGINE_ROOT (explicit override, wins unconditionally)
#   2. the persisted engine-root indirection: ~/.local/bin/agent-sync's OWN
#      symlink target's directory -- the same source of truth
#      Env._persisted_engine_root reads, so a live post-cutover engine root
#      is found even with no env var exported (the normal way anyone types
#      this command), instead of silently reverting to the vault default.
#   3. this script's own co-located sibling (the common case).
AGENT_SYNC=""
if [ -n "${AGENT_ENGINE_ROOT:-}" ] && [ -f "$AGENT_ENGINE_ROOT/scripts/agent_sync.py" ]; then
  AGENT_SYNC="$AGENT_ENGINE_ROOT/scripts/agent_sync.py"
fi
if [ -z "$AGENT_SYNC" ] && [ -L "$HOME/.local/bin/agent-sync" ]; then
  LINK_TARGET="$(readlink -f "$HOME/.local/bin/agent-sync" 2>/dev/null || true)"
  if [ -n "$LINK_TARGET" ] && [ -f "$(dirname "$LINK_TARGET")/agent_sync.py" ]; then
    AGENT_SYNC="$(dirname "$LINK_TARGET")/agent_sync.py"
  fi
fi
if [ -z "$AGENT_SYNC" ] && [ -f "$SCRIPT_DIR/agent_sync.py" ]; then
  AGENT_SYNC="$SCRIPT_DIR/agent_sync.py"
fi

PYBIN=""
command -v python3 >/dev/null 2>&1 && PYBIN=python3

if [ -n "$AGENT_SYNC" ] && [ -n "$PYBIN" ]; then
  exec "$PYBIN" "$AGENT_SYNC" vault-push "$@"
fi

UNAVAILABLE_REASON=""
if [ -z "$AGENT_SYNC" ]; then
  UNAVAILABLE_REASON="$SCRIPT_DIR/agent_sync.py not found (vault=$VAULT) -- engine checkout is incomplete"
else
  UNAVAILABLE_REASON="python3 is required"
fi

if [ -z "${KNOWLEDGE_VAULT_REMOTE:-}" ]; then
  echo "vault-push: $UNAVAILABLE_REASON" >&2
  exit 2
fi

echo "vault-push: degraded emergency lane (engine unavailable: $UNAVAILABLE_REASON)" >&2

# ---- degraded emergency lane (bash + git only, no python) -----------------
# Mirrors agent_sync.py's own vault-push commit/publish shape exactly
# (_vault_push_locked / _vault_push_publish) but stripped to what plain
# bash+git can do: no mirrors (a mirror realignment failure must never look
# like the authoritative push failed, and this lane has no structured config
# loader to resolve mirror names from), no strict remote-name validation
# (this IS the emergency override, same trust level KNOWLEDGE_VAULT_REMOTE
# already carries everywhere else in the engine).

parse_degraded_args() {
  MSG=""
  FILES=()
  while [ $# -gt 0 ]; do
    case "$1" in
      -m)
        if [ $# -lt 2 ]; then
          echo "vault-push: argument missing for -m" >&2
          return 2
        fi
        MSG="$2"
        shift 2
        ;;
      -m*)
        MSG="${1#-m}"
        shift
        ;;
      --)
        shift
        FILES+=("$@")
        break
        ;;
      *)
        FILES+=("$1")
        shift
        ;;
    esac
  done
  if [ -z "$MSG" ]; then
    echo 'vault-push: needs -m "message"' >&2
    return 2
  fi
  return 0
}

degraded_lane() {
  if [ ! -d "$VAULT" ]; then
    echo "vault-push: vault not found ($VAULT)"
    return 1
  fi

  # Best-effort lock on the SAME lock file agent_sync.py's SyncRunLock would
  # use, so a concurrent guard/apply cycle on this machine still serializes
  # against a degraded-lane push -- best-effort only: flock may itself be
  # unavailable (this lane already exists because the Python engine is
  # not usable), in which case the push proceeds unlocked rather than
  # blocking the emergency lane entirely.
  LOCK_FILE="${AGENT_SYNC_LOCK_FILE:-$HOME/.local/state/agent-sync.lock}"
  mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
  LOCK_TIMEOUT="${AGENT_SYNC_LOCK_TIMEOUT_SECONDS:-2}"
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -w "$LOCK_TIMEOUT" 9; then
      echo "vault-push: sync lock busy (another agent-sync/vault-push is running) -- aborting" >&2
      return 75
    fi
  fi

  if [ "${#FILES[@]}" -gt 0 ] && ! git -C "$VAULT" add -- "${FILES[@]}"; then
    echo "vault-push: git add failed"
    return 1
  fi
  if git -C "$VAULT" diff --cached --quiet; then
    echo "vault-push: nothing staged, nothing to commit"
    return 0
  fi
  if ! git -C "$VAULT" commit -q -m "$MSG"; then
    echo "vault-push: commit failed"
    return 1
  fi
  SHORT="$(git -C "$VAULT" rev-parse --short HEAD)"
  echo "vault-push: commit $SHORT"

  if [ "$KNOWLEDGE_VAULT_REMOTE" = "local" ] || [ "$KNOWLEDGE_VAULT_REMOTE" = "none" ]; then
    echo "vault-push: push skipped (Local-Only mode, remote=$KNOWLEDGE_VAULT_REMOTE)"
    return 0
  fi

  BRANCH="${KNOWLEDGE_VAULT_BRANCH:-main}"
  if git -C "$VAULT" push "$KNOWLEDGE_VAULT_REMOTE" "$BRANCH"; then
    echo "vault-push: push $KNOWLEDGE_VAULT_REMOTE OK"
    return 0
  fi
  if ! git -C "$VAULT" fetch --prune "$KNOWLEDGE_VAULT_REMOTE" "$BRANCH"; then
    echo "vault-push: $KNOWLEDGE_VAULT_REMOTE OFFLINE — the commit stays local; run agent-sync publish later"
    return 1
  fi
  if [ -n "$(git -C "$VAULT" status --porcelain --untracked-files=no)" ]; then
    echo "vault-push: $KNOWLEDGE_VAULT_REMOTE rejected but the working tree has uncommitted changes — NOT rebasing, resolve by hand"
    return 1
  fi
  if git -C "$VAULT" rebase "$KNOWLEDGE_VAULT_REMOTE/$BRANCH"; then
    if ! git -C "$VAULT" push "$KNOWLEDGE_VAULT_REMOTE" "$BRANCH"; then
      echo "vault-push: $KNOWLEDGE_VAULT_REMOTE still rejected after rebase — try again"
      return 1
    fi
    echo "vault-push: push $KNOWLEDGE_VAULT_REMOTE OK (after a clean rebase)"
    return 0
  fi
  git -C "$VAULT" rebase --abort
  echo "vault-push: $KNOWLEDGE_VAULT_REMOTE DIVERGENCE WITH CONFLICT — needs a manual 'git pull --rebase $KNOWLEDGE_VAULT_REMOTE $BRANCH'"
  return 1
}

parse_degraded_args "$@" || exit $?
degraded_lane
exit $?
