#!/usr/bin/env bash
# Bootstrap VPS — deploy the NeXgen Engine self-hosted stack.
#
# Run ON the VPS, from the repo root (after cloning NeXgen-Engine):
#   cd NeXgen-Engine/03-INFRA/deploy
#   cp .env.example .env   # fill in secrets
#   bash bootstrap-vps.sh
#
# Brings up: n8n, Firecrawl, Vault OCR, vault-mcp (the Git-backed write door
# for vault notes). Each stack is independent; you can comment out the ones
# you do not need (vault-mcp also honors VAULT_MCP_ENABLED=0).
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
require openssl
docker compose version >/dev/null 2>&1 || {
  echo "missing: the Docker Compose v2 plugin ('docker compose'). Legacy"
  echo "docker-compose (v1) is not supported by this script."
  exit 1
}

configure_firewall() {
  # Host-level baseline: 127.0.0.1 binding in each docker-compose.yml is
  # the primary defense against exposing these services to the internet,
  # but that is a single point of failure -- one mis-typed binding and a
  # service is world-reachable with nothing else in the way. ufw is a
  # second, independent layer.
  #
  # This is a MINIMUM baseline, not an enterprise firewall: it only
  # guarantees "deny inbound except SSH" so a bad compose binding fails
  # closed instead of open. It does not do rate limiting, egress
  # filtering, fail2ban, or per-service rules -- add those separately if
  # the threat model needs them.
  #
  # Idempotent and safe to re-run: `ufw allow`/`ufw default` are no-ops
  # (or overwrite harmlessly) if already set, and `ufw enable --force`
  # on an already-enabled firewall just reasserts the same rules.
  if ! command -v ufw >/dev/null 2>&1; then
    echo "WARNING: ufw not found -- skipping host firewall setup."
    echo "  The only thing standing between these services and the public"
    echo "  internet is the 127.0.0.1 binding in each docker-compose.yml."
    echo "  Install ufw (e.g. 'apt-get install ufw' on Debian/Ubuntu) and"
    echo "  re-run this script, or configure an equivalent firewall by hand."
    return 0
  fi

  # ufw needs root. Without this check, a non-root user (the default login
  # on Oracle Cloud's recommended free-tier image, the platform this README
  # points at) would hit a bare "Permission denied" from the first `ufw`
  # call below and abort under `set -e` -- before any stack comes up, but
  # with no actionable message either. Prefer sudo (it prompts for a
  # password interactively same as any other first-run setup step); only
  # skip, gracefully, if there is truly no escalation path.
  local SUDO=""
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
      SUDO="sudo"
    else
      echo "WARNING: ufw needs root and no 'sudo' is available on this host"
      echo "  -- skipping host firewall setup. Re-run as root, install sudo,"
      echo "  or configure ufw by hand: ufw allow OpenSSH && ufw default deny"
      echo "  incoming && ufw default allow outgoing && ufw --force enable"
      echo "  The only thing standing between these services and the public"
      echo "  internet is the 127.0.0.1 binding in each docker-compose.yml."
      return 0
    fi
  fi

  echo "==> Host firewall (ufw baseline)"
  # Allow SSH FIRST -- must land before default-deny/enable below, or a
  # fresh `ufw enable` on a box with no prior rules can cut off the very
  # SSH session running this script.
  $SUDO ufw allow OpenSSH
  # OpenSSH's ufw profile only covers the default port 22. A host with SSH
  # moved to a non-standard port (a common hardening step) would otherwise
  # lock itself out on `default deny incoming` below. Derive the real
  # listening port(s) from sshd itself rather than only from this session's
  # own connection: SSH_CONNECTION alone is blind on the browser/serial
  # console case (no SSH_CONNECTION at all) -- exactly the lockout this
  # exists to catch -- and only ever reports the one port THIS session
  # happens to be using, missing any other port sshd also listens on.
  # Preference order: sshd's own effective config (authoritative, requires
  # the sshd binary and enough privilege to dump it) -> the raw config file
  # (works even when `sshd -T` can't run) -> this session's SSH_CONNECTION
  # (works with neither of the above, but only sees its own port) -> no
  # extra port detected, same as before (the OpenSSH profile above still
  # covers 22).
  detected_ssh_ports=""
  if command -v sshd >/dev/null 2>&1; then
    detected_ssh_ports="$($SUDO sshd -T 2>/dev/null | awk '/^port /{print $2}')"
  fi
  if [ -z "$detected_ssh_ports" ] && [ -f /etc/ssh/sshd_config ]; then
    detected_ssh_ports="$(awk 'tolower($1)=="port"{print $2}' /etc/ssh/sshd_config 2>/dev/null)"
  fi
  if [ -z "$detected_ssh_ports" ]; then
    detected_ssh_ports="$(printf '%s' "${SSH_CONNECTION:-}" | awk '{print $4}')"
  fi
  # sshd can listen on several ports at once -- allow every one detected,
  # not just the first.
  while IFS= read -r ssh_port; do
    case "$ssh_port" in
      '') continue ;;
      *[!0-9]*) continue ;;  # defensive: never pass a non-numeric token to ufw
      22) continue ;;        # already covered by the OpenSSH profile above
    esac
    $SUDO ufw allow "$ssh_port"/tcp
    echo "  also allowed detected non-default SSH port $ssh_port/tcp"
  done <<EOF
$detected_ssh_ports
EOF
  $SUDO ufw default deny incoming
  $SUDO ufw default allow outgoing
  $SUDO ufw --force enable
  echo "  ufw enabled: deny incoming by default, OpenSSH allowed, outgoing allowed."
  echo "  This is a minimum baseline, not a substitute for per-service hardening."
}

configure_firewall

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

# Auto-generates a secret INTO .env the first time it's needed, instead of
# asking the user to invent or remember one by hand. Idempotent: a value
# already present in .env (non-empty) is left untouched on every later run,
# so this never rotates a secret out from under a running deploy.
ensure_env_secret() {
  local var="$1"
  local current
  current="$(grep -E "^${var}=" .env | tail -1 | cut -d '=' -f2-)"
  if [ -n "$current" ]; then
    return 0
  fi
  local generated
  generated="$(openssl rand -hex 32)"
  if grep -qE "^${var}=" .env; then
    local tmp
    tmp="$(mktemp)"
    awk -v var="$var" -v val="$generated" -F'=' 'BEGIN{OFS="="} $1==var{$0=var"="val} {print}' .env > "$tmp"
    mv "$tmp" .env
  else
    printf '%s=%s\n' "$var" "$generated" >> .env
  fi
  chmod 600 .env
  echo "  generated ${var} in .env (auto, first run)"
}

# N8N_ENCRYPTION_KEY: n8n encrypts every credential it stores with this key.
# Leave it unset and n8n auto-generates one INSIDE the n8n-data volume on
# first boot instead -- which means it silently rides along in plaintext
# inside every volume backup made by backup-restore.sh, so anyone who gets a
# copy of a backup tarball can decrypt every credential n8n ever held.
# Generating it here means it lives in .env (chmod 600 above, git-ignored)
# rather than inside the backed-up volume.
ensure_env_secret N8N_ENCRYPTION_KEY

# FIRECRAWL_REDIS_PASSWORD (finding C: firecrawl-redis had no requirepass/
# ACL at all before). VAULT_OCR_TOKEN is deliberately NOT auto-generated
# here -- the OCR API treats it as opt-in (see ocr/api/app.py) and defaults
# to logging a warning rather than requiring it, to stay backward-compatible
# with existing local/tunnel-only deploys; generate it yourself in .env if
# you want to turn enforcement on.
ensure_env_secret FIRECRAWL_REDIS_PASSWORD

# FIRECRAWL_POSTGRES_PASSWORD: the NUQ queue Postgres (2.11 architecture,
# firecrawl/docker-compose.yml) gets the same auto-generated treatment as
# Redis -- never an unauthenticated datastore, never a hand-invented value.
ensure_env_secret FIRECRAWL_POSTGRES_PASSWORD

# VAULT_LIBRARY_TOKEN: bearer auth for the vault-mcp container AND the
# workstation CLIs (manifest.yaml reads the same variable name). Generated
# unconditionally, like the n8n key: a write-enabled vault server must never
# come up open, and the value has to exist before its compose stack starts.
ensure_env_secret VAULT_LIBRARY_TOKEN

# Non-secret sibling of ensure_env_secret: pins a machine-derived value
# (e.g. the deploy user's uid/gid) into .env on first run, so later compose
# invocations — cron, CI, another shell — resolve the exact same value.
# Same idempotence rule: a non-empty value already in .env is never touched.
ensure_env_value() {
  local var="$1"
  local value="$2"
  local current
  current="$(grep -E "^${var}=" .env | tail -1 | cut -d '=' -f2-)"
  if [ -n "$current" ]; then
    return 0
  fi
  if grep -qE "^${var}=" .env; then
    local tmp
    tmp="$(mktemp)"
    awk -v var="$var" -v val="$value" -F'=' 'BEGIN{OFS="="} $1==var{$0=var"="val} {print}' .env > "$tmp"
    mv "$tmp" .env
  else
    printf '%s=%s\n' "$var" "$value" >> .env
  fi
  chmod 600 .env
  echo "  pinned ${var} in .env (auto, first run)"
}

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

# vault-mcp: the Git-backed write door for vault NOTES (notes go through
# MCP, never raw git — see 03-INFRA/vault-write-architecture.md). Provision
# the bare repo + worktree first (idempotent), then start the container.
# VAULT_MCP_ENABLED=0 skips the whole stack (e.g. a VPS that only hosts n8n
# for a Local-Only install, which has no remote vault at all).
if [ "${VAULT_MCP_ENABLED:-1}" != "0" ]; then
  echo "==> vault-mcp"
  bash vault-mcp/provision-vault-repo.sh
  ensure_env_value VAULT_MCP_UID "$(id -u)"
  ensure_env_value VAULT_MCP_GID "$(id -g)"
  docker compose -f vault-mcp/docker-compose.yml --env-file .env up -d --build
else
  echo "==> vault-mcp skipped (VAULT_MCP_ENABLED=0)"
fi

echo
echo "Stacks up. Health checks:"
echo "  n8n:        http://127.0.0.1:5678/healthz"
echo "  firecrawl:  curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3002/"
echo "  ocr:        curl -s http://127.0.0.1:3033/health"
echo "  vault-mcp:  curl -s http://127.0.0.1:8081/healthz"
echo "  (or: docker compose -f <stack>/docker-compose.yml ps, once the"
echo "  per-service healthchecks above have had time to run)"
echo
echo "Next: create SSH tunnels from your workstation to these ports."
echo "See 03-INFRA/remote-automation.md for the tunnel map and 99-INDEX/USER-PROFILE.md"
echo "for the port variables to fill in."
echo
echo "Backup / restore / rollback: see ./backup-restore.sh --help and the"
echo "'Backup, restore, rollback' section of README.md."
