#!/usr/bin/env bash
# backup-restore — volume backup, restore, and image-pin rollback for the
# Cloud-Server deploy stacks (n8n, firecrawl, ocr).
#
# Usage:
#   backup-restore.sh backup [n8n|firecrawl|ocr|all]
#     Tars + gzips every named volume declared in the target stack's
#     docker-compose.yml, writes it to $BACKUP_DIR, then prunes old
#     archives so only the $RETENTION_COUNT most recent per volume remain.
#     A stack with no named volumes (firecrawl, ocr today) is a no-op.
#
#   backup-restore.sh restore <backup-file> <volume-name>
#     Restores one archive into a docker volume, replacing its current
#     content. Destructive — stop the stack that mounts the volume first.
#
#   backup-restore.sh rollback <stack> <image-env-var> <previous-image-ref>
#     Redeploys one stack with an image env var pinned to an older tag
#     (e.g. rollback n8n N8N_IMAGE n8nio/n8n:2.29.9). Nothing is reverted
#     automatically: you choose the prior tag from your own deploy history
#     or registry. If it should stick, also edit the default in the
#     compose file so a future plain `docker compose up -d` keeps it.
#
# Env:
#   BACKUP_DIR        where archives are written (default: ./backups)
#   RETENTION_COUNT   archives kept per volume (default: 7)
#
# Archive naming: <volume>_<UTC timestamp>.tar.gz, e.g.
#   n8n-data_20260712T140502Z.tar.gz

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$DEPLOY_DIR/backups}"
RETENTION_COUNT="${RETENTION_COUNT:-7}"
HELPER_IMAGE="alpine:3.21"

usage() {
  cat <<'EOF'
Usage:
  backup-restore.sh backup [n8n|firecrawl|ocr|all]
  backup-restore.sh restore <backup-file> <volume-name>
  backup-restore.sh rollback <stack> <image-env-var> <previous-image-ref>

Env:
  BACKUP_DIR        where archives are written (default: ./backups)
  RETENTION_COUNT   archives kept per volume (default: 7)
EOF
}

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }
}

compose_file_for() {
  case "$1" in
    n8n) echo "$DEPLOY_DIR/n8n/docker-compose.yml" ;;
    firecrawl) echo "$DEPLOY_DIR/firecrawl/docker-compose.yml" ;;
    ocr) echo "$DEPLOY_DIR/ocr/docker-compose.yml" ;;
    *) echo "unknown stack: $1 (expected n8n, firecrawl, or ocr)" >&2; exit 2 ;;
  esac
}

# Prints the named top-level volumes declared by a compose file, one per
# line. Matches this repo's compose style: a bare top-level `volumes:`
# block with 2-space-indented `<name>:` entries and no nested driver
# config. Prints nothing if the file has no such block.
volumes_for() {
  local compose_file="$1"
  awk '
    /^volumes:/ { in_block=1; next }
    in_block && /^[^[:space:]]/ { in_block=0 }
    in_block && /^  [A-Za-z0-9_.-]+:/ { sub(/:.*/, "", $1); print $1 }
  ' "$compose_file"
}

do_backup_one() {
  local volume="$1"
  mkdir -p "$BACKUP_DIR"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local archive_name="${volume}_${stamp}.tar.gz"
  echo "==> backing up volume '$volume' -> $BACKUP_DIR/$archive_name"
  docker run --rm \
    -v "${volume}:/volume:ro" \
    -v "$BACKUP_DIR:/backup" \
    "$HELPER_IMAGE" \
    tar czf "/backup/$archive_name" -C /volume .
  prune_old_backups "$volume"
}

prune_old_backups() {
  local volume="$1"
  # shellcheck disable=SC2012
  ls -1t "$BACKUP_DIR/${volume}"_*.tar.gz 2>/dev/null \
    | tail -n "+$((RETENTION_COUNT + 1))" \
    | while IFS= read -r old; do
        echo "    pruning old backup: $old"
        rm -f -- "$old"
      done
}

cmd_backup() {
  local target="${1:-all}"
  local stacks=()
  case "$target" in
    all) stacks=(n8n firecrawl ocr) ;;
    n8n|firecrawl|ocr) stacks=("$target") ;;
    *) echo "unknown backup target: $target" >&2; usage >&2; exit 2 ;;
  esac
  local any_volume=0
  local stack compose_file volume
  for stack in "${stacks[@]}"; do
    compose_file="$(compose_file_for "$stack")"
    while IFS= read -r volume; do
      [ -n "$volume" ] || continue
      any_volume=1
      do_backup_one "$volume"
    done < <(volumes_for "$compose_file")
  done
  if [ "$any_volume" -eq 0 ]; then
    echo "no named volumes declared for target '$target' — nothing to back up"
    echo "(firecrawl and ocr are stateless in this stack today; only n8n has a volume)"
  fi
}

cmd_restore() {
  local archive="${1:?usage: backup-restore.sh restore <backup-file> <volume-name>}"
  local volume="${2:?usage: backup-restore.sh restore <backup-file> <volume-name>}"
  [ -f "$archive" ] || { echo "no such backup file: $archive" >&2; exit 1; }
  local archive_dir archive_base
  archive_dir="$(cd "$(dirname "$archive")" && pwd)"
  archive_base="$(basename "$archive")"
  echo "==> restoring $archive into volume '$volume'"
  echo "    this REPLACES the current content of '$volume'."
  echo "    stop the stack that mounts this volume first (docker compose -f <stack>/docker-compose.yml down)."
  read -r -p "    type 'yes' to continue: " confirm
  [ "$confirm" = "yes" ] || { echo "aborted"; exit 1; }
  docker volume create "$volume" >/dev/null
  docker run --rm \
    -v "${volume}:/volume" \
    -v "${archive_dir}:/backup:ro" \
    "$HELPER_IMAGE" \
    sh -c "rm -rf /volume/* /volume/.[!.]* 2>/dev/null; tar xzf /backup/${archive_base} -C /volume"
  echo "==> restore done. Restart the stack: docker compose -f <stack>/docker-compose.yml up -d"
}

cmd_rollback() {
  local stack="${1:?usage: backup-restore.sh rollback <stack> <image-env-var> <previous-image-ref>}"
  local image_var="${2:?usage: backup-restore.sh rollback <stack> <image-env-var> <previous-image-ref>}"
  local previous_ref="${3:?usage: backup-restore.sh rollback <stack> <image-env-var> <previous-image-ref>}"
  local compose_file
  compose_file="$(compose_file_for "$stack")"
  echo "==> rolling back $stack: $image_var=$previous_ref"
  env "${image_var}=${previous_ref}" docker compose -f "$compose_file" up -d
  echo "==> rollback deployed. Verify health, then decide whether to also update"
  echo "    the default pin in $compose_file so it survives a fresh checkout"
  echo "    (env overrides only apply to this invocation)."
}

cmd="${1:-}"
if [ -z "$cmd" ] || [ "$cmd" = "-h" ] || [ "$cmd" = "--help" ]; then
  usage
  exit 0
fi
shift || true

require docker

case "$cmd" in
  backup) cmd_backup "${1:-all}" ;;
  restore) cmd_restore "${1:-}" "${2:-}" ;;
  rollback) cmd_rollback "${1:-}" "${2:-}" "${3:-}" ;;
  *) echo "unknown command: $cmd" >&2; usage >&2; exit 2 ;;
esac
