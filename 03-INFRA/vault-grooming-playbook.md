---
tags:
  - infra
  - vault
  - hygiene
  - agent-layer
status: active
type: runbook
last_reviewed: 2026-07-04
---

# Vault grooming playbook — the gardener's run

Canonical, **runner-agnostic** procedure for the scheduled *gardener*: the LLM pass that keeps the KnowledgeVault pruned. This file is the brain; the runner (`claude -p` headless today, `opencode run` + <cheap-model> tomorrow) is just the hand that executes it. Swapping runner = changing one variable in the wrapper, never rewriting this.

**Role.** The gardener is NOT a guardian. Guardians watch the *infrastructure* (containers, tokens, sync — see [[agentic-layer-concept-map]] / `agent-guardians-map`). The gardener tends the *content*: it reads the knowledge with a whole-vault view and prunes it. Different plane, one clean new role — not another sentinel.

**Governing rule** (source: `AGENTS.md` → "Keep the garden"). Grooming is **semantic judgement, not a threshold sweep**. Line-count and age are never the verdict. Every action is conservative and reversible: compress / merge / **archive** over deletion; in doubt, archive, don't delete; every change is a git commit the user can revert.

## The run, step by step

1. **Orient cheaply — get the whole-vault view without reading everything.**
   - Read `99-INDEX/note-index.md` for the shape, and `99-INDEX/vault-cleanup-backlog.md` for decisions already made — **do not re-litigate closed calls**, do not re-open what a past tranche settled.
   - Run the heat-map: `python3 03-INFRA/scripts/vault-lifecycle-audit.py --today <DATE>`. Treat its output as a **list of suspects to open**, never as a verdict or a deletion list.

2. **Find the real candidates.**
   - Use `semantic_search` (vault-library MCP) to surface overlaps and near-duplicates — the thing thresholds can't see.
   - Cross the semantic hits with the heat-map, then pick **ONE tranche** for this run (a coherent cluster: one topic area, or one audit category). Do NOT sweep the whole vault in a single run — bounded scope keeps cost and blast-radius small.

3. **Judge each candidate — semantically.** Ask, per note: superseded by a newer note? contradicted? redundant/overlapping with another (→ merge)? a brick bloated with debug diary (→ compress to outcome)? completed plan / dead TODOs (→ collapse)? orphaned or broken `[[links]]`? fragmentation that belongs in a hub? A 400-line runbook can be perfect; a six-month-old decision can be immutable truth; a note from yesterday can already be dead. Judge value, not size.

4. **Act — conservative, from safest to riskiest.**
   - **Compress** in place per `knowledge-vault-hygiene` → Update Style (keep the operative facts: commands, endpoints, paths, decisions).
   - **Merge** duplicates into the single canonical home (one topic, one home); leave a `[[link]]` from any surviving pointer.
   - **Archive** heavy history to `03-INFRA/archive/<NAME>-archive-<DATE>.md` with `status: archive` + `type: archive`, and add a `[[link]]` to it from the slimmed live note. (This is the pattern the 2026-07-04 pass used — proven safe.)
   - **Fix** frontmatter/status to match reality (active / draft / archive).
   - **Delete ONLY** cache, test files, temporary exports, or a duplicate already fully absorbed elsewhere. Never delete a real note — archive it.

5. **Keep coherence.** Update `99-INDEX/note-index.md`, repair `[[links]]` you touched, and record the tranche in `99-INDEX/vault-cleanup-backlog.md` (what you decided and why — the backlog is the memory that stops the next run redoing this).

6. **Commit + report.** Atomic commits per tranche (clear message), then push. Append a one-line human report to the backlog: `giro <DATE>: compresso X, fuso Y→Z, archiviato W`. That line is the user's after-the-fact supervision surface.

## Budget
One tranche per run. If the heat-map is huge, do the highest-signal cluster and leave the rest for the next scheduled run — steady grooming, not a big-bang pass. Reading is cheap (index + semantic + targeted opens); avoid reading whole notes you're not acting on.

## Hard guardrails (never cross)
- **Never** touch `99-SECRETS/**`, never read decrypted secrets, never commit a token/key/password — even while compressing a note that mentions one (strip it, don't relocate it).
- **Never** hand-edit `AGENTS.md`, generated/derived configs, or anything under `agent-universal-layer/` templates — those have their own source-of-truth flow; the gardener grooms *knowledge notes*, not the agent layer's canonical config.
- **Never** delete a non-cache note; archive instead. Deletion is only for cache/test/export/absorbed-duplicate.
- Respect the **concurrent-write protocol**: if a write fails on `expected_hash`, re-read and re-apply only your delta — never clobber another agent's note.
- In doubt about whether something is superseded, **leave it and note it in the backlog** rather than acting. A missed prune costs nothing; a wrong delete costs trust.

## Execution model — on-demand on both machines; only the reminder is automatic
- **Source of truth:** this file. The wrapper (`03-INFRA/scripts/vault-groom.sh` on Linux, `vault-groom.ps1` on Windows) feeds it to the runner. **Same tool on both machines** — no asymmetry, same on-demand pattern as `firecrawl-local`.
- **On demand, NOT autonomous.** The gardener does not self-start. the user (or an agent on his behalf) runs `vault-groom` when he wants, or says "pota il vault". A self-scheduled autonomous pass was **rejected on purpose**: two machines grooming the shared vault would collide on git, and an unattended writer is exactly the risk to avoid. On-demand keeps both machines aligned AND keeps a human on the trigger.
- **Runner priority.** (1) **<frontier-model>** (`claude-sonnet-5`) via Claude Code headless — primary, fixed-cost on Claude Pro, enough judgement under these guardrails. (2) **<mid-tier-model>** (NOT Flash — too weak for this judgement) via `opencode run` — fallback ONLY if Sonnet is down; consumption-billed (~4x the raw API on the Go quota), so never the default. One variable in the wrapper.
- **The reminder is the only automatic piece — and it lives on n8n, not on the laptops.** An n8n workflow on the always-on remote backend (`Vault Grooming Reminder (14gg)`, id `<workflow-id>`) fires every 14 days and sends the user one messaging nudge: *"quando puoi lancia vault-groom"*. It's a **blind reminder** — it does NOT run the audit or gate on debt, on purpose: gating would need Python + the vault inside the n8n container (it has neither), overkill for a nudge — if the vault is already clean, `vault-groom plan` is a free read-only peek. Running on the remote backend means it doesn't depend on which PC is powered on. The automation is only in the reminder; the pass itself is always the user running `vault-groom` by hand on Linux or Windows. Import-ready template for this workflow (not auto-imported by anything): `03-INFRA/deploy/n8n/workflows/vault-grooming-reminder.json`, see the README next to it for the two-minute setup.
- Related: [[vault-cleanup-backlog]] (state/decisions), `knowledge-vault-hygiene` (per-note compression), [[agentic-layer-concept-map]] (where this sits).
