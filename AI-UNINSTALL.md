# AI-UNINSTALL.md — Autonomous Removal Mode

You are an AI agent with shell and file access, asked to remove NeXgen
Vault for this user. This is the autonomous companion to
`docs/uninstall.md`: same removal, you run the commands instead of the
user typing each one by hand.

**Do the work yourself**, but this deletes real things — confirm with the
user before any step marked destructive below. Never combine multiple
destructive steps into one unconfirmed batch.

Follow `docs/uninstall.md` in order; it is the single source of truth for
exactly what each step removes:

1. **Stop the recurring sync** (MULTI profile, systemd only) — safe, no
   confirmation needed.
2. **Restore or clean per-CLI config** from the `.bak-*` backups next to
   each managed file — safe, no confirmation needed.
3. **Remove the bootstrap pointer files** (`~/CLAUDE.md`,
   `~/.codex/AGENTS.md`, `~/.gemini/config/AGENTS.md`) — ask first if the
   user might be using any of them for something unrelated to this
   project.
4. **Remove the skill folders this project added** — read
   `~/.agents/skills/INDEX.md`, then inspect `~/.agents/skill-library/` to
   see the real bodies (never assume specific names, see `docs/uninstall.md`)
   — **ask for confirmation** before deleting, this removes real folders.
5. **Remove the vault clone** — **ask for explicit confirmation**, this
   deletes the user's own notes, projects, and secrets registry along with
   the engine. Suggest a backup first if there's any doubt.
6. **Cloud-Server mode only**: point out that the VPS stack
   (`03-INFRA/deploy/`) needs its own `docker compose down -v` run on the
   VPS itself — do not attempt this remotely unless the user explicitly
   asks you to connect and run it.
7. **Report what's left behind** — the log file (safe to delete, no
   secrets) and any `99-SECRETS/` copy that lived outside the vault clone
   you just removed — exactly as `docs/uninstall.md` describes.

Finish with a short summary of what was removed, what was left in place,
and why.
