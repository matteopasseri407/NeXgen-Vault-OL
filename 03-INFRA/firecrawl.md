---
tags:
  - infra
  - firecrawl
  - agents
  - scraping
status: active
---

# Firecrawl — default web scraping/search lane (self-hosted)

Agent-facing runbook. For deployment/infra (containers, remote repo, fixes) see `03-INFRA/remote-automation.md`. For per-provider config see `agent-universal-layer/mcp/manifest.yaml`.

## Usage rule

When a task touches the web read-only — search, scrape, crawl, map, structured extraction — the **first tool is Firecrawl**, not the agent's native `web_search`/browser, and not local headless. Firecrawl runs server-side on the remote backend, no local browser. Local headless is an exception (read-only, anonymous, when Firecrawl is not enough). Interactive/authenticated/state-changing → shared visible Chrome (`03-INFRA/agent-browser-cdp.md`), never Firecrawl.

In a **Local-Only setup** (no remote backend), Firecrawl is absent. The CLI's native web search is the default, and local headless is the scraping lane. See `99-INDEX/USER-PROFILE.md` for the user's architecture.

## Order for searches + date in the query

1. **`firecrawl_search`** is the default, always, if Firecrawl is up.
2. **Firecrawl down?** First a quick health check (section below: most of the time it is the tunnel). If it is genuinely down: the CLI's **native web search** is the legitimate fallback for searches — a degraded lane, not the default.
3. **The date in the query, ALWAYS.** Before any date-sensitive search — news, versions/releases, prices, job ads, "latest" — run `agent-now` and use the CURRENT year/date in the query and when judging result freshness. The training cutoff lies about the current year.

## How agents reach it

- **MCP `firecrawl`** (the only path for agents): the generated config runs the exact `firecrawl-mcp@3.22.3` pin from `agent-universal-layer/mcp/manifest.yaml`, with `FIRECRAWL_API_URL=http://127.0.0.1:<firecrawl-tunnel-port>`. Tools: `firecrawl_scrape`, `firecrawl_search`, `firecrawl_map`, `firecrawl_crawl`, `firecrawl_extract`.
- **Wrapper `firecrawl-local`** (only for L0/deterministic scripts, no MCP): calls `/v2/scrape` and `/v2/search` defaulting to the tunnel URL. Source: `03-INFRA/scripts/firecrawl-local.sh`.

## Health check (before concluding "Firecrawl is down")

The API is bound on the remote backend at `127.0.0.1:3002` and exposed here via SSH tunnel at `127.0.0.1:<firecrawl-tunnel-port>`. If tools seem absent or fail, the first suspect is the tunnel down or the MCP not started, not "Firecrawl does not exist".

- Quick check: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:<firecrawl-tunnel-port>/` → `302` = up. Or `firecrawl-local status`.
- Tunnel down — Linux/Mac: `systemctl --user restart firecrawl-<remote>-tunnel.service`. Windows: verify/restart the SSH process holding the tunnel.
- If the remote is down: `ssh <remote-alias> "sudo docker ps | grep firecrawl"` (should show `firecrawl-api-1` & worker).
