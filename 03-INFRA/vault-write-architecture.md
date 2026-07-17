# KnowledgeVault Write Architecture

Principle: **one door per kind of thing.** This is conditional on the
installation's Mode (`99-INDEX/USER-PROFILE.md`), the same split that file
itself uses:

- **Cloud-Server mode**: Cloud-first — the remote backend is the source of
  truth, the local filesystem is a read-only mirror + offline parachute. The
  rule below for notes is unconditional and MCP-only.
- **Local-Only mode**: there is no remote at all (no VPS, no `vault-library`
  MCP container) — the local filesystem itself is the only copy and the
  source of truth. See the Local-Only branch under "The two doors" below.

## The two doors

- **Notes / knowledge (markdown)**:
  - **Cloud-Server mode** → ONLY via the `vault-library` MCP (`create_note`, `append_note`, `update_note`, `update_section`). The MCP serializes writes with a lock (`flock`) and an `expected_hash` check (whole-note for `update_note`, per-section for `update_section`), and commits directly to the remote bare repo as author "Vault MCP". Agents **never commit notes by hand with git** — an unreachable server is an outage to report (`03-INFRA/offline-emergency-mode.md`), never permission to fall back to raw git. (This branch is unconditional and unchanged — it is correct as-is.)
  - **Local-Only mode** → direct edits to the local Markdown files, committed with plain `git` by the agent, are the correct and only path. The whole premise of the Cloud-Server rule — "the MCP serializes concurrent writes against a shared remote" — does not apply: a genuinely single-machine Local-Only install has no remote `vault-library` container and nothing to serialize against. There is no second door being opened here; there is no first door (no MCP) to begin with.
- **Infra files (scripts, manifests, hooks, config)** → `vault-push -m "message" <file...>`: git commit + publication to the configured authoritative remote, then its mirrors. A mirror never becomes an independent source of truth. This bullet describes Cloud-Server mode; `vault-push` recognizes the Local-Only `local`/`none` sentinel the same way `agent_sync.py`'s `publish()` does — it commits locally and skips only the remote push, it never refuses the local commit.

## Live components

- **`vault-library` MCP** (remote backend, container `vault-mcp`, `:rw`): serialized note writes, commits to the bare repo. The deployable source ships in this repo at `03-INFRA/deploy/vault-mcp/` and is stood up by `bootstrap-vps.sh` (bare repo + worktree provisioning included) — a Cloud-Server install without it is incomplete, not a different mode.
- **`cloud-pull.service`** (enabled): refreshes the local mirror by pulling from the remote backend.
- **`agent-sync.timer` / Windows scheduled task `KnowledgeVault Agent Sync`**: `guard` mode, i.e. locked authoritative pull + automatic propagation of runtime derivatives + healthcheck, with no automatic push. Unsafe Git states block propagation. `apply` is the manual alias of guard. Publishing already-made local commits is a separate `publish` or `vault-push` action. Running without arguments is help-only; there is no combined `full` mode.
- **`sync/remotes.yaml`**: the data-owned declaration of one authoritative remote and optional publication mirrors. `agent-sync`, `agent-doctor`, and the private publishing helper resolve this same policy.
- **`vault-push`**: publishes infra files. Cross-platform: the actual logic is one implementation, `agent_sync.py`'s `vault-push` subcommand. `03-INFRA/scripts/vault-push.sh` (symlinked into `~/.local/bin` on Linux/Mac) and `vault-push.ps1` (relinked + a `.cmd` wrapper on Windows) are both thin wrappers that forward into it — same single door on either OS.

## Retired

- **`autosync.service`** (a filesystem watchdog that auto-committed every 60s): REMOVED. It was the "second door" that generated commits blindly. Inert code left behind in `~/.local/share/knowledge-vault-autosync`, not run anymore.

## Golden rules

1. One source of truth for everything; everything else is generated or a read-only mirror.
2. Notes → MCP; infra → `vault-push`. Never two doors on the same thing.
3. Volatile data (e.g. a calendar agenda) is never versioned: read it live from the MCP connectors instead. The n8n workflow that used to sync it into the vault was archived for this reason.
4. Every new stable note must be made **discoverable from the hub**: right after `create_note`, add the INBOUND pointer in `00-START-HERE.md` — under `Current high-value topics` (permanent/high-value) or the `Retrieval rule` (conditional/task-scoped), with a one-line description and the note path. An outbound note→hub link is not enough: a note the hub does not list is orphaned and unfindable by other agents without semantic search.

## Known follow-ups

- Move any plaintext tokens from CLI settings into env vars, so no config file holds a secret literally.

## Resolved follow-ups

- ~~Exercise the transaction contract on a physical Windows host.~~ Done (2026-07-14/15): `agent-sync apply`'s locked pull-then-propagate transaction ran for real on physical Windows, twice — a full guided MULTI install (three CLIs, plus a Cloud-Server VPS deploy) and a separate realignment of an existing install to the current release. Still open: a cold install with no maintainer present to walk through failures — see README's "Platform status" section.
