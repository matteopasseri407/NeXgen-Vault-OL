#!/usr/bin/env bash
# vault-push — commits + publishes the KnowledgeVault's INFRA FILES to the
# configured authoritative remote, then its optional mirrors. It uses a CLEAN
# rebase on benign authoritative divergence and stops on real conflicts.
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
BRANCH="${KNOWLEDGE_VAULT_BRANCH:-main}"
SELF="$0"
while [ -L "$SELF" ]; do
  TARGET="$(readlink "$SELF")"
  case "$TARGET" in
    /*) SELF="$TARGET" ;;
    *) SELF="$(dirname "$SELF")/$TARGET" ;;
  esac
done
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$SELF")" && pwd)"

if [ -n "${KNOWLEDGE_VAULT_REMOTE:-}" ]; then
  REMOTE="$KNOWLEDGE_VAULT_REMOTE"
  MIRRORS="$(printf '%s' "${KNOWLEDGE_VAULT_MIRRORS:-}" | tr ',' '\n')"
else
  command -v python3 >/dev/null 2>&1 || {
    echo "vault-push: python3 is required to resolve the sync remote policy"
    exit 2
  }
  REMOTE="$(python3 "$SCRIPT_DIR/agent_sync.py" config authoritative_remote)" || exit 2
  MIRRORS="$(python3 "$SCRIPT_DIR/agent_sync.py" config mirrors)" || exit 2
fi
[ -n "$REMOTE" ] || { echo "vault-push: authoritative remote is empty"; exit 2; }

MSG=""
FILES=()
while [ $# -gt 0 ]; do
  case "$1" in
    -m)
      if [ $# -lt 2 ]; then
        echo "vault-push: argument missing for -m"
        exit 2
      fi
      MSG="$2"; shift 2 
      ;;
    -m*) MSG="${1#-m}"; shift ;;
    --) shift; while [ $# -gt 0 ]; do FILES+=("$1"); shift; done ;;
    *) FILES+=("$1"); shift ;;
  esac
done
[ -z "$MSG" ] && { echo "vault-push: needs -m \"message\""; exit 2; }
cd "$VAULT" || { echo "vault-push: vault not found ($VAULT)"; exit 1; }

# Same host-wide lock file agent_sync.py's SyncRunLock uses (fcntl.flock on
# the same path), acquired here via the flock(1) CLI so this bash script
# and the Python provisioner never write the vault at the same time.
# Without this, a `vault-push` running concurrently with an `agent-sync`
# guard cycle could interleave a commit with a mid-apply working tree.
# Best-effort, not a hard requirement: `flock` is standard on Linux
# (util-linux) but not always present on macOS, so its absence degrades to
# a loud warning rather than blocking every commit on a missing binary.
LOCK_FILE="${AGENT_SYNC_LOCK_FILE:-$HOME/.local/state/agent-sync.lock}"
LOCK_TIMEOUT="${AGENT_SYNC_LOCK_TIMEOUT_SECONDS:-2}"
if command -v flock >/dev/null 2>&1; then
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  if ! flock -w "$LOCK_TIMEOUT" 9; then
    echo "vault-push: sync lock busy (another agent-sync/vault-push is running) -- aborting" >&2
    exit 75
  fi
else
  echo "vault-push: WARNING - 'flock' not found, proceeding WITHOUT the cross-process sync lock" >&2
fi

# Local-Only sentinel (same "local"/"none" values agent_sync.py's publish()
# already special-cases): no remote is ever meant to exist, so skip the
# "is it configured" check below instead of failing on a git remote that
# was never supposed to be there. The commit itself still happens further
# down — Local-Only means no publication target, not no local history.
LOCAL_ONLY=0
case "$REMOTE" in
  local|none) LOCAL_ONLY=1 ;;
esac

if [ "$LOCAL_ONLY" != 1 ]; then
  git remote get-url "$REMOTE" >/dev/null 2>&1 || {
    echo "vault-push: authoritative remote '$REMOTE' is not configured"
    exit 1
  }
fi

if [ "${#FILES[@]}" -gt 0 ]; then
  git add -- "${FILES[@]}" || { echo "vault-push: git add failed"; exit 1; }
fi
if git diff --cached --quiet; then
  echo "vault-push: nothing staged, nothing to commit"; exit 0
fi

git commit -q -m "$MSG" || { echo "vault-push: commit failed"; exit 1; }
echo "vault-push: commit $(git rev-parse --short HEAD)"

if [ "$LOCAL_ONLY" = 1 ]; then
  echo "vault-push: push skipped (Local-Only mode, remote=$REMOTE)"
  exit 0
fi

# Publish to the authoritative remote: direct if fast-forward; otherwise a CLEAN rebase
# (only on a clean working tree); aborts and flags on a real conflict.
if git push "$REMOTE" "$BRANCH" >/dev/null 2>&1; then
  echo "vault-push: push $REMOTE OK"
else
  if ! git fetch --prune "$REMOTE" "$BRANCH" >/dev/null 2>&1; then
    echo "vault-push: $REMOTE OFFLINE — the commit stays local; run agent-sync publish later"
    exit 1
  fi
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "vault-push: $REMOTE rejected but the working tree has uncommitted changes — NOT rebasing, resolve by hand"
    exit 1
  fi
  if git rebase "$REMOTE/$BRANCH" >/dev/null 2>&1; then
    if ! git push "$REMOTE" "$BRANCH" >/dev/null 2>&1; then
      echo "vault-push: $REMOTE still rejected after rebase — try again"
      exit 1
    fi
    echo "vault-push: push $REMOTE OK (after a clean rebase)"
  else
    git rebase --abort >/dev/null 2>&1
    echo "vault-push: $REMOTE DIVERGENCE WITH CONFLICT — needs a manual 'git pull --rebase $REMOTE $BRANCH'"
    exit 1
  fi
fi

# Mirrors are explicit downstream replicas. They never rewrite the canonical
# local history. A stale mirror is aligned with force-with-lease only after the
# authoritative remote has accepted the same commit.
printf '%s\n' "$MIRRORS" | while IFS= read -r mirror; do
  mirror="$(printf '%s' "$mirror" | xargs)"
  [ -n "$mirror" ] || continue
  [ "$mirror" != "$REMOTE" ] || continue
  if ! git remote get-url "$mirror" >/dev/null 2>&1; then
    echo "vault-push: mirror '$mirror' is not configured; skipped"
    continue
  fi
  if git push "$mirror" "$BRANCH" >/dev/null 2>&1; then
    echo "vault-push: push mirror $mirror OK"
  elif git fetch --prune "$mirror" "$BRANCH" >/dev/null 2>&1 \
       && git push --force-with-lease "$mirror" "$BRANCH" >/dev/null 2>&1; then
    echo "vault-push: mirror $mirror aligned to authoritative $REMOTE"
  else
    echo "vault-push: mirror $mirror not updated; authoritative $REMOTE is safe"
  fi
done
