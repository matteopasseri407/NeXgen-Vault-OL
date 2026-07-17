---
name: vault-save
description: Save one durable fact to the KnowledgeVault with the hygiene decision rule applied. Use when the user asks to remember, save, or note something for the future.
---

# Save a durable fact

The text after the command is the fact to save.

1. Apply the decision rule first: will this still matter in weeks or
   months? Will a future session reuse it to decide, restore, or avoid
   repeating work? Is it non-sensitive and safe to sync? If any answer is
   no, tell the user why it does not belong in the Vault and stop.
2. Find the canonical home: search the vault (`search_notes` or
   `semantic_search`) for a note that already covers the topic. Prefer
   updating that note over creating a new one.
3. Write through the `vault-library` MCP (`append_note`, or `update_note`
   with `expected_hash`). Only `create_note` for a genuinely new stable
   topic, and then add an inbound pointer in `00-START-HERE.md`.
4. Confirm the write landed and report exactly where the fact now lives
   (note path plus the line or section).

Never store secrets, tokens, or credential-adjacent material; keep the
entry short (a fact, not a diary).
