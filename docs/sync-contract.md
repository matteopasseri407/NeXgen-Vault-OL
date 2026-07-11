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

The Linux and Windows launchers call the same Python implementation. Automated
tests cover both path dialects and Windows lock code, but an architecture
change is not operationally complete until it has also been exercised on a
physical Windows installation.
