# Deploy — self-hosted stack (Cloud-Server mode)

This directory deploys the remote backend services that the Cloud-Server
install mode relies on: **n8n** (automation), **Firecrawl** (self-hosted
scraping/search), **Vault OCR** (self-hosted text extraction), and
**vault-mcp** (the `vault-library` MCP server — the Git-backed write door
for vault notes).

A **Local-Only** install does NOT need any of this — everything runs on the
workstation with native CLI search, model vision for OCR, and no remote
automations.

If you're standing this stack up for more than one person, its credentials
(`.env`, the n8n/Firecrawl/OCR tunnels) are shared infrastructure with no
per-person scoping today — read `docs/team.md` before you adopt it as
shared team infra.

## What is here

```
deploy/
├── bootstrap-vps.sh        # one-command deploy on the VPS
├── backup-restore.sh       # volume backup/restore + image-pin rollback
├── .env.example            # copy to .env, fill in secrets
├── semantic-search-recipe.md  # spec to build a semantic_search backend (no compose file, see below)
├── n8n/
│   └── docker-compose.yml  # n8n automation engine
├── firecrawl/
│   ├── docker-compose.yml  # Firecrawl base: scrape, crawl, map, extract
│   ├── docker-compose.search.yml  # Optional pinned SearXNG + Brave search
│   └── searxng/            # Secret-safe settings renderer and entrypoint
├── ocr/
│   ├── docker-compose.yml  # Vault OCR API (RapidOCR)
│   ├── api/                # OCR service source (FastAPI + RapidOCR)
│   └── mcp/                # OCR MCP server (stdio bridge)
└── vault-mcp/
    ├── docker-compose.yml  # vault-library MCP server (Git-backed note writes)
    ├── provision-vault-repo.sh  # idempotent bare repo + worktree + hook
    └── src/                # server source (Python, streamable-http MCP)
```

## Semantic search — recipe, not a deployable stack

Unlike the stacks above, there is no `docker-compose.yml` here for the
`vault-library` MCP's `semantic_search` backend — it is not bundled with
this repository. [`semantic-search-recipe.md`](semantic-search-recipe.md)
is a build specification instead: the exact embedding model, hybrid
ranking algorithm and weights, reranker, and resource footprint the
maintainer's own instance runs, precise enough for an AI coding agent to
implement a compatible backend from scratch. Without one, `vault-mcp`'s
`semantic_search` tool falls back to `semantic_unavailable` and agents use
`search_notes` instead.

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

To enable Web search, put a Brave Search API key in
`BRAVE_SEARCH_API_KEY` inside `.env`. The bootstrap generates the separate
SearXNG session secret and merges `firecrawl/docker-compose.search.yml`
automatically. Leave the key empty for a scrape-only deployment. Set the
Brave dashboard usage limit to **Free credits only** so the provider rejects
requests before they can become paid overage.

## Image pins

Every service image in the four `docker-compose.yml` files is pinned to an
explicit version tag — or, where upstream publishes no versioned tags at
all (`firecrawl/nuq-postgres`, `firecrawl/playwright-service`), to a sha256
digest, which is even stronger — so a redeploy on a fresh VPS reproduces
the same images instead of drifting to whatever shipped that day. Every pin
can be overridden with an env var (`N8N_IMAGE`, `OCR_IMAGE`,
`VAULT_MCP_IMAGE`, `FIRECRAWL_IMAGE`, `FIRECRAWL_REDIS_IMAGE`,
`FIRECRAWL_RABBITMQ_IMAGE`, `FIRECRAWL_NUQ_POSTGRES_IMAGE`,
`FIRECRAWL_PLAYWRIGHT_IMAGE`, `FIRECRAWL_SEARXNG_IMAGE`) — see
`.env.example`.

Note: the Firecrawl images use the `ghcr.io/firecrawl/*` path. The project
moved there from `ghcr.io/mendableai/*` upstream; the old path no longer
resolves at all (VERIFIED 2026-07-12, see the comment in
`firecrawl/docker-compose.yml`) — do not use it if you find it in an older
note or a search result.

## Healthchecks

Every service in all four compose files has a `healthcheck:`. Where a
service exposes no HTTP endpoint of its own, the healthcheck uses the
closest available liveness signal (e.g. a TCP connect for Playwright,
`rabbitmq-diagnostics` for RabbitMQ, `pg_isready` for the NUQ Postgres).
`docker compose -f <stack>/docker-compose.yml ps` shows current health once
a service has been up longer than its `start_period`.

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
archives per volume are kept (default 7). n8n has `n8n-data` and Firecrawl
has `firecrawl-nuq-data` (the NUQ queue Postgres — restoring it requires
the matching `FIRECRAWL_POSTGRES_PASSWORD` in `.env`); Firecrawl's Redis
and the OCR service are stateless by design, and vault-mcp's data is the
vault itself (backed up by its own Git remotes, not by this script). Run
`./backup-restore.sh --help` for the full usage.

Each stack binds to `127.0.0.1` only — they are NOT exposed on the public
interface. You reach them from your workstation over SSH tunnels (see
`03-INFRA/remote-automation.md`).

That binding is the primary defense, but it is a single point of failure —
one mis-typed `127.0.0.1:` in a `docker-compose.yml` and a service is
reachable from the internet with nothing else in the way. As a second,
independent layer, `bootstrap-vps.sh` also configures a **minimum** host
firewall baseline via `ufw` if it is installed: allow OpenSSH, default deny
incoming, default allow outgoing, then enable. It is idempotent (safe to
re-run) and applies the SSH allow rule before enabling deny-by-default, so
it will not lock you out of an existing SSH session. This is not an
enterprise firewall — no rate limiting, no egress filtering, no per-service
rules — just a fail-closed backstop for the compose binding. If `ufw` is
not available, the script prints a warning and skips it rather than
failing; install `ufw` (or configure an equivalent firewall by hand) and
re-run.

## Reach the stacks from your workstation

Create persistent SSH tunnels (port variables come from
`99-INDEX/USER-PROFILE.md`):

```bash
ssh -L 127.0.0.1:<n8n-tunnel-port>:127.0.0.1:5678 \
    -L 127.0.0.1:<firecrawl-tunnel-port>:127.0.0.1:3002 \
    -L 127.0.0.1:<ocr-tunnel-port>:127.0.0.1:3033 \
    -L 127.0.0.1:<vault-mcp-tunnel-port>:127.0.0.1:8081 \
    <remote-alias> -N
```

Or run them as systemd user units (recommended for always-on use).

## Vault MCP (`vault-library`)

The `vault-library` MCP server (Git-backed read/write to the vault from
agents) ships in `vault-mcp/` and is deployed by `bootstrap-vps.sh` as the
fourth stack: it provisions the bare vault repo + worktree on the VPS
(`vault-mcp/provision-vault-repo.sh`, idempotent), generates
`VAULT_LIBRARY_TOKEN` into `.env`, and starts the container on
`127.0.0.1:8081`. In Cloud-Server mode this is the ONLY door for writing
notes — agents never commit notes with raw git; see
`03-INFRA/vault-write-architecture.md` for the write model (note → MCP,
infra → `vault-push`, one door per type) and `vault-mcp/README.md` for the
component details.

To wire the agent CLIs on the workstation, set (then re-run `agent-sync`):

```bash
VAULT_LIBRARY_URL=http://127.0.0.1:<vault-mcp-tunnel-port>/mcp
VAULT_LIBRARY_TOKEN=<value bootstrap-vps.sh wrote into the VPS .env>
```

Set `VAULT_MCP_ENABLED=0` before running `bootstrap-vps.sh` to skip this
stack (e.g. a VPS that only hosts n8n for a Local-Only install).

## Resource notes

- n8n: 1g memory limit.
- Firecrawl: ~6g total cap for the base services (harness API 3g + Playwright
  1g + NUQ Postgres 1g + RabbitMQ 512m + Redis 512m), plus up to 768m when
  the SearXNG search overlay is enabled. The 2.11 architecture is heavier
  than the old API+worker shape — budget the VPS accordingly.
- OCR: 2g memory, 1.5 CPU (RapidOCR model load). Keep one worker.
- vault-mcp: 512m memory limit.
- Add `--memory` caps to any extra container you add; an uncapped OOM can
  take down the whole VPS.
