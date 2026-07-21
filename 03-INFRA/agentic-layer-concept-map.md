---
tags:
  - infra
  - architecture
  - map
status: active
type: map
---

# Agentic Layer Concept Map

Logical map + technical choices and their *why*. For the write flow in detail: `03-INFRA/vault-write-architecture.md`. For the project, register and backlog: `02-PROJECTS/`.

## Principle: one soul, many machines

The user runs one agent system across multiple CLIs and machines that must act as a single soul. Behaviour, MCP config, skills, and memory each have ONE canonical source in the vault; what each CLI or machine sees is a GENERATED, read-only derivative.

## Topology

```
                          ┌─────────────────────────────────────┐
                          │   THE USER (human-in-the-loop)      │
                          └───────────────────┬─────────────────┘
                                              │
        ┌─────────────────────────────────────┴─────────────────────────────┐
        │  MACHINE A (e.g. laptop)              MACHINE B (e.g. desktop)     │
        │  mobile / fallback                    workstation                  │
        │  local worker: on-demand only         local worker: on-demand only │
        └──────────────┬──────────────────────────────────────┬─────────────┘
                       │      (same layer on both)            │
        ┌──────────────┴───────────────┐              ┌───────┴────────────────┐
        ▼              ▼               ▼              ▼        ▼              ▼
   ┌─────────┐  ┌──────────┐  ┌────────────┐  ┌──────────┐  (idem on the other machine)
   │ CLI 1   │  │  CLI 2   │  │   CLI 3    │  │  CLI 4   │
   │frontier │  │frontier  │  │ reasoning  │  │  cheap   │
   └────┬────┘  └────┬─────┘  └─────┬──────┘  └────┬─────┘
        └────────────┴───────┬──────┴──────────────┘
                             │  UNIVERSAL LAYER (source in vault, derivatives read-only)
        ┌────────────────────┼─────────────────────────────┬──────────────────┐
        ▼                    ▼                             ▼                  ▼
   BEHAVIOUR            CONFIG (MCP)                  MEMORY              HOOKS
   AGENTS.md        manifest.yaml +               KnowledgeVault      checkpoint hook
   (1 file,         render.py →                   (markdown notes)    (per-CLI, optional)
   every CLI)       per-CLI dialect
```

## The three planes

1. **Behaviour** — `AGENTS.md` is the single bootstrap. Every CLI's pointer file references it. One file, every agent, drift impossible.
2. **Config** — `mcp/manifest.yaml` describes every MCP server once; `render.py` translates it into each CLI's dialect. Unknown live entries stay additive, while exact names in `retired_servers` are explicit user-authorized tombstones propagated to every CLI. Every local MCP package launched through `npx` has an exact version pin, so an upgrade is a tested engine change rather than an implicit upstream update. Antigravity's authenticated HTTP servers pass through an engine-owned Node bridge that derives the header from the named environment variable without writing a token into JSON. On Windows, generated stdio commands resolve to absolute launchers with a bounded Node-safe `PATH`, and Codex aliases are validated after hyphen-to-underscore normalization before any live file is touched. `skills/skills.manifest.yaml` does the same for skills. GitHub skills declare a full commit SHA, and `skills-sync.py` fetches and checks that exact object before materializing it in `~/.agents/skill-library`. Only explicit `exposure: core` skills enter the discovery-safe `~/.agents/skills`; all other bodies are selected with `agent-skill find|show`. Codex progressive-discloses the bodies it does discover, but the stricter manual policy keeps the initial metadata catalog consistent across CLIs.

The default MCP set stays small: the Vault Library carries versioned memory and semantic retrieval, while Firecrawl and Vault OCR mount when their self-hosted tunnels are configured. Playwright stays available on every CLI through a safety wrapper that preserves the shared browser's native file chooser and Downloads directory. When it attaches to an existing browser over CDP, the wrapper detaches when an MCP client closes instead of closing the user's browser context. Calendar access remains an on-demand command unless the user deliberately mounts its MCP server for one task. NeXgen does not add convenience MCP servers outside the canonical manifest by inference.
3. **Memory** — the KnowledgeVault (markdown notes, Git-backed). Written through one door per type: notes via the `vault-library` MCP, infra files via `vault-push`.

## Sync transaction boundary

`agent-sync guard` is a host-wide locked transaction: resolve the data-owned
remote policy, prove the local branch fresh against its authoritative remote,
then regenerate derivatives and run health checks. Dirty, wrong-branch, ahead,
diverged, missing-remote, and failed-fetch states block apply. A network-only manual
override exists as `agent-sync apply --allow-offline`; the recurring guard can
never use it. Each phase returns an explicit result and any required failure
propagates to the process exit code.

Windows host mutations are a separate transaction boundary. Tests and
sandboxed integrations set `NEXGEN_DISABLE_HOST_MUTATIONS=1`, which makes
registry and Task Scheduler adapters no-op before any external call. Real
scheduled-task wrappers are generated under per-user runtime state and carry
the resolved engine/data topology into the hidden process; machine-specific
paths never belong in the public checkout. Windows command launchers are real
local shims, rather than file symlinks, so PowerShell resolves `$PSScriptRoot`
inside the engine checkout. The PowerShell control plane probes `import yaml`
once and reuses that validated Python runtime across sync and doctor operations.

The authoritative remote and publication mirrors are declared once in the
private data vault at `03-INFRA/agent-universal-layer/sync/remotes.yaml`. Doctor and
publish resolve that same policy. Mirrors may lag without becoming a second
source of truth. Running `agent-sync` without a command is help-only, and the
old implicit `full` operation no longer exists. Full contract:
`docs/sync-contract.md`.

## Why one source

Hand-patching per-CLI configs creates drift: one CLI behaves differently from another, one machine falls behind, a fix on one side does not propagate. The single-source + provisioner model means a change is made once and carries everywhere. The cost is the provisioner machinery; the benefit is a system that stays coherent as it grows.

## Council prompt transport

Council keeps the full user brief out of the operating system command line. Codex and Antigravity receive it through stdin. OpenCode receives a protected temporary attachment, because that is its documented non-argv interface. The attachment is created inside the private session tree and removed after the seat exits, including on failure. This prevents Windows and POSIX command-line limits from turning a valid large review into an opaque invocation error, while keeping the existing ephemeral-session policy intact. Each seat call has a 300-second conservative default, an optional positive `timeout_seconds` per seat, and a per-command `--timeout-seconds` override. The command line wins over the seat policy, so an urgent invocation can be bounded without editing the data plane.

When the private data plane enables `routing`, Council also reads the declared private decision document as a decision input. The public engine never writes that document and no external workflow writes `seats.yaml`: a local in-memory resolver matches only declared routing IDs or labels to an exact seat model and optional reasoning effort, then probes the local CLI where possible. It presents the document's explicit fallback order as a verified proposal, never as an automatic invocation. A human must explicitly choose `--seat` for a single call or `--sequence` for a relay, including how many seats to call. An unavailable, unverified, or privacy-blocked candidate is skipped with an explanation. No candidate means a visible stop, never a guessed provider or model. This keeps global routing strategy in the private decision plane while preserving host-specific truth and human agency at the execution boundary.

As of 2026-07-15, `agy` (Antigravity) is refused as a seat outright, at the same point immediately before process spawn that every other seat's invocation funnels through: a live relay run showed `agy --print` ignores both the model selection and the given prompt, reading real local files instead of answering — a violation of the stateless text-in/text-out contract every seat above assumes. This does not affect `agy` as a caller of Council (a human working in Antigravity shelling out to `council` is unaffected by anything in this section). Full finding and reactivation conditions: `docs/council.md`, "Current limitations".

## Guardians

- **`agent-sync`** — locks, proves authoritative data freshness, then reconciles live configs with the canonical sources on each machine.
- **`agent-doctor`** — the single diagnostic: git state, MCP reachability, instruction drift, env tokens, skills, local worker. The only command to run by hand when something seems off.
- **`agent-open-folder`** — generated cross-platform desktop action for revealing a validated absolute local folder after an agent download, without driving the browser UI.
- **healthcheck step (inside `agent-sync`)** — grouped health summary; sends an alert only on FAIL. Was a standalone `agent-healthcheck.sh`, folded into `agent_sync.py`.
- **`vault-lifecycle-audit.py`** — read-only heat-map for vault grooming candidates.

Full guardian map: `03-INFRA/agent-guardians-map.md`.

## Cross-platform definition of done

No architecture change is "done" until it is carried and verified on every machine and CLI it touches. The map is part of "done": if a change alters the architecture, update this map in the same pass.

## Related notes

- `03-INFRA/vault-write-architecture.md`
- `03-INFRA/agent-guardians-map.md`
- `03-INFRA/agent-universal-layer.md`
- `03-INFRA/agent-orchestration-protocol.md`
