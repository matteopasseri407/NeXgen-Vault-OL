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

echo "==> n8n"
docker compose -f n8n/docker-compose.yml up -d --build

echo "==> Firecrawl"
docker compose -f firecrawl/docker-compose.yml up -d --build

echo "==> Vault OCR"
docker compose -f ocr/docker-compose.yml up -d --build

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
