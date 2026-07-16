# What gets written

This lists every file the installer (`INIT.md`), `install.sh`, and the MULTI-profile scripts (`agent-sync`, `render.py`) can create or modify outside this repo. Nothing here touches files outside your home directory, and nothing runs with elevated privileges.

## `install.sh`

`bash install.sh --check` is strictly read-only: a missing scaffold folder is reported as a failed check (`✗`), never created.

Plain `bash install.sh` (the default, guided mode) is almost as quiet: the only thing it can write is missing scaffold folders inside the repo itself (`01-NOTES/`, `02-PROJECTS/`, `04-NOW/`, `99-INDEX/`, `99-SECRETS/`, each with a `.gitkeep`), and only if they're missing from your clone — which normally does not happen on a full clone. It never writes outside the repo and asks no questions it doesn't discard afterward (the guided profile interview at the end is recommendation-only; nothing you answer is persisted by this script).

## `INIT.md` (the AI-guided installer, any profile)

- `99-INDEX/USER-PROFILE.md`: your profile, hardware, CLI/machine list, and architecture choice.
- Optionally `04-NOW/current-focus.md` or a note under `01-NOTES/`/`02-PROJECTS/`, if you choose to have it ingest a document (CV, project brief, brand rules) during setup.

## Per-CLI bootstrap and MCP config (MINIMAL: done by the agent by hand; MULTI: done by `agent-sync`/`render.py`)

| CLI | Bootstrap file | MCP config | Skills folder |
|---|---|---|---|
| Claude Code | `~/CLAUDE.md` (pointer to this repo's `AGENTS.md`) | `mcpServers` field in `~/.claude.json` | declared native-lazy view in `~/.claude/skills/` |
| Codex | `~/.codex/AGENTS.md` | Codex's own config file | only `exposure: core` views in `~/.codex/skills/` |
| OpenCode | `instructions` field in `opencode.json` | MCP section of the same `opencode.json` | `agent-skill find|show`, backed by `~/.agents/skill-library/` |
| Antigravity | `~/.gemini/config/AGENTS.md` | `~/.gemini/antigravity/mcp_config.json` | `agent-skill find|show`, backed by `~/.agents/skill-library/` |

MCP sections are additive by default. A server is removed from generated CLI
configs only when its exact old name is deliberately added to the canonical
manifest's `retired_servers` list. Authenticated Antigravity HTTP entries use
the engine-owned `mcp-http-bridge.mjs`; generated JSON stores only the bearer
environment-variable name, never its value.

These are patches to files that must already exist (each CLI creates its own default config the first time you open it). Nothing here creates a CLI's config file from scratch; if a chosen CLI has never been opened, that step is skipped for it.

## MULTI profile only, additional writes by `agent-sync`

- `~/.config/systemd/user/agent-sync.service` and `agent-sync.timer`: a recurring user-level timer that runs `agent-sync guard` (pull + regenerate CLI runtime files + healthcheck, no push). Only on Linux/systemd.
- Before overwriting a file it manages, `agent-sync` copies the previous version alongside it with a `.pre-<reason>-<timestamp>.bak` suffix in the same folder.
- `~/.local/state/agent-sync.log`: a plain-text run log.
- `~/.local/state/agent-sync.lock`: the stable one-byte host-wide transaction lock.
- `~/ANTIGRAVITY.md`: removed if present as a dead symlink (Antigravity doesn't read that path).

### Windows equivalent: scheduled task and hidden wrapper

On Windows, every `agent-sync apply`/`guard` run (`install_scheduler` in
`agent_sync.py`) self-heals the same recurring trigger that systemd provides
on Linux, using only your own user account (no admin elevation, no service):

- A hidden VBS wrapper, `start-agent-sync-hidden.vbs`, is written under the
  user's runtime state (`~/.local/state/`). It preserves the resolved engine,
  vault-data, vault, and branch values, then shells out to
  `agent-sync.ps1 guard` via `powershell.exe -NoProfile -ExecutionPolicy
  Bypass`, run with a hidden window so no console flashes on each cycle.
- Two Task Scheduler entries named `KnowledgeVault Agent Sync` and
  `KnowledgeVault Agent Sync Logon` are created or updated via `schtasks.exe`:
  one fires every 30 minutes, the other on logon. Both invoke the hidden VBS
  wrapper through `wscript.exe`.
- If the logon trigger can't be registered (`schtasks.exe` failure), a copy
  of the same hidden VBS wrapper is placed in your Startup folder
  (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\KnowledgeVault
  Agent Sync.vbs`) as a fallback so the recurring guard still runs after you
  log in.

The remote policy itself is private data, not an engine runtime derivative. In
a MULTI vault it may be declared at
`03-INFRA/agent-universal-layer/sync/remotes.yaml`, starting from the public
`remotes.yaml.example`. See `docs/sync-contract.md`.

## `99-SECRETS/`

Local only. `agent-sync`/agents may write to `99-SECRETS/archive/master-secrets.md.gpg` (GPG-encrypted, git-ignored) and `99-SECRETS/secrets-registry.md` (names and env vars only, never values, tracked in git). See `99-SECRETS/README.md` for the workflow.

## What this never does

No sudo, no changes outside your home directory, no telemetry, no network call you didn't configure (Cloud-Server mode only reaches the VPS you point it at over the SSH tunnel you set up), and no push to a git remote unless you or an agent explicitly runs a publish step.
