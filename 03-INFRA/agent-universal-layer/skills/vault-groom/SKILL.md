---
name: vault-groom
description: Run one Vault grooming pass (preview first, guarded apply on request) to consolidate stale, oversized, or duplicated notes. Use for periodic memory maintenance or when the user says the vault needs cleaning.
---

# Groom the vault

Any text after the command is an optional focus hint (an area or note to
prioritize).

1. Start read-only, always: run `vault-groom` (bare = preview). It
   proposes one grooming tranche and writes NOTHING -- a bare invocation
   can never modify the vault.
2. Explain the proposed tranche in plain language: which notes it would
   compress, merge, archive, or delete, and why. Flag anything that looks
   risky (recovery notes, recent material) before the user decides.
3. Only if the user explicitly asks to proceed, run `vault-groom apply`.
   Its own guardrails stay in charge: it re-shows the tranche, requires a
   typed "yes", executes in a throwaway clone with no remote, audits the
   result, and only then promotes it into the real vault. Never work
   around any of that, and never edit vault notes by hand to "help" the
   groomer.
4. Report what actually changed (or that nothing did), and whether a
   follow-up pass looks worthwhile.
