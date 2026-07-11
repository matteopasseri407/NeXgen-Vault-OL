# Upgrading the engine

The engine you run (your consumer clone, e.g. `~/.nexgen-engine`) is pinned
to a specific commit, recorded in `99-INDEX/ENGINE-PIN.txt` in your data
root. `agent-sync`/`agent-doctor` never move that pin on their own — an
upgrade is always something you decide and run by hand.

## Why a pin at all

Your data (manifests, instructions, skills) is written against a known
engine behavior. Consuming whatever is newest on `main` at every sync would
mean a merge to this repo could change your running setup without you ever
deciding it should. The pin is the boundary: you always know exactly which
engine version is live, and you move it only when you choose to.

## Checking whether an upgrade is available

`agent-doctor` fetches this repo's tags (read-only — it never checks
anything out) and compares the latest tagged release against your pin. If
they differ, you'll see a `WARN`:

```
new engine version available: v0.2.0 (pinned: v0.1.0) -- see docs/upgrade.md, update is always deliberate
```

This is informational only. Nothing else changes because of it.

## Upgrading

1. Read `CHANGELOG.md` in this repo for the version(s) between your current
   pin and the one you're moving to. Pay attention to the `### Changed` and
   `### Removed` sections — `### Added` is always safe.
2. In your consumer clone, fetch and check out the target tag:
   ```bash
   git -C ~/.nexgen-engine fetch --tags origin
   git -C ~/.nexgen-engine checkout vX.Y.Z
   ```
3. Move the pin to match:
   ```bash
   git -C ~/.nexgen-engine rev-parse HEAD > ~/KnowledgeVault/99-INDEX/ENGINE-PIN.txt
   ```
   (adjust the vault path if your data root lives elsewhere)
4. Run a provisioning pass and check the result:
   ```bash
   agent-sync apply
   agent-doctor --strict --summary
   ```
   `agent-sync apply` first proves that the data branch is fresh against the
   authoritative remote declared in
   `03-INFRA/agent-universal-layer/sync/remotes.yaml`. It then validates the
   configuration contract before it runs any pending data migration or writes
   a generated CLI file. Unsafe Git states and invalid configuration stop the
   apply. See `docs/sync-contract.md`.
5. If `agent-doctor` reports new `FAIL`s that weren't there before the
   upgrade, something in the new version doesn't fit your setup. Roll back
   by checking out your previous pin's commit and moving `ENGINE-PIN.txt`
   back, then report what broke.

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
  `99-INDEX/DATA-SCHEMA-VERSION.txt` (in your data root, not the engine —
  this file is yours, not published).
- If your data is already at the schema the engine expects, this step does
  nothing at all — no file is touched, no backup is created.

There are no migrations registered yet as of `v0.2.0`: today's data shape
is the baseline this mechanism starts counting from. A migration runs only
after the preflight has accepted the data source, and before runtime files are
generated.

## What never happens automatically

- The pin never moves by itself.
- `agent-sync`/`agent-doctor` never `git pull` or `git checkout` your
  consumer engine clone.
- Your consumer clone never publishes engine code. Maintainer-only controls
  such as `engine-push` and public-repo anti-leak hooks are for contributors
  publishing this repository, not for normal private vault usage.
- A data migration never runs against a schema version newer than what the
  installed engine understands — if that happens (e.g. you rolled the
  engine back), `agent-sync` leaves your data untouched and logs why.

## MCP package pins

The engine runs local MCP packages through exact `npx` versions. The pins live
in `03-INFRA/agent-universal-layer/mcp/manifest.yaml`, with the Antigravity
HTTP bridge pinned in `mcp/render.py`.

Do not replace a pin with `latest`. Test one package update in a disposable
setup, run the engine checks, then publish the engine change. If the new
package causes a regression, return the consumer clone and `ENGINE-PIN.txt`
to the previous engine commit.
