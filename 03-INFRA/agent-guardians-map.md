# Agent Layer Guardians Map

Operational companion to [[agentic-layer-concept-map]]: the "observability" view of the layer, i.e. who executes, who diagnoses, and who alerts.

## Golden rule

One single place decides whether something is broken, one single place tells the user, everything else executes silently.

## The three roles

- **A brain, `agent-doctor`.** Full diagnosis: git vs the configured authoritative remote and publication mirrors, MCP reachability, MCP drift via `render.py`, canonical instructions, tokens in env, skills, resolved local worker, Claude hooks, strict-check of real consumers. On Windows it also fails on a user, combined, or current-process `PATH` beyond `cmd.exe`'s 8,191-character inherited-variable limit and reports legacy skill views awaiting explicit quarantine. Each connector probe follows the protocol it is checking, so the Streamable HTTP Vault Library probe sends its MCP `Accept` header instead of treating a protocol rejection as an outage. It is the only judge and the only command meant to be run by hand: `agent-doctor` (or `--summary` for the one-line `PASS/WARN/FAIL`).
- **A megaphone, the `_send_healthcheck` step inside `agent_sync.py`.** The ONLY thing authorized to notify. It queries the doctor and alerts only on FAIL, with debounce (immediately if the problem is new, once a day if it persists), plain-language message with `[technical: ...]` appended. Transport order: messaging bot, webhook, desktop `notify-send`, log. It used to be a standalone `agent-healthcheck.sh` (+ a mirrored PS function); both were unified into one cross-platform step of `agent_sync.py` and the standalone script was removed.
- **A clock, `agent-sync.timer`** (Linux every 30 min, a scheduled task on Windows). The only recurring scheduler. It runs `agent-sync guard` = acquire the host lock + prove authoritative freshness + apply derived config + healthcheck, all inside the one `agent_sync.py` process. The Windows task points to a generated wrapper in per-user runtime state, which preserves the resolved engine and data roots without dirtying the public checkout. It never publishes.

## Consolidation pass (single megaphone)

`agent-sync` used to notify MCP drift on its own (an inline sentinel, no debounce, separate sender), duplicating what `agent-doctor` already computes and the healthcheck step already announces at the end of each run. That path was removed: there is now ONE single alert surface. A later pass folded the standalone `agent-healthcheck.sh` script itself into `agent_sync.py` too (same debounce state file, same interval, same doctor-summary/Telegram/webhook contract), so there is now one implementation for both OSes instead of a shell script plus a mirrored PS function. Verified: the only `notify-send` call in the layer lives in `agent_sync.py`'s `_send_healthcheck`.

## Automatic run flow

`agent-sync.timer` triggers `agent-sync guard` (all inside `agent_sync.py`):

1. resolve `sync/remotes.yaml` and acquire the host-wide lock
2. fetch the authoritative remote and classify the local state
3. only for a fresh or local-only state, render MCP config, propagate the rendered Antigravity source, then reconcile skills. MCP is additive except for exact, user-authorized `retired_servers` tombstones
4. aggregate required phase failures into the exit code
5. `_send_healthcheck` queries `agent-doctor` and notifies ONLY on FAIL

## Inventory

| Guardian | Role | What it does |
|---|---|---|
| `agent-sync` (+ `.timer`) | clock | locked recurring run: prove authoritative freshness, apply config and skills, call the healthcheck. Never publishes. |
| `agent-doctor` | brain | full alignment diagnosis; the only judge |
| `agent_sync.py`'s `_send_healthcheck` | megaphone | notifies only on FAIL, with debounce and human-readable format; the only alert |
| `render.py` | executor | generates MCP configs from the manifest, preserves unknown entries by default, removes only explicit `retired_servers`, and computes drift |
| `skills-sync.py` | executor | validates the skill manifest, materializes bodies in the non-discovered library, and exposes only declared core or Claude-native views. Codex progressive-discloses discovered bodies, while manual entries remain outside discovery to bound its initial metadata catalog. Legacy discovery folders move only through the explicit, reversible `--migrate-legacy` quarantine. Third-party GitHub sources require a full commit SHA, fetched and verified with noninteractive Git and a bounded timeout. |
| `skill-check` | executor | advisory security check of a skill (SkillSpector) |
| authoritative pull inside `agent_sync.py` | executor | typed freshness gate; unsafe Git states block apply instead of degrading to best effort |
| `n8n-vault-backup` | executor | nightly backup of n8n workflows (cron on the remote backend) |
| domain refreshers | executor | optional user-owned dashboard/data refreshers, configured in the private data vault |
| tunnels to the remote | executor | persistent SSH tunnels (OCR, n8n, firecrawl) |

## Extension principle

A new check goes INSIDE `agent-doctor`, not into a new script that notifies on its own. Anything new that needs to reach the user goes through the `_send_healthcheck` step in `agent_sync.py`. Never add `notify-send` anywhere else: it would break the single-megaphone rule and bring back the scattered noise this consolidation removed.
