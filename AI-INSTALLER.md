# AI-INSTALLER.md — Autonomous Setup Mode

You are an AI agent with shell and file-write access, asked to set up NeXgen
Vault for a new user in this folder. This is the **fast,
autonomous companion to `INIT.md`**: same install, minimal back-and-forth —
you do the work instead of walking the user through it one question at a
time. If the user would rather be guided through a slower interview
instead, use `INIT.md` directly.

**Do the work yourself.** Do not just describe these steps to the user —
run the commands and write the files.

## 1. Preflight

Run the deterministic preflight check first — it verifies prerequisites,
the vault scaffold, and detects installed CLIs, so you don't have to
re-derive any of that by hand:

```bash
bash install.sh --check
```

Fix anything reported as a hard requirement (`✗`) before continuing.
Warnings (`○`) are fine to skip.

## 2. One combined question

Ask the user a single message with these questions together, not one at a
time:

1. How many CLIs do you want to use — just one, or more than one (Claude
   Code / Codex / OpenCode / Antigravity)?
2. How many machines need to stay aligned — just this one, or more?
3. Local-only, or do you have a VPS for n8n/Firecrawl/OCR (Cloud-Server
   mode)?
4. Any key documents (CV, project brief, brand rules) you want ingested
   into the vault right away?

From the answers, derive the `profile` exactly like `INIT.md`'s Step 1: 1
CLI and 1 machine → `MINIMAL`; 2+ of either → `MULTI`.

## 3. Execute

Follow `INIT.md`'s Steps 2 through 7 (English section) exactly, but as
direct action instead of a guided conversation: write
`99-INDEX/USER-PROFILE.md`, ingest any documents given, install MCP
servers/skills per the chosen profile (skills are the user's own data —
never assume specific skill names), run `agent-sync.sh apply` /
`agent-sync.ps1 apply` if MULTI, deploy the remote stack if Cloud-Server
was chosen. `INIT.md` is the single source of truth for exactly what each
step does; this file only changes the pacing, not the mechanism.

Finish with `agent-doctor` (MULTI) or a visual check that the chosen CLI
loads `AGENTS.md`, mounts MCP servers, and sees its skills (MINIMAL).

## 4. If anything fails

Report the exact command and error to the user in plain language, together
with the fix suggested by `install.sh`'s own message or `agent-doctor`'s
output. Never silently skip a required step or guess at a fix.
