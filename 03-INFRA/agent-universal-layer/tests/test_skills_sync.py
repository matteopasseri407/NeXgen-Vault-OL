"""Regression coverage for the non-discovered lazy skill library."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_skills_sync_module


def _populate_library(sandbox):
    sandbox.skill_library.mkdir(parents=True, exist_ok=True)
    for name in ("fake-skill-a", "fake-skill-excluded"):
        shutil.copytree(sandbox.skills_dir / name, sandbox.skill_library / name)


def test_index_lists_every_managed_skill_without_exposing_bodies(sandbox):
    sb = sandbox
    _populate_library(sb)

    claude_rt = sb.home / ".claude" / "skills"
    claude_rt.mkdir(parents=True, exist_ok=True)

    mod = load_skills_sync_module(sb)
    mod.write_index(apply=True)

    index_path = sb.active_skills / "INDEX.md"
    assert index_path.is_file(), "--index non ha generato INDEX.md"
    text = index_path.read_text(encoding="utf-8")
    assert "fake-skill-a" in text
    assert "fake-skill-excluded" in text
    assert "Skill sintetica per i test B1" in text  # descrizione dal frontmatter
    assert text.count("- **") == 2, f"attesa una riga per skill, trovato:\n{text}"
    assert not any(sb.active_skills.glob("*/SKILL.md"))
    assert not any(claude_rt.iterdir())


def test_index_is_idempotent_when_content_unchanged(sandbox):
    sb = sandbox
    _populate_library(sb)
    mod = load_skills_sync_module(sb)

    mod.write_index(apply=True)
    first = (sb.active_skills / "INDEX.md").read_bytes()

    mod2 = load_skills_sync_module(sb)
    mod2.write_index(apply=True)
    second = (sb.active_skills / "INDEX.md").read_bytes()

    assert first == second


def test_manual_skill_stays_out_of_eager_views_but_claude_gets_native_lazy_access(sandbox, monkeypatch):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 0
    library = sandbox.skill_library / "fake-skill-a"
    assert (library / "SKILL.md").is_file()
    assert not (sandbox.active_skills / "fake-skill-a").exists()
    assert (sandbox.home / ".claude" / "skills" / "fake-skill-a").resolve() == library.resolve()
    assert not (sandbox.home / ".codex" / "skills" / "fake-skill-a").exists()


def test_core_skill_uses_only_the_official_shared_codex_root(sandbox, monkeypatch):
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(
        "skills:\n"
        "  fake-skill-a:\n"
        "    origin: vault\n"
        "    targets: [claude, codex]\n"
        "    exposure: core\n",
        encoding="utf-8",
    )
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 0
    library = sandbox.skill_library / "fake-skill-a"
    assert (sandbox.active_skills / "fake-skill-a").resolve() == library.resolve()
    assert not (sandbox.home / ".codex" / "skills" / "fake-skill-a").exists()


def test_codex_core_body_size_is_not_reported_as_eager_startup_cost(sandbox, monkeypatch, capsys):
    # Codex progressive-discloses the full body. NeXgen still limits the
    # discovered catalog to explicit core entries, but body size itself is
    # paid only when the matching skill is selected.
    (sandbox.skills_dir / "fake-skill-a" / "SKILL.md").write_text(
        "---\ndescription: oversized on purpose\n---\n" + ("x " * 3000) + "\n",
        encoding="utf-8",
    )
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(
        "skills:\n"
        "  fake-skill-a:\n"
        "    origin: vault\n"
        "    targets: [codex]\n"
        "    exposure: core\n",
        encoding="utf-8",
    )
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "no native lazy loading" not in out
    assert (sandbox.active_skills / "fake-skill-a" / "SKILL.md").is_file()
    assert not (sandbox.home / ".codex" / "skills" / "fake-skill-a").exists()


def _write_user_profile_with_team_members(sandbox) -> None:
    profile_dir = sandbox.vault / "99-INDEX"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "USER-PROFILE.md").write_text(
        "# User Profile\n\n"
        "## Team members (optional)\n\n"
        "- **marco**:\n"
        "  - Host(s): Primary workstation\n",
        encoding="utf-8",
    )


def _personal_skill_manifest(owner: str = "marco") -> str:
    return (
        "skills:\n"
        "  fake-skill-a:\n"
        "    origin: vault\n"
        "    targets: [claude, codex]\n"
        "    scope: personal\n"
        f"    owner: {owner}\n"
    )


def test_personal_skill_syncs_like_before_without_a_team_members_section(sandbox, monkeypatch):
    """Retrocompatibilita': mono-utente (nessuna sezione Team members in
    USER-PROFILE.md, il caso di gran lunga piu' comune) -> `scope` non
    cambia alcun comportamento osservabile, anche se dichiarato personal
    per qualcun altro e AGENT_TEAM_MEMBER non e' impostato."""
    monkeypatch.delenv("AGENT_TEAM_MEMBER", raising=False)
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(
        _personal_skill_manifest(owner="marco"), encoding="utf-8",
    )
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 0
    assert (sandbox.skill_library / "fake-skill-a" / "SKILL.md").is_file()


def test_personal_skill_is_skipped_on_a_machine_that_is_not_its_owner(sandbox, monkeypatch):
    """Team dichiarato (sezione presente) + AGENT_TEAM_MEMBER assente/diverso
    dal proprietario -> la skill personal non si materializza su questa
    macchina."""
    _write_user_profile_with_team_members(sandbox)
    monkeypatch.delenv("AGENT_TEAM_MEMBER", raising=False)
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(
        _personal_skill_manifest(owner="marco"), encoding="utf-8",
    )
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 0
    assert not (sandbox.skill_library / "fake-skill-a").exists()


def test_personal_skill_syncs_on_its_declared_owners_machine(sandbox, monkeypatch):
    """Stesso team dichiarato, ma AGENT_TEAM_MEMBER combacia col proprietario
    dichiarato -> la skill personal si materializza normalmente."""
    _write_user_profile_with_team_members(sandbox)
    monkeypatch.setenv("AGENT_TEAM_MEMBER", "marco")
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(
        _personal_skill_manifest(owner="marco"), encoding="utf-8",
    )
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 0
    assert (sandbox.skill_library / "fake-skill-a" / "SKILL.md").is_file()


def test_team_scope_skill_still_syncs_everywhere_even_with_team_members_declared(sandbox, monkeypatch):
    """`scope: team` (o assente) non e' mai filtrato, con o senza team
    dichiarato: si propaga a tutti come oggi."""
    _write_user_profile_with_team_members(sandbox)
    monkeypatch.setenv("AGENT_TEAM_MEMBER", "someone-else")
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 0
    assert (sandbox.skill_library / "fake-skill-a" / "SKILL.md").is_file()
    assert (sandbox.skill_library / "fake-skill-excluded" / "SKILL.md").is_file()


def test_personal_skill_without_owner_is_skipped_with_a_warning_while_team_is_declared(sandbox, monkeypatch, capsys):
    _write_user_profile_with_team_members(sandbox)
    monkeypatch.setenv("AGENT_TEAM_MEMBER", "marco")
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(
        "skills:\n"
        "  fake-skill-a:\n"
        "    origin: vault\n"
        "    targets: [claude, codex]\n"
        "    scope: personal\n",
        encoding="utf-8",
    )
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 0
    assert not (sandbox.skill_library / "fake-skill-a").exists()
    assert "no 'owner'" in capsys.readouterr().out


def test_legacy_migration_is_explicit_and_preserves_the_old_body(sandbox, monkeypatch):
    old = sandbox.active_skills / "old-local-skill"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("legacy body\n", encoding="utf-8")
    mod = load_skills_sync_module(sandbox)

    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])
    assert mod.main() == 0
    assert old.exists(), "ordinary guard must not move unknown local skills"

    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply", "--migrate-legacy"])
    assert mod.main() == 0
    archived = sandbox.skill_library / "legacy" / "shared" / "old-local-skill" / "SKILL.md"
    assert archived.read_text(encoding="utf-8") == "legacy body\n"
    assert not old.exists()


def test_legacy_migration_keeps_declared_claude_lazy_view(sandbox, monkeypatch):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply", "--migrate-legacy"])

    assert mod.main() == 0
    library = sandbox.skill_library / "fake-skill-a"
    claude_view = sandbox.home / ".claude" / "skills" / "fake-skill-a"
    assert claude_view.resolve() == library.resolve()


def test_legacy_migration_keeps_managed_windows_copy_fallbacks(sandbox, monkeypatch):
    """Declared views copied without symlink privilege are not legacy debt."""
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(
        "skills:\n"
        "  fake-skill-a:\n"
        "    origin: vault\n"
        "    targets: [claude, codex]\n"
        "    exposure: core\n",
        encoding="utf-8",
    )

    def fail_symlink(self, target, target_is_directory=False):
        raise OSError("symlink privilege unavailable")

    monkeypatch.setattr(Path, "symlink_to", fail_symlink)
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply", "--migrate-legacy"])

    assert mod.main() == 0
    library = sandbox.skill_library / "fake-skill-a"
    shared_view = sandbox.active_skills / "fake-skill-a"
    claude_view = sandbox.home / ".claude" / "skills" / "fake-skill-a"
    assert mod.same_tree_content(library, shared_view)
    assert mod.same_tree_content(library, claude_view)
    assert not (sandbox.home / ".codex" / "skills" / "fake-skill-a").exists()
    assert not (sandbox.skill_library / "legacy" / "shared" / "fake-skill-a").exists()
    assert not (sandbox.skill_library / "legacy" / "claude" / "fake-skill-a").exists()


def test_diff_does_not_create_lazy_views(sandbox, monkeypatch):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py"])

    assert mod.main() == 0
    assert not sandbox.skill_library.exists()
    assert not sandbox.active_skills.exists()


def test_vault_copy_fallback_refreshes_stale_body_with_backup(sandbox, monkeypatch):
    mod = load_skills_sync_module(sandbox)
    source = sandbox.skills_dir / "fake-skill-a"
    destination = sandbox.skill_library / "fake-skill-a"

    def fail_symlink(self, target, target_is_directory=False):
        raise OSError("symlink privilege unavailable")

    monkeypatch.setattr(Path, "symlink_to", fail_symlink)
    mod.ensure_link(source, destination, apply=True, label="library/fake-skill-a")
    original = (destination / "SKILL.md").read_text(encoding="utf-8")
    (source / "SKILL.md").write_text("updated canonical body\n", encoding="utf-8")

    mod.ensure_link(source, destination, apply=True, label="library/fake-skill-a")

    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "updated canonical body\n"
    backups = list(
        (sandbox.skill_library / "legacy" / "refreshed-copies" / "library").glob(
            "fake-skill-a.local-edit.bak-*/SKILL.md"
        )
    )
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-specific")
def test_link_like_directory_is_removed_without_rmtree_crash(sandbox):
    """A Windows junction is link-like even when Path.is_symlink() is false.

    The live Windows failure was ``shutil.rmtree(junction)`` from ensure_link.
    Simulate that reparse-point classification here so the regression stays
    covered on every CI runner, including POSIX-only local development hosts.
    """
    mod = load_skills_sync_module(sandbox)
    source = sandbox.skills_dir / "fake-skill-a"
    destination = sandbox.skill_library / "fake-skill-a"
    stale_target = sandbox.home / "stale-skill-target"
    stale_target.mkdir(parents=True, exist_ok=True)
    (stale_target / "SKILL.md").write_text("stale runtime view\n", encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    junction = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(destination), str(stale_target)],
        capture_output=True,
        text=True,
    )
    if junction.returncode != 0:
        pytest.skip(f"directory junction unavailable: {junction.stdout}{junction.stderr}")
    assert mod._is_link_like(destination)

    mod.ensure_link(source, destination, apply=True, label="library/fake-skill-a")

    assert destination.resolve() == source.resolve()
    assert (destination / "SKILL.md").read_text(encoding="utf-8") == (source / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-specific")
def test_manual_skill_removes_a_windows_junction_without_touching_its_target(sandbox):
    mod = load_skills_sync_module(sandbox)
    source = sandbox.skills_dir / "fake-skill-a"
    destination = sandbox.active_skills / "fake-skill-a"
    destination.parent.mkdir(parents=True, exist_ok=True)
    junction = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(destination), str(source)],
        capture_output=True,
        text=True,
    )
    if junction.returncode != 0:
        pytest.skip(f"directory junction unavailable: {junction.stdout}{junction.stderr}")

    mod.ensure_absent_link(destination, apply=True, label="active/fake-skill-a")

    assert not destination.exists()
    assert (source / "SKILL.md").is_file()


def test_missing_vault_source_fails_without_creating_a_library_link(sandbox, monkeypatch):
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(
        "skills:\n"
        "  missing-source:\n"
        "    origin: vault\n"
        "    targets: []\n",
        encoding="utf-8",
    )
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 1
    assert not (sandbox.skill_library / "missing-source").exists()


PINNED_COMMIT = hashlib.sha1(b"skills-sync test revision").hexdigest()


def test_github_fetch_disables_interactive_credentials_and_has_timeout(sandbox, monkeypatch):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.shutil, "which", lambda command: "/usr/bin/git")
    observed = {}

    def fake_run(command, **kwargs):
        observed.setdefault("commands", []).append(command)
        observed["kwargs"] = kwargs
        if "fetch" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="authentication required")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert not mod.install_github(
        "remote-skill", {"repo": "example/remote-skill", "commit": PINNED_COMMIT}, apply=True
    )
    fetch = next(command for command in observed["commands"] if "fetch" in command)
    assert fetch[-2:] == ["origin", PINNED_COMMIT]
    assert "credential.interactive=never" in fetch
    assert observed["kwargs"]["stdin"] is subprocess.DEVNULL
    assert observed["kwargs"]["timeout"] == mod.GIT_CLONE_TIMEOUT_SECONDS
    assert observed["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert observed["kwargs"]["env"]["GCM_INTERACTIVE"] == "Never"


def test_github_clone_timeout_is_reported_without_crashing(sandbox, monkeypatch, capsys):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.shutil, "which", lambda command: "/usr/bin/git")

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert not mod.install_github(
        "remote-skill", {"repo": "example/remote-skill", "commit": PINNED_COMMIT}, apply=True
    )
    assert f"timed out after {mod.GIT_CLONE_TIMEOUT_SECONDS}s" in capsys.readouterr().out


def test_github_skill_fetches_and_records_the_pinned_commit(sandbox, monkeypatch):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.shutil, "which", lambda command: "/usr/bin/git")
    observed = []

    def fake_run(command, **kwargs):
        observed.append(command)
        if command[1] == "init":
            Path(command[-1]).mkdir(parents=True)
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout=f"{PINNED_COMMIT}\n", stderr="")
        if "checkout" in command:
            repo_dir = Path(command[command.index("-C") + 1])
            (repo_dir / "SKILL.md").write_text("# pinned skill\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    spec = {"repo": "example/remote-skill", "commit": PINNED_COMMIT}

    assert mod.install_github("remote-skill", spec, apply=True)
    installed = sandbox.skill_library / "remote-skill"
    source = (installed / ".source").read_text(encoding="utf-8")
    assert f"commit: {PINNED_COMMIT}" in source
    fetch = next(command for command in observed if "fetch" in command)
    assert fetch[-1] == PINNED_COMMIT

    def fail_if_called(*args, **kwargs):
        raise AssertionError("a verified pinned skill must not be fetched again")

    monkeypatch.setattr(mod.subprocess, "run", fail_if_called)
    assert mod.install_github("remote-skill", spec, apply=True)


def test_github_skill_rejects_a_different_fetched_commit(sandbox, monkeypatch, capsys):
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.shutil, "which", lambda command: "/usr/bin/git")

    def fake_run(command, **kwargs):
        if "rev-parse" in command:
            return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert not mod.install_github(
        "remote-skill", {"repo": "example/remote-skill", "commit": PINNED_COMMIT}, apply=True
    )
    assert "does not match" in capsys.readouterr().out
    assert not (sandbox.skill_library / "remote-skill").exists()


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ("not-a-mapping\n", "root must be a mapping"),
        ("skills: []\n", "'skills' must be a mapping"),
        ("skills:\n  broken: nope\n", "skill 'broken' must be a mapping"),
        (
            "skills:\n  remote:\n    origin: github\n    repo: example/remote\n    targets: []\n",
            "needs a full 40-character commit SHA",
        ),
        (
            "skills:\n  remote:\n    origin: github\n    repo: example/remote\n    commit: short\n    targets: []\n",
            "needs a full 40-character commit SHA",
        ),
        (
            "skills:\n  bad:\n    origin: vault\n    targets: []\n    exposure: eager\n",
            "exposure must be 'manual' or 'core'",
        ),
        (
            "skills:\n  bad:\n    origin: vault\n    targets: []\n    scope: whoever\n",
            "scope must be 'personal' or 'team'",
        ),
        (
            "skills:\n  bad:\n    origin: vault\n    targets: []\n    owner: 5\n",
            "owner must be a string",
        ),
        # PACCHETTO 6 (security audit, MEDIUM): a skill name is used
        # unconfined as a path component (LIBRARY/name, RUNTIME[...]/name).
        # Anything that isn't a single safe path segment must be rejected
        # at manifest-load time, before any of those paths get built.
        (
            "skills:\n  \"../../../../marker-escape-dir/payload\":\n    origin: vault\n    targets: []\n",
            "must use only letters, digits",
        ),
        (
            "skills:\n  \"../escape\":\n    origin: vault\n    targets: []\n",
            "must use only letters, digits",
        ),
        (
            "skills:\n  \".hidden-leading-dot\":\n    origin: vault\n    targets: []\n",
            "must use only letters, digits",
        ),
        (
            "skills:\n  \"nested/slash\":\n    origin: vault\n    targets: []\n",
            "must use only letters, digits",
        ),
        (
            "skills:\n  back\\slash:\n    origin: vault\n    targets: []\n",
            "must use only letters, digits",
        ),
    ],
)
def test_invalid_manifest_returns_a_readable_error_without_traceback(sandbox, monkeypatch, capsys, manifest, message):
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(manifest, encoding="utf-8")
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py"])

    assert mod.main() == 1
    output = capsys.readouterr().out
    assert message in output
    assert "AttributeError" not in output


def test_path_traversal_skill_name_is_rejected_before_any_write_to_disk(sandbox, tmp_path, monkeypatch):
    """Reproduces the audit's PoC directly: a manifest whose skill name is a
    traversal payload must never reach LIBRARY/name, ACTIVE/name, or any
    RUNTIME[...]/name construction -- --apply must fail closed with zero
    filesystem mutation, not just zero write to the *escaped* location."""
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(
        "skills:\n"
        "  \"../../../../marker-escape-dir/payload\":\n"
        "    origin: vault\n"
        "    targets: [claude, codex]\n"
        "    exposure: core\n",
        encoding="utf-8",
    )
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 1

    # Nothing was ever created: load_skills_manifest() fails before main()
    # reaches the `if apply: LIBRARY.mkdir(...)` line, so not even the
    # library/active roots exist, let alone anything escaping them.
    assert not sandbox.skill_library.exists()
    assert not sandbox.active_skills.exists()
    assert not (sandbox.home / ".claude" / "skills").exists()
    assert not (sandbox.home / ".codex" / "skills").exists()

    # Belt and suspenders: the literal escape target the payload names must
    # not exist anywhere near the scratch HOME (proves the traversal never
    # executed, not just that we didn't look for it in the right place).
    assert not any(tmp_path.parent.rglob("marker-escape-dir"))


def test_valid_skill_names_with_dots_and_dashes_still_sync_normally(sandbox, monkeypatch):
    """Guardrail against a too-strict regex: ordinary manifest names (the
    only kind real manifests use) must be unaffected by the new check."""
    (sandbox.skills_dir / "skills.manifest.yaml").write_text(
        "skills:\n"
        "  fake-skill-a:\n"
        "    origin: vault\n"
        "    targets: [claude, codex]\n"
        "    exposure: core\n",
        encoding="utf-8",
    )
    mod = load_skills_sync_module(sandbox)
    monkeypatch.setattr(mod.sys, "argv", ["skills-sync.py", "--apply"])

    assert mod.main() == 0
    library = sandbox.skill_library / "fake-skill-a"
    assert (library / "SKILL.md").is_file()
    assert (sandbox.active_skills / "fake-skill-a").resolve() == library.resolve()
    assert (sandbox.home / ".claude" / "skills" / "fake-skill-a").resolve() == library.resolve()
    assert not (sandbox.home / ".codex" / "skills" / "fake-skill-a").exists()


def test_install_github_rejects_a_path_traversal_name_called_directly(sandbox):
    """Defense in depth: install_github() takes a raw `name` string and is
    called directly by other code/tests, bypassing load_skills_manifest's
    regex check. It must independently refuse to build LIBRARY/name from an
    unsafe name instead of trusting its caller."""
    mod = load_skills_sync_module(sandbox)

    assert not mod.install_github(
        "../../../../marker-escape-dir/payload",
        {"repo": "example/remote-skill", "commit": PINNED_COMMIT},
        apply=True,
    )
    assert not sandbox.skill_library.exists()
