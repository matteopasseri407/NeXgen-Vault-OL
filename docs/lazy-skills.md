# Lazy skills

Skills are optional task playbooks, not part of every agent's identity.
NeXgen keeps the always-on layer small: `AGENTS.md` carries policy and tool
awareness, the MCP manifest carries tools, and a skill body is opened only
when the task actually matches it.

## Layout

| Location | Purpose |
|---|---|
| `03-INFRA/agent-universal-layer/skills/skills.manifest.yaml` | Canonical, cross-machine choice of skills. |
| `~/.agents/skill-library/` | Complete managed bodies. Never an eager discovery root. |
| `~/.agents/skills/INDEX.md` | Tiny generated catalog. No optional `SKILL.md` bodies live beside it. |
| `~/.claude/skills/` | Declared native-lazy Claude views, when Claude is a target. |
| `~/.codex/skills/` | Legacy root. NeXgen does not mirror skills here, because Codex already discovers the shared root. |

Every manifest entry has an `origin`, optional native `targets`, and an
`exposure`:

- `manual` is the default. The skill stays out of eager discovery roots.
- `core` is the rare exception for a genuinely universal policy body. It is
  visible through the active shared view, so use it sparingly.

Two more optional fields exist for a small team sharing this framework:

- `scope: personal | team` — default `team`, i.e. every existing manifest
  entry behaves exactly as before this field existed.
- `owner: <member-id>` — the identifier of whoever this skill is personal
  to, matching an entry in `99-INDEX/USER-PROFILE.md` → "Team members
  (optional)".

`scope: personal` only has an effect when USER-PROFILE.md declares a Team
members section at all (the mono-user default has none, so `scope` is
inert there). When it does, `skills-sync.py` materializes a
`scope: personal` skill only on the machine whose `AGENT_TEAM_MEMBER`
environment variable matches the skill's `owner`; everywhere else it is
skipped, with a clear line saying so. `scope: team` (or no `scope` at
all) still propagates to every machine, same as today.

Claude can keep all declared manual views because it loads them lazily. Codex
also progressive-discloses discovered skill bodies, but NeXgen intentionally
keeps manual entries outside its discovery roots to minimize the initial
metadata catalog and preserve one routing contract across every CLI. Codex,
OpenCode, Antigravity, and local workers therefore use the same explicit
command for manual skills:

```bash
agent-skill list
agent-skill find debugging
agent-skill show systematic-debugging
```

## Synchronization and migration

Change the manifest and its source body in the Vault, then commit and push
that canonical change. On each machine, `agent-sync guard` runs
`skills-sync.py --apply` and reconciles only managed entries.

An existing installation may still have old folders in discovery roots. First
run the normal provisioner so it repairs any old whole-root link, then move
the old folders out with the one-time explicit migration:

```bash
agent-sync guard
python3 03-INFRA/scripts/skills-sync.py --apply --migrate-legacy
```

It preserves unknown folders under `~/.agents/skill-library/legacy/`, outside
the catalog and runtime roots. Review those folders deliberately: promote a
worthwhile one into the pinned manifest, or delete it later with explicit
user approval. The recurring guard never performs that migration by itself.

Run `agent-doctor --strict --summary` after the rollout. A cross-machine
change is complete only after the same result is verified on every declared
machine and CLI.
