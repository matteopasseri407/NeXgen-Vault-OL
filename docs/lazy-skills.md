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
| `~/.gemini/antigravity-cli/skills/` | Native Antigravity views, when `antigravity` is a target. Skills here surface as `/name` slash commands in the agy TUI. |

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

## Cross-CLI command skills

Every runtime NeXgen renders for now consumes the same agentskills.io shape
(`<name>/SKILL.md`, frontmatter `name` + `description`), and each one lets
the user invoke a discovered skill explicitly. That turns a skill into a
cross-CLI slash command: declare it once, and one canned procedure becomes
invocable everywhere. The recipe is plain manifest fields, no new schema:

```yaml
my-command:
  origin: vault
  targets: [claude, antigravity, opencode]
  exposure: core
```

- `exposure: core` puts it in `~/.agents/skills`, which **Codex** (typed as
  a `$my-command` mention) and **OpenCode** (`/my-command`) read natively.
- The `claude` target links the native view for **Claude Code**
  (`/my-command`).
- The `antigravity` target links the native view into
  `~/.gemini/antigravity-cli/skills/` (`/my-command` in the agy TUI).
- The `opencode` target writes nothing — OpenCode reads the shared roots —
  but makes the sync verify the skill is actually discoverable there.

Conventions that keep a command portable: lowercase-hyphen names that match
the folder name; never reuse a CLI built-in name (Claude Code ships a
bundled `/doctor` that a same-named skill would override); write the body
argument-free — "the text after the command is the request" — because
placeholder syntaxes like `$ARGUMENTS` diverge per CLI while plain
instructions behave identically on all four.

The engine ships seven starter command skills, registered in
`skills.manifest.yaml.example`: `vault-doctor` (run the alignment doctor and
explain it in plain language), `vault-close` (distill the session into the
Vault, publish, verify), `vault-save` (save one durable fact with the
hygiene decision rule), `vault-council` (convene the AI Council on a
question, confirming before it spends the seats' quota), `vault-groom` (one
grooming pass, preview-first; apply keeps all of its own guardrails), and
`vault-update` (check for a newer engine release, upgrade only on explicit
confirmation, verify with the doctor), and `vault-map` (the deterministic
structural map of the vault — broken wikilinks, orphan notes, hubs —
explained in plain language, fixes proposed but never applied without
confirmation). Each body encodes the documented runbook of the tool it
wraps — the guarded flows stay guarded.

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
