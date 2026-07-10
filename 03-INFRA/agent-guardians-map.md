# Agent Layer Guardians Map

Operational companion to [[agentic-layer-concept-map]]: the "observability" view of the layer, i.e. who executes, who diagnoses, and who alerts.

## Golden rule

One single place decides whether something is broken, one single place tells the user, everything else executes silently.

## The three roles

- **A brain, `agent-doctor`.** Full diagnosis: git vs remote, MCP reachability, MCP drift via `render.py`, canonical instructions, tokens in env, skills, resolved local worker, Claude hooks, strict-check of real consumers. Each connector probe follows the protocol it is checking, so the Streamable HTTP Vault Library probe sends its MCP `Accept` header instead of treating a protocol rejection as an outage. It is the only judge and the only command meant to be run by hand: `agent-doctor` (or `--summary` for the one-line `PASS/WARN/FAIL`).
- **A megaphone, the `_send_healthcheck` step inside `agent_sync.py`.** The ONLY thing authorized to notify. It queries the doctor and alerts only on FAIL, with debounce (immediately if the problem is new, once a day if it persists), plain-language message with `[technical: ...]` appended. Transport order: messaging bot, webhook, desktop `notify-send`, log. It used to be a standalone `agent-healthcheck.sh` (+ a mirrored PS function); both were unified into one cross-platform step of `agent_sync.py` and the standalone script was removed.
- **A clock, `agent-sync.timer`** (Linux every 30 min, a scheduled task on Windows). The only recurring scheduler. It runs `agent-sync guard` = pull + apply derived config + healthcheck, all inside the one `agent_sync.py` process.

## Consolidation pass (single megaphone)

`agent-sync` used to notify MCP drift on its own (an inline sentinel, no debounce, separate sender), duplicating what `agent-doctor` already computes and the healthcheck step already announces at the end of each run. That path was removed: there is now ONE single alert surface. A later pass folded the standalone `agent-healthcheck.sh` script itself into `agent_sync.py` too (same debounce state file, same interval, same doctor-summary/Telegram/webhook contract), so there is now one implementation for both OSes instead of a shell script plus a mirrored PS function. Verified: the only `notify-send` call in the layer lives in `agent_sync.py`'s `_send_healthcheck`.

## Automatic run flow

`agent-sync.timer` triggers `agent-sync guard` (all inside `agent_sync.py`):

1. pull the vault from the remote
2. apply: MCP config (`render.py`, additive) + skills (`skills-sync.py`)
3. `_send_healthcheck` queries `agent-doctor` and notifies ONLY on FAIL

## Inventory

| Guardian | Role | What it does |
|---|---|---|
| `agent-sync` (+ `.timer`) | clock | recurring run: pull, apply config and skills, calls the healthcheck. Silent except for the healthcheck step. |
| `agent-doctor` | brain | full alignment diagnosis; the only judge |
| `agent_sync.py`'s `_send_healthcheck` | megaphone | notifies only on FAIL, with debounce and human-readable format; the only alert |
| `render.py` | executor | generates MCP configs from the manifest (additive); computes drift |
| `skills-sync.py` | executor | validates the skill manifest, then propagates skills from it to every machine. Third-party GitHub sources require a full commit SHA, which it fetches and verifies with noninteractive Git and a bounded timeout. |
| `skill-check` | executor | advisory security check of a skill (SkillSpector) |
| `sync-vault-from-remote` | executor | pulls the vault from the remote before apply |
| `n8n-vault-backup` | executor | nightly backup of n8n workflows (cron on the remote backend) |
| domain refreshers | executor | optional user-owned dashboard/data refreshers, configured in the private data vault |
| tunnels to the remote | executor | persistent SSH tunnels (OCR, n8n, firecrawl) |

## Extension principle

A new check goes INSIDE `agent-doctor`, not into a new script that notifies on its own. Anything new that needs to reach the user goes through the `_send_healthcheck` step in `agent_sync.py`. Never add `notify-send` anywhere else: it would break the single-megaphone rule and bring back the scattered noise this consolidation removed.
