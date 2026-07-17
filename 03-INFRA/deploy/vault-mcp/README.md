# vault-mcp — the `vault-library` MCP server (Git-backed vault writes)

This is the deployable source of the `vault-library` MCP server: the ONLY
door through which agents write NOTES (Markdown knowledge) to the
KnowledgeVault in Cloud-Server mode. Infra files go through `vault-push`;
notes go through this server. One door per kind of thing — see
`03-INFRA/vault-write-architecture.md` for the full write model.

A small Python package (`src/vault_mcp_server/`, MCP `streamable-http`
transport) that mounts the vault worktree plus its bare repo and exposes:

- read tools: `get_start_here`, `read_note`, `search_notes`, `list_related`,
  `recent_activity`
- write tools (only when `VAULT_WRITE_ENABLED=true`): `create_note`,
  `append_note`, `update_note` (guarded by `expected_hash`), and
  `update_section` (surgical single-section edits guarded by a
  per-section hash from `read_note`'s `sections` — concurrent edits to
  other sections of the same note stay valid)

Every write is serialized with a process lock, restricted to Markdown paths
(`99-SECRETS` and `.git` are always refused), and committed to the bare repo
as author "Vault MCP" — so vault history stays clean and attributable.

## Deploy (part of the standard VPS stack)

`bootstrap-vps.sh` in the parent directory does all of this for you (it is
the fourth stack, after n8n / Firecrawl / OCR):

1. generates `VAULT_LIBRARY_TOKEN` into `.env` on first run,
2. runs `provision-vault-repo.sh` (idempotent: bare repo + worktree +
   `post-receive` hook, correct ownership),
3. `docker compose -f vault-mcp/docker-compose.yml --env-file .env up -d --build`.

Set `VAULT_MCP_ENABLED=0` in the environment to skip the stack entirely
(e.g. a VPS that only hosts n8n for a Local-Only install — Local-Only
installs have no remote vault and do not run this server at all).

Like every other stack here, the server binds to `127.0.0.1` only and is
reached exclusively through an SSH tunnel:

```bash
ssh -L 127.0.0.1:<vault-mcp-tunnel-port>:127.0.0.1:8081 <remote-alias> -N
```

## Wiring the agent CLIs

The MCP manifest (`03-INFRA/agent-universal-layer/mcp/manifest.yaml`) already
declares `vault-library`, gated on these two workstation env vars — set them
and re-run `agent-sync`; nothing else to configure:

```bash
VAULT_LIBRARY_URL=http://127.0.0.1:<vault-mcp-tunnel-port>/mcp
VAULT_LIBRARY_TOKEN=<the value bootstrap-vps.sh wrote into the VPS .env>
```

`agent-doctor` probes the endpoint whenever `VAULT_LIBRARY_URL` is set.

## Configuration (container env, set by docker-compose.yml)

| Variable | Default | Meaning |
|---|---|---|
| `VAULT_ROOT` | `/vault` | Worktree mount (host: `VAULT_WORKTREE_DIR`, default `/opt/knowledge-vault`) |
| `VAULT_GIT_DIR` | `/vault.git` | Bare repo mount (host: `VAULT_BARE_DIR`, default `/opt/knowledge-vault.git`) |
| `VAULT_TOKEN` | — | Bearer auth; wired to `VAULT_LIBRARY_TOKEN` from `.env`. Required: a write-enabled server never runs open. |
| `VAULT_WRITE_ENABLED` | `true` | The whole point of this deploy; set `false` for a read-only instance |
| `MAX_WRITE_BYTES` | `262144` | Per-write size cap |
| `WRITE_EXCLUDE_PATH_PREFIXES` | `99-SECRETS,.git` | Never writable, regardless of token |
| `SEMANTIC_ENABLED` | `false` | Optional semantic-search sidecar (not bundled; leave off) |

The container runs read-only (`read_only: true`, `tmpfs /tmp`) as the host
deploy user's uid/gid (`VAULT_MCP_UID`/`VAULT_MCP_GID`, pinned into `.env` by
bootstrap) so Git sees one consistent owner on the mounted repo.

## Health

```bash
curl -s http://127.0.0.1:8081/healthz
```
