# Sync transaction contract

In a MULTI installation, `agent-sync` treats propagation as one guarded
transaction. It never regenerates CLI files merely because a pull command was
attempted. The authoritative data state must first be proven safe.

## Remote ownership

The data vault owns the remote policy in:

```text
03-INFRA/agent-universal-layer/sync/remotes.yaml
```

Start from `remotes.yaml.example`. `authoritative_remote` is the only remote
used to decide whether local data is fresh, ahead, dirty, or diverged. Entries
under `mirrors` are publication copies. A stale or unavailable mirror produces
a warning, but it never replaces the authoritative history.

`KNOWLEDGE_VAULT_REMOTE` and `KNOWLEDGE_VAULT_MIRRORS` form a complete emergency
override. If no file or override exists, the portable default is `origin` with
no mirrors. Invalid configuration stops before the provisioner creates runtime
files. Inspect the resolved values with:

```bash
agent-sync config authoritative_remote
agent-sync config mirrors
```

## Commands

| Command | Contract |
|---|---|
| `agent-sync guard` | Recurring pull, apply and healthcheck. Never pushes. A busy lock is a safe skip. |
| `agent-sync apply` | Manual name for the same pull and apply transaction. Never pushes. |
| `agent-sync pull` | Pull and healthcheck only. Never regenerates CLI files. |
| `agent-sync publish` | Publishes existing commits to the authoritative remote, then configured mirrors. It never pulls or applies. |
| `agent-sync preflight` | Validates the local configuration contract without pulling or generating runtime files. |
| `agent-sync doctor` | Runs diagnostics and alerts only. |
| `agent-sync bootstrap-alerts` | Provisions optional alert credentials, then runs diagnostics. |

Running `agent-sync` without a mode prints help and changes nothing. The old
implicit `full` path was removed so a typo or forgotten argument cannot combine
pull, runtime mutation, credential work, and publication.

## Freshness gate

Apply is allowed only when the local branch matches the authoritative branch,
has just fast-forwarded to it, or is explicitly configured as local-only. It is
blocked when the tracked tree is dirty, the remote is missing, fetch fails, the
expected branch is not checked out, the local branch is ahead, the histories
diverge, or Git cannot prove their state.

A deliberate manual recovery is available for a network outage only:

```bash
agent-sync apply --allow-offline
```

This override is rejected for `guard` and never bypasses dirty, ahead, or
diverged states.

## Configuration gate

After a successful pull, and before data migrations or generated runtime
files, `guard` and `apply` run the same preflight as `agent-sync preflight`.
It checks the versioned MCP manifest, the optional Council seats file, the
skills manifest and local Vault skill sources, the portion of Claude settings
that the hook merger may change, and the host remote declaration already read
by the provisioner.

MCP and Council files use `schema_version: 1`. The MCP contract rejects an
unknown CLI target, unsupported transport, invalid environment variable name,
missing HTTP bearer reference, invalid timeout, or malformed Windows override.
Council remains optional, so a missing `seats.yaml` keeps it inert. If the file
exists, it must satisfy its schema before an apply can continue.

This makes an invalid source a stop condition before the engine changes a CLI
configuration. The preflight command itself writes only its normal lock and
run log.

## Lock and result

One host-wide lock covers the complete operation. Manual contention exits with
code `75`; recurring `guard` contention exits successfully because the active
run already owns the work. Every declared phase reports success or failure.
Failures are aggregated, later independent checks still run, and the final exit
code is non-zero if any required phase failed.

`vault-push`'s own commit/rebase/publish logic is the `vault-push` subcommand
of this same `agent_sync.py` — not a separate implementation. It locks the
same lock file by default (`AGENT_SYNC_LOCK_FILE`, else `agent-sync.lock`
under this same host's state directory), so a `guard` cycle and a manual
`vault-push` on the same machine still serialize against each other. Both
`vault-push.sh` (Linux/Mac) and `vault-push.ps1` (Windows) are thin wrappers
that forward into it, matching this contract's own launcher pattern below.
When the engine itself is unreachable (no resolvable `agent_sync.py` or no
Python) and `KNOWLEDGE_VAULT_REMOTE` is set, both wrappers fall back to a
minimal shell-and-git emergency lane — commit, push with one rebase retry,
no mirrors — announcing the degraded mode loudly rather than failing.

The Linux and Windows launchers call the same Python implementation. Automated
tests cover both path dialects and Windows lock code, but an architecture
change is not operationally complete until it has also been exercised on a
physical Windows installation.

## Known limitation

This whole contract is built for one person keeping several machines of
their own in sync, not for a team writing to one shared vault at the same
time. The lock described above (see "Lock and result") is per-machine, not
per-owner: it is a local file lock under that machine's own home directory,
and it only serializes concurrent processes running ON THAT SAME machine
(e.g. a `guard` cycle overlapping a manual `vault-push`). It does nothing to
arbitrate between a single owner's own several machines running
concurrently, let alone between 30-40 different people's machines against
the same vault. Concurrent writers are instead protected by git's own
atomic push: a non-fast-forward push is rejected by the remote outright,
never silently overwritten, and the publish path fetches, compares, and
retries with a clean rebase or aborts and asks for manual resolution on a
real conflict. The one gap that leaves open: two machines editing
different, non-conflicting parts of the same file at nearly the same time
can be merged by that automatic rebase with no alert to either owner --
nothing is lost, but the merge itself is never reviewed. If a team shares
one vault as common infrastructure (see `docs/team.md` for why that's
already a mono-user fit problem before sync even enters the picture),
concurrent writes from multiple people are not a tested or supported
scenario today: expect ordinary Git merge conflicts with no additional
tooling in this contract to resolve them.
