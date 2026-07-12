#!/usr/bin/env bash
# Bootstrap VPS — deploy the Agent-OS self-hosted stack.
#
# Run ON the VPS, from the repo root (after cloning NeXgen-Engine):
#   cd NeXgen-Engine/03-INFRA/deploy
#   cp .env.example .env   # fill in secrets
#   bash bootstrap-vps.sh
#
# Brings up: n8n, Firecrawl, Vault OCR. Each stack is independent; you can
# comment out the ones you do not need.
#
# Images are pinned to explicit version tags in each docker-compose.yml (no
# :latest — see the comment at the top of each compose file for how to also
# pin a sha256 digest). To roll a stack back to a previous image after a bad
# deploy, or to back up / restore the n8n volume, use backup-restore.sh next
# to this script:
#   ./backup-restore.sh backup [n8n|firecrawl|ocr|all]
#   ./backup-restore.sh restore <backup-file> <volume-name>
#   ./backup-restore.sh rollback <stack> <image-env-var> <previous-image-ref>

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DEPLOY_DIR"

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing: $1"; exit 1; }
}

require docker
docker compose version >/dev/null 2>&1 || {
  echo "missing: the Docker Compose v2 plugin ('docker compose'). Legacy"
  echo "docker-compose (v1) is not supported by this script."
  exit 1
}

if [ -f .env ]; then
  # .env.example is world-readable on purpose (placeholder values, no
  # secrets, lives in a public repo). A plain `cp .env.example .env` does
  # not change that, so the REAL secrets filled into .env can stay
  # world-readable unless something tightens it explicitly. Idempotent and
  # safe to run on every invocation, not just first setup, in case it
  # drifts back open later.
  chmod 600 .env
else
  echo "missing: .env (cp .env.example .env, then fill in secrets)"
  exit 1
fi

# n8n encrypts every credential it stores with N8N_ENCRYPTION_KEY. Leave it
# unset and n8n auto-generates one INSIDE the n8n-data volume on first
# boot instead -- which means it silently rides along in plaintext inside
# every volume backup made by backup-restore.sh, so anyone who gets a copy
# of a backup tarball can decrypt every credential n8n ever held. Generate
# one explicitly here instead, so it lives in .env (chmod 600 above,
# git-ignored) rather than inside the backed-up volume. Idempotent: only
# fires when the line is missing or empty, so it never overwrites a key
# that's already in use -- safe to run on every invocation, not just first
# setup.
if ! grep -q '^N8N_ENCRYPTION_KEY=.\+' .env; then
  require openssl
  n8n_key="$(openssl rand -hex 32)"
  awk -v key="$n8n_key" '
    /^N8N_ENCRYPTION_KEY=/ { print "N8N_ENCRYPTION_KEY=" key; found=1; next }
    { print }
    END { if (!found) print "N8N_ENCRYPTION_KEY=" key }
  ' .env > .env.tmp && mv .env.tmp .env
  chmod 600 .env
  echo "==> generated N8N_ENCRYPTION_KEY in .env (first run)"
fi

# --env-file .env is explicit and REQUIRED here, not cosmetic: docker
# compose's default .env lookup is relative to the directory of the first
# -f file (e.g. n8n/), not this script's cwd, so a bare `-f n8n/docker-
# compose.yml` silently ignores the .env created at DEPLOY_DIR above and
# every ${VAR} falls back to its compose-file default (often empty),
# dropping secrets without any error. Confirmed live with `docker compose
# ... config`, 2026-07-12: OPENAI_API_KEY resolved to the empty fallback
# without --env-file, and correctly from .env with it.
echo "==> n8n"
docker compose -f n8n/docker-compose.yml --env-file .env up -d --build

echo "==> Firecrawl"
docker compose -f firecrawl/docker-compose.yml --env-file .env up -d --build

echo "==> Vault OCR"
docker compose -f ocr/docker-compose.yml --env-file .env up -d --build

echo
echo "Stacks up. Health checks:"
echo "  n8n:        http://127.0.0.1:5678/healthz"
echo "  firecrawl:  curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3002/"
echo "  ocr:        curl -s http://127.0.0.1:3033/health"
echo "  (or: docker compose -f <stack>/docker-compose.yml ps, once the"
echo "  per-service healthchecks above have had time to run)"
echo
echo "Next: create SSH tunnels from your workstation to these ports."
echo "See 03-INFRA/remote-automation.md for the tunnel map and 99-INDEX/USER-PROFILE.md"
echo "for the port variables to fill in."
echo
echo "Backup / restore / rollback: see ./backup-restore.sh --help and the"
echo "'Backup, restore, rollback' section of README.md."
