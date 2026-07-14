---
tags:
  - index
  - secrets
status: active
type: registry
---

# Secrets registry (non-sensitive)

Index of the secrets held in the encrypted archive (`archive/master-secrets.md.gpg`).
**Names and env vars only — never values** (policy: `AGENTS.md` → Secrets). This
file is git-tracked; the encrypted archive is not.

| Name | Provider | Env var | Scope | Last rotated | Notes |
|---|---|---|---|---|---|
| _(example — delete this row)_ | OpenAI | `OPENAI_API_KEY` | all machines | 2026-01-01 | LLM extract endpoints |
