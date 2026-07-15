---
tags:
  - infra
  - agents
  - model-routing
status: active
---

# Cheap Model Handoff Template

Use this when a frontier agent decides that a cheaper model, a subagent, or a local worker should do the heavy work.

## User-facing alert

Budget gate: the cheap route costs less here. Prepare a tight handoff for the cheap model and bring the frontier back only for judgment, risks, and final verification.

## Handoff

### Objective

State the concrete result wanted in one or two sentences.

### Context

- Machine:
- CWD/repo:
- Relevant project/vault note:
- Why this is cheap-route work:

### Scope

Read only:

- `path/or/pattern`

May edit only:

- `path/or/file`

Do not touch:

- secrets, `.env`, credentials, tokens, SSH keys, browser profiles, auth/session files
- KnowledgeVault writes
- destructive git commands
- system-wide config
- unrelated files

### Task

1. Gather narrow evidence.
2. Produce the requested analysis or patch.
3. Run only the listed verification commands.
4. Return concise findings, not raw logs.

### Verification

Commands allowed:

```bash
# fill with exact commands
```

### Output Format

Return:

- Result:
- Files touched:
- Commands run:
- Verification result:
- Risks/uncertainty:
- Recommended frontier review points:

### Budget Rules

- Keep context narrow.
- Do not read whole trees unless explicitly asked.
- Stop and report if the task becomes ambiguous, risky, secret-sensitive, or requires irreversible changes.
- If two attempts fail, stop and return evidence for frontier escalation.

## Execution Recipes

Use the cheapest effective route, not the most elaborate route. Adapt the commands to the runtimes the user has configured (see `99-INDEX/USER-PROFILE.md`).

### Built-in cheap subagent

Use when the work is small enough that staying inside the current frontier runtime is cheaper than launching another CLI/session.

Expected behavior:

- delegate only the bulk/mechanical part;
- return conclusions, not raw dumps;
- frontier keeps final judgment.

### External cheap CLI

Use for broad or repeated work where an external cheap runtime saves meaningful frontier quota. Replace `<cheap-cli>` with the user's configured cheap runner:

```bash
<cheap-cli> run --dir "$PWD" --model <cheap-model> "<handoff>" < /dev/null
```

On Windows PowerShell, pass the handoff as an argument and use the configured
wrapper directly, for example:

```powershell
& cheap-cli run --dir (Get-Location) --model <cheap-model> "<handoff>"
```

For bulk-only work:

```bash
<cheap-cli> run --dir "$PWD" --agent bulk "<handoff>" < /dev/null
```

PowerShell equivalent:

```powershell
& cheap-cli run --dir (Get-Location) --agent bulk "<handoff>"
```

Notes:

- requires the cheap CLI to be installed and authenticated (see `USER-PROFILE.md`);
- use `--format json` when the orchestrator needs machine-readable output;
- keep the handoff narrow; do not create an isolated open-ended chat.

### Local worker

Use for offline, privacy/NDA, or disposable-draft work when no cloud route is available. The local worker must not write to the Vault directly; it drafts updates for the orchestrating agent to apply.
