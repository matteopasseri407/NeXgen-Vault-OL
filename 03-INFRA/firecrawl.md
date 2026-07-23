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

## Deep research contract

An open-ended research request is not complete after reading two search snippets.

1. Ask `firecrawl_search` for up to 20 results in the first query. This uses one search-provider request while giving the agent enough candidates to judge coverage.
2. Select and scrape several relevant pages, preferring primary sources. For a substantive comparison or investigation, aim to inspect at least 5 useful sources when that many exist.
3. Add another targeted search only when the first result set has a named coverage gap. Do not issue automatic paraphrase loops or retry the same failed query.
4. For a known site or domain, prefer `firecrawl_map`, `firecrawl_crawl`, or direct `firecrawl_scrape` over repeated Web searches.
5. For a narrow fact lookup, use a smaller result set when additional sources would not improve confidence.

The current self-hosted search adapter has two important constraints. Do not use `categories=["research"]`: it expands into a long query that Brave rejects. Do not rely on `includeDomains` as a security or relevance boundary with the pinned MCP version. Put an explicit `site:example.com` clause in the query and post-filter returned URLs before scraping them.

## How agents reach it

- **MCP `firecrawl`** (the only path for agents): the generated config runs the exact `firecrawl-mcp@3.22.3` pin from `agent-universal-layer/mcp/manifest.yaml`, with `FIRECRAWL_API_URL=http://127.0.0.1:<firecrawl-tunnel-port>`. Tools: `firecrawl_scrape`, `firecrawl_search`, `firecrawl_map`, `firecrawl_crawl`, `firecrawl_extract`.
- **Wrapper `firecrawl-local`** (only for L0/deterministic scripts, no MCP): calls `/v2/scrape` and `/v2/search` defaulting to the tunnel URL. Its search default is 20 results. Source: `03-INFRA/scripts/firecrawl-local.sh` on Linux/macOS and `03-INFRA/scripts/firecrawl-local.ps1` on Windows.

## Zero-cost search backend

The Cloud-Server deployment can add a pinned SearXNG service backed only by the Brave Search API. Put `BRAVE_SEARCH_API_KEY` in `03-INFRA/deploy/.env` and run `bootstrap-vps.sh`. The bootstrap generates the private SearXNG secret and activates `firecrawl/docker-compose.search.yml`; an empty key leaves scrape, crawl, map, and extract available without Web search.

Set the Brave dashboard usage limit to **Free credits only**. This is the provider-side hard stop that prevents a paid overage. NeXgen batches up to 20 candidates into each search request and avoids redundant query loops, but it cannot guarantee that a third-party provider will keep the same free allowance forever.

The overlay sets only `SEARXNG_ENGINES=braveapi`. Never add `SEARXNG_CATEGORIES=general` alongside it: SearXNG treats the two selectors as a union and silently enables public engines that can fail with captchas or rate limits.

## Health check (before concluding "Firecrawl is down")

The API is bound on the remote backend at `127.0.0.1:3002` and exposed here via SSH tunnel at `127.0.0.1:<firecrawl-tunnel-port>`. If tools seem absent or fail, the first suspect is the tunnel down or the MCP not started, not "Firecrawl does not exist".

- Quick check: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:<firecrawl-tunnel-port>/` → `302` = up. Or `firecrawl-local status`.
- Functional check: `agent-doctor --strict` runs one real search and requires a result from the expected domain. A successful result is cached locally for 24 hours, so repeated strict checks do not consume repeated provider requests.
- Tunnel down — Linux/Mac: `systemctl --user restart firecrawl-<remote>-tunnel.service`. Windows: verify/restart the SSH process holding the tunnel.
- If the remote is down: `ssh <remote-alias> "sudo docker ps | grep firecrawl"` (should show `firecrawl-api-1` & worker).
