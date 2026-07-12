# Security

## Reporting a vulnerability

Open a private security advisory on GitHub (Security tab of this repo) or email the address on the maintainer's GitHub profile. (A security advisory is just a private draft report that only the maintainer can see until it's fixed, not a public post — GitHub's Security tab walks you through creating one, no special account setup beyond having a GitHub account.) Do not open a public issue for anything that could expose a real credential or an active exploit path. A regular bug that doesn't touch secrets or code execution is fine as a normal issue.

## What must never be committed

- Anything under `99-SECRETS/` except `README.md`, `.gitkeep`, and `secrets-registry.md`. The `.gitignore` already blocks the rest, but double-check before force-adding files in that folder.
- Real API keys, tokens, SSH keys, webhook secrets, or tunnel credentials, in any file, including MCP manifests, `.env` files, or example configs. `03-INFRA/deploy/*/.env.example` files must only ever contain placeholders.
- Any personal data belonging to you or a third party: real names tied to private context, private chat IDs, internal project numbers, customer data. If you fork this repo and adapt it for yourself, keep that kind of detail out of the parts you intend to push publicly.

If you think you've already committed one of these, treat it as a leak: rotate the credential first, then clean the git history (not just the latest commit) before doing anything else.

## Trust boundaries

- **The vault itself is a set of plain files.** Any agent CLI with filesystem access to it can read and write everything inside. There is no per-file access control beyond your OS permissions. If more than one person would share a Cloud-Server backend, see `docs/org-deployment.md` for what that means in practice.
- **`agent-sync`/`agent-doctor` run with your user's permissions.** They read and patch CLI config files (see `docs/what-gets-written.md`) and, in MULTI profile, install a systemd user timer (Linux) or a Task Scheduler entry (Windows). They do not use sudo/admin elevation and do not touch files outside your home directory except through the paths documented there.
- **MCP servers run as local processes or connect to your own VPS.** None of the tools in the default manifest send vault content to a third-party model or SaaS API as part of normal operation; the semantic search, OCR, and scraping services are self-hosted by you, on infrastructure you deploy and own (see `03-INFRA/deploy/`), not a service this project or its author runs for you. If you add a hosted MCP server yourself, that server's own privacy and security posture applies.
- **The browser MCP attaches to a real, visible Chrome window over the DevTools protocol.** Agents are expected to never launch a headless browser behind your back; if you see one, that's a bug, not a feature.
- **Cloud-Server mode reaches your VPS over an SSH tunnel you configure.** The tunnel ports and credentials live in your own `99-INDEX/USER-PROFILE.md` and `99-SECRETS/`, not in this repo.

## Two different leak-scans, two different audiences

The engine ships one shared secret-detection module
(`03-INFRA/agent-universal-layer/leak-scan/leak_scan.py`), but it backs two
unrelated gates — don't conflate them:

- **Council's egress/output scan is an end-user protection, always on.**
  Every `council.py` call scans the outgoing brief (and the text a seat sends
  back) for likely secrets before it can reach, or come back from, a
  third-party model seat. This runs for every user, every session, with no
  opt-out beyond not using Council. See `docs/council.md`'s Guardrails
  section.
- **`engine-push` and the CI leak-scan are a maintainer-only publishing
  gate for this GitHub repository.** They exist to stop a contributor from
  pushing a real secret or personal path into the public engine history.
  Normal users never run `engine-push`; it has nothing to do with your own
  vault data or your own Council sessions.

## Supported versions

This project does not yet follow a formal LTS/patch schedule. Security fixes land on `main`; there are no older release branches receiving backports at this time.

## Release signing

Every commit on `main` from `8fcd351` (2026-07-08) onward, and every tag from
`v0.3.1` onward, carries a verifiable GPG signature (`git verify-commit` /
`git verify-tag`). Tags `v0.1.0`–`v0.3.0` predate that discipline and are
unsigned; treat them as historical, not as a verification baseline. Every
future release tag must be signed — an unsigned tag past `v0.3.0` is a
process bug, not a style choice.
