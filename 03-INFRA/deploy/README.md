# Deploy — self-hosted stack (Cloud-Server mode)

This directory deploys the remote backend services that the Cloud-Server
install mode relies on: **n8n** (automation), **Firecrawl** (self-hosted
scraping/search), and **Vault OCR** (self-hosted text extraction).

A **Local-Only** install does NOT need any of this — everything runs on the
workstation with native CLI search, model vision for OCR, and no remote
automations.

## What is here

```
deploy/
├── bootstrap-vps.sh        # one-command deploy on the VPS
├── backup-restore.sh       # volume backup/restore + image-pin rollback
├── .env.example            # copy to .env, fill in secrets
├── n8n/
│   └── docker-compose.yml  # n8n automation engine
├── firecrawl/
│   └── docker-compose.yml  # Firecrawl API + worker + Redis + Playwright
└── ocr/
    ├── docker-compose.yml  # Vault OCR API (RapidOCR)
    ├── api/                # OCR service source (FastAPI + RapidOCR)
    └── mcp/                # OCR MCP server (stdio bridge)
```

## Deploy on a VPS

```bash
# on the VPS
git clone https://github.com/<github-user>/NeXgen-Engine.git
cd NeXgen-Engine/03-INFRA/deploy
cp .env.example .env        # edit and fill in secrets
bash bootstrap-vps.sh
```

Requirements on the VPS: Docker, and the Compose v2 plugin (`docker compose`,
not the legacy standalone `docker-compose`).

## Image pins

Every service image in the three `docker-compose.yml` files is pinned to an
explicit version tag — none of them use `:latest`, so a redeploy on a fresh
VPS reproduces the same images instead of drifting to whatever shipped that
day. Each compose file's header comment explains how to also pin a sha256
digest once you have Docker/registry access (this repo's CI and sandboxes
don't). Every pin can be overridden with an env var (`N8N_IMAGE`,
`OCR_IMAGE`, `FIRECRAWL_IMAGE`, `FIRECRAWL_REDIS_IMAGE`,
`FIRECRAWL_PLAYWRIGHT_IMAGE`) — see `.env.example`.

Note: the Firecrawl images below use the `ghcr.io/mendableai/*` path that
matches this repo's existing simple API+worker+Redis+Playwright shape.
Upstream Firecrawl has been reorganizing its self-host images and
architecture; confirm the pinned image still resolves before a production
deploy (see the comment in `firecrawl/docker-compose.yml`).

## Healthchecks

Every service in all three compose files has a `healthcheck:`. Where a
service exposes no HTTP endpoint of its own (the Firecrawl worker), the
healthcheck instead checks connectivity to the dependency it needs (Redis) —
the closest available liveness signal. `docker compose -f
<stack>/docker-compose.yml ps` shows current health once a service has been
up longer than its `start_period`.

## Backup, restore, rollback

`backup-restore.sh` (next to `bootstrap-vps.sh`) covers all three:

```bash
# Back up every named volume in one stack (or all of them)
./backup-restore.sh backup n8n
./backup-restore.sh backup all

# Restore one archive into a volume (stop the stack first)
docker compose -f n8n/docker-compose.yml down
./backup-restore.sh restore backups/n8n-data_20260712T140502Z.tar.gz n8n-data
docker compose -f n8n/docker-compose.yml up -d

# Roll a stack back to a previous image tag (no backup needed)
./backup-restore.sh rollback n8n N8N_IMAGE n8nio/n8n:2.29.9
```

Backups land in `./backups` (override with `BACKUP_DIR`), named
`<volume>_<UTC timestamp>.tar.gz`; only the `RETENTION_COUNT` most recent
archives per volume are kept (default 7). Today only n8n has a named
volume (`n8n-data`) — Firecrawl's Redis and the OCR service are stateless
by design in this stack, so `backup all` is a no-op for them. Run
`./backup-restore.sh --help` for the full usage.

Each stack binds to `127.0.0.1` only — they are NOT exposed on the public
interface. You reach them from your workstation over SSH tunnels (see
`03-INFRA/remote-automation.md`).

## Reach the stacks from your workstation

Create persistent SSH tunnels (port variables come from
`99-INDEX/USER-PROFILE.md`):

```bash
ssh -L 127.0.0.1:<n8n-tunnel-port>:127.0.0.1:5678 \
    -L 127.0.0.1:<firecrawl-tunnel-port>:127.0.0.1:3002 \
    -L 127.0.0.1:<ocr-tunnel-port>:127.0.0.1:3033 \
    <remote-alias> -N
```

Or run them as systemd user units (recommended for always-on use).

## Vault MCP (optional)

The `vault-library` MCP server (Git-backed read/write to the vault from
agents) is a separate component. If you want agents to write notes via MCP
rather than direct git, deploy it alongside this stack and point it at a
bare vault repo on the VPS. The deployment source for that container is not
bundled here; see `03-INFRA/vault-write-architecture.md` for the write model
(note → MCP, one door per type).

## Resource notes

- n8n: 1g memory limit.
- Firecrawl: ~2.5g total (API + worker + Redis + Playwright).
- OCR: 2g memory, 1.5 CPU (RapidOCR model load). Keep one worker.
- Add `--memory` caps to any extra container you add; an uncapped OOM can
  take down the whole VPS.
