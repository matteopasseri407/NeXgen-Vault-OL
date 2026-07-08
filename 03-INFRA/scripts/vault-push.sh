#!/usr/bin/env bash
# vault-push — commits + publishes the KnowledgeVault's INFRA FILES to the
# configured remote (origin), with a CLEAN rebase on benign divergence and a
# safe STOP on real conflicts (never forces, never merges, never loses work).
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
REMOTE="${KNOWLEDGE_VAULT_REMOTE:-origin}"

MSG=""
FILES=()
while [ $# -gt 0 ]; do
  case "$1" in
    -m) MSG="${2:-}"; shift 2 ;;
    -m*) MSG="${1#-m}"; shift ;;
    --) shift; while [ $# -gt 0 ]; do FILES+=("$1"); shift; done ;;
    *) FILES+=("$1"); shift ;;
  esac
done
[ -z "$MSG" ] && { echo "vault-push: needs -m \"message\""; exit 2; }
cd "$VAULT" || { echo "vault-push: vault not found ($VAULT)"; exit 1; }

if [ "${#FILES[@]}" -gt 0 ]; then
  git add -- "${FILES[@]}" || { echo "vault-push: git add failed"; exit 1; }
fi
if git diff --cached --quiet; then
  echo "vault-push: nothing staged, nothing to commit"; exit 0
fi

git commit -q -m "$MSG" || { echo "vault-push: commit failed"; exit 1; }
echo "vault-push: commit $(git rev-parse --short HEAD)"

# Publish to the remote: direct if fast-forward; otherwise a CLEAN rebase
# (only on a clean working tree); aborts and flags on a real conflict.
if git push "$REMOTE" "$BRANCH" >/dev/null 2>&1; then
  echo "vault-push: push $REMOTE OK"; exit 0
fi
if ! git fetch --prune "$REMOTE" "$BRANCH" >/dev/null 2>&1; then
  echo "vault-push: $REMOTE OFFLINE — the commit stays local (agent-sync will publish it)"; exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "vault-push: $REMOTE rejected but the working tree has uncommitted changes — NOT rebasing, resolve by hand"; exit 1
fi
if git rebase "$REMOTE/$BRANCH" >/dev/null 2>&1; then
  if git push "$REMOTE" "$BRANCH" >/dev/null 2>&1; then
    echo "vault-push: push $REMOTE OK (after a clean rebase)"; exit 0
  fi
  echo "vault-push: $REMOTE still rejected after rebase — try again"; exit 1
fi
git rebase --abort >/dev/null 2>&1
echo "vault-push: $REMOTE DIVERGENCE WITH CONFLICT — needs a manual 'git pull --rebase $REMOTE $BRANCH'"; exit 1
