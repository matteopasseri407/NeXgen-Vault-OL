#!/usr/bin/env python3
"""agent_sync — unified cross-platform provisioner for the KnowledgeVault
agent-universal-layer (Linux + Windows, one implementation).

Replaces the parallel agent-sync.sh / agent-sync.ps1 scripts, which used to
carry the same logic written twice in two languages with two mental models
(the root cause of "the twins drift apart": symlink vs junction, npx vs
npx.cmd, a scheduler that only self-heals on one OS). agent-sync.sh and
agent-sync.ps1 now only launch this script; the CLI, exit codes and log
file are unchanged, so the systemd timer, the Windows scheduled task and the
B1 test suite see no difference.

Modes (same contract as before):
  pull     Pull the KnowledgeVault from the remote and run healthcheck. Does not rewrite CLI runtime files.
  guard    Recurring safe propagation: pull, regenerate CLI runtime files, run healthcheck. Does not push.
  apply    Same as guard, explicit manual name for provisioning.
  publish  Push already-committed local vault changes to the remote (and mirror origin if configured).
  doctor   Run healthcheck/alerts only.
  full     Legacy full run: pull, apply runtime files, publish, creds, healthcheck.
With no arguments: full, for backward compatibility. The recurring
timer/scheduled task should use: agent_sync.py guard
Never auto-commits content: whoever writes commits (agents or the user).
"""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

IS_WINDOWS = platform.system() == "Windows"

HELP_TEXT = """agent_sync modes:
  pull     Pull the KnowledgeVault from the remote and run healthcheck. Does not rewrite CLI runtime files.
  guard    Recurring safe propagation: pull, regenerate CLI runtime files, run healthcheck. Does not push.
  apply    Same as guard, explicit manual name for provisioning.
  publish  Push already-committed local vault changes to the remote (and mirror origin if configured).
  doctor   Run healthcheck/alerts only.
  full     Legacy full run: pull, apply runtime files, publish, creds, healthcheck.

Default without arguments: full, for backward compatibility.
The recurring timer/scheduled task should use: agent_sync.py guard
"""

MODES = {
    "pull":    dict(pull=True,  apply=False, push=False, creds=False, health=True),
    "guard":   dict(pull=True,  apply=True,  push=False, creds=False, health=True),
    "apply":   dict(pull=True,  apply=True,  push=False, creds=False, health=True),
    "publish": dict(pull=False, apply=False, push=True,  creds=False, health=False),
    "doctor":  dict(pull=False, apply=False, push=False, creds=False, health=True),
    "full":    dict(pull=True,  apply=True,  push=True,  creds=True,  health=True),
}


class Env:
    """Resolves every path/env var once. Path.home() honors $HOME on POSIX
    and %USERPROFILE% on Windows, so the same code runs unmodified in the
    B1 sandbox tests on either OS (see tests/conftest.py)."""

    def __init__(self) -> None:
        self.home = Path.home()
        self.vault = Path(os.environ.get("KNOWLEDGE_VAULT_PATH") or str(self.home / "KnowledgeVault"))
        self.remote = os.environ.get("KNOWLEDGE_VAULT_REMOTE") or "origin"
        self.branch = os.environ.get("KNOWLEDGE_VAULT_BRANCH") or "main"
        # Engine/data separation (Vault 2.1, Strangler Fig): defaults reproduce
        # the historical single-tree layout exactly, zero breakage.
        self.vault_data = Path(os.environ.get("AGENT_VAULT_DATA") or str(self.vault))
        self.local_bin = self.home / ".local" / "bin"
        default_engine_root = self.vault / "03-INFRA"
        # AGENT_ENGINE_ROOT wins when set. Otherwise, fall back to where
        # ~/.local/bin/agent-sync ACTUALLY resolves right now, not silently
        # to the vault default: a bare 'agent-sync apply/guard' run (no env
        # var exported -- the normal way anyone would type it) must not
        # revert an already-live cutover. Confirmed live: without this,
        # utils()'s self-healing agent-now symlink flipped back to the
        # vault's (now-deleted) copy on the very first plain invocation
        # after the S3 cutover. engine-rollback.sh remains the one
        # intentional way back: it swaps the symlink first, which this
        # then reads as "already at the default".
        self.engine_root = Path(os.environ.get("AGENT_ENGINE_ROOT") or self._persisted_engine_root(default_engine_root) or str(default_engine_root))
        self.engine_scripts = self.engine_root / "scripts"
        self.ul = self.engine_root / "agent-universal-layer"
        # Instance data (the user's own AGENTS.md, host-specific files): ALWAYS
        # from vault_data, regardless of where the engine lives. The engine
        # only ships the generic/universal AGENTS.md template; the personal
        # instance is data, never something the engine repo should serve.
        self.instance_ul = self.vault_data / "03-INFRA" / "agent-universal-layer"
        # Vault-only infra scripts that never get published to the engine
        # repo (sync-vault-from-oracle.sh, vault-push.sh, ...): always
        # data-anchored, regardless of where the engine lives.
        self.vault_scripts = self.vault_data / "03-INFRA" / "scripts"
        self.agents_hub = self.home / ".agents" / "skills"
        self.log_dir = self.home / ".local" / "state"
        self.log_path = self.log_dir / "agent-sync.log"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.agents_hub.mkdir(parents=True, exist_ok=True)

    def _persisted_engine_root(self, default_engine_root: Path) -> str | None:
        link = self.local_bin / "agent-sync"
        if not link.is_symlink():
            return None
        try:
            target = link.resolve()
            default_resolved = default_engine_root.resolve()
        except OSError:
            return None
        root = target.parent.parent          # .../<engine-root>/scripts/agent-sync.sh -> <engine-root>
        return None if root == default_resolved else str(root)

    def log(self, message: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{_iso_now()} {message}\n")


def _iso_now() -> str:
    """Matches `date -Is` (e.g. 2026-07-08T15:36:42+02:00): local time,
    second precision, colon-separated UTC offset."""
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _append_log(env: Env, *chunks: str) -> None:
    text = "".join(c for c in chunks if c)
    if not text:
        return
    with env.log_path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


# ── generic OS adapters (the only place OS differences are allowed) ─────────

_REPARSE_POINT = 0x0400


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    if not IS_WINDOWS:
        return False
    is_junction = getattr(path, "is_junction", None)
    try:
        if callable(is_junction) and is_junction():
            return True
        # Path.is_junction() is unavailable on older supported Python builds.
        # Junctions are still directory reparse points there, so inspect the
        # Windows lstat attribute directly instead of treating them as normal
        # directories and recursing into their target.
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attributes & _REPARSE_POINT)
    except OSError:
        return False


def _points_to(path: Path, target: Path) -> bool:
    try:
        return _is_link_like(path) and path.resolve() == target.resolve()
    except OSError:
        return False


def _same_file_content(left: Path, right: Path) -> bool:
    try:
        return left.is_file() and right.is_file() and left.read_bytes() == right.read_bytes()
    except OSError:
        return False


def _same_tree_content(src: Path, dst: Path) -> bool:
    if not src.is_dir() or not dst.is_dir():
        return False
    try:
        src_entries = {p.relative_to(src).as_posix(): p for p in src.rglob("*")}
        dst_entries = {p.relative_to(dst).as_posix(): p for p in dst.rglob("*")}
    except OSError:
        return False
    if set(src_entries) != set(dst_entries):
        return False
    for rel, sp in src_entries.items():
        dp = dst_entries[rel]
        if sp.is_symlink() or dp.is_symlink():
            try:
                if not (sp.is_symlink() and dp.is_symlink() and os.readlink(sp) == os.readlink(dp)):
                    return False
            except OSError:
                return False
        elif sp.is_dir() or dp.is_dir():
            if not (sp.is_dir() and dp.is_dir()):
                return False
        elif sp.is_file() or dp.is_file():
            if not _same_file_content(sp, dp):
                return False
    return True


def _remove_path(path: Path) -> None:
    if _is_link_like(path):
        try:
            path.unlink()
        except OSError:
            path.rmdir()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def make_link(src: Path, dst: Path, *, is_dir: bool) -> bool:
    """Ensures dst points at src. POSIX: symlink. Windows: SymbolicLink for
    files, Junction for directories (no elevated privilege needed), falling
    back to a copy if the privilege is missing. Returns True if it changed
    anything on disk."""
    if _points_to(dst, src):
        return False
    if IS_WINDOWS and dst.exists() and not _is_link_like(dst):
        if is_dir and _same_tree_content(src, dst):
            return False
        if not is_dir and _same_file_content(src, dst):
            return False
        # Content differs and this isn't a link: on Windows this branch is
        # reached when a previous run fell back to a real copy (no
        # symlink/junction privilege) and the content has since diverged --
        # possibly a local edit, not necessarily just staleness. Back it up
        # before removing instead of destroying it silently (found in a
        # cross-vendor audit, 2026-07-09; not verified live on Windows, see
        # agentic-layer-concept-map.md backlog).
        bak = dst.with_name(dst.name + ".local-edit.bak-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            if dst.is_dir():
                shutil.copytree(dst, bak)
            else:
                shutil.copy2(dst, bak)
        except OSError:
            # Backup failed (locked file, permission denied, ...): do not
            # fall through to _remove_path below without a confirmed backup,
            # that would silently destroy a local edit with nothing to
            # restore it from. Bail out and retry on the next run. Found in
            # a full-codebase audit, Gemini via agy, 2026-07-09.
            return False
    _remove_path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        try:
            dst.symlink_to(src, target_is_directory=is_dir)
        except FileExistsError:
            # Two overlapping agent-sync runs (cron timer + manual run) can
            # both pass _remove_path above and then race here -- if the
            # other one already created the same correct link, that's a race
            # we lost harmlessly, not a real error. Found in a full-codebase
            # audit, Gemini via agy, 2026-07-09.
            if not (_is_link_like(dst) and dst.resolve() == src.resolve()):
                raise
        return True
    if is_dir:
        # RISK (flagged in a full-codebase audit, Gemini via agy, 2026-07-09,
        # NOT verified live on Windows -- needs physical access, see
        # agentic-layer-concept-map.md backlog): subprocess's list2cmdline
        # only quotes on space/tab/quote, not on cmd.exe metacharacters
        # (&, |, <, >, ^). A dst/src path containing one of those without a
        # space would reach cmd.exe unquoted and be parsed as a shell
        # operator. Do not "fix" this blind with hand-rolled quoting; a
        # wrong escaping scheme is its own bug and this needs a real
        # Windows run to confirm behavior before changing it.
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                            capture_output=True, text=True)
        if r.returncode == 0:
            return True
        shutil.copytree(src, dst)
        return True
    try:
        dst.symlink_to(src)
        return True
    except OSError:
        shutil.copy2(src, dst)
        return True


def resolve_cmd(name: str) -> str | None:
    """shutil.which with OS-specific candidate names (npx/npx.cmd,
    python3/py) — used for optional external tools (systemctl, notify-send)."""
    candidates = {"python3": ["python3", "py"], "npx": ["npx", "npx.cmd"]}.get(name, [name])
    for c in candidates:
        p = shutil.which(c)
        if p:
            return p
    return None


def _process_running(name: str) -> bool:
    # RISK (flagged in a cross-vendor audit, 2026-07-09, NOT verified live on
    # Windows -- needs physical access, see agentic-layer-concept-map.md
    # backlog): if the npm-installed "claude" CLI runs as a node.exe wrapper
    # rather than a standalone claude.exe on Windows, `tasklist /FI
    # "IMAGENAME eq claude.exe"` never matches, and the caller (which skips
    # rewriting .claude.json while Claude is "running") always sees it as
    # closed -- rewriting live under it. Confirm the real process name on the
    # Windows machine before trusting this; do not "fix" it blind, a wrong
    # guess (e.g. matching any node.exe) would create a worse false positive.
    try:
        if not IS_WINDOWS:
            r = subprocess.run(["pgrep", "-x", name], capture_output=True)
            return r.returncode == 0
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}.exe"], capture_output=True, text=True)
        return name.lower() in r.stdout.lower()
    except (OSError, FileNotFoundError):
        return False


def _post_form(url: str, fields: dict) -> bool:
    try:
        data = urllib.parse.urlencode(fields).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:
        return False


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """write_text() truncates then writes: a live CLI re-reading the same
    file (settings.json, CLAUDE.md, the systemd unit...) while the 30-minute
    recurring timer regenerates it can catch a truncated/empty file. Write to
    a same-directory temp file and os.replace() it in, which POSIX/Windows
    both guarantee atomic for a rename onto an existing path. Copies the
    existing file's mode onto the temp file first: os.replace is a rename,
    not an in-place write, so without this a plain rewrite would silently
    reset any non-default permission bits to the process umask."""
    old_mode = None
    if path.exists():
        try:
            old_mode = path.stat().st_mode
        except OSError:
            pass
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding=encoding)
    if old_mode is not None:
        try:
            os.chmod(tmp, old_mode)
        except OSError:
            pass
    os.replace(tmp, path)


def _write_if_different(path: Path, content: str) -> bool:
    """Writes content to path unless it's already a regular file with the
    identical content (mirrors agent-sync.sh's write_claude_pointer guard:
    a symlink or differing content always gets replaced)."""
    if path.exists() and not path.is_symlink():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except (OSError, UnicodeDecodeError):
            pass
    if path.is_symlink() or path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, content)
    return True


def _git(env: Env, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(env.vault_data), *args],
                               capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


# ── 0.5 data_migrations ──────────────────────────────────────────────────
# Schema version of the DATA the engine reads (manifest.yaml,
# skills.manifest.yaml, USER-PROFILE.md, ...) -- separate from the engine's
# own release version (VERSION file). Bump TARGET_SCHEMA_VERSION and add an
# entry to MIGRATIONS whenever a future engine release needs to reshape an
# existing data file. Today's data shape IS version 1: MIGRATIONS is empty
# on purpose, not a stub -- there is nothing to migrate from yet.
TARGET_SCHEMA_VERSION = 1

# from_version -> callable(env) that migrates from_version to from_version+1
# and returns the list of paths it modified. Each migration is responsible
# for calling _backup_before_migration(env, [affected_paths]) itself BEFORE
# writing anything, then applying the change, then returning the touched
# paths. Populate this dict in future releases; keep it empty otherwise.
MIGRATIONS: dict[int, Callable[["Env"], list[Path]]] = {}


def _backup_before_migration(env: Env, paths: list[Path]) -> None:
    """Same .bak-<timestamp> + keep-3 convention as render.py's backups.
    A migration function must call this BEFORE writing, on the pre-migration
    content -- never after, or the backup would just be a copy of the new
    (already migrated) file."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    for path in paths:
        if not path.is_file():
            continue
        bak = path.with_name(path.name + ".bak-" + ts)
        shutil.copy2(path, bak)
        backs = sorted(path.parent.glob(path.name + ".bak-*"))
        for stale in backs[:-3]:
            stale.unlink(missing_ok=True)


def data_migrations(env: Env) -> None:
    schema_file = env.vault_data / "99-INDEX" / "DATA-SCHEMA-VERSION.txt"
    if schema_file.is_file():
        try:
            current = int(schema_file.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            env.log(f"data-migrations: {schema_file} has non-numeric content, leaving data untouched")
            return
    else:
        # No marker yet: today's data shape already IS the target version,
        # there is nothing to migrate -- just stamp the baseline.
        current = TARGET_SCHEMA_VERSION

    if current > TARGET_SCHEMA_VERSION:
        env.log(f"data-migrations: data schema v{current} is newer than this engine supports (v{TARGET_SCHEMA_VERSION}) -- leaving data untouched, upgrade the engine")
        return

    while current < TARGET_SCHEMA_VERSION:
        step = MIGRATIONS.get(current)
        if step is None:
            env.log(f"data-migrations: no migration registered for v{current} -> v{current + 1}, stopping (data left at v{current})")
            return
        touched = step(env)
        env.log(f"data-migrations: applied v{current} -> v{current + 1}, touched {touched}")
        current += 1

    if _write_if_different(schema_file, f"{TARGET_SCHEMA_VERSION}\n"):
        env.log(f"data-migrations: stamped {schema_file} at v{TARGET_SCHEMA_VERSION}")


# ── 1. pull ──────────────────────────────────────────────────────────────

def pull(env: Env) -> None:
    if env.remote in ("local", "none"):
        env.log("pull: skipped (Local-Only mode)")
        return
    helper = env.vault_scripts / "sync-vault-from-oracle.sh"
    if not IS_WINDOWS and helper.is_file() and os.access(helper, os.X_OK):
        r = subprocess.run(["bash", str(helper)], capture_output=True, text=True)
        _append_log(env, r.stdout, r.stderr)
        if r.returncode == 0:
            env.log("pull: ok (dedicated helper)")
        else:
            env.log("pull: cloud unreachable or state not syncable — continuing with the local copy")
        return
    if _git(env, "remote", "get-url", env.remote).returncode != 0:
        env.log(f"pull: no remote '{env.remote}' configured — local copy only (fine for a single machine)")
        return
    status = _git(env, "status", "--porcelain", "--untracked-files=no")
    if status.stdout.strip():
        env.log("pull: skipped because the vault has uncommitted tracked changes (untracked files do not block)")
        return
    if _git(env, "fetch", "--prune", env.remote, env.branch).returncode != 0:
        env.log(f"pull: {env.remote} unreachable or not fast-forward — continuing with the local copy")
        return
    lh = _git(env, "rev-parse", env.branch)
    rh = _git(env, "rev-parse", f"{env.remote}/{env.branch}")
    mb = _git(env, "merge-base", env.branch, f"{env.remote}/{env.branch}")
    if lh.returncode or rh.returncode or mb.returncode:
        env.log(f"pull: {env.remote} unreachable or not fast-forward — continuing with the local copy")
        return
    lh, rh, mb = lh.stdout.strip(), rh.stdout.strip(), mb.stdout.strip()
    if lh == rh:
        env.log("pull: already up to date")
    elif mb == lh:
        if _git(env, "merge", "--ff-only", f"{env.remote}/{env.branch}").returncode == 0:
            env.log(f"pull: fast-forwarded from {env.remote}/{env.branch}")
        else:
            env.log(f"pull: {env.remote} unreachable or not fast-forward — continuing with the local copy")
    elif mb == rh:
        env.log(f"pull: local branch ahead of {env.remote}/{env.branch}; skipping merge")
    else:
        env.log(f"pull: local branch diverged from {env.remote}/{env.branch}; manual resolution required")


# ── 2. instructions ──────────────────────────────────────────────────────
# NOTE (B2.5 reconciliation, see the launch report): agent-sync.ps1 still
# actively re-links ~/ANTIGRAVITY.md, but agent-sync.sh's own comment records
# a verified behavioral probe: Antigravity never reads that file, it was
# dead wiring copied from the Codex pattern. That fact isn't OS-dependent,
# so the fix (stop managing it, clean up the leftover symlink) is ported
# uniformly instead of kept Windows-only.

def instructions(env: Env) -> None:
    canon = env.instance_ul / "instructions" / "AGENTS.md"
    if not canon.is_file():
        env.log(f"WARNING: missing {canon} — instructions not relinked")
        return
    claude_md = env.home / "CLAUDE.md"
    content = (
        "# Claude compatibility pointer\n\n"
        "Canonical instructions live at:\n"
        f"{canon}\n\n"
        "At session start, read and follow that file when the user-specific agent policy is needed.\n"
        "Do not duplicate the full bootstrap in CLAUDE.md.\n"
    )
    if _write_if_different(claude_md, content):
        env.log(f"instructions: wrote Claude pointer {claude_md}")

    for target in (env.home / ".gemini" / "config" / "AGENTS.md", env.home / ".codex" / "AGENTS.md"):
        try:
            same = target.is_symlink() and target.resolve() == canon.resolve()
        except OSError:
            same = False
        if same:
            continue
        make_link(canon, target, is_dir=False)
        env.log(f"instructions: relinked {target}")

    antigravity_md = env.home / "ANTIGRAVITY.md"
    try:
        if antigravity_md.is_symlink() and antigravity_md.resolve() == canon.resolve():
            antigravity_md.unlink()
            env.log("instructions: removed dead symlink ~/ANTIGRAVITY.md (Antigravity doesn't read it)")
    except OSError:
        pass

    if IS_WINDOWS:
        for src_name, target_name in (("GEMMA.md", "GEMMA.md"), ("LOCAL-WORKER.md", "LOCAL-WORKER.md")):
            src = env.instance_ul / "instructions" / src_name
            if src.is_file() and make_link(src, env.home / target_name, is_dir=False):
                env.log(f"instructions: relinked {target_name}")


# ── 2.5 antigravity_mcp ──────────────────────────────────────────────────
# Distributes the ONE file mcp_render (below) generates from the manifest to
# Antigravity's other config paths. Not a second generator: render.py is the
# single source of truth, this section is pure fan-out via symlink/junction.

def antigravity_mcp(env: Env) -> None:
    src = env.home / ".gemini" / "antigravity" / "mcp_config.json"
    if not src.is_file():
        return
    for target in (
        env.home / ".gemini" / "antigravity-cli" / "mcp_config.json",
        env.home / ".gemini" / "antigravity-ide" / "mcp_config.json",
        env.home / ".gemini" / "config" / "mcp_config.json",
    ):
        try:
            same = target.is_symlink() and target.resolve() == src.resolve()
        except OSError:
            same = False
        if same:
            continue
        make_link(src, target, is_dir=False)
        env.log(f"mcp: relinked {target}")


# ── 2.7 utils ────────────────────────────────────────────────────────────

def _link_util(src: Path, dst: Path, env: Env, label: str) -> None:
    if not src.is_file():
        return
    if not IS_WINDOWS and not (src.stat().st_mode & 0o111):
        env.log(f"utils: source {src} is not executable, refusing to mutate an engine source")
        return
    try:
        same = dst.is_symlink() and dst.resolve() == src.resolve()
    except OSError:
        same = False
    if same:
        return
    make_link(src, dst, is_dir=False)
    env.log(f"utils: relinked {label}")


def utils(env: Env) -> None:
    env.local_bin.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        _link_util(env.engine_scripts / "agent-now.sh", env.local_bin / "agent-now", env, "agent-now")
        _link_util(env.engine_scripts / "council.sh", env.local_bin / "council", env, "council")
        _link_util(env.vault_scripts / "vault-push.sh", env.local_bin / "vault-push", env, "vault-push")
        _link_util(env.vault_scripts / "vault-ocr-local.sh", env.local_bin / "vault-ocr-local", env, "vault-ocr-local")
        return
    for name in ("agent-now", "council"):
        src = env.engine_scripts / f"{name}.ps1"
        if not src.is_file():
            env.log(f"utils: missing source {src}")
            continue
        dst = env.local_bin / f"{name}.ps1"
        try:
            same = dst.is_symlink() and dst.resolve() == src.resolve()
        except OSError:
            same = False
        if not same:
            make_link(src, dst, is_dir=False)
            env.log(f"utils: relinked {name}.ps1")
        wrapper = (
            "@echo off\r\n"
            f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"%~dp0{name}.ps1\" %*\r\n"
        )
        if _write_if_different(env.local_bin / f"{name}.cmd", wrapper):
            env.log(f"utils: installed {name}.cmd wrapper")


def local_model_runtime(env: Env) -> None:
    if not IS_WINDOWS:
        return
    src = env.engine_scripts / "local-model-agent.ps1"
    if not src.is_file():
        env.log(f"local-model: missing source {src}")
        return
    env.local_bin.mkdir(parents=True, exist_ok=True)
    runtime = env.local_bin / "local-model-agent.ps1"
    if make_link(src, runtime, is_dir=False):
        env.log("local-model: relinked local-model-agent.ps1")
    for old_name in ("gemma-worker.cmd", "gemma-agent.cmd"):
        old = env.local_bin / old_name
        if old.exists() or old.is_symlink():
            _remove_path(old)
    wrappers = {
        "local-worker.ps1": "$ScriptPath = Join-Path $PSScriptRoot 'local-model-agent.ps1'\r\n& $ScriptPath -Mode worker @args\r\n",
        "local-agent.ps1": "$ScriptPath = Join-Path $PSScriptRoot 'local-model-agent.ps1'\r\n& $ScriptPath -Mode agent @args\r\n",
        "gemma-worker.ps1": "$ScriptPath = Join-Path $PSScriptRoot 'local-model-agent.ps1'\r\n& $ScriptPath -Mode worker @args\r\n",
        "gemma-agent.ps1": "$ScriptPath = Join-Path $PSScriptRoot 'local-model-agent.ps1'\r\n& $ScriptPath -Mode agent @args\r\n",
    }
    changed = False
    for name, content in wrappers.items():
        changed = _write_if_different(env.local_bin / name, content) or changed
    if changed:
        env.log("local-model: installed runtime shims")


# ── 2.75 scheduler ───────────────────────────────────────────────────────
# Self-healing recurring trigger on EVERY apply/guard, on both OSes: the
# opt-in-only Windows switch (-InstallScheduledTask) is the gap this section
# closes, per Fable's adapter list naming install_scheduler() as a normal
# per-run step, not an opt-in one.

def _systemd_service_content(env: "Env") -> str:
    """Carries AGENT_ENGINE_ROOT/AGENT_VAULT_DATA into the recurring timer.
    The timer re-reads its unit file, not a shell environment, so a one-off
    engine-cutover run (env var passed on the command line) makes the switch
    persistent for every future guard run. env.engine_root is already the
    single source of truth (env var, else the persisted agent-sync symlink,
    else the vault default -- see Env._persisted_engine_root), so a plain
    unadorned 'agent-sync apply' never silently reverts an already-live
    cutover here either."""
    lines = ["[Unit]",
             "Description=KnowledgeVault agent sync guard (pull + apply + healthcheck, no publish)",
             "", "[Service]", "Type=oneshot"]
    default_engine_root = (env.vault / "03-INFRA").resolve()
    if env.engine_root.resolve() != default_engine_root:
        lines.append(f"Environment=AGENT_ENGINE_ROOT={env.engine_root}")
    # env.vault_data (not the raw env var): a bare run with AGENT_VAULT_DATA
    # unset must not erase an already-persisted cutover the same way a bare
    # AGENT_ENGINE_ROOT-less run used to silently revert engine_root above.
    if env.vault_data.resolve() != env.vault.resolve():
        lines.append(f"Environment=AGENT_VAULT_DATA={env.vault_data}")
    lines.append("ExecStart=%h/.local/bin/agent-sync guard")
    return "\n".join(lines) + "\n"


_SYSTEMD_TIMER = """[Unit]
Description=agent-sync guard every 30 minutes and shortly after login

[Timer]
OnStartupSec=3min
OnUnitActiveSec=30min
Persistent=true

[Install]
WantedBy=timers.target
"""


def _install_systemd_units(env: Env) -> None:
    unit_dir = env.home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    changed = False
    for path, content, label in (
        (unit_dir / "agent-sync.service", _systemd_service_content(env), "agent-sync.service set to pull mode"),
        (unit_dir / "agent-sync.timer", _SYSTEMD_TIMER, "agent-sync.timer updated"),
    ):
        if path.exists():
            try:
                if path.read_text(encoding="utf-8") == content:
                    continue
            except (OSError, UnicodeDecodeError):
                pass
            stamp = time.strftime("%Y%m%d-%H%M%S")
            shutil.copy2(path, path.with_name(f"{path.name}.pre-pull-mode-{stamp}.bak"))
        _atomic_write_text(path, content)
        changed = True
        env.log(f"systemd: {label}")
    if changed and resolve_cmd("systemctl"):
        r = subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True)
        if r.returncode != 0:
            env.log("systemd: user daemon-reload failed (best-effort)")


_VBS_TEMPLATE = (
    'Set shell = CreateObject("WScript.Shell")\r\n'
    'script = "{script}"\r\n'
    'shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & script & Chr(34) '
    '& " guard", 0, True\r\n'
)


def _install_scheduled_task(env: Env) -> None:
    task_name = "KnowledgeVault Agent Sync"
    script_path = env.engine_scripts / "agent-sync.ps1"
    wrapper_path = env.engine_scripts / "start-agent-sync-hidden.vbs"
    content = _VBS_TEMPLATE.format(script=str(script_path).replace('"', '""'))
    if _write_if_different(wrapper_path, content):
        env.log("scheduled-task: hidden wrapper updated")
    run_cmd = f'wscript.exe "{wrapper_path}"'
    every30 = ["schtasks.exe", "/Create", "/TN", task_name, "/SC", "MINUTE", "/MO", "30", "/TR", run_cmd, "/F"]
    logon = ["schtasks.exe", "/Create", "/TN", f"{task_name} Logon", "/SC", "ONLOGON", "/TR", run_cmd, "/F"]
    r = subprocess.run(every30, capture_output=True, text=True)
    if r.returncode != 0:
        env.log(f"scheduled-task: schtasks.exe failed for '{task_name}': {r.stdout}{r.stderr}")
        return
    env.log(f"scheduled-task: installed/updated '{task_name}' via schtasks.exe")
    r = subprocess.run(logon, capture_output=True, text=True)
    if r.returncode == 0:
        env.log(f"scheduled-task: installed/updated '{task_name} Logon' via schtasks.exe")
        return
    env.log(f"scheduled-task: logon trigger failed via schtasks.exe ({r.stdout}{r.stderr})")
    startup_dir = os.environ.get("APPDATA")
    if startup_dir:
        startup_vbs = Path(startup_dir) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "KnowledgeVault Agent Sync.vbs"
        startup_vbs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wrapper_path, startup_vbs)
        env.log(f"startup: installed hidden logon fallback {startup_vbs}")


def install_scheduler(env: Env) -> None:
    if IS_WINDOWS:
        _install_scheduled_task(env)
    else:
        _install_systemd_units(env)


# ── 2.8 mcp_render ───────────────────────────────────────────────────────
# render.py is already cross-platform (proven by the 4-dialect B1 matrix):
# invoked as a subprocess with sys.executable (the SAME interpreter running
# agent_sync.py), so there is no python3-vs-python-vs-py resolution needed.

_SUMMARY_RE_MATCH = re.compile(r"match, (\d+) with differences")
_SUMMARY_RE_EXTRA = re.compile(r"differences, (\d+) outside the manifest")


def mcp_render(env: Env) -> None:
    render_path = env.ul / "mcp" / "render.py"
    if not render_path.is_file():
        return
    for cli in ("opencode", "antigravity", "codex"):
        r = subprocess.run([sys.executable, str(render_path), "--write", cli], capture_output=True, text=True)
        _append_log(env, r.stdout, r.stderr)
        if r.returncode == 0:
            env.log(f"mcp-gen: {cli} aligned with the manifest")
        elif r.returncode == 3:
            env.log(f"mcp-gen: {cli} has no default config file yet (never launched?) — open it once, then re-run agent-sync")
        else:
            env.log(f"mcp-gen: {cli} NOT aligned (best-effort, continuing)")

    if _process_running("claude"):
        env.log("mcp-gen: claude ACTIVE -> not touching .claude.json live (sentinel only)")
    else:
        r = subprocess.run([sys.executable, str(render_path), "--write", "claude"], capture_output=True, text=True)
        _append_log(env, r.stdout, r.stderr)
        if r.returncode == 0:
            env.log("mcp-gen: claude aligned (was closed)")
        elif r.returncode == 3:
            env.log("mcp-gen: claude has no .claude.json yet (never launched?) — open Claude Code once, then re-run agent-sync")
        else:
            env.log("mcp-gen: claude not aligned (best-effort)")

    diag = subprocess.run([sys.executable, str(render_path)], capture_output=True, text=True)
    lines = [ln for ln in diag.stdout.strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    m_drift = _SUMMARY_RE_MATCH.search(last)
    m_extra = _SUMMARY_RE_EXTRA.search(last)
    drift = int(m_drift.group(1)) if m_drift else 0
    extra = int(m_extra.group(1)) if m_extra else 0
    if drift > 0:
        env.log(f"mcp-gen: SENTINEL — {drift} servers diverge from the manifest")
    if extra > 0:
        env.log(f"mcp-gen: NOTE — {extra} servers outside the manifest (kept as-is): register them in manifest.yaml to propagate them everywhere")
    # Drift notification: NOT here (single-megaphone rule). agent_sync stays
    # silent; the only alert surface is creds_health -> agent-doctor.


# ── 3. vault_skills ──────────────────────────────────────────────────────

def vault_skills(env: Env) -> None:
    skills_src = env.instance_ul / "skills"
    if not skills_src.is_dir():
        return
    for d in sorted(skills_src.iterdir()):
        if not d.is_dir():
            continue
        dst = env.agents_hub / d.name
        try:
            same = dst.is_symlink() and dst.resolve() == d.resolve()
        except OSError:
            same = False
        if same:
            continue
        make_link(d, dst, is_dir=True)
        env.log(f"vault skill: relinked {d.name}")


# ── 3.5 skills_index ─────────────────────────────────────────────────────

def skills_index(env: Env) -> None:
    skills_sync = env.engine_scripts / "skills-sync.py"
    if not skills_sync.is_file():
        return
    r = subprocess.run([sys.executable, str(skills_sync), "--apply"], capture_output=True, text=True)
    if r.returncode == 0:
        summary = next((ln for ln in r.stdout.splitlines() if "Total:" in ln), "")
        summary = re.sub(r"\x1b\[[0-9;]*m", "", summary).strip()
        env.log(f"skills-manifest: apply ok ({summary})")
    else:
        env.log("skills-manifest: apply failed (best-effort, detail in the manual diff)")


# ── 4. runtimes ──────────────────────────────────────────────────────────
# Hub -> per-CLI runtime links, honoring exclude lists (lazy loading) and
# guarding against the 2026-07-01 self-loop bug (runtime = whole-hub symlink).

def runtimes(env: Env) -> None:
    for rel, excl_name in ((".claude/skills", "skills-exclude-claude.txt"),
                            (".codex/skills", "skills-exclude-codex.txt")):
        rt = env.home / Path(rel)
        if not rt.is_dir():
            if not IS_WINDOWS:
                continue
            rt.mkdir(parents=True, exist_ok=True)
        if _points_to(rt, env.agents_hub):
            _remove_path(rt)
            rt.mkdir(parents=True, exist_ok=True)
            env.log(f"runtime: {rt} was a symlink to the hub — converted to a real folder (per-skill links + active exclusions)")

        excl_path = env.instance_ul / excl_name
        excludes = set()
        if excl_path.is_file():
            excludes = {ln.strip() for ln in excl_path.read_text(encoding="utf-8").splitlines()
                        if ln.strip() and not ln.strip().startswith("#")}

        if not env.agents_hub.is_dir():
            continue
        for d in sorted(env.agents_hub.iterdir()):
            if not d.is_dir():
                continue
            name = d.name
            link = rt / name
            if name in excludes:
                if _is_link_like(link) or link.exists():
                    _remove_path(link)
                    env.log(f"runtime: {name} excluded from {rt} (lazy)")
                continue
            if not _points_to(link, d) and make_link(d, link, is_dir=True):
                env.log(f"runtime: relinked {name} in {rt}")


# ── 4.5 claude_hooks ─────────────────────────────────────────────────────
# Pure-Python JSON merge instead of shelling out to jq (a real external
# dependency agent-sync.sh silently no-ops without): same behavior, one
# fewer thing that has to be installed on either OS.

def claude_hooks(env: Env) -> None:
    hook_src = env.ul / "hooks" / "claude-vault-checkpoint.mjs"
    claude_dir = env.home / ".claude"
    if not hook_src.is_file() or not claude_dir.is_dir():
        return
    hook_dst = claude_dir / "claude-vault-checkpoint.mjs"
    src_bytes = hook_src.read_bytes()
    if not hook_dst.exists() or hook_dst.read_bytes() != src_bytes:
        hook_dst.write_bytes(src_bytes)
        env.log(f"claude-hooks: deployed {hook_dst}")

    settings_path = claude_dir / "settings.json"
    if not settings_path.is_file():
        return
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        env.log("claude-hooks: settings.json not valid JSON; skipping merge")
        return
    if not isinstance(settings, dict):
        # Valid JSON but not an object (e.g. "[]") -- settings.setdefault
        # below would crash with AttributeError, aborting the rest of this
        # agent-sync run (publish/creds/health all run after this call in
        # main()). Found in a full-codebase audit, Gemini via agy, 2026-07-09.
        env.log("claude-hooks: settings.json root is not an object; skipping merge")
        return

    command = f'node "{hook_dst}"'
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event in ("SessionStart", "PreCompact"):
        entries = hooks.setdefault(event, [])
        present = any(h.get("command") == command for matcher in entries for h in matcher.get("hooks", []))
        if not present:
            entries.append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
            changed = True
    if not changed:
        return
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(settings_path, settings_path.with_name(f"settings.json.pre-hooks-{stamp}.bak"))
    _atomic_write_text(settings_path, json.dumps(settings, indent=2) + "\n")
    env.log(f"claude-hooks: merged SessionStart/PreCompact into {settings_path}")


# ── 5. publish ───────────────────────────────────────────────────────────

def publish(env: Env) -> None:
    if env.remote in ("local", "none"):
        env.log("push: skipped (Local-Only mode)")
        return
    if _git(env, "rev-parse", "--verify", f"{env.remote}/{env.branch}").returncode != 0:
        return
    ahead_r = _git(env, "rev-list", "--count", f"{env.remote}/{env.branch}..{env.branch}")
    try:
        ahead = int(ahead_r.stdout.strip() or "0")
    except ValueError:
        ahead = 0
    if ahead <= 0:
        return

    push_ok = False
    if _git(env, "push", env.remote, env.branch).returncode == 0:
        push_ok = True
        env.log(f"push: {ahead} commit(s) published to {env.remote}")
    elif _git(env, "fetch", "--prune", env.remote, env.branch).returncode == 0:
        dirty = _git(env, "status", "--porcelain", "--untracked-files=no")
        if dirty.stdout.strip():
            env.log("push: rejected but the working tree has uncommitted tracked changes — not rebasing, resolve by hand")
        elif _git(env, "rebase", f"{env.remote}/{env.branch}").returncode == 0:
            if _git(env, "push", env.remote, env.branch).returncode == 0:
                push_ok = True
                env.log(f"push: divergence resolved via clean rebase, published to {env.remote}")
            else:
                env.log("push: still rejected after rebase — will retry next run")
        else:
            _git(env, "rebase", "--abort")
            env.log("push: DIVERGENCE WITH CONFLICTS — manual 'git pull --rebase' needed (the healthcheck will flag it)")
    else:
        env.log(f"push: {env.remote} unreachable (offline) — commits stay local, will retry")

    if push_ok and _git(env, "push", "origin", env.branch).returncode != 0:
        if (_git(env, "fetch", "--prune", "origin", env.branch).returncode == 0
                and _git(env, "push", "--force-with-lease", "origin", env.branch).returncode == 0):
            env.log("push: origin (mirror) realigned to the remote-hub line (force-with-lease)")
        else:
            env.log("push: GitHub (origin) unreachable or lease expired — will retry next run")


# ── 6. creds_health ──────────────────────────────────────────────────────
# Unifies agent-sync.sh's external agent-healthcheck.sh call and
# agent-sync.ps1's inline Send-Healthcheck: same debounce state file, same
# interval, same doctor-summary/Telegram/webhook contract, one
# implementation for both OSes instead of a shell script + a PS function.

_FAIL_RE = re.compile(r"FAIL=([1-9]\d*)")


def _load_env_conf(env: Env) -> None:
    conf = env.home / ".config" / "environment.d" / "91-telegram-alert.conf"
    if not conf.is_file():
        return
    for line in conf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def _ensure_alert_creds(env: Env) -> None:
    if env.remote in ("local", "none"):
        return
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        return
    cred_id = os.environ.get("N8N_TELEGRAM_CRED_ID")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    remote_alias = os.environ.get("REMOTE_ALIAS")
    container = os.environ.get("N8N_CONTAINER", "n8n-n8n-1")
    if not (cred_id and chat and remote_alias):
        env.log("alert-creds: n8n source not configured (N8N_TELEGRAM_CRED_ID / TELEGRAM_CHAT_ID / REMOTE_ALIAS) — skipping, using env-based alerts if present")
        return
    remote_script = (
        "set -eu\n"
        "cred_id=$1\n"
        "tmpfile=$(mktemp /tmp/agent-sync-n8n-creds.XXXXXX)\n"
        "trap 'rm -f \"$tmpfile\"' EXIT HUP INT TERM\n"
        "chmod 600 \"$tmpfile\"\n"
        "n8n export:credentials --all --decrypted --output=\"$tmpfile\" >/dev/null 2>&1\n"
        "CRED_FILE=\"$tmpfile\" N8N_TELEGRAM_CRED_ID=\"$cred_id\" "
        "node -e 'const d=require(process.env.CRED_FILE);"
        "const list=Array.isArray(d)?d:[];"
        "const c=list.find(x=>x&&x.id===process.env.N8N_TELEGRAM_CRED_ID);"
        "process.stdout.write((c&&c.data&&(c.data.accessToken||c.data.token))||\"\")' 2>/dev/null\n"
    )
    token = ""
    for attempt in range(3):
        try:
            # shlex.quote(container): N8N_CONTAINER is attacker-controllable
            # by anything that can set a local env var before this runs (a
            # compromised dependency, a malicious skill/MCP server) -- an
            # unquoted value becomes a command the remote shell parses,
            # turning a local env-var write into arbitrary root execution on
            # the remote host via sudo. Found in a full-codebase audit,
            # Gemini via agy, 2026-07-09.
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=12", "-o", "BatchMode=yes", remote_alias,
                 f"sudo -n docker exec -i {shlex.quote(container)} sh -s -- {shlex.quote(cred_id)}"],
                input=remote_script, capture_output=True, text=True, timeout=20,
            )
            token = r.stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            token = ""
        if token or attempt == 2:
            break
        time.sleep(4)
    if not token:
        env.log("alert-creds: Telegram provisioning did NOT succeed after 3 attempts (remote unreachable or cred not retrieved) — will retry on the next agent-sync")
        return
    os.environ["TELEGRAM_BOT_TOKEN"] = token
    os.environ["TELEGRAM_CHAT_ID"] = chat
    env.log("alert-creds: Telegram provisioning from n8n completed")


def _doctor_summary(env: Env, timeout: int) -> str | None:
    if not IS_WINDOWS:
        doctor = env.engine_scripts / "agent-doctor.sh"
        if not doctor.is_file():
            return None
        cmd = ["bash", str(doctor), "--summary"]
    else:
        doctor = env.engine_scripts / "agent-doctor.ps1"
        if not doctor.is_file():
            return None
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(doctor), "-Summary"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        env.log(f"healthcheck: skipped (agent-doctor timeout after {timeout}s)")
        return None
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _localize_alert(env: Env, msg: str) -> str:
    """The engine's own strings are English-only, deliberately: this is a
    public repo, and mixing languages in the SOURCE is worse than being
    all-English. Translation is the user's own concern, done in their DATA,
    never hardcoded here. If vault_data/03-INFRA/alert-translate.sh exists
    and is executable, it gets the English message on stdin and its stdout
    (if non-empty) replaces it; any failure (missing, not executable,
    non-zero exit, timeout, empty output) falls back to the English
    original — a broken translator must never swallow a real alert."""
    translator = env.vault_data / "03-INFRA" / "alert-translate.sh"
    if not (translator.is_file() and os.access(translator, os.X_OK)):
        return msg
    try:
        r = subprocess.run(["bash", str(translator)], input=msg, capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    return msg


def _send_healthcheck(env: Env) -> None:
    timeout = int(os.environ.get("AGENT_DOCTOR_TIMEOUT_SECONDS") or 20)
    summary = _doctor_summary(env, timeout=timeout)
    if not summary:
        return
    problem = bool(_FAIL_RE.search(summary))
    sig = "".join(summary.split())
    state_file = env.log_dir / "agent-healthcheck.state"
    interval = int(os.environ.get("AGENT_HEALTHCHECK_INTERVAL") or 86400)
    now = int(time.time())
    last, last_sig = 0, ""
    if state_file.is_file():
        lines = state_file.read_text(encoding="utf-8").splitlines()
        if lines and lines[0].isdigit():
            last = int(lines[0])
        if len(lines) >= 2:
            last_sig = lines[1]

    if not problem:
        _atomic_write_text(state_file, f"{now}\nok\n")
        return

    send = sig != last_sig or (now - last) >= interval
    if not send:
        return

    hostn = platform.node()
    msg = f"[AGENT ALERT] [{hostn}] {time.strftime('%Y-%m-%d %H:%M')}\n{summary}"
    msg = _localize_alert(env, msg)
    sent = False
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    webhook = os.environ.get("VAULT_ALERT_WEBHOOK")
    if token and chat:
        sent = _post_form(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat, "text": msg})
    elif webhook:
        sent = _post_form(webhook, {"host": hostn, "text": msg})
    if not sent and not IS_WINDOWS and resolve_cmd("notify-send"):
        r = subprocess.run(["notify-send", "-u", "critical", "-a", "agent-healthcheck",
                             "Agents: something is wrong", msg], capture_output=True)
        sent = r.returncode == 0
    if sent:
        env.log(f"healthcheck: sent ({sig})")
    else:
        env.log(f"healthcheck: {summary} (no transport configured)")
    _atomic_write_text(state_file, f"{now}\n{sig}\n")


def creds_health(env: Env, *, do_creds: bool, do_health: bool) -> None:
    if do_creds:
        try:
            _ensure_alert_creds(env)
        except Exception as exc:
            env.log(f"alert-creds: provisioning failed ({exc})")
    if do_health:
        _load_env_conf(env)
        try:
            _send_healthcheck(env)
        except Exception as exc:
            env.log(f"healthcheck: non-fatal error ({exc})")


def _parse_cli(argv: list[str]) -> tuple[str, bool]:
    skip_mcp = False
    cleaned: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-Mode", "--mode"):
            if i + 1 >= len(argv):
                return arg, skip_mcp
            cleaned.append(argv[i + 1])
            i += 2
            continue
        if arg in ("-InstallScheduledTask", "--install-scheduled-task"):
            # Backward-compatible no-op: B2.5 installs/repairs the scheduler
            # during every apply/guard/full run.
            i += 1
            continue
        if arg in ("-SkipMcp", "--skip-mcp"):
            skip_mcp = True
            i += 1
            continue
        cleaned.append(arg)
        i += 1
    return (cleaned[0] if cleaned else "full"), skip_mcp


# ── entry point ──────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = Env()
    mode, skip_mcp = _parse_cli(argv)

    if mode in ("-h", "--help", "help"):
        print(HELP_TEXT)
        return 0
    if mode not in MODES:
        print(f"agent_sync: unknown mode: {mode}\nUse: agent_sync --help", file=sys.stderr)
        return 2

    flags = MODES[mode]
    env.log(f"agent-sync: start mode={mode}")

    if flags["pull"]:
        pull(env)

    if flags["apply"]:
        data_migrations(env)
        instructions(env)
        antigravity_mcp(env)
        utils(env)
        local_model_runtime(env)
        install_scheduler(env)
        if skip_mcp:
            env.log("mcp-gen: skipped by --skip-mcp")
        else:
            mcp_render(env)
        vault_skills(env)
        skills_index(env)
        runtimes(env)
        claude_hooks(env)

    if flags["push"]:
        publish(env)

    creds_health(env, do_creds=flags["creds"], do_health=flags["health"])

    dirty = _git(env, "status", "--porcelain")
    dirty_lines = [l for l in dirty.stdout.splitlines() if l.strip()]
    if dirty_lines:
        env.log(f"note: {len(dirty_lines)} uncommitted file(s) in the vault (not touching them)")

    env.log(f"agent-sync: completed mode={mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
