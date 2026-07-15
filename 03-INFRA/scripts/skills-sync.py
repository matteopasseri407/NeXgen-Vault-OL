#!/usr/bin/env python3
"""SKILL synchronizer — agent-layer (mirror of mcp/render.py).

Reads skills.manifest.yaml and makes sure that, on THIS machine, the
non-discovered skill library and the runtime views contain exactly the skills
chosen in the manifest. One single script for Fedora and Windows.

  - default (--diff): READ-ONLY. Shows what it would do, touches nothing.
  - --apply:          runs the actions (creates/repairs links, flags missing
                      installs). Idempotent: does nothing if already aligned.

Byte model (per the manifest):
  - origin vault  -> the library points (symlink, or a copy on Windows) to the
                     folder vendored in the vault. Git has already carried
                     the bytes everywhere.
  - origin github -> third-party, not vendored: the manifest pins a full Git
                     commit SHA. The synchronizer fetches exactly that object,
                     verifies it, then copies the skill. If missing, --apply
                     fails promptly when Git cannot fetch without interaction.

Layout:
  - ~/.agents/skill-library: complete managed library, intentionally not a
    discovery root for eager runtimes.
  - ~/.agents/skills: tiny active view, containing only `exposure: core`
    skills plus INDEX.md. It is the safe discovery root for Codex-like CLIs.
  - Claude may receive a native per-skill or whole-library view because it
    loads skill bodies lazily. Other runtimes use `agent-skill find/show`.

NOT authoritative for deletion: it never removes a skill absent from the manifest.
"""
from __future__ import annotations
import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
try:
    import yaml
except ModuleNotFoundError:
    print("skills-sync.py needs PyYAML: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# Windows console in cp1252: the unicode glyphs (checkmark) would crash the print.
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOME = Path.home()
HERE = Path(__file__).resolve().parent
# Reuse the manifest-entry-name safety contract already enforced for MCP
# server and Council seat names (config_schema.ENTRY_NAME_RE): a skill name
# becomes a path component below (library/name, active/name, runtime
# targets), so the same "single segment, no path separators, no leading
# dot" rule applies here. Same import pattern as agent_sync.py; a validator
# import must not create __pycache__ on a read-only --diff run.
sys.dont_write_bytecode = True
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from config_schema import ENTRY_NAME_RE  # noqa: E402

# NOT HERE.parent.parent: when this script runs from a separate engine
# checkout (AGENT_ENGINE_ROOT), the manifest still needs
# to come from the user's actual data, same resolution as agent_sync.py's
# Env.vault_data.
_vault = Path(os.environ.get("KNOWLEDGE_VAULT_PATH") or str(HOME / "KnowledgeVault"))
VAULT = Path(os.environ.get("AGENT_VAULT_DATA") or str(_vault))
UL = VAULT / "03-INFRA" / "agent-universal-layer"
MANIFEST = UL / "skills" / "skills.manifest.yaml"
USER_PROFILE = VAULT / "99-INDEX" / "USER-PROFILE.md"

LIBRARY = HOME / ".agents" / "skill-library"
ACTIVE = HOME / ".agents" / "skills"
LEGACY = LIBRARY / "legacy"
RUNTIME = {
    "claude": HOME / ".claude" / "skills",
    "codex": HOME / ".codex" / "skills",
}
IS_WINDOWS = platform.system() == "Windows"
# Windows directory junctions are reparse points.  On older supported Python
# versions (and on some junction shapes in newer ones), Path.is_symlink() is
# false even though unlink/rmdir is the safe removal operation.  Keep the
# adapter here in sync with agent_sync.py so a stale Claude junction is never
# handed to shutil.rmtree().
_REPARSE_POINT = 0x0400
GIT_CLONE_TIMEOUT_SECONDS = 60
# Codex has no native progressive-disclosure mechanism the way Claude does:
# its "lazy" guarantee is entirely the discipline of keeping RUNTIME["codex"]
# near-empty (only rare `exposure: core` entries), not a CLI-side mechanism
# that loads bodies on demand. A big `core` skill landing there defeats that
# discipline silently -- this is the tripwire for it (2026-07-13 review).
CODEX_CORE_SIZE_WARN_BYTES = 4096
# Several core skills can each stay under the per-skill guideline above and
# still pile up into a large eagerly-scanned directory for Codex. The
# aggregate is the actual thing that hurts (total bytes Codex reads on every
# run), so it gets its own, independent tripwire.
CODEX_CORE_AGGREGATE_WARN_BYTES = 8192
GIT_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
TEAM_MEMBERS_HEADING_RE = re.compile(r"(?im)^##\s+team members\b")

PASS = WARN = ACT = FAILN = 0


def ok(m):
    global PASS
    PASS += 1
    print(f"  \033[32m✓\033[0m {m}")


def warn(m):
    global WARN
    WARN += 1
    print(f"  \033[33m⚠\033[0m {m}")


def act(m):
    global ACT
    ACT += 1
    print(f"  \033[36m+\033[0m {m}")


def fail(m):
    global FAILN
    FAILN += 1
    print(f"  \033[31m✗\033[0m {m}")


def sec(m):
    print(f"\n\033[1m{m}\033[0m")


def _is_link_like(path: Path) -> bool:
    """Recognize symlinks and Windows directory junctions without following them."""
    if path.is_symlink():
        return True
    if not IS_WINDOWS:
        return False
    is_junction = getattr(path, "is_junction", None)
    try:
        if callable(is_junction) and is_junction():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attributes & _REPARSE_POINT)
    except OSError:
        return False


def _remove_path(path: Path) -> None:
    """Remove a file, real directory, symlink, or Windows junction safely."""
    if _is_link_like(path):
        try:
            path.unlink()
        except OSError:
            path.rmdir()
    elif path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def resolves_to(link: Path, target: Path) -> bool:
    """True if `link` is a link-like path that resolves to `target`."""
    try:
        return _is_link_like(link) and link.resolve() == target.resolve()
    except OSError:
        return False


def safe_name(name: str, label: str) -> bool:
    """Defense in depth: re-validate `name` as a single safe path segment
    right where it is about to become a filesystem path component (mirrors
    ENTRY_NAME_RE, already enforced once by load_skills_manifest). This
    protects any caller that builds a library/runtime path from a name
    that did not go through the manifest loader -- e.g. install_github()
    invoked directly, as the test suite already does.

    Deliberately NOT a `dst.resolve()`-must-stay-inside-root check like the
    one install_github applies to a GitHub skill's `sub` subpath: a
    library entry is *meant* to become a symlink pointing outside its
    immediate parent by design (a vault-origin skill's library entry links
    back into the vault; runtime views link into the library), so
    resolving the destination and demanding it stay "inside" would
    misfire on that legitimate, by-design case -- confirmed by an existing
    regression test that fails against exactly that check.
    """
    if not ENTRY_NAME_RE.fullmatch(name):
        fail(f"{label}: unsafe name {name!r}, refusing to build a path from it")
        return False
    return True


def same_tree_content(src: Path, dst: Path) -> bool:
    """Byte-compare directory trees, including names and symlink targets."""
    if not src.is_dir() or not dst.is_dir():
        return False
    try:
        src_entries = {path.relative_to(src).as_posix(): path for path in src.rglob("*")}
        dst_entries = {path.relative_to(dst).as_posix(): path for path in dst.rglob("*")}
    except OSError:
        return False
    if set(src_entries) != set(dst_entries):
        return False
    for rel, src_entry in src_entries.items():
        dst_entry = dst_entries[rel]
        try:
            if src_entry.is_symlink() or dst_entry.is_symlink():
                if not (
                    src_entry.is_symlink()
                    and dst_entry.is_symlink()
                    and os.readlink(src_entry) == os.readlink(dst_entry)
                ):
                    return False
            elif src_entry.is_dir() or dst_entry.is_dir():
                if not (src_entry.is_dir() and dst_entry.is_dir()):
                    return False
            elif not (src_entry.is_file() and dst_entry.is_file() and src_entry.read_bytes() == dst_entry.read_bytes()):
                return False
        except OSError:
            return False
    return True


def next_backup_path(dst: Path, label: str) -> Path:
    """Pick a non-discovered, non-clobbering backup path for a stale copy."""
    scope = label.split("/", 1)[0]
    backup_root = LEGACY / "refreshed-copies" / scope
    base = backup_root / (dst.name + ".local-edit.bak-" + time.strftime("%Y%m%d-%H%M%S"))
    candidate = base
    suffix = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = base.with_name(base.name + f"-{suffix}")
        suffix += 1
    return candidate


def ensure_link(src: Path, dst: Path, apply: bool, label: str) -> None:
    """Make ``dst`` point to / mirror ``src``.

    Windows can fall back to a real copy when links are unavailable. A stale
    generated copy must not silently prevent a canonical change from reaching
    Windows, so it is backed up before being replaced. A malformed folder with
    no SKILL.md remains untouched for manual inspection.
    """
    if resolves_to(dst, src):
        ok(f"{label}: already aligned")
        return
    if dst.exists() and not _is_link_like(dst):
        if not dst.is_dir() or not (dst / "SKILL.md").is_file():
            warn(f"{label}: exists as a real folder with no SKILL.md, not touching it (check by hand)")
            return
        if same_tree_content(src, dst):
            ok(f"{label}: already aligned as a real copy")
            return
        if not apply:
            act(f"{label}: would back up and refresh a stale real copy")
            return
        backup = next_backup_path(dst, label)
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(dst, backup)
        except OSError as exc:
            fail(f"{label}: cannot back up stale real copy before refresh: {exc}")
            return
        _remove_path(dst)
        act(f"{label}: backed up stale real copy to {backup.name}")
    # dst is missing or a broken/wrong symlink here: (re)create it.
    if not apply:
        act(f"{label}: would create link -> {src}")
        return
    if _is_link_like(dst) or dst.exists():
        _remove_path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.symlink_to(src, target_is_directory=True)
        act(f"{label}: symlink created -> {src}")
    except OSError:
        # Windows without symlink privilege: fall back to a copy.
        shutil.copytree(src, dst)
        act(f"{label}: copied (symlink unavailable) <- {src}")


def ensure_absent_link(dst: Path, apply: bool, label: str) -> None:
    """Remove one managed manual view without touching a real local folder."""
    if dst.is_symlink():
        if apply:
            dst.unlink()
            act(f"{label}: link removed (manual skill, loaded on demand)")
        else:
            act(f"{label}: would remove the link (manual, lazy)")
    elif dst.exists():
        warn(f"{label}: manual but exists as a real folder, not touching it (check by hand)")
    else:
        ok(f"{label}: manual (loaded on demand)")


def github_source_matches(dst: Path, repo: str, sub: str, commit: str) -> bool:
    """Return whether an installed third-party skill has trusted provenance.

    A SKILL.md alone is not enough: older installs used the upstream default
    branch and could silently contain different bytes on two machines. The
    sidecar is written only after the exact pinned commit has been fetched
    and verified.
    """
    source = dst / ".source"
    if not (dst / "SKILL.md").is_file() or not source.is_file():
        return False
    try:
        metadata = dict(
            line.split(": ", 1)
            for line in source.read_text(encoding="utf-8").splitlines()
            if ": " in line
        )
    except OSError:
        return False
    return (
        metadata.get("upstream") == repo
        and metadata.get("path") == sub
        and metadata.get("commit", "").lower() == commit.lower()
    )


def run_git(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess | None:
    """Run one bounded, non-interactive Git command for a third-party skill."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=GIT_CLONE_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None
    return result


def install_github(name: str, spec: dict, apply: bool) -> bool:
    """Third-party skill missing from the library: reinstall it from upstream
    at the manifest's immutable commit (no npx: it collides with Claude's
    whole-folder symlink). `path` in the manifest = subfolder containing
    SKILL.md (default: repo root). Returns True if present at the end."""
    if not safe_name(name, f"library/{name}"):
        return False
    repo = spec.get("repo", "")
    sub = spec.get("path", ".")
    commit = spec.get("commit", "")
    dst = LIBRARY / name
    if not isinstance(commit, str) or not GIT_COMMIT_SHA.fullmatch(commit):
        fail(f"library/{name}: GitHub skill needs a full 40-character commit SHA")
        return False
    expected_commit = commit.lower()
    # defensive: in the library, a GitHub skill must be a real folder (a copy).
    # if a symlink is found here (self-loop, broken, or leftover), that's
    # never a valid state and would send the `.exists()` check below into
    # ELOOP, blocking the sync. Remove it right away so the sync self-heals
    # instead of getting stuck.
    if dst.is_symlink():
        if not apply:
            act(f"library/{name}: anomalous symlink (self-loop/broken), --apply would remove it and reinstall from {repo}")
            return False
        warn(f"library/{name}: anomalous symlink, removing it and reinstalling from {repo}")
        dst.unlink()
    if github_source_matches(dst, repo, sub, expected_commit):
        ok(f"library/{name}: present (third-party {repo} at {expected_commit})")
        return True
    if not apply:
        extra = f" [{sub}]" if sub != "." else ""
        act(
            f"library/{name}: missing or unverified for {expected_commit}, "
            f"would install from {repo}{extra}"
        )
        return False
    if shutil.which("git") is None:
        fail(f"library/{name}: missing and git isn't available. Copy the skill by hand from https://github.com/{repo}")
        return False
    # dst missing or broken/empty (no SKILL.md here): clean it up first.
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        shutil.rmtree(dst)
    with tempfile.TemporaryDirectory() as tmp:
        url = f"https://github.com/{repo}.git"
        repo_dir = Path(tmp) / "repo"
        clone_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
        print(f"    … git fetch --depth 1 {url} {expected_commit}")
        init = run_git(["git", "init", "--quiet", str(repo_dir)], clone_env)
        if init is None:
            fail(f"library/{name}: Git setup timed out after {GIT_CLONE_TIMEOUT_SECONDS}s")
            return False
        if init.returncode != 0:
            fail(f"library/{name}: Git setup failed. {init.stderr.strip()[:200]}")
            return False
        remote = run_git(["git", "-C", str(repo_dir), "remote", "add", "origin", url], clone_env)
        if remote is None:
            fail(f"library/{name}: Git remote setup timed out after {GIT_CLONE_TIMEOUT_SECONDS}s")
            return False
        if remote.returncode != 0:
            fail(f"library/{name}: Git remote setup failed. {remote.stderr.strip()[:200]}")
            return False
        fetched = run_git(
            [
                "git", "-C", str(repo_dir), "-c", "credential.interactive=never",
                "fetch", "--quiet", "--depth", "1", "origin", expected_commit,
            ],
            clone_env,
        )
        if fetched is None:
            fail(f"library/{name}: fetch timed out after {GIT_CLONE_TIMEOUT_SECONDS}s")
            return False
        if fetched.returncode != 0:
            fail(f"library/{name}: fetch failed. {fetched.stderr.strip()[:200]}")
            return False
        resolved = run_git(["git", "-C", str(repo_dir), "rev-parse", "FETCH_HEAD"], clone_env)
        if resolved is None:
            fail(f"library/{name}: commit verification timed out after {GIT_CLONE_TIMEOUT_SECONDS}s")
            return False
        if resolved.returncode != 0:
            fail(f"library/{name}: commit verification failed. {resolved.stderr.strip()[:200]}")
            return False
        actual_commit = resolved.stdout.strip().lower()
        if actual_commit != expected_commit:
            fail(f"library/{name}: fetched commit {actual_commit or '<none>'} does not match {expected_commit}")
            return False
        checkout = run_git(
            ["git", "-C", str(repo_dir), "checkout", "--detach", "--quiet", expected_commit],
            clone_env,
        )
        if checkout is None:
            fail(f"library/{name}: checkout timed out after {GIT_CLONE_TIMEOUT_SECONDS}s")
            return False
        if checkout.returncode != 0:
            fail(f"library/{name}: checkout failed. {checkout.stderr.strip()[:200]}")
            return False
        src = repo_dir / sub
        # `sub` comes from the manifest. If it's absolute (e.g. "/etc") or
        # escapes via "..", pathlib's `/` operator honors that and silently
        # walks src outside repo_dir -- the copytree below would then vendor
        # arbitrary host paths into ~/.agents/skills. Confine it.
        repo_real = repo_dir.resolve()
        src_real = src.resolve()
        if src_real != repo_real and repo_real not in src_real.parents:
            fail(f"library/{name}: invalid path '{sub}' (escapes the cloned repo)")
            return False
        if not (src / "SKILL.md").exists():
            fail(f"library/{name}: SKILL.md not found in '{sub}' of repo {repo}")
            return False
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", ".claude-plugin"))
        (dst / ".source").write_text(
            f"source: https://github.com/{repo}\nupstream: {repo}\npath: {sub}\n"
            f"commit: {expected_commit}\n"
            f"model: vendored-as-is (unmodified)\n", encoding="utf-8")
        act(f"library/{name}: installed from {repo} at {expected_commit}")
        return True


def write_index(apply: bool) -> None:
    """Generate the tiny active catalog from the non-discovered library.

    The index deliberately lives in ~/.agents/skills, but its bodies live in
    ~/.agents/skill-library. Eager runtimes therefore see no optional skill
    metadata at startup, while every CLI can run `agent-skill find/show` when
    a vertical workflow is actually needed.
    """
    rows = []
    directories = sorted(LIBRARY.iterdir()) if LIBRARY.is_dir() else []
    for d in directories:
        md = d / "SKILL.md"
        try:
            if not md.is_file():
                continue
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # broken/self-loop symlink: must not kill the index
        desc = ""
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                try:
                    fm = yaml.safe_load(text[3:end]) or {}
                    desc = " ".join(str(fm.get("description") or "").split())
                except yaml.YAMLError:
                    pass
        if len(desc) > 240:
            desc = desc[:237].rstrip() + "..."
        rows.append(f"- **{d.name}**: {desc or '(no description)'}")
    body = (
        "# Skill catalog (GENERATED by skills-sync.py --index, do not edit)\n\n"
        "Managed skills are lazy. Their bodies live outside discovery roots.\n"
        "Use `agent-skill find <words>` when the matching skill is uncertain, "
        "then `agent-skill show <name>` and follow only that body.\n"
        "Never preload or scan the whole library.\n\n"
        + "\n".join(rows) + "\n")
    dst = ACTIVE / "INDEX.md"
    old = dst.read_text(encoding="utf-8") if dst.exists() else ""
    if old == body:
        ok(f"INDEX.md: already up to date ({len(rows)} managed skills)")
        return
    if not apply:
        act(f"INDEX.md: would regenerate the catalog ({len(rows)} managed skills)")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(body, encoding="utf-8")
    act(f"INDEX.md: catalog regenerated ({len(rows)} managed skills)")


def _has_skill_body(path: Path) -> bool:
    """True for a readable skill directory or a link to one, never crashes."""
    try:
        return path.is_dir() and (path / "SKILL.md").is_file()
    except OSError:
        return False


def migrate_legacy_views(apply: bool, skills: dict[str, dict]) -> None:
    """Quarantine pre-existing eager skill folders only on explicit request.

    Older installs put arbitrary third-party folders directly under discovery
    roots. They must not be deleted, nor silently moved by the recurring
    guard. `--migrate-legacy` preserves each body under a local, non-indexed
    quarantine so the user can later promote it into the pinned manifest.
    """
    views = {
        "shared": ACTIVE,
        "codex": RUNTIME["codex"],
        "claude": RUNTIME["claude"],
    }
    for scope, root in views.items():
        if not root.is_dir() or root.is_symlink():
            continue
        for entry in sorted(root.iterdir()):
            if entry.name.startswith(".") or entry.name == "INDEX.md":
                continue
            if not _has_skill_body(entry):
                continue
            # A managed view is already backed by the library. Keep views that
            # the manifest still declares. Remove only stale managed links,
            # never a real local folder. In particular, Claude's native-lazy
            # manual links must survive the migration.
            managed = LIBRARY / entry.name
            if resolves_to(entry, managed):
                spec = skills.get(entry.name, {})
                targets = spec.get("targets", [])
                exposure = spec.get("exposure", "manual")
                expected = (
                    (scope == "shared" and exposure == "core")
                    or (scope == "claude" and "claude" in targets)
                    or (scope == "codex" and exposure == "core" and "codex" in targets)
                )
                if expected:
                    ok(f"legacy/{scope}/{entry.name}: declared managed view kept")
                    continue
                if entry.is_symlink():
                    if apply:
                        entry.unlink()
                        act(f"legacy/{scope}/{entry.name}: removed stale managed view")
                    else:
                        act(f"legacy/{scope}/{entry.name}: would remove stale managed view")
                continue
            destination = LEGACY / scope / entry.name
            if destination.exists() or destination.is_symlink():
                warn(
                    f"legacy/{scope}/{entry.name}: destination already exists; "
                    "leaving the eager copy untouched"
                )
                continue
            if not apply:
                act(f"legacy/{scope}/{entry.name}: would quarantine outside discovery roots")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry), str(destination))
            act(f"legacy/{scope}/{entry.name}: quarantined outside discovery roots")


def team_mode_active() -> bool:
    """True only when USER-PROFILE.md declares a "Team members" section.

    A mono-user install (the default, and the overwhelming majority of
    installs today) either has no USER-PROFILE.md worth parsing or one
    without that section, and this returns False -- which keeps every
    skill's optional `scope` field fully inert: sync behaves exactly as it
    did before `scope` existed. This is a cheap presence check, not a
    parse of the section's contents: which member owns which host is a
    human/agent-facing declaration (see USER-PROFILE.md), not something
    this script resolves on its own.
    """
    try:
        text = USER_PROFILE.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(TEAM_MEMBERS_HEADING_RE.search(text))


def load_skills_manifest() -> dict | None:
    """Load the user-owned manifest without letting malformed YAML crash sync."""
    try:
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    except OSError as exc:
        fail(f"cannot read skills manifest: {exc}")
        return None
    except yaml.YAMLError as exc:
        fail(f"invalid skills manifest YAML: {exc}")
        return None

    if data is None:
        return {}
    if not isinstance(data, dict):
        fail("invalid skills manifest: root must be a mapping")
        return None

    skills = data.get("skills", {})
    if skills is None:
        return {}
    if not isinstance(skills, dict):
        fail("invalid skills manifest: 'skills' must be a mapping")
        return None

    for name, spec in skills.items():
        if not isinstance(name, str) or not name.strip():
            fail("invalid skills manifest: every skill name must be a non-empty string")
            return None
        if not ENTRY_NAME_RE.fullmatch(name):
            fail(
                f"invalid skills manifest: skill name {name!r} must use only letters, "
                "digits, '.', '_' or '-' (single path segment, no '/', '\\', no leading dot) -- "
                f"'{name}' would be used as a library/runtime folder name"
            )
            return None
        if not isinstance(spec, dict):
            fail(f"invalid skills manifest: skill '{name}' must be a mapping")
            return None
        origin = spec.get("origin")
        if origin not in {"vault", "github"}:
            fail(f"invalid skills manifest: skill '{name}' has unsupported origin {origin!r}")
            return None
        targets = spec.get("targets", [])
        if not isinstance(targets, list) or not all(isinstance(target, str) for target in targets):
            fail(f"invalid skills manifest: skill '{name}' targets must be a list of strings")
            return None
        exposure = spec.get("exposure", "manual")
        if exposure not in {"manual", "core"}:
            fail(
                f"invalid skills manifest: skill '{name}' exposure must be "
                "'manual' or 'core'"
            )
            return None
        if origin == "github":
            repo = spec.get("repo")
            if not isinstance(repo, str) or not repo.strip():
                fail(f"invalid skills manifest: GitHub skill '{name}' needs a repository")
                return None
            commit = spec.get("commit")
            if not isinstance(commit, str) or not GIT_COMMIT_SHA.fullmatch(commit):
                fail(
                    f"invalid skills manifest: GitHub skill '{name}' needs a full "
                    "40-character commit SHA"
                )
                return None
        if "path" in spec and not isinstance(spec["path"], str):
            fail(f"invalid skills manifest: skill '{name}' path must be a string")
            return None
        scope = spec.get("scope", "team")
        if scope not in {"personal", "team"}:
            fail(
                f"invalid skills manifest: skill '{name}' scope must be "
                "'personal' or 'team'"
            )
            return None
        if "owner" in spec and not isinstance(spec["owner"], str):
            fail(f"invalid skills manifest: skill '{name}' owner must be a string")
            return None

    return skills


def validate_manifest_sources(skills: dict) -> bool:
    """Check local manifest references without creating runtime views.

    A structural manifest can still point to a missing Vault skill. The
    provisioning preflight needs to catch that before any generated config is
    touched, while GitHub sources remain intentionally network-free here.
    """
    healthy = True
    for name, spec in skills.items():
        if spec.get("origin") != "vault":
            continue
        source = UL / "skills" / name / "SKILL.md"
        if source.is_file():
            continue
        fail(f"library/{name}: missing canonical Vault source {source}")
        healthy = False
    return healthy


def main() -> int:
    ap = argparse.ArgumentParser(description="Syncs the agent-layer's skills from the manifest.")
    ap.add_argument("--apply", action="store_true", help="run the actions (default: read-only diff only)")
    ap.add_argument(
        "--validate",
        action="store_true",
        help="validate the manifest and local Vault references without writing runtime views",
    )
    ap.add_argument("--index", action="store_true", help="regenerate ONLY the INDEX.md catalog and exit")
    ap.add_argument(
        "--migrate-legacy",
        action="store_true",
        help="explicitly quarantine old eager skill folders outside discovery roots",
    )
    args = ap.parse_args()
    apply = args.apply

    if args.validate and (args.apply or args.index or args.migrate_legacy):
        ap.error("--validate cannot be combined with --apply, --index, or --migrate-legacy")

    if args.index:
        print(f"\033[1m=== skills-sync [INDEX] · {platform.system()} ===\033[0m")
        LIBRARY.mkdir(parents=True, exist_ok=True)
        ACTIVE.mkdir(parents=True, exist_ok=True)
        write_index(apply=True)
        return 1 if FAILN else 0

    if args.validate:
        if not MANIFEST.exists():
            print(f"manifest not found: {MANIFEST} (fresh install or no skills configured yet -- valid)")
            return 0
        skills = load_skills_manifest()
        if skills is None:
            return 1
        return 0 if validate_manifest_sources(skills) else 1

    # The manifest is vault DATA (a user's personal skill choices), not
    # something the engine ships with. A fresh install has none yet -- that
    # is a valid state, not an error: fall through with an empty set instead
    # of exiting, so the rest of the sync (library scan, INDEX.md) still runs.
    if MANIFEST.exists():
        skills = load_skills_manifest()
        if skills is None:
            return 1
    else:
        print(f"manifest not found: {MANIFEST} (fresh install or no skills configured yet -- skipping)", file=sys.stderr)
        skills = {}

    mode = "APPLY" if apply else "DIFF (read-only)"
    print(f"\033[1m=== skills-sync [{mode}] · {platform.system()} ===\033[0m")
    if apply:
        LIBRARY.mkdir(parents=True, exist_ok=True)
        ACTIVE.mkdir(parents=True, exist_ok=True)

    # state of the Claude runtime: symlink-folder pointing at the library?
    claude_is_library_link = resolves_to(RUNTIME["claude"], LIBRARY)
    team_mode = team_mode_active()
    codex_core_total_bytes = 0
    for name, spec in skills.items():
        sec(f"skill: {name}")

        # scope: personal only ever filters anything when USER-PROFILE.md
        # declares a Team members section. Without that section (the
        # mono-user default), a personal-scoped skill syncs exactly like a
        # team-scoped one, unchanged from before `scope` existed.
        scope = spec.get("scope", "team")
        if scope == "personal" and team_mode:
            owner = spec.get("owner")
            member = os.environ.get("AGENT_TEAM_MEMBER")
            if not owner:
                warn(f"{name}: scope 'personal' has no 'owner', skipping while Team members is declared")
                continue
            if member != owner:
                ok(f"{name}: personal to '{owner}', not this machine ('{member or 'unset'}'), skipping")
                continue

        origin = spec.get("origin")
        targets = spec.get("targets", [])
        exposure = spec.get("exposure", "manual")

        # Defense in depth: `name` is already restricted to a single safe
        # path segment by ENTRY_NAME_RE inside load_skills_manifest(), so
        # every join below (LIBRARY/name, ACTIVE/name, RUNTIME[...]/name)
        # is guaranteed safe by construction. Re-check anyway in case a
        # future caller ever feeds `main()` a `skills` mapping that did not
        # go through the manifest loader.
        if not safe_name(name, f"skill '{name}'"):
            continue

        # 1) materialize in the non-discovered library
        if origin == "vault":
            source = UL / "skills" / name
            if not (source / "SKILL.md").is_file():
                fail(f"library/{name}: missing canonical Vault source {source / 'SKILL.md'}")
                continue
            ensure_link(source, LIBRARY / name, apply, f"library/{name}")
        elif origin == "github":
            install_github(name, spec, apply)
        else:
            fail(f"unknown origin '{origin}' for {name}")
            continue

        # 2) expose only core skills through the eager shared root. Manual
        # skills stay in the non-discovered library and are read with
        # `agent-skill show <name>`.
        if exposure == "core":
            ensure_link(LIBRARY / name, ACTIVE / name, apply, f"active/{name}")
        else:
            ensure_absent_link(ACTIVE / name, apply, f"active/{name}")

        # 3) Claude is the native lazy runtime. It may see its declared
        # skills directly. Codex gets only explicit core views; OpenCode,
        # Antigravity and local workers use the universal command/catalog.
        for t in targets:
            if t == "claude":
                if claude_is_library_link:
                    ok("claude: covered (whole-library lazy view)")
                else:
                    ensure_link(LIBRARY / name, RUNTIME["claude"] / name, apply, f"claude/{name}")
            elif t == "codex" and exposure == "core":
                ensure_link(LIBRARY / name, RUNTIME["codex"] / name, apply, f"codex/{name}")
                skill_md = LIBRARY / name / "SKILL.md"
                if skill_md.is_file():
                    size = skill_md.stat().st_size
                    codex_core_total_bytes += size
                    if size > CODEX_CORE_SIZE_WARN_BYTES:
                        warn(
                            f"codex/{name}: SKILL.md is {size}B, over the "
                            f"{CODEX_CORE_SIZE_WARN_BYTES}B core-on-Codex guideline -- Codex has no "
                            "native lazy loading, so this sits in its eagerly-scanned directory on "
                            "every run. Consider exposure: manual (read via `agent-skill show`) "
                            "instead of core if Codex doesn't need this every session."
                        )
            elif t == "codex":
                ensure_absent_link(RUNTIME["codex"] / name, apply, f"codex/{name}")
            else:
                warn(f"unknown target '{t}'")

    if codex_core_total_bytes > CODEX_CORE_AGGREGATE_WARN_BYTES:
        warn(
            f"codex: core skills total {codex_core_total_bytes}B in its eagerly-scanned "
            f"directory, over the {CODEX_CORE_AGGREGATE_WARN_BYTES}B aggregate guideline -- "
            "each one can be under the per-skill limit and still add up on every run. Consider "
            "demoting rarely-used ones to exposure: manual (read via `agent-skill show`)."
        )

    sec("universal catalog")
    write_index(apply)
    if args.migrate_legacy:
        sec("legacy eager-skill migration")
        migrate_legacy_views(apply, skills)

    print(f"\n\033[1mTotal:\033[0m {PASS} ok · {ACT} actions · {WARN} warn · {FAILN} fail")
    if not apply and ACT:
        print("  (run again with --apply to apply them)")
    return 1 if FAILN else 0


if __name__ == "__main__":
    sys.exit(main())
