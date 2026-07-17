---
name: vault-close
description: Close a working session the NeXgen way, distilling durable outcomes into the KnowledgeVault and verifying they landed. Use at the end of a work session or when the user asks to wrap up.
---

# Close the session

Any text after the command is an optional hint about what mattered most in
the session.

1. Distill the session into durable outcomes only, following the vault
   hygiene rules in AGENTS.md: current state, decisions taken, verified
   commands, rollback notes, open risks. No transcripts, no raw logs, no
   play-by-play history.
2. Write through the `vault-library` MCP: prefer updating the existing
   canonical note on the topic (`update_section` with its per-section hash
   when the change fits one heading, `update_note` with `expected_hash` for
   whole-note rewrites, or `append_note` for additive updates). Create a
   new note only for a
   genuinely new stable topic, and then add an inbound pointer to it in
   `00-START-HERE.md` so it stays discoverable.
3. If the session touched infra files (scripts, manifests, instructions),
   publish them with `vault-push`; notes written through `vault-library` are
   already committed.
4. Verify the writes landed (read the note back or check
   `recent_activity`), then report what was saved as one short list.

If the Vault is unreachable, say so and stop: durable facts must not be
persisted anywhere else in the meantime.
