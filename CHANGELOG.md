# Changelog

All notable changes to the NeXgen engine are documented here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/); versioning is
[Semantic Versioning](https://semver.org/).

This file tracks the **engine** (this repo). Your own data — manifests,
instructions, skills, secrets — lives in your KnowledgeVault and is not part
of any engine release.

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
