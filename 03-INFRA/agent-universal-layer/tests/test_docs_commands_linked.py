"""Class-level invariant: every project command the shipped docs promise
works as a bare command must actually be provisioned by utils(), and every
LINKED_COMMANDS entry that claims an OS must actually get created by that
OS's utils() branch.

History this guards against (2026-07-13 review): agent-sync, agent-doctor,
vault-groom and firecrawl-local were all documented -- README, INIT.md,
AGENTS.md, playbooks -- as bare commands for a stretch where utils() never
actually linked one or more of them onto PATH. Four separate "same bug
class, different command" fix commits landed before this test existed. This
makes that class of bug fail CI instead of waiting for a live run to notice.
"""
from __future__ import annotations

import importlib.util
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from conftest import REAL_SCRIPTS, REAL_VAULT, Sandbox, load_agent_sync_module

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_agent_sync_python import _make_fake_winreg  # noqa: E402  (shared fake, see that module)

DOC_FILES = sorted(
    p for p in (
        [REAL_VAULT / "README.md", REAL_VAULT / "INIT.md",
         REAL_VAULT / "AI-INSTALLER.md", REAL_VAULT / "AI-UNINSTALL.md"]
        + list((REAL_VAULT / "docs").glob("*.md"))
        + list((REAL_VAULT / "03-INFRA").glob("*.md"))
        + list((REAL_VAULT / "03-INFRA" / "agent-universal-layer" / "instructions").glob("*.md"))
    )
    if p.is_file()
)

# .sh/.ps1 twins that exist under 03-INFRA/scripts but are deliberately NOT
# bare commands linked onto PATH by utils() -- invoked by an explicit path
# instead. A reason is required so a future omission has to be a deliberate
# choice, not silence (the exact bug class this file guards against).
# install.sh does NOT belong here: it lives at the repo ROOT, outside
# 03-INFRA/scripts entirely, so _candidate_command_names() (built from
# REAL_SCRIPTS's own *.sh/*.ps1 STEMS, unioned with LINKED_COMMANDS' keys)
# never produces "install" as a candidate in the first place -- an entry
# for it here was dead weight, unreachable by the check below (2026-07-13
# adversarial review).
NOT_LINKED_ALLOWLIST: dict[str, str] = {
    "engine-push": (
        "Maintainer-only publication gate. Normal end users never publish the "
        "public engine; maintainers invoke it through an explicit platform path."
    ),
}

# LINKED_COMMANDS entries that are POSIX-only (windows: False) and
# referenced in docs without a caveat inline every single time. Accepted via
# allowlist + reason instead of a fragile "OS caveat within N lines" text
# heuristic. Only bring-your-own vault-ocr-local remains in this category.
POSIX_ONLY_DOC_ALLOWLIST = {
    "vault-ocr-local": "bring-your-own POSIX tool by design (vault-ocr.md, AGENTS.md); no .ps1 twin ships",
}


def _load_real_agent_sync():
    spec = importlib.util.spec_from_file_location("agent_sync_docs_check", REAL_SCRIPTS / "agent_sync.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load_real_agent_sync()


def _doc_texts() -> dict[Path, str]:
    return {p: p.read_text(encoding="utf-8") for p in DOC_FILES}


def _candidate_command_names() -> set[str]:
    """Pool of names worth checking: every .sh/.ps1 stem actually shipped
    under 03-INFRA/scripts (the one place utils() sources bare commands
    from), unioned with LINKED_COMMANDS' own keys (so a documented-but-
    never-shipped optional command like vault-ocr-local is still checked)."""
    stems = {p.stem for p in REAL_SCRIPTS.glob("*.sh")} | {p.stem for p in REAL_SCRIPTS.glob("*.ps1")}
    return stems | set(MOD.LINKED_COMMANDS.keys())


def _referenced_in_docs(name: str, texts: dict[Path, str]) -> bool:
    # The character right after `name` (when present) must be the closing
    # backtick, or a non-identifier separator that is also NOT '.' -- a '.'
    # there means an extension-qualified mention (`vault-push.sh`,
    # `agent-doctor.ps1`), which documents the FILE, not the bare PATH
    # command, and must not count as a reference to the bare command
    # (2026-07-13 adversarial review: the old class excluded only
    # `a-zA-Z0-9_-, so `.` slipped through as an accepted boundary and
    # extension-qualified mentions were wrongly counted as bare-command
    # references).
    pattern = re.compile(r"`" + re.escape(name) + r"(?:[^`a-zA-Z0-9_.-][^`]*)?`")
    return any(pattern.search(text) for text in texts.values())


def test_referenced_in_docs_does_not_count_extension_qualified_mentions():
    """A backticked `vault-push.sh` documents the FILE, not the bare PATH
    command -- it must not satisfy _referenced_in_docs('vault-push', ...)
    on its own (2026-07-13 adversarial review)."""
    only_file_mention = {Path("fake.md"): "see `vault-push.sh` for the implementation."}
    assert not _referenced_in_docs("vault-push", only_file_mention)

    ps1_mention = {Path("fake.md"): "the Windows twin is `vault-push.ps1`."}
    assert not _referenced_in_docs("vault-push", ps1_mention)

    # A real bare-command reference (immediate close, or a space-separated
    # argument list) must still be detected.
    bare_immediate = {Path("fake.md"): "run `vault-push` first."}
    assert _referenced_in_docs("vault-push", bare_immediate)

    bare_with_args = {Path("fake.md"): 'run `vault-push -m "message" [file ...]`.'}
    assert _referenced_in_docs("vault-push", bare_with_args)


def test_doc_files_exist_and_are_readable():
    assert DOC_FILES, "no shipped doc files matched -- the glob list is wrong"
    assert (REAL_VAULT / "README.md") in DOC_FILES


def test_every_doc_referenced_bare_command_is_linked_or_allowlisted():
    texts = _doc_texts()
    for name in sorted(_candidate_command_names()):
        if not _referenced_in_docs(name, texts):
            continue
        if name in MOD.LINKED_COMMANDS:
            cfg = MOD.LINKED_COMMANDS[name]
            if not cfg["windows"] and name not in POSIX_ONLY_DOC_ALLOWLIST:
                pytest.fail(
                    f"{name} is POSIX-only (windows=False) and referenced in docs as a bare "
                    "command with no allowlist reason -- add it to POSIX_ONLY_DOC_ALLOWLIST "
                    "with a reason, or confirm every doc reference carries an inline OS caveat."
                )
            continue
        assert name in NOT_LINKED_ALLOWLIST, (
            f"{name} is referenced in the docs as a bare command but is missing from "
            "LINKED_COMMANDS (agent_sync.py) -- either wire it into utils(), or add it to "
            "NOT_LINKED_ALLOWLIST with a reason. This is the exact bug class the 2026-07-13 "
            "review found for agent-sync/agent-doctor/vault-groom/firecrawl-local."
        )


def _make_fake_sources(sandbox: Sandbox) -> None:
    sandbox.scripts_dir.mkdir(parents=True, exist_ok=True)
    for name, cfg in MOD.LINKED_COMMANDS.items():
        if cfg["posix"]:
            sh = sandbox.scripts_dir / f"{name}.sh"
            if not sh.exists():
                sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
        if cfg["windows"]:
            ps1 = sandbox.scripts_dir / f"{name}.ps1"
            if not ps1.exists():
                ps1.write_text("# fake twin for the docs-linked invariant test\n", encoding="utf-8")
    skill = sandbox.scripts_dir / "agent-skill.py"
    if not skill.exists():
        skill.write_text("# fake\n", encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX branch behavior is covered on Linux and macOS.")
def test_posix_linked_commands_are_all_actually_created_by_utils(sandbox, monkeypatch):
    _make_fake_sources(sandbox)
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", False)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    env = mod.Env()

    mod.utils(env)

    for name, cfg in mod.LINKED_COMMANDS.items():
        if not cfg["posix"]:
            continue
        dst = sandbox.home / ".local" / "bin" / name
        assert dst.is_symlink(), f"LINKED_COMMANDS[{name!r}]['posix'] is True but utils() did not link it"


def test_windows_linked_commands_are_all_actually_created_by_utils(sandbox, monkeypatch):
    _make_fake_sources(sandbox)
    mod = load_agent_sync_module(sandbox)
    monkeypatch.setattr(mod, "IS_WINDOWS", True)
    monkeypatch.setenv("HOME", str(sandbox.home))
    monkeypatch.setenv("USERPROFILE", str(sandbox.home))
    monkeypatch.setenv("KNOWLEDGE_VAULT_PATH", str(sandbox.vault))
    monkeypatch.setitem(sys.modules, "winreg", _make_fake_winreg())
    env = mod.Env()

    mod.utils(env)

    for name, cfg in mod.LINKED_COMMANDS.items():
        if not cfg["windows"]:
            continue
        launcher = sandbox.home / ".local" / "bin" / f"{name}.ps1"
        wrapper = sandbox.home / ".local" / "bin" / f"{name}.cmd"
        assert launcher.exists(), f"LINKED_COMMANDS[{name!r}]['windows'] is True but utils() did not link {name}.ps1"
        assert f"{name}.ps1" in wrapper.read_text(encoding="utf-8")
