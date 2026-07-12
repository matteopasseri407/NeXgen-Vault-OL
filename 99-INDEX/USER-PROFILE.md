# User Profile and Host Awareness

> **NOTE FOR THE INSTALLER AGENT**: Replace the `[BRACKET]` placeholders with the real data collected during the `INIT.md` interview. This file is the "map" of the user's machine.

This file contains the user's personal context and hardware specifics. Agents must read it at initialization to map the framework's abstract concepts onto the real machine.

## Installation profile

- **profile**: `[MINIMAL | MULTI]` — MINIMAL = 1 CLI on 1 machine. MULTI = 2+ CLIs and/or 2+ machines, uses `agent-sync` to propagate. Most "single source / propagate to all / cross-platform" rules in `AGENTS.md` are conditional on `profile: MULTI`.
- **clis**: `[list of installed CLIs, e.g. claude-code | codex | opencode | antigravity]`
- **machines**: `[list of machines, e.g. primary (this one) | secondary (optional)]`
- **sync_method**: `[manual | agent-sync]` — in MINIMAL, just install the CLI and mount MCP/skills by hand; in MULTI, use the `agent-sync` provisioner to align every CLI and machine to the canonical source.

## Host Awareness

- **Primary workstation**: `[FILL IN OS — e.g. Windows/Mac/Linux]`, `[FILL IN SPECS — e.g. M2 Max, RTX 4090, 32GB RAM]`. `[FILL IN NOTES — e.g. Use it for local models / Not suited for local models]`.
- **Secondary device (optional)**: `[FILL IN SECOND DEVICE SPECS, or remove if not present]`.

## Team members (optional)

> Leave this whole section out for a single-user install — the default,
> and the overwhelming majority of installs today. Nothing below changes
> any existing behavior (Council, skills sync, or anything else) unless
> this section is present.

This section declares who else uses this framework alongside the vault
owner, so a small team can route Council seats and skills without
contending for the same files. **It is a ROUTING/organizational aid
only — who owns which host, which seat file, which skills. It is NOT a
security boundary**: it grants no per-person credential isolation, no
access control, no audit trail. For the real state of multi-user support
(deliberately not built yet), see `docs/team.md`.

- **`[MEMBER NAME/ID, e.g. marco]`**:
  - **Host(s)**: `[one or more entries from "Host Awareness" above, e.g. Primary workstation]`
  - **CLI(s)**: `[the CLIs this person uses, e.g. claude-code | codex]`
  - **Seats file (optional)**: `[FILL IN, e.g. seats.marco.yaml — see council/seats.yaml.example for the naming convention. Omit to keep using the shared default seats.yaml]`

Repeat one entry per member. The identifier you write above only becomes
actual behavior through the `AGENT_TEAM_MEMBER` environment variable, set
per machine (e.g. in that person's own shell profile) to their name/id
from this list — this file's entries are read by humans and agents, not
parsed field-by-field by code. Setting `AGENT_TEAM_MEMBER` is what makes
Council resolve `seats.<member>.yaml` (see `council/seats.yaml.example`)
and lets `skills-sync.py` propagate a skill's `scope: personal` only to
its declared owner's machine(s) — and only once this section exists at
all; see `docs/lazy-skills.md`.

## Knowledge Vault

- **Primary workstation**: `[FILL IN ABSOLUTE PATH — e.g. /home/user/KnowledgeVault or C:\Users\user\KnowledgeVault]`
- **Git remote (optional)**: `[FILL IN YOUR FORK'S URL, or remove if you don't version the vault remotely]`

## Architecture: Local-Only or Cloud-Server

- **Mode**: `[LOCAL-ONLY] or [CLOUD-SERVER]`

Mode declares a MINIMUM baseline that `agent-doctor` verifies, never a maximum.
It only decides which connector checks are "expected" (a real FAIL if
missing/unreachable) versus correct-by-design for a Local-Only install. You
can always add more than Mode declares — upgrading from Local-Only to
Cloud-Server, or wiring in a single cloud connector while staying local for
everything else — and `agent-doctor` recognizes it automatically the moment
the matching environment variable is set, with no manual edits to the doctor
itself required.

### If LOCAL-ONLY

- Everything runs on the local machine. No VPS.
- Web search → the CLI's native tool (Firecrawl absent).
- OCR → model vision (self-hosted OCR absent).
- Remote automations → not available (remote n8n absent).
- Environment variable: `KNOWLEDGE_VAULT_REMOTE="local"`

### If CLOUD-SERVER

- **Remote backend (VPS)**:
  - SSH alias: `[FILL IN SSH ALIAS]`
  - Public IP: `[FILL IN IP]`
  - Home directory on the VPS: `[FILL IN REMOTE PATH]`
- **Local SSH tunnels**:
  - n8n: `127.0.0.1:[N8N_PORT]` → remote `127.0.0.1:5678`
  - Firecrawl: `127.0.0.1:[FIRECRAWL_PORT]` → remote `127.0.0.1:3002`
  - OCR: `127.0.0.1:[OCR_PORT]` → remote `127.0.0.1:3033`

## Model team (configured by the user)

In plain language: these "lanes" just say which model/CLI handles which kind
of task. A lane's role name describes the job (deep reasoning, hands-on
building, quick mechanical work), not a separate product to install — in
MINIMAL you can point every lane at the one CLI you already have.

- **Frontier (reasoning/architecture)**: `[FILL IN MODEL/CLI — e.g. Claude Opus, GPT-5, Gemini Pro]`
- **Frontier (orchestration/build)**: `[FILL IN MODEL/CLI]`
- **Mid-tier (component execution)**: `[FILL IN MODEL/CLI — e.g. DeepSeek V4-Pro]`
- **Frontier (terminal/sysadmin)**: `[FILL IN MODEL/CLI]`
- **Bulk (mechanical data)**: `[FILL IN MODEL/CLI — e.g. Gemini Flash, DeepSeek Flash]`
- **Local worker (fallback)**: `[FILL IN LOCAL MODEL — e.g. Ollama/Gemma, or "none"]`

In MINIMAL with a single CLI, you can map several lanes onto the same model/CLI. In MULTI, each lane usually corresponds to a different CLI.

## Identity & Tone

- You operate inside the user's KnowledgeVault as a disciplined member of their agent team.
- Keep the user visible: close substantial work with a short summary.
- The default browser profile is the user's single working profile. Never drive it headless.
- `[ADD THE USER'S COMMUNICATION PREFERENCES HERE (e.g. language, style, formality)]`
