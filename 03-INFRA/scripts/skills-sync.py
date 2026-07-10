#!/usr/bin/env python3
"""SKILL synchronizer — agent-layer (mirror of mcp/render.py).

Reads skills.manifest.yaml and makes sure that, on THIS machine, the
~/.agents/skills hub and the runtimes (Claude, Codex) contain exactly the
skills chosen in the manifest. One single script for Fedora and Windows.

  - default (--diff): READ-ONLY. Shows what it would do, touches nothing.
  - --apply:          runs the actions (creates/repairs links, flags missing
                      installs). Idempotent: does nothing if already aligned.

Byte model (per the manifest):
  - origin vault  -> the hub points (symlink, or a copy on Windows) to the
                     folder vendored in the vault. Git has already carried
                     the bytes everywhere.
  - origin github -> third-party, not vendored: the bytes get reinstalled
                     from upstream with a shallow Git clone. If missing,
                     --apply tries to install it and fails promptly when Git
                     cannot clone without interaction.

Runtime:
  - Codex: per-skill symlink (or copy on Windows) in ~/.codex/skills/<name>.
  - Claude: ~/.claude/skills is normally a symlink of the ENTIRE folder
            pointing at the hub, so it sees everything automatically; the
            script verifies this. If instead it's a real folder (the
            copy-based model, typical on Windows), it mirrors the single skill.

NOT authoritative for deletion: it never removes a skill absent from the manifest.
"""
from __future__ import annotations
import argparse, os, platform, shutil, subprocess, sys, tempfile
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
# NOT HERE.parent.parent: when this script runs from a separate engine
# checkout (AGENT_ENGINE_ROOT), the manifest and exclude lists still need
# to come from the user's actual data, same resolution as agent_sync.py's
# Env.vault_data.
_vault = Path(os.environ.get("KNOWLEDGE_VAULT_PATH") or str(HOME / "KnowledgeVault"))
VAULT = Path(os.environ.get("AGENT_VAULT_DATA") or str(_vault))
UL = VAULT / "03-INFRA" / "agent-universal-layer"
MANIFEST = UL / "skills" / "skills.manifest.yaml"

HUB = HOME / ".agents" / "skills"
RUNTIME = {
    "claude": HOME / ".claude" / "skills",
    "codex": HOME / ".codex" / "skills",
}
IS_WINDOWS = platform.system() == "Windows"
GIT_CLONE_TIMEOUT_SECONDS = 60

PASS = WARN = ACT = FAILN = 0


def ok(m):   global PASS; PASS += 1; print(f"  \033[32m✓\033[0m {m}")
def warn(m): global WARN; WARN += 1; print(f"  \033[33m⚠\033[0m {m}")
def act(m):  global ACT;  ACT += 1;  print(f"  \033[36m+\033[0m {m}")
def fail(m): global FAILN; FAILN += 1; print(f"  \033[31m✗\033[0m {m}")
def sec(m):  print(f"\n\033[1m{m}\033[0m")


def resolves_to(link: Path, target: Path) -> bool:
    """True if `link` is a symlink that resolves to `target`."""
    try:
        return link.is_symlink() and link.resolve() == target.resolve()
    except OSError:
        return False


def ensure_link(src: Path, dst: Path, apply: bool, label: str) -> None:
    """Makes `dst` point to / mirror `src`. Never destroys an unexpected real
    folder: in that case it flags it and stops (no clobber)."""
    if resolves_to(dst, src):
        ok(f"{label}: already aligned")
        return
    if dst.exists() and not dst.is_symlink():
        # real folder: on Windows (copy-based model) this is fine as long as
        # it has content; never delete it.
        if (dst / "SKILL.md").exists():
            ok(f"{label}: present as a real copy (leaving it as-is)")
        else:
            warn(f"{label}: exists as a real folder with no SKILL.md, not touching it (check by hand)")
        return
    # dst is missing or a broken/wrong symlink here: (re)create it.
    if not apply:
        act(f"{label}: would create link -> {src}")
        return
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        dst.symlink_to(src, target_is_directory=True)
        act(f"{label}: symlink created -> {src}")
    except OSError:
        # Windows without symlink privilege: fall back to a copy.
        shutil.copytree(src, dst)
        act(f"{label}: copied (symlink unavailable) <- {src}")


def load_excludes(cli: str) -> set:
    """Skills excluded from preloading for a runtime (lazy: they stay in the
    hub, read on-demand). Same source used by agent-sync §4: the two
    provisioners MUST read the same list, or they'll fight/break each other."""
    f = UL / f"skills-exclude-{cli}.txt"
    if not f.exists():
        return set()
    return {ln.strip() for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")}


def ensure_absent_link(dst: Path, apply: bool, label: str) -> None:
    """The skill is excluded from the runtime: the per-skill link must NOT be
    there. Only removes symlinks; a real folder isn't ours and isn't touched."""
    if dst.is_symlink():
        if apply:
            dst.unlink()
            act(f"{label}: link removed (excluded from preload, lazy in the hub)")
        else:
            act(f"{label}: would remove the link (excluded, lazy)")
    elif dst.exists():
        warn(f"{label}: excluded but exists as a real folder, not touching it (check by hand)")
    else:
        ok(f"{label}: excluded (lazy, on-demand from the hub)")


def install_github(name: str, spec: dict, apply: bool) -> bool:
    """Third-party skill missing from the hub: reinstalls it from upstream
    with a controlled `git clone` (no npx: it collides with Claude's
    whole-folder symlink). `path` in the manifest = subfolder containing
    SKILL.md (default: repo root). Returns True if present at the end."""
    repo = spec.get("repo", "")
    sub = spec.get("path", ".")
    dst = HUB / name
    # defensive: in the hub, a github skill must be a real folder (a copy).
    # if a symlink is found here (self-loop, broken, or leftover), that's
    # never a valid state and would send the `.exists()` check below into
    # ELOOP, blocking the sync. Remove it right away so the sync self-heals
    # instead of getting stuck.
    if dst.is_symlink():
        if not apply:
            act(f"hub/{name}: anomalous symlink in the hub (self-loop/broken), --apply would remove it and reinstall from {repo}")
            return False
        warn(f"hub/{name}: anomalous symlink in the hub, removing it and reinstalling from {repo}")
        dst.unlink()
    if (dst / "SKILL.md").exists():
        ok(f"hub/{name}: present (third-party {repo})")
        return True
    if not apply:
        extra = f" [{sub}]" if sub != "." else ""
        act(f"hub/{name}: MISSING, would install from {repo}{extra}  (git clone)")
        return False
    if shutil.which("git") is None:
        fail(f"hub/{name}: missing and git isn't available. Copy the skill by hand from https://github.com/{repo}")
        return False
    # dst missing or broken/empty (no SKILL.md here): clean it up first.
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        shutil.rmtree(dst)
    with tempfile.TemporaryDirectory() as tmp:
        url = f"https://github.com/{repo}.git"
        print(f"    … git clone --depth 1 {url}")
        repo_dir = Path(tmp) / "repo"
        clone_env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }
        try:
            r = subprocess.run(
                ["git", "-c", "credential.interactive=never", "clone", "--depth", "1", url, str(repo_dir)],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=GIT_CLONE_TIMEOUT_SECONDS,
                env=clone_env,
            )
        except subprocess.TimeoutExpired:
            fail(f"hub/{name}: clone timed out after {GIT_CLONE_TIMEOUT_SECONDS}s")
            return False
        if r.returncode != 0:
            fail(f"hub/{name}: clone failed. {r.stderr.strip()[:200]}")
            return False
        src = repo_dir / sub
        # `sub` comes from the manifest. If it's absolute (e.g. "/etc") or
        # escapes via "..", pathlib's `/` operator honors that and silently
        # walks src outside repo_dir -- the copytree below would then vendor
        # arbitrary host paths into ~/.agents/skills. Confine it.
        repo_real = repo_dir.resolve()
        src_real = src.resolve()
        if src_real != repo_real and repo_real not in src_real.parents:
            fail(f"hub/{name}: invalid path '{sub}' (escapes the cloned repo)")
            return False
        if not (src / "SKILL.md").exists():
            fail(f"hub/{name}: SKILL.md not found in '{sub}' of repo {repo}")
            return False
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", ".claude-plugin"))
        (dst / ".source").write_text(
            f"source: https://github.com/{repo}\nupstream: {repo}\npath: {sub}\n"
            f"model: vendored-as-is (unmodified)\n", encoding="utf-8")
        act(f"hub/{name}: installed from {repo}")
        return True


def write_index(apply: bool) -> None:
    """Generates ~/.agents/skills/INDEX.md: a one-line-per-skill catalog
    (name + description from SKILL.md's frontmatter). This is the UNIVERSAL
    lazy-loading mechanism: every CLI/model (even without a skill format:
    Antigravity, OpenCode, local worker) reads the catalog and opens the
    right SKILL.md only when the task requires it. Idempotent: rewrites only
    if the content changes."""
    rows = []
    for d in sorted(HUB.iterdir()):
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
        "Catalog for ALL agents and CLIs, lazy by design.\n"
        "Usage: when the task matches an entry, read `~/.agents/skills/<skill>/SKILL.md` and follow it.\n"
        "Never preload the whole set.\n\n"
        + "\n".join(rows) + "\n")
    dst = HUB / "INDEX.md"
    old = dst.read_text(encoding="utf-8") if dst.exists() else ""
    if old == body:
        ok(f"INDEX.md: already up to date ({len(rows)} skills)")
        return
    if not apply:
        act(f"INDEX.md: would regenerate the catalog ({len(rows)} skills)")
        return
    dst.write_text(body, encoding="utf-8")
    act(f"INDEX.md: catalog regenerated ({len(rows)} skills)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Syncs the agent-layer's skills from the manifest.")
    ap.add_argument("--apply", action="store_true", help="run the actions (default: read-only diff only)")
    ap.add_argument("--index", action="store_true", help="regenerate ONLY the INDEX.md catalog and exit")
    args = ap.parse_args()
    apply = args.apply

    if args.index:
        print(f"\033[1m=== skills-sync [INDEX] · {platform.system()} ===\033[0m")
        HUB.mkdir(parents=True, exist_ok=True)
        write_index(apply=True)
        return 1 if FAILN else 0

    # The manifest is vault DATA (a user's personal skill choices), not
    # something the engine ships with. A fresh install has none yet -- that
    # is a valid state, not an error: fall through with an empty set instead
    # of exiting, so the rest of the sync (hub scan, INDEX.md) still runs.
    if MANIFEST.exists():
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
        skills = data.get("skills") or {}
    else:
        print(f"manifest not found: {MANIFEST} (fresh install or no skills configured yet -- skipping)", file=sys.stderr)
        skills = {}

    mode = "APPLY" if apply else "DIFF (read-only)"
    print(f"\033[1m=== skills-sync [{mode}] · {platform.system()} ===\033[0m")
    HUB.mkdir(parents=True, exist_ok=True)

    # state of the Claude runtime: symlink-folder pointing at the hub (sees everything)?
    claude_is_hub_link = resolves_to(RUNTIME["claude"], HUB)
    excludes = {cli: load_excludes(cli) for cli in RUNTIME}

    for name, spec in skills.items():
        sec(f"skill: {name}")
        origin = spec.get("origin")
        targets = spec.get("targets", [])

        # 1) materialize in the hub
        if origin == "vault":
            ensure_link(UL / "skills" / name, HUB / name, apply, f"hub/{name}")
            present = (HUB / name / "SKILL.md").exists() or resolves_to(HUB / name, UL / "skills" / name)
        elif origin == "github":
            present = install_github(name, spec, apply)
        else:
            fail(f"unknown origin '{origin}' for {name}")
            continue

        # 2) hook up the runtimes (honoring the exclude lists: lazy > preload)
        for t in targets:
            if t in RUNTIME and name in excludes[t]:
                if t == "claude" and claude_is_hub_link:
                    warn(f"claude/{name}: exclusion IMPOSSIBLE while ~/.claude/skills is a symlink to the hub")
                else:
                    ensure_absent_link(RUNTIME[t] / name, apply, f"{t}/{name}")
                continue
            if t == "claude":
                if claude_is_hub_link:
                    ok("claude: covered (whole-folder symlink pointing at the hub)")
                else:
                    ensure_link(HUB / name, RUNTIME["claude"] / name, apply, f"claude/{name}")
            elif t == "codex":
                ensure_link(HUB / name, RUNTIME["codex"] / name, apply, f"codex/{name}")
            else:
                warn(f"unknown target '{t}'")

    sec("universal catalog")
    write_index(apply)

    print(f"\n\033[1mTotal:\033[0m {PASS} ok · {ACT} actions · {WARN} warn · {FAILN} fail")
    if not apply and ACT:
        print("  (run again with --apply to apply them)")
    return 1 if FAILN else 0


if __name__ == "__main__":
    sys.exit(main())
