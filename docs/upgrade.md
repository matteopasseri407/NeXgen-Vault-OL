# Upgrading the engine

The install documented in `README.md`/`INIT.md` is a single `git clone` into
one folder (e.g. `~/KnowledgeVault`). That one clone plays both roles at
once: it is the engine code you run (`03-INFRA/`, the scripts, the docs) and
the data root where your own notes, `99-INDEX/USER-PROFILE.md`, and
manifests live. There is no separate "engine-only" clone by default —
everything below matches that single-clone topology, because that is what
the documented install actually produces.

The codebase does contain the plumbing for a second, more advanced
topology: `AGENT_ENGINE_ROOT`/`AGENT_VAULT_DATA` let you split the engine
into its own clone (referred to internally as the "consumer engine clone")
separate from your data root, and `agent-doctor` has a version-pin check
(section S2) plus a new-version-available warning that key off that split
clone's `.git`. That section only activates once you have deliberately
created a second clone and pointed those variables at it; it is a future
"cutover" path the engine is being built toward, not something `INIT.md`
sets up for you today. If you followed the documented install and never set
those variables, that section of `agent-doctor` stays silent, and none of
the `~/.nexgen-engine`-style pin mechanics described below apply to you.

## Why track your version at all

Your data (manifests, instructions, skills) is written against a known
engine behavior. Silently jumping to whatever is newest on `main` every time
you pull could change your running setup without you ever deciding it
should. Your `VERSION` file (`cat VERSION` in your clone) is the version
you're actually running; move it only when you choose to.

## Checking whether an upgrade is available

`agent-doctor` checks this for you on **both** topologies: on the default
single-clone install it fetches `origin`'s tags (read-only, bounded) and
warns — informationally, never a FAIL — when a released tag newer than your
`VERSION` file exists ("new engine version available: vX.Y.Z"). Nothing is
ever updated automatically; the warning just tells you the choice exists.
A vault whose `origin` is your own private data remote (no engine tags) or
that has no `VERSION` file skips the check silently.

To check by hand instead:

```bash
cd ~/KnowledgeVault   # or wherever you cloned it
git fetch --tags origin
git tag --merged origin/main --sort=-v:refname | head -1   # latest released tag
cat VERSION                                                  # the version you're actually running
```

If the latest tag is newer than your `VERSION` file, an upgrade is
available.

## Upgrading

1. Read `CHANGELOG.md` for the version(s) between your current `VERSION`
   and the one you're moving to. Pay attention to the `### Changed` and
   `### Removed` sections — `### Added` is always safe.
2. Make sure your working tree is clean (`git status`). Your own vault
   content — `01-NOTES/`, `02-PROJECTS/`, `99-INDEX/USER-PROFILE.md`,
   `03-INFRA/agent-universal-layer/skills/skills.manifest.yaml`, and so on —
   lives as ordinary commits in this same clone, so commit or stash
   anything in progress before moving the ref.
3. Fetch and bring in the target tag:
   ```bash
   git fetch --tags origin
   git merge vX.Y.Z
   ```
   Prefer `merge` over `git checkout vX.Y.Z`: a bare checkout detaches
   `HEAD` and leaves any local commits you made on `main` (your notes,
   your profile) stranded off the branch. A merge keeps them on `main` and
   only asks you to resolve a conflict if the new engine version and one of
   your own edits touched the exact same file — which shouldn't happen if
   you've kept customization inside your own data (notes, manifests,
   `USER-PROFILE.md`) rather than hand-editing engine-owned scripts.
4. Run a provisioning pass and check the result:
   - **MULTI profile:**
     ```bash
     agent-sync apply
     agent-doctor --strict --summary
     ```
     `agent-sync apply` first proves that the data branch is fresh against
     the authoritative remote declared in
     `03-INFRA/agent-universal-layer/sync/remotes.yaml`. It then validates
     the configuration contract before it runs any pending data migration or
     writes a generated CLI file. Unsafe Git states and invalid
     configuration stop the apply. See `docs/sync-contract.md`.
   - **MINIMAL profile:** there is no `agent-sync`/`agent-doctor` to run —
     per `README.md`, MINIMAL never installs them. Diagnostics are visual:
     open the CLI you configured and confirm it still loads `AGENTS.md`,
     still mounts the MCP servers you expect, and still sees your skills.
5. If `agent-doctor` reports new `FAIL`s that weren't there before the
   upgrade (MULTI), or your CLI stops behaving the way it did before
   (MINIMAL), something in the new version doesn't fit your setup. Roll
   back with `git reset --hard <previous-commit-or-tag>`, then report what
   broke.

## Data migrations

Some engine releases may need to reshape a data file (a manifest field
renamed, a new required key). When that happens, `agent-sync apply` runs
the needed migration automatically, in order, the first time it sees your
data at an older schema version:

- Before writing anything, it backs up the affected file next to itself as
  `<file>.bak-<timestamp>` (same convention as the config backups you'll
  already have seen from `render.py`; the last 3 are kept).
- Each migration is idempotent: running it again on already-migrated data
  is a no-op.
- Your data schema version is tracked in
  `99-INDEX/DATA-SCHEMA-VERSION.txt` (in your data root — this file is
  yours, not published with the engine).
- If your data is already at the schema the engine expects, this step does
  nothing at all — no file is touched, no backup is created.

This runs only as part of `agent-sync apply`, so it is a MULTI-profile
mechanism. There are no migrations registered yet as of the current engine
`VERSION` — today's data shape is still the baseline this mechanism starts
counting from, for both profiles. A migration runs only after the preflight
has accepted the data source, and before runtime files are generated.

## What never happens automatically

- Your `VERSION` never moves by itself.
- `agent-sync`/`agent-doctor` never `git pull` or `git checkout` your
  clone.
- Your clone never publishes engine code back upstream. GitHub repository
  controls and CI apply to maintainers publishing changes, not to normal
  private vault usage.
- A data migration never runs against a schema version newer than what the
  installed engine understands — if that happens (e.g. you rolled the
  engine back), `agent-sync` leaves your data untouched and logs why.

## MCP package pins

The engine runs local MCP packages through exact `npx` versions. The pins live
in `03-INFRA/agent-universal-layer/mcp/manifest.yaml`, with the Antigravity
HTTP bridge pinned in `mcp/render.py`.

Do not replace a pin with `latest`. Test one package update in a disposable
setup, run the engine checks, then publish the engine change. If the new
package causes a regression, roll your clone back to the previous tag as
described above.
