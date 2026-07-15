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

Modes:
  pull     Pull the KnowledgeVault from the remote and run healthcheck. Does not rewrite CLI runtime files.
  guard    Recurring safe propagation: pull, regenerate CLI runtime files, run healthcheck. Does not push.
  apply    Same as guard, explicit manual name for provisioning.
  publish  Push already-committed local vault changes to the authoritative remote, then configured mirrors.
  preflight  Validate every configuration input used by apply. Does not regenerate runtime files.
  doctor   Run healthcheck/alerts only.
  bootstrap-alerts  Provision optional alert credentials and run healthcheck.
With no arguments: print help and change nothing. The recurring
timer/scheduled task uses: agent_sync.py guard
Never auto-commits content: whoever writes commits (agents or the user).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
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

import yaml

# This script is often run directly from a user's data-root compatibility
# layout. Importing a validator must not create __pycache__ entries there,
# especially on help/error paths that promise zero mutation.
sys.dont_write_bytecode = True
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config_schema import (
    ConfigValidationError,
    load_council_config,
    load_mcp_manifest,
    validate_claude_settings,
)  # noqa: E402

IS_WINDOWS = platform.system() == "Windows"
HOST_MUTATIONS_DISABLED_ENV = "NEXGEN_DISABLE_HOST_MUTATIONS"
WINDOWS_CMD_ENV_LIMIT = 8191


def _host_mutations_disabled(env: "Env", operation: str) -> bool:
    """Keep tests and dry-run harnesses away from machine-wide Windows state.

    HOME/USERPROFILE overrides redirect normal file writes, but they do not
    virtualize HKCU or Task Scheduler. Every sandboxed integration process
    must therefore cross this explicit boundary before touching either one.
    """
    value = os.environ.get(HOST_MUTATIONS_DISABLED_ENV, "").strip().lower()
    if value not in {"1", "true", "yes", "on"}:
        return False
    env.log(f"host-mutations: {operation} skipped ({HOST_MUTATIONS_DISABLED_ENV}=1)")
    return True


def _opencode_config_path(home: Path) -> Path:
    """Return OpenCode's native config path without using Unix .config on Windows."""
    if not IS_WINDOWS:
        return home / ".config" / "opencode" / "opencode.json"
    appdata_path = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming")) / "opencode" / "opencode.json"
    legacy_path = home / ".config" / "opencode" / "opencode.json"
    return legacy_path if legacy_path.exists() and not appdata_path.exists() else appdata_path

HELP_TEXT = """agent_sync modes:
  pull     Pull the KnowledgeVault from the remote and run healthcheck. Does not rewrite CLI runtime files.
  guard    Recurring safe propagation: pull, regenerate CLI runtime files, run healthcheck. Does not push.
  apply    Same as guard, explicit manual name for provisioning.
  publish  Push already-committed local vault changes to the authoritative remote, then configured mirrors.
  preflight  Validate every configuration input used by apply. Does not regenerate runtime files.
  doctor   Run healthcheck/alerts only.
  bootstrap-alerts  Provision optional alert credentials and run healthcheck.
  config FIELD  Print resolved sync data. FIELD is authoritative_remote or mirrors.
  vault-push -m MSG [file ...]  Commit (+ stage given files) and publish the
    vault's infra files to the authoritative remote, then its mirrors. See
    docs/sync-contract.md and vault-write-architecture.md.

Default without arguments: help only, no writes.
The recurring timer/scheduled task should use: agent_sync.py guard
Use --allow-offline only with a deliberate manual apply when the authoritative
remote is temporarily unreachable and the local tracked tree is known-good.
"""

MODES = {
    "pull":    dict(pull=True,  apply=False, push=False, creds=False, health=True),
    "guard":   dict(pull=True,  apply=True,  push=False, creds=False, health=True),
    "apply":   dict(pull=True,  apply=True,  push=False, creds=False, health=True),
    "publish": dict(pull=False, apply=False, push=True,  creds=False, health=False),
    "preflight": dict(pull=False, apply=False, push=False, creds=False, health=False),
    "doctor":  dict(pull=False, apply=False, push=False, creds=False, health=True),
    "bootstrap-alerts": dict(pull=False, apply=False, push=False, creds=True, health=True),
}


class RemoteConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RemoteConfig:
    authoritative_remote: str
    mirrors: tuple[str, ...]
    source: str


_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _validate_remote_name(value: str, source: str) -> str:
    value = value.strip()
    if not value or not _REMOTE_NAME_RE.fullmatch(value):
        raise RemoteConfigError(
            f"remote config {source}: invalid Git remote name {value!r}"
        )
    return value


def _parse_mirrors(value: object, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = [item.strip() for item in value if item.strip()]
    else:
        raise RemoteConfigError(f"remote config {source}: mirrors must be a list of strings")
    return tuple(dict.fromkeys(_validate_remote_name(item, source) for item in items))


def _parse_mirrors_lenient(value: object, source: str) -> tuple[str, ...]:
    """Same shape as _parse_mirrors, for the KNOWLEDGE_VAULT_MIRRORS
    emergency/bootstrap override only: one malformed entry is skipped with
    a warning instead of failing the whole config load. The data-owned
    remotes.yaml path keeps using the strict _parse_mirrors above -- a typo
    there is a real config bug worth stopping on. An ad-hoc env var typed
    by hand during an actual emergency must never brick the authoritative
    push over one bad mirror name (old vault-push.sh's behavior, restored
    here)."""
    if value is None:
        return ()
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        items = [item.strip() for item in value if item.strip()]
    else:
        print(f"vault-push: {source} mirrors must be a list of strings; ignoring KNOWLEDGE_VAULT_MIRRORS", file=sys.stderr)
        return ()
    valid: list[str] = []
    for item in items:
        try:
            valid.append(_validate_remote_name(item, source))
        except RemoteConfigError as exc:
            print(f"vault-push: {exc} -- skipping this mirror", file=sys.stderr)
    return tuple(dict.fromkeys(valid))


def load_remote_config(*, home: Path | None = None, vault_data: Path | None = None) -> RemoteConfig:
    """Resolve the authoritative Vault remote before any runtime mutation.

    A complete environment override is the emergency/bootstrap lane. Normal
    operation reads one data-owned YAML file shared by sync, doctor and
    publish. Missing data keeps the portable product default, origin.
    """
    env_remote = os.environ.get("KNOWLEDGE_VAULT_REMOTE", "").strip()
    if env_remote:
        env_remote = _validate_remote_name(env_remote, "environment")
        mirrors = _parse_mirrors_lenient(os.environ.get("KNOWLEDGE_VAULT_MIRRORS"), "environment")
        mirrors = tuple(item for item in mirrors if item != env_remote)
        return RemoteConfig(env_remote, mirrors, "environment")

    home = home or Path.home()
    vault_data = vault_data or Path(os.environ.get("AGENT_VAULT_DATA") or os.environ.get("KNOWLEDGE_VAULT_PATH") or str(home / "KnowledgeVault"))
    path = Path(os.environ.get("AGENT_SYNC_REMOTES_FILE") or str(vault_data / "03-INFRA" / "agent-universal-layer" / "sync" / "remotes.yaml"))
    if not path.exists():
        return RemoteConfig("origin", (), "default")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RemoteConfigError(f"remote config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RemoteConfigError(f"remote config {path}: root must be a mapping")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise RemoteConfigError(f"remote config {path}: schema_version must be 1")
    remote = data.get("authoritative_remote")
    if not isinstance(remote, str) or not remote.strip():
        raise RemoteConfigError(f"remote config {path}: authoritative_remote must be a non-empty string")
    remote = _validate_remote_name(remote, str(path))
    mirrors = tuple(item for item in _parse_mirrors(data.get("mirrors"), str(path)) if item != remote)
    return RemoteConfig(remote, mirrors, str(path))


class Env:
    """Resolves every path/env var once. Path.home() honors $HOME on POSIX
    and %USERPROFILE% on Windows, so the same code runs unmodified in the
    B1 sandbox tests on either OS (see tests/conftest.py)."""

    def __init__(self) -> None:
        self.home = Path.home()
        self.vault = Path(os.environ.get("KNOWLEDGE_VAULT_PATH") or str(self.home / "KnowledgeVault"))
        self.branch = os.environ.get("KNOWLEDGE_VAULT_BRANCH") or "main"
        # Engine/data separation (Vault 2.1, Strangler Fig): defaults reproduce
        # the historical single-tree layout exactly, zero breakage.
        self.vault_data = Path(os.environ.get("AGENT_VAULT_DATA") or str(self.vault))
        remote_config = load_remote_config(home=self.home, vault_data=self.vault_data)
        self.remote = remote_config.authoritative_remote
        self.mirrors = remote_config.mirrors
        self.remote_config_source = remote_config.source
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
        # `skills` is the intentionally tiny discovery view. The complete
        # library lives next to it, outside eager runtime discovery roots.
        self.active_skills = self.home / ".agents" / "skills"
        self.skill_library = self.home / ".agents" / "skill-library"
        self.log_dir = self.home / ".local" / "state"
        self.log_path = self.log_dir / "agent-sync.log"
        self.lock_path = self.log_dir / "agent-sync.lock"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # Skill roots may still be broken whole-root links from a previous
        # eager layout. `vault_skills()` normalizes them before creating
        # anything, so do not call mkdir here and turn that recoverable state
        # into a FileExistsError.

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


def _cmd_escape(value: Path | str) -> str:
    """Escape a path passed as an argv item to ``cmd.exe``.

    ``subprocess`` quotes argv items containing whitespace before handing
    them to cmd.exe.  Inside those quotes, caret-escaping ``&`` would become
    part of the literal path, so only escape metacharacters on unquoted
    paths.  This keeps both ``source&folder`` and ``source & folder`` safe.
    """
    text = str(value)
    if any(char.isspace() for char in text):
        return text
    return re.sub(r"([&|<>^])", lambda match: "^" + match.group(1), text)


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
        r = _run_external(["cmd.exe", "/d", "/c", "mklink", "/J",
                           _cmd_escape(dst), _cmd_escape(src)],
                           timeout=15, capture_output=True, text=True)
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
    """Detect a live CLI, including npm-installed ``node.exe`` wrappers."""
    try:
        if not IS_WINDOWS:
            r = _run_external(["pgrep", "-x", name], timeout=15, capture_output=True)
            return r.returncode == 0
        powershell_query = (
            "(Get-CimInstance Win32_Process -Filter \"Name = 'node.exe'\").CommandLine"
        )
        for probe in (
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", powershell_query],
            ["wmic.exe", "process", "where", "name='node.exe'", "get", "CommandLine"],
        ):
            try:
                r = _run_external(probe, timeout=15, capture_output=True, text=True)
            except OSError:
                continue
            if r.returncode == 0 and name.casefold() in (r.stdout or "").casefold():
                return True
        r = _run_external(["tasklist", "/FI", f"IMAGENAME eq {name}.exe"], timeout=15, capture_output=True, text=True)
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
    delay = 0.05
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(delay)
            delay *= 2


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


def _run_python_script(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    """subprocess.run for a Python helper script (render.py, skills-sync.py)
    invoked from phases that run inside the host-wide sync lock: a hang here
    must never hold that lock forever (the risk `_git`'s own timeout= above
    already guards against for git itself). TimeoutExpired is caught and
    turned into a synthetic non-zero CompletedProcess, matching `_git`'s own
    pattern, so every call site's existing "non-zero exit code -> best-
    effort, continue" handling covers a timeout too -- without this, the
    exception would propagate out of a for-loop mid-iteration (mcp_render
    renders 4 CLIs in one loop) and silently skip whatever the loop had
    left to do, not just the one CLI that actually hung."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        return subprocess.CompletedProcess(args, 1, stdout, f"timed out after {timeout}s")


def _run_external(args: list[str], *, timeout: int, **kw) -> subprocess.CompletedProcess:
    """subprocess.run for a short-lived external tool (mklink, pgrep,
    tasklist, systemctl, schtasks.exe, notify-send) invoked from phases that
    run inside the host-wide sync lock: same TimeoutExpired-swallowing
    pattern as _run_python_script above, so a hung external command degrades
    to a non-zero CompletedProcess (every call site already treats rc!=0 as
    "best-effort, log, continue") instead of holding the lock -- or the
    whole run -- forever."""
    try:
        return subprocess.run(args, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        return subprocess.CompletedProcess(args, 1, stdout, f"timed out after {timeout}s")


class SyncRunLock:
    """Small standard-library cross-platform lock for the whole sync run."""

    def __init__(self, path: Path, *, timeout: float = 2.0) -> None:
        self.path = path
        self.timeout = max(0.0, timeout)
        self.acquired = False
        self._fh = None

    def __enter__(self) -> "SyncRunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            # Best-effort seed byte. On Windows another process may already
            # hold an msvcrt byte-range lock on byte 0 of a still-empty file
            # (exactly the "lock is busy" case): the write/flush then raises
            # PermissionError, and letting it propagate turned a clean
            # exit-75 "busy" into an uncaught crash. Swallow it -- the lock
            # loop below re-detects the contention and returns not-acquired.
            try:
                self._fh.write(b"0")
                self._fh.flush()
            except OSError:
                pass
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fh.seek(0)
                if IS_WINDOWS:
                    import msvcrt
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    return self
                time.sleep(0.1)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is None:
            return
        try:
            if self.acquired:
                self._fh.seek(0)
                if IS_WINDOWS:
                    import msvcrt
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            # close() re-flushes any buffered seed byte; on Windows that can
            # re-raise the same PermissionError the busy-lock path already
            # tolerated in __enter__. A close failure when we never acquired
            # the lock is harmless -- don't let it clobber the caller's
            # exit-75 return with a crash.
            try:
                self._fh.close()
            except OSError:
                pass


class PullState(Enum):
    FRESH = "fresh"
    LOCAL_ONLY = "local_only"
    WRONG_BRANCH = "wrong_branch"
    DIRTY = "dirty"
    REMOTE_MISSING = "remote_missing"
    FETCH_FAILED = "fetch_failed"
    AHEAD = "ahead"
    DIVERGED = "diverged"
    ERROR = "error"


@dataclass(frozen=True)
class PullOutcome:
    state: PullState
    message: str

    @property
    def allows_apply(self) -> bool:
        return self.state in {PullState.FRESH, PullState.LOCAL_ONLY}


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


def data_migrations(env: Env) -> bool:
    schema_file = env.vault_data / "99-INDEX" / "DATA-SCHEMA-VERSION.txt"
    if schema_file.is_file():
        try:
            current = int(schema_file.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            env.log(f"data-migrations: {schema_file} has non-numeric content, leaving data untouched")
            return False
    else:
        # No marker yet: today's data shape already IS the target version,
        # there is nothing to migrate -- just stamp the baseline.
        current = TARGET_SCHEMA_VERSION
        if _write_if_different(schema_file, f"{current}\n"):
            env.log(f"data-migrations: stamped {schema_file} at v{current}")

    if current > TARGET_SCHEMA_VERSION:
        env.log(f"data-migrations: data schema v{current} is newer than this engine supports (v{TARGET_SCHEMA_VERSION}) -- leaving data untouched, upgrade the engine")
        return False

    while current < TARGET_SCHEMA_VERSION:
        step = MIGRATIONS.get(current)
        if step is None:
            env.log(f"data-migrations: no migration registered for v{current} -> v{current + 1}, stopping (data left at v{current})")
            return False
        touched = step(env)
        # Stamped right after THIS step succeeds, not only once the whole
        # chain reaches TARGET_SCHEMA_VERSION: a crash partway through a
        # multi-step chain must not force a retry to redo an already-applied
        # (and possibly non-idempotent) earlier step (finding 22).
        current += 1
        if _write_if_different(schema_file, f"{current}\n"):
            env.log(f"data-migrations: stamped {schema_file} at v{current}")
        env.log(f"data-migrations: applied v{current - 1} -> v{current}, touched {touched}")

    return True


def preflight(env: Env) -> bool:
    """Reject invalid data before this run changes a generated runtime file.

    The remote/host declaration is validated while ``Env`` is constructed.
    This phase covers the remaining data inputs used by apply: MCP, optional
    Council seats, skills, and the Claude hooks section that we may merge.
    """
    manifest_path = env.instance_ul / "mcp" / "manifest.yaml"
    council_path = env.instance_ul / "council" / "seats.yaml"
    settings_path = env.home / ".claude" / "settings.json"
    try:
        load_mcp_manifest(manifest_path)
        if council_path.exists():
            load_council_config(council_path)
        validate_claude_settings(settings_path)
    except ConfigValidationError as exc:
        env.log(f"preflight: BLOCKED ({exc})")
        return False

    skills_sync = env.engine_scripts / "skills-sync.py"
    if not skills_sync.is_file():
        env.log(f"preflight: missing skills validator {skills_sync}")
        return False
    result = _run_python_script([sys.executable, str(skills_sync), "--validate"])
    _append_log(env, result.stdout, result.stderr)
    if result.returncode != 0:
        env.log("preflight: skills manifest or local source is invalid")
        return False

    env.log("preflight: MCP, Council, skills, Claude settings, and host remote config are valid")
    return True


# ── 1. pull ──────────────────────────────────────────────────────────────

def pull(env: Env) -> PullOutcome:
    if env.remote in ("local", "none"):
        env.log("pull: skipped (Local-Only mode)")
        return PullOutcome(PullState.LOCAL_ONLY, "Local-Only mode")
    if _git(env, "remote", "get-url", env.remote).returncode != 0:
        message = f"no authoritative remote '{env.remote}' configured"
        env.log(f"pull: blocked ({message})")
        return PullOutcome(PullState.REMOTE_MISSING, message)
    current = _git(env, "symbolic-ref", "--quiet", "--short", "HEAD")
    if current.returncode != 0 or current.stdout.strip() != env.branch:
        found = current.stdout.strip() or "detached HEAD"
        message = f"current branch is {found}, expected {env.branch}"
        env.log(f"pull: blocked ({message})")
        return PullOutcome(PullState.WRONG_BRANCH, message)
    status = _git(env, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        message = "cannot inspect tracked working-tree state"
        env.log(f"pull: blocked ({message})")
        return PullOutcome(PullState.ERROR, message)
    if status.stdout.strip():
        message = "the vault has uncommitted tracked changes"
        env.log(f"pull: blocked ({message}; untracked files do not block)")
        return PullOutcome(PullState.DIRTY, message)
    if _git(env, "fetch", "--prune", env.remote, env.branch).returncode != 0:
        message = f"fetch of {env.remote}/{env.branch} failed"
        env.log(f"pull: blocked ({message})")
        return PullOutcome(PullState.FETCH_FAILED, message)
    lh = _git(env, "rev-parse", env.branch)
    rh = _git(env, "rev-parse", f"{env.remote}/{env.branch}")
    mb = _git(env, "merge-base", env.branch, f"{env.remote}/{env.branch}")
    if lh.returncode or rh.returncode or mb.returncode:
        message = f"cannot compare local branch with {env.remote}/{env.branch}"
        env.log(f"pull: blocked ({message})")
        return PullOutcome(PullState.ERROR, message)
    lh, rh, mb = lh.stdout.strip(), rh.stdout.strip(), mb.stdout.strip()
    if lh == rh:
        env.log("pull: already up to date")
        return PullOutcome(PullState.FRESH, "already up to date")
    elif mb == lh:
        if _git(env, "merge", "--ff-only", f"{env.remote}/{env.branch}").returncode == 0:
            env.log(f"pull: fast-forwarded from {env.remote}/{env.branch}")
            return PullOutcome(PullState.FRESH, "fast-forwarded")
        else:
            message = f"fast-forward from {env.remote}/{env.branch} failed"
            env.log(f"pull: blocked ({message})")
            return PullOutcome(PullState.ERROR, message)
    elif mb == rh:
        message = f"local branch is ahead of {env.remote}/{env.branch}"
        env.log(f"pull: blocked ({message})")
        return PullOutcome(PullState.AHEAD, message)
    else:
        message = f"local branch diverged from {env.remote}/{env.branch}"
        env.log(f"pull: blocked ({message}; manual resolution required)")
        return PullOutcome(PullState.DIVERGED, message)


# ── 2. instructions ──────────────────────────────────────────────────────
# NOTE (B2.5 reconciliation, see the launch report): agent-sync.ps1 still
# actively re-links ~/ANTIGRAVITY.md, but agent-sync.sh's own comment records
# a verified behavioral probe: Antigravity never reads that file, it was
# dead wiring copied from the Codex pattern. That fact isn't OS-dependent,
# so the fix (stop managing it, clean up the leftover symlink) is ported
# uniformly instead of kept Windows-only.

def instructions(env: Env) -> bool:
    canon = env.instance_ul / "instructions" / "AGENTS.md"
    if not canon.is_file():
        env.log(f"WARNING: missing {canon} — instructions not relinked")
        return False
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

    _sync_opencode_instructions(env, canon)
    return True


def _sync_opencode_instructions(env: Env, canon: Path) -> None:
    # OpenCode has no separate pointer/symlink mechanism like Claude/Gemini/
    # Codex above: the canonical bootstrap path is an entry in opencode.json's
    # own top-level "instructions" array (confirmed against a real working
    # config, not guessed). Was previously never written by this provisioner
    # at all -- a fresh install left OpenCode with no bootstrap pointer, and
    # agent-doctor's "OpenCode instructions -> AGENTS.md" check failed
    # permanently with no code path that could ever fix it.
    oc_path = _opencode_config_path(env.home)
    if not oc_path.is_file():
        env.log("instructions: opencode.json not present (OpenCode never launched yet) -- skipping")
        return
    try:
        config = json.loads(oc_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        env.log("instructions: opencode.json not valid JSON; skipping instructions merge")
        return
    if not isinstance(config, dict):
        env.log("instructions: opencode.json root is not an object; skipping instructions merge")
        return
    try:
        canon_entry = "~/" + str(canon.relative_to(env.home))
    except ValueError:
        canon_entry = str(canon)
    entries = config.setdefault("instructions", [])
    if not isinstance(entries, list):
        env.log("instructions: opencode.json 'instructions' is not a list; skipping instructions merge")
        return
    if canon_entry in entries or str(canon) in entries:
        return
    entries.append(canon_entry)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(oc_path, oc_path.with_name(f"opencode.json.pre-instructions-{stamp}.bak"))
    _atomic_write_text(oc_path, json.dumps(config, indent=2) + "\n")
    env.log(f"instructions: added canonical AGENTS.md to opencode.json instructions ({oc_path})")


# ── 2.5 antigravity_mcp ──────────────────────────────────────────────────
# Distributes the ONE file mcp_render (below) generates from the manifest to
# Antigravity's other config paths. Not a second generator: render.py is the
# single source of truth, this section is pure fan-out via symlink/junction.

def antigravity_mcp(env: Env) -> bool:
    src = env.home / ".gemini" / "antigravity" / "mcp_config.json"
    if not src.is_file():
        return True
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
    return True


# ── 2.7 utils ────────────────────────────────────────────────────────────
# LINKED_COMMANDS is the single source for every bare command utils() puts
# on PATH -- both the POSIX (symlink onto a *.sh twin) and Windows (relink a
# *.ps1 twin + write a *.cmd wrapper) branches below consume the SAME dict
# instead of each carrying their own hardcoded list. Real bug history this
# closes (2026-07-13 review, four separate commits over the same root
# cause): agent-sync, agent-doctor, vault-groom and firecrawl-local were all
# documented everywhere as bare commands while nothing ever actually linked
# them -- a hardcoded list in one branch is exactly the kind of place a new
# command silently falls through the cracks of.
#   source:   'engine' -> env.engine_scripts, 'vault' -> env.vault_scripts.
#   posix/windows: whether a same-named <name>.sh / <name>.ps1 twin ships
#     and should be linked on that OS (vault-ocr-local remains POSIX-only by
#     design, while firecrawl-local ships a native .ps1 twin).
#   optional: bring-your-own -- absence of the source is the documented
#     default, not a failure (see _link_util below).
# agent-skill is deliberately NOT here: utils() generates its wrapper from a
# template at write time (there is no agent-skill.sh/.ps1 twin to symlink),
# a different enough shape that folding it into this table would obscure
# rather than simplify it.
LINKED_COMMANDS: dict[str, dict[str, object]] = {
    "agent-sync":      {"source": "engine", "posix": True,  "windows": True},
    "agent-doctor":    {"source": "engine", "posix": True,  "windows": True},
    "agent-now":       {"source": "engine", "posix": True,  "windows": True},
    "council":         {"source": "engine", "posix": True,  "windows": True},
    "firecrawl-local": {"source": "engine", "posix": True,  "windows": True},
    "vault-push":      {"source": "vault",  "posix": True,  "windows": True},
    "vault-groom":     {"source": "vault",  "posix": True,  "windows": True},
    "vault-ocr-local": {"source": "vault",  "posix": True,  "windows": False, "optional": True},
}


def _linked_command_src_dir(env: Env, source: str) -> Path:
    return env.engine_scripts if source == "engine" else env.vault_scripts


def _link_util(src: Path, dst: Path, env: Env, label: str, *, optional: bool = False) -> bool:
    if not src.is_file():
        if optional:
            # Documented as bring-your-own, same as local-model-agent.ps1
            # (LOCAL-WORKER.md) and the semantic-search backend (README):
            # vault-ocr-local.sh is referenced by AGENTS.md/vault-ocr.md but
            # never actually shipped in 03-INFRA/scripts -- verified absent
            # from `git ls-files`, not a sandbox/test-fixture artifact.
            # Absence here is the documented default, not a failure.
            env.log(f"utils: missing source {src} (optional, bring-your-own)")
            return True
        # A missing REQUIRED source means the engine checkout itself is
        # incomplete -- a real problem, not a benign not-applicable case.
        env.log(f"utils: missing source {src}")
        return False
    if not IS_WINDOWS and not (src.stat().st_mode & 0o111):
        env.log(f"utils: source {src} is not executable, refusing to mutate an engine source")
        return False
    try:
        same = dst.is_symlink() and dst.resolve() == src.resolve()
    except OSError:
        same = False
    if same:
        return True
    make_link(src, dst, is_dir=False)
    env.log(f"utils: relinked {label}")
    return True


def utils(env: Env) -> bool:
    env.local_bin.mkdir(parents=True, exist_ok=True)
    skill_source = env.engine_scripts / "agent-skill.py"
    if not IS_WINDOWS:
        healthy = True
        # agent-sync/agent-doctor themselves, not just the tools they manage:
        # utils() is a phase THIS SAME agent-sync run executes, so the first
        # invocation ever has to happen by full path (INIT.md already
        # documents that correctly) -- but nothing then created the bare
        # command for every run after. Two concrete real-world consequences,
        # not just tidiness: _install_systemd_units() (below) writes the
        # recurring guard timer's ExecStart as bare '.local/bin/agent-sync
        # guard', which pointed at a symlink no code path ever created; and
        # _persisted_engine_root() reads that same symlink to detect an
        # existing engine-root cutover, silently dead for anyone who never
        # got a working one.
        for name, cfg in LINKED_COMMANDS.items():
            if not cfg["posix"]:
                continue
            src_dir = _linked_command_src_dir(env, cfg["source"])
            src = src_dir / f"{name}.sh"
            healthy = _link_util(src, env.local_bin / name, env, name, optional=bool(cfg.get("optional"))) and healthy
        if skill_source.is_file():
            wrapper = f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(skill_source))} \"$@\"\n"
            target = env.local_bin / "agent-skill"
            if _write_if_different(target, wrapper):
                env.log("utils: installed agent-skill")
            target.chmod(0o755)
        else:
            env.log(f"utils: missing source {skill_source}")
            healthy = False
        return healthy
    healthy = True
    for name, cfg in LINKED_COMMANDS.items():
        if not cfg["windows"]:
            continue
        src_dir = _linked_command_src_dir(env, cfg["source"])
        src = src_dir / f"{name}.ps1"
        if not src.is_file():
            env.log(f"utils: missing source {src}")
            healthy = False
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
    if skill_source.is_file():
        wrapper = (
            "@echo off\r\n"
            f"\"{sys.executable}\" \"{skill_source}\" %*\r\n"
        )
        if _write_if_different(env.local_bin / "agent-skill.cmd", wrapper):
            env.log("utils: installed agent-skill.cmd")
    else:
        env.log(f"utils: missing source {skill_source}")
        healthy = False
    # Registry-only PATH fix (release-critical: without this, every bare
    # command above resolves only in a terminal that already had
    # ~/.local/bin on PATH from some other source). Best-effort: a registry
    # failure is loud in the log but never flips this phase to failed --
    # the wrappers themselves were still written correctly, and a future
    # doctor check surfaces a PATH that's still missing them.
    _ensure_user_path_entry(env)
    return healthy


def _ensure_user_path_entry(env: Env) -> None:
    """Adds env.local_bin to HKCU\\Environment's Path so bare commands (not
    just full-path invocations) work in a NEW terminal. Windows has no
    always-sourced profile equivalent to POSIX's typical ~/.local/bin
    PATH entry -- without this, every wrapper utils() just wrote is
    reachable only by full path, forever, on a fresh install. Registry
    only: a running terminal's own os.environ is a snapshot from when it
    started and cannot be fixed retroactively, same as a manual `setx`."""
    if not IS_WINDOWS:
        return
    if _host_mutations_disabled(env, "user PATH registry update"):
        return
    try:
        import winreg
    except ImportError as exc:
        env.log(f"path: WARNING -- winreg unavailable ({exc}); add {env.local_bin} to PATH manually")
        return
    target = str(env.local_bin)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                             winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, kind = "", winreg.REG_EXPAND_SZ
            if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                kind = winreg.REG_EXPAND_SZ
            entries = [e for e in current.split(";") if e.strip()]
            # Case-insensitive, trailing-slash-tolerant: Windows paths are
            # case-insensitive and a prior run (or the user by hand) may
            # have added the same folder with a trailing backslash.
            normalized = {e.strip().rstrip("\\/").lower() for e in entries}
            if target.rstrip("\\/").lower() in normalized:
                env.log(f"path: {target} already on user PATH")
                return
            entries.append(target)
            new_value = ";".join(entries)
            current_process_path = os.environ.get("PATH", "")
            projected_process_length = len(current_process_path) + max(0, len(new_value) - len(current))
            if max(len(new_value), projected_process_length) > WINDOWS_CMD_ENV_LIMIT:
                env.log(
                    "path: WARNING -- refusing to append the launcher directory: "
                    f"the resulting User PATH would be {len(new_value)} characters and the "
                    f"projected process PATH {projected_process_length}, "
                    f"over cmd.exe's {WINDOWS_CMD_ENV_LIMIT}-character inherited-variable limit. "
                    f"Shorten PATH or invoke {target} by absolute path."
                )
                return
            winreg.SetValueEx(key, "Path", 0, kind, new_value)
    except OSError as exc:
        # Loud in the log, not a failed phase (see utils()'s call site): the
        # wrappers this run wrote are still correct, only the PATH entry
        # didn't happen -- a later doctor check can surface and retry it.
        env.log(f"path: WARNING -- could not update user PATH via registry ({exc}); add {target} to PATH manually")
        return
    env.log(f"path: added {target} to user PATH -- open a new terminal for bare commands to work")
    _broadcast_environment_change()


def _broadcast_environment_change() -> None:
    """Tells already-open top-level windows (Explorer, etc.) that the
    environment changed, matching what `setx`/System Properties does after
    an Environment Variables edit. Best-effort only: a NEW terminal already
    picks up the registry value on its own by re-reading HKCU\\Environment
    at process start, so a failure here just means already-open windows
    stay stale a little longer, never a correctness problem."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_long()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
        )
    except Exception:
        pass


def local_model_runtime(env: Env) -> bool:
    if not IS_WINDOWS:
        return True
    src = env.engine_scripts / "local-model-agent.ps1"
    if not src.is_file():
        # Not shipped in the public engine by design (bring-your-own, see
        # LOCAL-WORKER.md) -- absence is the expected default for most
        # installs, not a failure to surface as a red exit code.
        env.log(f"local-model: missing source {src} (optional, bring-your-own)")
        return True
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
    return True


# ── 2.75 scheduler ───────────────────────────────────────────────────────
# Self-healing recurring trigger on EVERY apply/guard, on both OSes: the
# opt-in-only Windows switch (-InstallScheduledTask) was the gap this section
# closes -- install_scheduler() runs as a normal per-run step, not an
# opt-in one.

def _systemd_env_line(key: str, value: str) -> str:
    """Quotes the whole assignment per systemd.syntax(7): unquoted
    Environment= values split on whitespace, so a path with a space (e.g.
    '/opt/agents/nexgen engine') silently truncates the variable instead of
    failing loud. Backslash and double-quote are C-escaped, matching
    systemd's own quoted-string escaping."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{key}={escaped}"'


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
        lines.append(_systemd_env_line("AGENT_ENGINE_ROOT", str(env.engine_root)))
    # env.vault_data (not the raw env var): a bare run with AGENT_VAULT_DATA
    # unset must not erase an already-persisted cutover the same way a bare
    # AGENT_ENGINE_ROOT-less run used to silently revert engine_root above.
    if env.vault_data.resolve() != env.vault.resolve():
        lines.append(_systemd_env_line("AGENT_VAULT_DATA", str(env.vault_data)))
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


def _install_systemd_units(env: Env) -> bool:
    unit_dir = env.home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    changed = False
    healthy = True
    # Defense in depth, not the primary fix: utils() (which links the bare
    # agent-sync command this timer's ExecStart depends on) always runs
    # before this function in the same apply/guard phases list, so this
    # should never actually fire. It exists for the one edge case where
    # utils() partially fails on an unrelated required link and the phase
    # loop (which does not abort on a single phase's failure) still reaches
    # this one in the same pass -- silent instead of a loud, findable log
    # line otherwise.
    if not (env.local_bin / "agent-sync").exists():
        warning = (
            "systemd: WARNING -- ~/.local/bin/agent-sync does not exist yet; "
            "the timer's ExecStart references it anyway and will fail until "
            "utils() successfully links it on a future guard run"
        )
        env.log(warning)
        # Also stderr, not just the log file: this is defense-in-depth for a
        # case that should never fire (utils() always runs first in the same
        # apply/guard pass) -- if it ever does, it needs to be humanly
        # visible during an interactive apply, not only discoverable by
        # someone who thinks to open agent-sync.log.
        print(warning, file=sys.stderr)
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
    if not resolve_cmd("systemctl"):
        env.log("systemd: systemctl not found -- unit files written but not enabled")
        return healthy
    if changed:
        r = _run_external(["systemctl", "--user", "daemon-reload"], timeout=30, capture_output=True, text=True)
        if r.returncode != 0:
            env.log(f"systemd: user daemon-reload failed: {(r.stderr or r.stdout).strip()}")
            healthy = False
    # Unconditional, not gated on `changed`: writing the unit files was never
    # enough on its own -- systemd requires an explicit `enable` to create
    # the timers.target.wants/ symlink that actually makes the timer fire.
    # This call was missing entirely before (beta-readiness review,
    # 2026-07-13): a fresh install wrote inert unit files that never ran
    # unless a human happened to `systemctl --user enable` them by hand.
    # --now also starts it immediately rather than waiting for next login.
    r = _run_external(["systemctl", "--user", "enable", "--now", "agent-sync.timer"],
                       timeout=30, capture_output=True, text=True)
    if r.returncode != 0:
        env.log(
            "systemd: could not enable agent-sync.timer "
            f"({(r.stderr or r.stdout).strip()}) -- the recurring guard will not run. "
            "On a headless/SSH-only box this is often a missing "
            "`loginctl enable-linger $USER` (lets --user units run without an active login session)."
        )
        healthy = False
    return healthy


_VBS_TEMPLATE = (
    'Set shell = CreateObject("WScript.Shell")\r\n'
    'Set processEnv = shell.Environment("PROCESS")\r\n'
    'processEnv("AGENT_ENGINE_ROOT") = "{engine_root}"\r\n'
    'processEnv("AGENT_VAULT_DATA") = "{vault_data}"\r\n'
    'processEnv("KNOWLEDGE_VAULT_PATH") = "{vault}"\r\n'
    'processEnv("KNOWLEDGE_VAULT_BRANCH") = "{branch}"\r\n'
    'script = "{script}"\r\n'
    'shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & script & Chr(34) '
    '& " guard", 0, True\r\n'
)


def _install_scheduled_task(env: Env) -> bool:
    if _host_mutations_disabled(env, "Task Scheduler update"):
        return True
    task_name = "KnowledgeVault Agent Sync"
    script_path = env.engine_scripts / "agent-sync.ps1"
    # Generated, machine-specific state must never dirty the public engine
    # checkout or risk being staged into a release.
    wrapper_path = env.log_dir / "start-agent-sync-hidden.vbs"
    def vbs_string(value: Path | str) -> str:
        return str(value).replace('"', '""')

    content = _VBS_TEMPLATE.format(
        script=vbs_string(script_path),
        engine_root=vbs_string(env.engine_root),
        vault_data=vbs_string(env.vault_data),
        vault=vbs_string(env.vault),
        branch=vbs_string(env.branch),
    )
    if _write_if_different(wrapper_path, content):
        env.log("scheduled-task: hidden wrapper updated")
    run_cmd = f'wscript.exe "{wrapper_path}"'
    every30 = ["schtasks.exe", "/Create", "/TN", task_name, "/SC", "MINUTE", "/MO", "30", "/TR", run_cmd, "/F"]
    logon = ["schtasks.exe", "/Create", "/TN", f"{task_name} Logon", "/SC", "ONLOGON", "/TR", run_cmd, "/F"]
    r = _run_external(every30, timeout=60, capture_output=True, text=True)
    if r.returncode != 0:
        env.log(f"scheduled-task: schtasks.exe failed for '{task_name}': {r.stdout}{r.stderr}")
        # The every-30-minutes task IS the recurring guard; without it there
        # is no self-healing trigger at all, unlike the logon trigger below
        # (a redundant nicety the every-30 task already covers within 30min).
        return False
    env.log(f"scheduled-task: installed/updated '{task_name}' via schtasks.exe")
    r = _run_external(logon, timeout=60, capture_output=True, text=True)
    if r.returncode == 0:
        env.log(f"scheduled-task: installed/updated '{task_name} Logon' via schtasks.exe")
        return True
    env.log(f"scheduled-task: logon trigger failed via schtasks.exe ({r.stdout}{r.stderr})")
    startup_dir = os.environ.get("APPDATA")
    if startup_dir:
        startup_vbs = Path(startup_dir) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "KnowledgeVault Agent Sync.vbs"
        startup_vbs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wrapper_path, startup_vbs)
        env.log(f"startup: installed hidden logon fallback {startup_vbs}")
    return True


def install_scheduler(env: Env) -> bool:
    if IS_WINDOWS:
        return _install_scheduled_task(env)
    return _install_systemd_units(env)


# ── 2.8 mcp_render ───────────────────────────────────────────────────────
# render.py is already cross-platform (proven by the 4-dialect B1 matrix):
# invoked as a subprocess with sys.executable (the SAME interpreter running
# agent_sync.py), so there is no python3-vs-python-vs-py resolution needed.

_SUMMARY_RE_MATCH = re.compile(r"match, (\d+) with differences")
_SUMMARY_RE_EXTRA = re.compile(r"differences, (\d+) outside the manifest")


def mcp_render(env: Env) -> bool:
    render_path = env.ul / "mcp" / "render.py"
    if not render_path.is_file():
        env.log(f"mcp-gen: missing renderer {render_path}")
        return False
    healthy = True
    for cli in ("opencode", "antigravity", "codex"):
        r = _run_python_script([sys.executable, str(render_path), "--write", cli])
        _append_log(env, r.stdout, r.stderr)
        if r.returncode == 0:
            env.log(f"mcp-gen: {cli} aligned with the manifest")
        elif r.returncode == 3:
            env.log(f"mcp-gen: {cli} has no default config file yet (never launched?) — open it once, then re-run agent-sync")
        else:
            env.log(f"mcp-gen: {cli} NOT aligned (best-effort, continuing)")
            healthy = False

    if _process_running("claude"):
        env.log("mcp-gen: claude ACTIVE -> not touching .claude.json live (sentinel only)")
    else:
        r = _run_python_script([sys.executable, str(render_path), "--write", "claude"])
        _append_log(env, r.stdout, r.stderr)
        if r.returncode == 0:
            env.log("mcp-gen: claude aligned (was closed)")
        elif r.returncode == 3:
            env.log("mcp-gen: claude has no .claude.json yet (never launched?) — open Claude Code once, then re-run agent-sync")
        else:
            env.log("mcp-gen: claude not aligned (best-effort)")
            healthy = False

    diag = _run_python_script([sys.executable, str(render_path)])
    if diag.returncode != 0:
        _append_log(env, diag.stdout, diag.stderr)
        env.log("mcp-gen: final drift diagnostic failed")
        return False
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
    return healthy and drift == 0


# ── 3. vault_skills ──────────────────────────────────────────────────────

def vault_skills(env: Env) -> bool:
    """Reserve the two local views; skills-sync materializes their contents.

    Directly linking every Vault skill into ~/.agents/skills was the eager
    discovery bug: Codex enumerated the entire library before any task began.
    The dedicated synchronizer now owns library materialization and exposure.
    """
    healthy = True
    for label, root in (("active skill view", env.active_skills), ("skill library", env.skill_library)):
        # A whole-root link was the original eager-discovery failure. Unlinking
        # the view is safe: it never removes the destination or any old bodies,
        # which the explicit legacy migration can quarantine afterwards.
        if root.is_symlink():
            _remove_path(root)
            root.mkdir(parents=True, exist_ok=True)
            env.log(f"skills: converted {label} from a whole-root link to a real directory")
        elif not root.exists():
            root.mkdir(parents=True, exist_ok=True)
        elif not root.is_dir():
            env.log(f"skills: {label} is not a directory, leaving it untouched for manual repair")
            healthy = False
    return healthy


# ── 3.5 skills_index ─────────────────────────────────────────────────────

def skills_index(env: Env) -> bool:
    skills_sync = env.engine_scripts / "skills-sync.py"
    if not skills_sync.is_file():
        env.log(f"skills-manifest: missing synchronizer {skills_sync}")
        return False
    r = _run_python_script([sys.executable, str(skills_sync), "--apply"])
    if r.returncode == 0:
        summary = next((ln for ln in r.stdout.splitlines() if "Total:" in ln), "")
        summary = re.sub(r"\x1b\[[0-9;]*m", "", summary).strip()
        env.log(f"skills-manifest: apply ok ({summary})")
    else:
        env.log("skills-manifest: apply failed (best-effort, detail in the manual diff)")
        return False
    return True


# ── 4. runtimes ──────────────────────────────────────────────────────────
# Runtime directory hygiene. skills-sync.py alone owns the per-skill views.
# Claude may point at the non-discovered library because its native loader is
# lazy. Codex must never point at the library or active shared root wholesale.

def runtimes(env: Env) -> bool:
    healthy = True
    for cli, rel in (("claude", ".claude/skills"), ("codex", ".codex/skills")):
        rt = env.home / Path(rel)
        points_to_library = _points_to(rt, env.skill_library)
        # Claude's native loader may see the non-discovered library as a whole.
        # Every other whole-root link, including a broken one, is unsafe:
        # normalize it before skills-sync creates any per-skill view.
        if _is_link_like(rt) and not (cli == "claude" and points_to_library):
            _remove_path(rt)
            rt.mkdir(parents=True, exist_ok=True)
            env.log(f"runtime: {rt} was an eager whole-library link — converted to a real folder")
        elif not rt.exists():
            rt.mkdir(parents=True, exist_ok=True)
        elif not rt.is_dir():
            env.log(f"runtime: {rt} is not a directory, leaving it untouched for manual repair")
            healthy = False
    return healthy


# ── 4.5 claude_hooks ─────────────────────────────────────────────────────
# Pure-Python JSON merge instead of shelling out to jq (a real external
# dependency agent-sync.sh silently no-ops without): same behavior, one
# fewer thing that has to be installed on either OS.

def claude_hooks(env: Env) -> bool:
    hook_src = env.ul / "hooks" / "claude-vault-checkpoint.mjs"
    claude_dir = env.home / ".claude"
    if not hook_src.is_file() or not claude_dir.is_dir():
        return True
    settings_path = claude_dir / "settings.json"
    try:
        validate_claude_settings(settings_path)
    except ConfigValidationError as exc:
        env.log(f"claude-hooks: settings preflight failed ({exc})")
        return False

    hook_dst = claude_dir / "claude-vault-checkpoint.mjs"
    src_bytes = hook_src.read_bytes()
    if not hook_dst.exists() or hook_dst.read_bytes() != src_bytes:
        hook_dst.write_bytes(src_bytes)
        env.log(f"claude-hooks: deployed {hook_dst}")

    if not settings_path.is_file():
        return True
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        env.log("claude-hooks: settings.json not valid JSON; skipping merge")
        return False
    if not isinstance(settings, dict):
        # Valid JSON but not an object (e.g. "[]") -- settings.setdefault
        # below would crash with AttributeError, aborting the rest of this
        # agent-sync run (publish/creds/health all run after this call in
        # main()). Found in a full-codebase audit, Gemini via agy, 2026-07-09.
        env.log("claude-hooks: settings.json root is not an object; skipping merge")
        return False

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
        return True
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(settings_path, settings_path.with_name(f"settings.json.pre-hooks-{stamp}.bak"))
    _atomic_write_text(settings_path, json.dumps(settings, indent=2) + "\n")
    env.log(f"claude-hooks: merged SessionStart/PreCompact into {settings_path}")
    return True


# ── 5. publish ───────────────────────────────────────────────────────────

def publish(env: Env) -> bool:
    if env.remote in ("local", "none"):
        env.log("push: skipped (Local-Only mode)")
        return True
    if _git(env, "remote", "get-url", env.remote).returncode != 0:
        env.log(f"push: authoritative remote {env.remote} is not configured")
        return False
    if _git(env, "fetch", "--prune", env.remote, env.branch).returncode != 0:
        env.log(f"push: {env.remote} unreachable — no publication attempted")
        return False

    lh = _git(env, "rev-parse", env.branch)
    rh = _git(env, "rev-parse", f"{env.remote}/{env.branch}")
    mb = _git(env, "merge-base", env.branch, f"{env.remote}/{env.branch}")
    if lh.returncode or rh.returncode or mb.returncode:
        env.log(f"push: cannot compare local branch with {env.remote}/{env.branch}")
        return False
    lh, rh, mb = lh.stdout.strip(), rh.stdout.strip(), mb.stdout.strip()

    if lh == rh:
        env.log(f"push: authoritative {env.remote}/{env.branch} already aligned")
    elif mb == rh:
        ahead_r = _git(env, "rev-list", "--count", f"{env.remote}/{env.branch}..{env.branch}")
        ahead = ahead_r.stdout.strip() or "?"
        if _git(env, "push", env.remote, env.branch).returncode != 0:
            env.log(f"push: authoritative publication to {env.remote} failed")
            return False
        env.log(f"push: {ahead} commit(s) published to {env.remote}")
    elif mb == lh:
        env.log(f"push: BLOCKED because local {env.branch} is behind authoritative {env.remote}/{env.branch}")
        return False
    else:
        env.log(f"push: BLOCKED because local {env.branch} diverged from authoritative {env.remote}/{env.branch}")
        return False

    for mirror in env.mirrors:
        if _git(env, "push", mirror, env.branch).returncode == 0:
            env.log(f"push: mirror {mirror} aligned")
            continue
        if (_git(env, "fetch", "--prune", mirror, env.branch).returncode == 0
                and _git(env, "push", "--force-with-lease", mirror, env.branch).returncode == 0):
            env.log(f"push: mirror {mirror} realigned to the authoritative line (force-with-lease)")
        else:
            # The authoritative publish succeeded. A mirror outage is
            # observable debt, not grounds to call the canonical write lost.
            env.log(f"push: mirror {mirror} unreachable or lease expired — authoritative remote is safe")
    return True


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
    data_dir = env.vault_data / "03-INFRA"
    commands: list[list[str]] = []
    if IS_WINDOWS:
        ps_translator = data_dir / "alert-translate.ps1"
        if ps_translator.is_file():
            commands.append([
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(ps_translator),
            ])
        cmd_translator = data_dir / "alert-translate.bat"
        if cmd_translator.is_file():
            commands.append(["cmd.exe", "/d", "/c", str(cmd_translator)])
    translator = data_dir / "alert-translate.sh"
    if translator.is_file() and (not IS_WINDOWS or os.access(translator, os.X_OK)):
        commands.append(["bash", str(translator)])
    for command in commands:
        try:
            r = subprocess.run(command, input=msg, capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
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
        r = _run_external(["notify-send", "-u", "critical", "-a", "agent-healthcheck",
                            "Agents: something is wrong", msg], timeout=5, capture_output=True)
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
        try:
            _load_env_conf(env)
        except Exception as exc:
            # Same resilience as _ensure_alert_creds/_send_healthcheck right
            # above/below: a malformed or non-UTF-8 conf file (a stray binary
            # write, a bad manual edit) must not skip _send_healthcheck
            # entirely by letting the exception propagate past it unguarded.
            env.log(f"alert-creds: 91-telegram-alert.conf unreadable, skipping ({exc})")
        try:
            _send_healthcheck(env)
        except Exception as exc:
            env.log(f"healthcheck: non-fatal error ({exc})")


def _parse_cli(argv: list[str]) -> tuple[str, bool, bool, list[str]]:
    skip_mcp = False
    allow_offline = False
    cleaned: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-Mode", "--mode"):
            if i + 1 >= len(argv):
                return arg, skip_mcp, allow_offline, []
            cleaned.append(argv[i + 1])
            i += 2
            continue
        if arg in ("-InstallScheduledTask", "--install-scheduled-task"):
            # Backward-compatible no-op: B2.5 installs/repairs the scheduler
            # during every apply/guard run.
            i += 1
            continue
        if arg in ("-SkipMcp", "--skip-mcp"):
            skip_mcp = True
            i += 1
            continue
        if arg == "--allow-offline":
            allow_offline = True
            i += 1
            continue
        cleaned.append(arg)
        i += 1
    return (cleaned[0] if cleaned else "help"), skip_mcp, allow_offline, cleaned[1:]


# ── vault-push (cross-platform port of vault-push.sh's exact behavior) ─────
# vault-push.sh/.ps1 are now thin OS wrappers that exec/forward into this
# subcommand (see docs/sync-contract.md and vault-write-architecture.md):
# one Python implementation instead of maintaining the git-commit/rebase/
# mirror logic twice. tests/test_vault_push.py (the POSIX acceptance
# harness) exercises this through the bash wrapper and must keep passing
# unchanged; tests/test_vault_push_python.py exercises this entry point
# directly, cross-platform.

class _VaultPushUsageError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _parse_vault_push_args(argv: list[str]) -> tuple[str, list[str]]:
    """Mirrors vault-push.sh's own `while [ $# -gt 0 ]` loop: -m MSG, glued
    -mMSG, `--` stops flag parsing and takes every remaining argument as a
    file verbatim (even one that looks like -m), anything else is a file."""
    msg: str | None = None
    files: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-m":
            if i + 1 >= len(argv):
                raise _VaultPushUsageError("argument missing for -m")
            msg = argv[i + 1]
            i += 2
            continue
        if arg.startswith("-m") and arg != "-m":
            msg = arg[2:]
            i += 1
            continue
        if arg == "--":
            files.extend(argv[i + 1:])
            break
        files.append(arg)
        i += 1
    if not msg:
        raise _VaultPushUsageError('needs -m "message"')
    return msg, files


def _vault_push_publish(env: Env) -> int:
    if _git(env, "push", env.remote, env.branch).returncode == 0:
        print(f"vault-push: push {env.remote} OK")
    else:
        if _git(env, "fetch", "--prune", env.remote, env.branch).returncode != 0:
            print(f"vault-push: {env.remote} OFFLINE — the commit stays local; run agent-sync publish later")
            return 1
        status = _git(env, "status", "--porcelain", "--untracked-files=no")
        if status.stdout.strip():
            print(f"vault-push: {env.remote} rejected but the working tree has uncommitted changes — NOT rebasing, resolve by hand")
            return 1
        if _git(env, "rebase", f"{env.remote}/{env.branch}").returncode == 0:
            if _git(env, "push", env.remote, env.branch).returncode != 0:
                print(f"vault-push: {env.remote} still rejected after rebase — try again")
                return 1
            print(f"vault-push: push {env.remote} OK (after a clean rebase)")
        else:
            _git(env, "rebase", "--abort")
            print(f"vault-push: {env.remote} DIVERGENCE WITH CONFLICT — needs a manual 'git pull --rebase {env.remote} {env.branch}'")
            return 1

    # Mirrors are explicit downstream replicas: never rewrite the canonical
    # local history, never affect the exit code. A stale mirror is aligned
    # with force-with-lease only after the authoritative remote already
    # accepted the same commit.
    for mirror in env.mirrors:
        if _git(env, "remote", "get-url", mirror).returncode != 0:
            print(f"vault-push: mirror '{mirror}' is not configured; skipped")
            continue
        if _git(env, "push", mirror, env.branch).returncode == 0:
            print(f"vault-push: push mirror {mirror} OK")
        elif (_git(env, "fetch", "--prune", mirror, env.branch).returncode == 0
              and _git(env, "push", "--force-with-lease", mirror, env.branch).returncode == 0):
            print(f"vault-push: mirror {mirror} aligned to authoritative {env.remote}")
        else:
            print(f"vault-push: mirror {mirror} not updated; authoritative {env.remote} is safe")
    return 0


def _vault_push_locked(env: Env, msg: str, files: list[str]) -> int:
    # Local-Only sentinel (same "local"/"none" values publish() already
    # special-cases): no remote is ever meant to exist, so skip the "is it
    # configured" check below instead of failing on a git remote that was
    # never supposed to be there. The commit itself still happens further
    # down -- Local-Only means no publication target, not no local history.
    local_only = env.remote in ("local", "none")
    if not local_only and _git(env, "remote", "get-url", env.remote).returncode != 0:
        print(f"vault-push: authoritative remote '{env.remote}' is not configured")
        return 1

    if files and _git(env, "add", "--", *files).returncode != 0:
        print("vault-push: git add failed")
        return 1
    if _git(env, "diff", "--cached", "--quiet").returncode == 0:
        print("vault-push: nothing staged, nothing to commit")
        return 0

    if _git(env, "commit", "-q", "-m", msg).returncode != 0:
        print("vault-push: commit failed")
        return 1
    short = _git(env, "rev-parse", "--short", "HEAD").stdout.strip()
    print(f"vault-push: commit {short}")

    if local_only:
        print(f"vault-push: push skipped (Local-Only mode, remote={env.remote})")
        return 0

    return _vault_push_publish(env)


def _vault_push_cli(argv: list[str]) -> int:
    try:
        msg, files = _parse_vault_push_args(argv)
    except _VaultPushUsageError as exc:
        print(f"vault-push: {exc.message}", file=sys.stderr)
        return 2

    try:
        env = Env()
    except RemoteConfigError as exc:
        print(f"vault-push: {exc}", file=sys.stderr)
        return 2

    if not env.vault_data.is_dir():
        print(f"vault-push: vault not found ({env.vault_data})")
        return 1

    # Same host-wide lock file agent_sync.py's own apply/guard/publish runs
    # use by default (env.log_dir / "agent-sync.lock"), acquired the same
    # way (SyncRunLock, fcntl.flock/msvcrt.locking): without this, a
    # `vault-push` running concurrently with an apply/guard cycle could
    # interleave a commit with a mid-apply working tree.
    lock_file = Path(os.environ.get("AGENT_SYNC_LOCK_FILE") or str(env.log_dir / "agent-sync.lock"))
    try:
        lock_timeout = float(os.environ.get("AGENT_SYNC_LOCK_TIMEOUT_SECONDS") or "2")
    except ValueError:
        print("vault-push: AGENT_SYNC_LOCK_TIMEOUT_SECONDS must be numeric", file=sys.stderr)
        return 2

    with SyncRunLock(lock_file, timeout=lock_timeout) as lock:
        if not lock.acquired:
            print("vault-push: sync lock busy (another agent-sync/vault-push is running) -- aborting", file=sys.stderr)
            return 75
        return _vault_push_locked(env, msg, files)


def _print_config(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"authoritative_remote", "mirrors"}:
        print("Use: agent-sync config authoritative_remote|mirrors", file=sys.stderr)
        return 2
    try:
        config = load_remote_config()
    except RemoteConfigError as exc:
        print(f"agent_sync: {exc}", file=sys.stderr)
        return 2
    if argv[1] == "authoritative_remote":
        print(config.authoritative_remote)
    else:
        print("\n".join(config.mirrors))
    return 0


def _run_phase(env: Env, name: str, fn: Callable[[Env], object]) -> bool:
    try:
        result = fn(env)
    except Exception as exc:
        env.log(f"phase {name}: ERROR ({type(exc).__name__}: {exc})")
        return False
    if result is False:
        env.log(f"phase {name}: ERROR (reported incomplete)")
        return False
    env.log(f"phase {name}: ok")
    return True


# ── entry point ──────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP_TEXT)
        return 0
    if argv[0] == "config":
        return _print_config(argv)
    if argv[0] == "vault-push":
        return _vault_push_cli(argv[1:])

    mode, skip_mcp, allow_offline, extras = _parse_cli(argv)
    if mode not in MODES:
        print(f"agent_sync: unknown mode: {mode}\nUse: agent_sync --help", file=sys.stderr)
        return 2
    if extras:
        print(f"agent_sync: unexpected arguments: {' '.join(extras)}", file=sys.stderr)
        return 2
    if allow_offline and mode != "apply":
        print("agent_sync: --allow-offline is accepted only with manual apply", file=sys.stderr)
        return 2
    try:
        env = Env()
    except RemoteConfigError as exc:
        print(f"agent_sync: {exc}", file=sys.stderr)
        return 2

    flags = MODES[mode]
    try:
        lock_timeout = float(os.environ.get("AGENT_SYNC_LOCK_TIMEOUT_SECONDS") or "2")
    except ValueError:
        print("agent_sync: AGENT_SYNC_LOCK_TIMEOUT_SECONDS must be numeric", file=sys.stderr)
        return 2

    with SyncRunLock(env.lock_path, timeout=lock_timeout) as lock:
        if not lock.acquired:
            env.log(f"agent-sync: lock busy, skipped mode={mode}")
            if mode == "guard":
                return 0
            print("agent_sync: another sync run is active", file=sys.stderr)
            return 75

        env.log(
            f"agent-sync: start mode={mode} authoritative_remote={env.remote} "
            f"config_source={env.remote_config_source}"
        )
        errors: list[str] = []
        apply_allowed = True

        if flags["pull"]:
            outcome = pull(env)
            if outcome.allows_apply:
                pass
            elif outcome.state is PullState.FETCH_FAILED and allow_offline:
                env.log(f"pull: manual offline override accepted ({outcome.message})")
            else:
                errors.append(f"pull:{outcome.state.value}")
                apply_allowed = False

        needs_preflight = flags["apply"] or mode == "preflight"
        if needs_preflight and apply_allowed:
            if not _run_phase(env, "preflight", preflight):
                errors.append("preflight")
                apply_allowed = False

        if flags["apply"] and apply_allowed:
            phases: list[tuple[str, Callable[[Env], object]]] = [
                ("data_migrations", data_migrations),
                ("instructions", instructions),
                ("antigravity_mcp", antigravity_mcp),
                ("utils", utils),
                ("local_model_runtime", local_model_runtime),
                ("install_scheduler", install_scheduler),
            ]
            if skip_mcp:
                env.log("mcp-gen: skipped by explicit --skip-mcp")
            else:
                phases.append(("mcp_render", mcp_render))
            phases.extend([
                ("vault_skills", vault_skills),
                ("runtimes", runtimes),
                ("skills_index", skills_index),
                ("claude_hooks", claude_hooks),
            ])
            for name, fn in phases:
                if not _run_phase(env, name, fn):
                    errors.append(name)
        elif flags["apply"]:
            env.log("apply: BLOCKED because the authoritative data state is not safe")

        if flags["push"] and not _run_phase(env, "publish", publish):
            errors.append("publish")

        creds_health(env, do_creds=flags["creds"], do_health=flags["health"])

        dirty = _git(env, "status", "--porcelain")
        dirty_lines = [line for line in dirty.stdout.splitlines() if line.strip()]
        if dirty_lines:
            env.log(f"note: {len(dirty_lines)} uncommitted file(s) in the vault (not touching them)")

        if errors:
            env.log(f"agent-sync: completed mode={mode} status=failed errors={','.join(errors)}")
            return 1
        env.log(f"agent-sync: completed mode={mode} status=ok")
        return 0


if __name__ == "__main__":
    sys.exit(main())
