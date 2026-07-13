# Changelog

All notable changes to the NeXgen engine are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[Semantic Versioning](https://semver.org/).

This file tracks the **engine** (this repo). Your own data — manifests,
instructions, skills, secrets — lives in your KnowledgeVault and is not part
of any engine release.

## [Unreleased]

The 2026-07-13 pre-beta hardening round: everything between the 0.4.0 tag
and handing the engine to its first two (Windows) beta testers. Fed by an
independent second-model review of the whole range, an external
architecture challenge (Council relay to a real codex seat), and three
implementation waves with adversarial re-review.

### Added

- `vault-push` is cross-platform: its staging/commit/push logic moved into
  an `agent_sync.py vault-push` subcommand (same host-wide lock and
  subprocess timeouts as the rest of the control plane), with
  `vault-push.sh` and a new `vault-push.ps1` as thin launchers. When the
  engine itself is unreachable and `KNOWLEDGE_VAULT_REMOTE` is set, both
  launchers fall back to a minimal shell+git emergency lane, announcing
  the degraded mode loudly instead of failing.
- `install.ps1`: native Windows preflight twin of `install.sh` (`-Check`
  mirrors `--check`), so the documented first command works in a default
  PowerShell prompt.
- On Windows, `agent-sync apply` now registers the commands directory on
  the user PATH (idempotent, registry-type-preserving), and
  `agent-doctor.ps1` verifies it by actually resolving `agent-sync` in a
  fresh process. Before this, the linked bare commands were documented
  but unreachable on a fresh Windows install.
- `render.py --expected-servers <cli>`: the manifest-derived,
  env-filtered list of MCP servers a CLI should have. `agent-doctor
  --strict` (both twins) derives its expected set from it instead of a
  hardcoded 4-name list that permanently failed legitimate Local-Only
  installs.
- The vault gardener's write pass now runs inside a disposable clone of
  the vault with no remote configured — it physically cannot push,
  whatever the runner. A mechanical audit checks the produced commits in
  both directions (planned-but-missing AND touched-but-unplanned,
  path-exact, archive-rename aware) on a strictly linear history, and
  only a fully clean, still-fresh run is promoted — a fast-forward of the
  exact audited commit — into the real vault; anything else stays
  quarantined in the clone with the vault untouched. Runners: `claude`,
  `codex`, `agy` via `GROOM_RUNNER` (`opencode` refuses loudly, it has no
  per-invocation permission scoping today).
- First behavioral CI coverage for the PowerShell twins: `vault-groom.ps1`
  now runs for real under pwsh on both ubuntu and windows runners, and
  the vault-push contract tests (divergence/rebase recovery, mirror
  realignment, genuinely concurrent pushes) run on windows-latest too.
- A class-level invariant test: every project command documented as a
  bare command must be linked by the provisioner on every OS it claims —
  the bug class this round kept finding one instance at a time, closed as
  a class.
- Council: codex seats pass `--skip-git-repo-check` (the first real
  multi-vendor run died on codex's trusted-directory startup check);
  `council relay --continue-on-reject` to run the full stage sequence
  despite an intermediate rejection.
- An import-ready n8n workflow
  (`03-INFRA/deploy/n8n/workflows/vault-grooming-reminder.json`) for the
  gardener's 14-day reminder. Reminder-only by design — the grooming pass
  itself stays on-demand, never self-scheduled.

### Changed

- `vault-groom` CLI contract: a bare run (or `preview`) is always
  read-only; the guarded propose → typed-yes → write lane is the explicit
  `vault-groom apply`. The interim `plan`/`run` arguments exit with a
  migration hint. The approved tranche's fingerprint is the sha256 of the
  plan-record file's raw bytes — identical on both OSes — and is
  re-checked immediately before the write pass runs.
- The gardener's push decision moved from an LLM instruction to
  deterministic code: promotion, the backlog record, and the publish all
  happen after the mechanical audit, never from inside the write pass.
- `council relay` stops on an intermediate `VERDICT: REJECT` by default,
  and verdict parsing is positional: only the response's last non-blank
  line counts (markdown emphasis and terminal punctuation tolerated,
  quoted verdicts ignored), so a cited rejection can no longer stop a
  relay and an inline-explained verdict still counts as one.
- `council`'s reasoning-effort forwarding and its display share one
  source per CLI: `--think` for ollama (with `xhigh`/`max` downmapped to
  `high`, labeled), `--variant` for opencode, and an explicit "(non
  applicato da questa CLI)" for agy.
- Every subprocess the sync control plane runs while holding the
  host-wide lock now has a timeout (`schtasks`, `systemctl`, `mklink`,
  `pgrep`/`tasklist`, `notify-send`) — the same hang class the 0.4.0
  round fixed for the render/skills subprocesses.
- `render.py` diff mode isolates a corrupted per-CLI config (whether it
  fails parsing or reading): the other CLIs still get diffed, the run
  still exits non-zero, and `agent-doctor` surfaces the actual STOP lines
  instead of a generic last-line summary.
- `skills-sync.py` warns on the aggregate size of Codex-targeted core
  skills, not just per-file, since several under-threshold skills can
  still defeat Codex's near-empty eager-scan discipline together.
- `bootstrap-vps.sh` derives the SSH ports to allow from sshd's real
  configuration (all of them), fixing the browser-console lockout case
  where `SSH_CONNECTION` is unset and sshd listens on a custom port.
- Shipped docs completed for a stranger on Windows: INIT/README document
  the native entry point and the new-terminal-after-first-apply step;
  AGENTS.md tells agents what to do when `vault-groom` is not yet linked;
  `offline-emergency-mode.md` no longer hardcodes the maintainer's own
  local model as universal fact.

### Fixed

- `agent-sync`, `agent-doctor`, `vault-groom` and `firecrawl-local` were
  documented everywhere as bare PATH commands but never linked by any
  code path on any OS — including the systemd guard timer's own
  `ExecStart`, which depended on a symlink nothing created. All linked
  now, from a single source of truth consumed by both OS branches of the
  provisioner.
- `vault_groom_audit.py` invoked `python3` by name to publish — broken on
  stock Windows, where only `python` exists; it now uses the running
  interpreter.
- Three `Write-Error`-then-`exit` branches in `vault-groom.ps1` and one
  in `vault-push.ps1` could never reach their exit codes under
  `$ErrorActionPreference = 'Stop'`.
- `vault-groom.ps1` no longer passes prompts through argv shapes a
  cmd.exe shim can reparse (`|`, `<`, embedded newlines): runner commands
  resolve to their `.ps1`/executable form, with a byte-intactness test.
- One malformed name in `KNOWLEDGE_VAULT_MIRRORS` skips that mirror with
  a warning instead of failing the whole push, matching the pre-port
  behavior.
- Kept from the earlier, pre-review cut of this section: `agent-doctor`'s
  "Tokens in env" check Mode-gated for `vault-library`; OpenCode's
  bootstrap-instructions pointer actually written; `bootstrap-vps.sh`
  sudo escalation on Oracle's default image; maintainer dogfooding purged
  from the shipped policy files; five resilience gaps in the sync control
  plane (lock-holding subprocess hangs, `vault-push.sh` not taking the
  lock, timer units written but never enabled, phases that could not
  report failure, non-UTF-8 alert config skipping the healthcheck);
  corrupted live config no longer reads as "CLI not installed"; the Codex
  known-bad-version check ported to Windows; stale `ghcr.io/mendableai`
  registry references; Windows CI skip patterns for two bash-only test
  files.

## [0.4.0] - 2026-07-13

A security-hardening and small-team-readiness pass: a dedicated audit found
28 issues across secrets handling, supply chain, injection surfaces, and
network exposure; every one confirmed by an independent adversarial check
before being fixed, and every fix verified against real CI, not just local
tests. Alongside it, the groundwork for evaluating this as shared
infrastructure for a small team, and the sync/skills work started earlier.

### Added

- Declarative team/organization routing: an optional `Team members` section
  in `USER-PROFILE.md`, per-member Council seat files, and a `personal`
  vs. `team` scope on skills. Explicitly a routing convenience, not access
  control. See `docs/team.md` and the new `docs/org-deployment.md`, which
  documents what a shared Cloud-Server backend does and does not protect
  for a small team today.
- Mode (Local-Only vs. Cloud-Server) is now a contract `agent-doctor` and
  `vault-push` actually verify, not just prose the LLM interprets. It stays
  a verified floor, never a ceiling: declaring Cloud-Server never blocks a
  connector you've configured anyway.
- Bearer-token authentication wired into n8n's MCP endpoint, the OCR API,
  and Firecrawl's Redis. None of these had a credential check before on a
  shared deploy.
- Host firewall baseline (`ufw`, idempotent) in the Cloud-Server bootstrap
  script.
- CI gates: ruff baseline, shellcheck, `pip-audit`, Docker Compose
  validation, and PowerShell static analysis. Previously only
  syntax/compile checks ran.
- `agent-skill list|find|show|path`, the small cross-platform command for
  loading exactly one managed skill body on demand.
- Explicit `exposure: manual|core` in the skill manifest, plus a generated
  safe catalog and a one-time `--migrate-legacy` quarantine for old eager
  folders.
- A data-owned `sync/remotes.yaml` policy, typed pull states, and a host-wide
  lock for the complete sync transaction.

### Changed

- Council seats for `codex`/`agy`/`opencode` now launch with an isolated,
  explicitly-allowlisted environment (and an isolated config directory
  where verified live) instead of inheriting the full host environment and
  every application token on it. Closes a path where a prompt-injected diff
  passed to `council code-review` could, in theory, reach a real MCP server
  despite the role prompt's text-only "no tools" instruction. The relay
  mode's output-redaction gate now runs in every Council mode, not only
  relay.
- The MCP manifest's npm-package pin check and its check against literal
  secrets in `env:` values now run on the manifest actually loaded at
  runtime (the user's vault copy), not only against a test fixture.
- Deploy image references pinned to explicit versions, with a Docker digest
  pin on the OCR image now that the leak-scan false positive below is
  fixed. GitHub Actions pinned to commit SHA instead of a mutable tag.
- CI workflow declares least-privilege `permissions: contents: read`.
- Managed skill bodies now live in `~/.agents/skill-library/`, outside eager
  discovery roots. Only explicitly core bodies enter `~/.agents/skills/` or
  Codex's runtime view. Claude retains declared native-lazy views.
- `agent-sync` normalizes unsafe whole-root links before materializing skill
  views, and `agent-doctor` verifies the library, catalog, and core exposure.
- `guard` and `apply` now regenerate runtime derivatives only after proving
  the vault fresh against its authoritative remote. Required phase failures
  are aggregated into a non-zero exit code. Publishing is a separate action,
  with configured mirrors downstream of the authoritative remote.
- Running `agent-sync` without arguments is help-only. The implicit combined
  `full` operation was removed.

### Fixed

- The anti-leak pattern for high-entropy secrets only matched a value
  wrapped in quotes: the same value unquoted (a bare `.env` line, an
  `Authorization: Bearer` header) passed both the CI gate and Council's
  always-on egress scan undetected.
- n8n backups were unencrypted and world-readable, and n8n's own encryption
  key was never set explicitly, so n8n generated one inside the same volume
  the backup archived. A copied backup exposed every credential n8n ever
  held. The documented GPG secrets workflow also decrypted to a
  world-readable temp file for the duration of every edit.
- Path traversal in skill names: an unvalidated manifest entry could write
  or symlink outside the intended skill library (confirmed with a live
  reproduction, not just static reading).
- Bearer tokens (`vault-library`, Firecrawl) were briefly visible on the
  process table via `curl`'s command-line arguments during a doctor probe
  or scrape call.
- `fastapi` bumped 0.115.6 → 0.139.0 (pulls a patched `starlette`), closing
  8 tracked CVEs in the OCR service's dependency chain. Validated with a
  real dependency-resolution check and a live RapidOCR round-trip rather
  than applied blind.
- A dependency-audit exception scoped to the OCR service's known debt used
  to apply to every `requirements*.txt` in the repo, not just that one file.
- Legacy migration preserves declared Claude native-lazy links instead of
  treating them as stale eager copies.
- Dirty, wrong-branch, ahead, diverged, missing-remote, fetch-failed, and
  malformed-manifest states can no longer degrade into a successful-looking
  propagation run.
- The distributed MCP manifest's `filesystem` server no longer mounts the
  user's entire home (a bare `${HOME}` argument). It now mounts two
  explicit, configurable roots: `AGENT_ENGINE_ROOT` and `AGENT_VAULT_DATA`
  (the same canonical engine/data roots the rest of the layer already
  resolves). A user can add more roots as extra `args` entries. The
  `memory` server is no longer mounted by default: it required
  `MCP_MEMORY_OPT_IN` because it is a second, non-authoritative memory
  channel outside the KnowledgeVault.

## [0.3.2] - 2026-07-10

### Fixed

- Windows CI no longer applies POSIX mode-bit assertions to NTFS files.
  The test still verifies that the generated configuration and backup exist;
  owner-only mode checks remain enforced on POSIX, where they are meaningful.

## [0.3.1] - 2026-07-10

### Fixed

- Windows runtime skill directories backed by Junctions now recover safely.
  The provisioner recognizes directory reparse points even on Python builds
  without `Path.is_junction()`, removes a whole-hub loop through the shared
  path adapter, and preserves per-skill Junctions already pointing at their
  hub source instead of recursing into them.

## [0.3.0] - 2026-07-09

### Added

- `AI-INSTALLER.md` / `AI-UNINSTALL.md`: fast, autonomous companions to
  `INIT.md` / `docs/uninstall.md` for an agent to run with minimal
  back-and-forth. Both defer to the existing guide for the actual
  mechanism (no duplicated/divergent instructions) and require explicit
  confirmation before any destructive step.
- `agent-doctor`: a short, pruneable "third-party CLI compatibility" check
  that flags a known-broken Codex CLI release (a real tool-dispatcher
  regression, not a general version pin) instead of failing silently or
  mysteriously when every tool call gets rejected.

## [0.2.0] - 2026-07-09

### Added

- Anti-leak gate (`engine-push`, pre-commit/commit-msg hooks, CI leak-scan)
  guarding every push to this repo: a single blocked finding stops the push.
- Regression test suite (`tests/run.sh`, 40 pytest cases) covering render.py,
  the provisioner, skills-sync.py and agent-doctor.sh in a sandboxed HOME.
- `agent_sync.py`: single cross-platform provisioner replacing the old
  `agent-sync.sh` / `agent-sync.ps1` duplication. The `.sh`/`.ps1` files are
  now 5-line launchers; same CLI, same exit codes, same log file.
- CI job `engine-tests-windows` (pytest on `windows-latest`), so Windows
  coverage no longer depends on physical access to a Windows machine.
- Consumer engine clone version-pin check in `agent-doctor` (S2): flags
  silent drift between the pinned commit and what is actually checked out.
- Data-schema migration framework (`data_migrations()` in `agent_sync.py`):
  versioned, idempotent, backs up affected files before writing. No
  migrations are registered yet — today's data shape is the baseline.
- `VERSION` file and this changelog.
- Path-traversal guard in `skills-sync.py`'s GitHub-origin skill installer.
- Atomic writes (temp file + replace) for live config files the provisioner
  regenerates on every run (`settings.json`, `CLAUDE.md`, the systemd unit,
  generated MCP configs).

### Changed

- All engine strings are English-only. Localizing alerts is a user-data
  concern: the engine calls an optional translator script if the vault
  provides one, falling back silently to English otherwise.
- The systemd timer persists `AGENT_ENGINE_ROOT`/`AGENT_VAULT_DATA` across a
  cutover instead of reverting to the default layout on the next run.
- Personal instance data (the user's own `AGENTS.md`, MCP manifest) is
  always resolved from the data root, never from wherever the engine happens
  to be installed.

### Fixed

- Several engine/data path-resolution bugs where a script silently fell back
  to reading the personal data copy instead of the installed engine after a
  cutover (`agent-doctor`, `skills-sync.py`, the provisioner itself).
- Fresh install with no skills manifest yet: `skills-sync.py` no longer
  crashes, and `agent-doctor`'s skill check no longer hardcodes anyone's
  personal skill names — zero configured skills is a warning, not a
  permanent failure.
- OCR MCP server: read-before-size-check memory exhaustion, double file
  read, and unsanitized multipart filename header injection.
- Symlink race (CWE-59) in a script's temp-file handling.
- A lifecycle-audit script silently auditing the wrong directory when run
  from the engine clone instead of the data root.
- Restored an executable bit lost since the first public release.

### Removed

- `agent-healthcheck.sh`: dead code, fully superseded by `agent_sync.py`'s
  built-in healthcheck step.

## [0.1.0] - 2026-07-07

Initial public release: repositioned as an AgentOps control layer, hardened
the public trust surface, calibrated the README's claims against what the
engine actually does today.
