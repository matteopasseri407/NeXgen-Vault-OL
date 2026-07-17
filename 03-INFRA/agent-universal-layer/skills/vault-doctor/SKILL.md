---
name: vault-doctor
description: Run the NeXgen alignment doctor and explain the result in plain language. Use when the user asks whether the agent layer is healthy or aligned, after a sync or engine cutover, or when something feels misconfigured.
---

# Vault doctor

Any text after the command is an optional specific concern to focus on.

1. Run the read-only doctor for this platform: `agent-doctor` (if the bare
   command is missing, fall back to `bash 03-INFRA/scripts/agent-doctor.sh`
   from the engine root on Linux/macOS, or
   `pwsh -File 03-INFRA\scripts\agent-doctor.ps1` on Windows). It never
   writes anything, so running it is always safe.
2. Open with one plain-language sentence: aligned or not, plus the
   PASS/WARN/FAIL counts.
3. For every FAIL: quote the failing line, explain what it means in simple
   terms, and propose the smallest safe fix as a ready-to-paste command.
   Do not apply fixes without explicit confirmation.
4. Summarize WARNs briefly; expand only the ones that need a user decision.
5. If the user gave a specific concern, address it explicitly against the
   doctor output.
