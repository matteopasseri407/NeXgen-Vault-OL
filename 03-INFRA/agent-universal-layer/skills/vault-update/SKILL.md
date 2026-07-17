---
name: vault-update
description: Check whether a newer NeXgen engine release exists and, on explicit confirmation, upgrade this machine to it and verify the result. Use when the user asks to update or upgrade the engine, or after a release announcement.
---

# Update the engine

Follow `docs/upgrade.md`; this is its canned form. Never move anything
without the explicit confirmation in step 3.

1. Check, read-only: `git fetch --tags origin` in the engine clone (the
   single documented install is the vault clone itself; a split install
   keeps a separate consumer engine clone -- respect `AGENT_ENGINE_ROOT`
   if set). Compare the newest released tag with the `VERSION` file and
   report both.
2. If an upgrade exists, read `CHANGELOG.md` for every version in
   between and summarize it in plain language. `### Added` is safe by
   design; call out anything under `### Changed` or `### Removed`
   explicitly.
3. Stop and ask before touching anything: state the exact target tag and
   what will run. Also check the working tree is clean first (`git
   status`); uncommitted data must be committed or stashed by the user,
   not by you.
4. On confirmation: `git merge <tag>` (a merge, never a bare checkout --
   a detached HEAD strands the user's own commits), then a provisioning
   pass: `agent-sync apply` and `agent-doctor --strict --summary` where
   installed (MULTI profile); on a MINIMAL install verify visually that
   the CLI still loads AGENTS.md, tools, and skills.
5. Compare the doctor result with the pre-upgrade state: any NEW FAIL
   means the release does not fit this setup -- offer the rollback
   (`git reset --hard <previous-tag>`) and report what broke.
6. On a multi-machine install, remind the user the other machines are
   now behind until they run the same update, and their doctors will say
   so.
