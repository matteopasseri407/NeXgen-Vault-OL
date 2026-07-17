"""Source-level invariants for the vendored starter command skills.

The engine ships three cross-CLI command skills (vault-doctor, vault-close,
vault-save). They must stay portable across every runtime that consumes the
agentskills.io shape (Claude Code, Codex, OpenCode, Antigravity), which the
dialect verification of 2026-07-17 reduced to: lowercase-hyphen name equal to
the folder name, a description in the frontmatter, and an argument-free body
(placeholder syntaxes like $ARGUMENTS diverge per CLI and are silently left
verbatim by skill-based runtimes).
"""
from __future__ import annotations

import re

import yaml

from conftest import REAL_VAULT

STARTERS = (
    "vault-doctor", "vault-close", "vault-save",
    "vault-council", "vault-groom", "vault-update",
    "vault-map",
)
SKILLS_ROOT = REAL_VAULT / "03-INFRA" / "agent-universal-layer" / "skills"
EXAMPLE_MANIFEST = SKILLS_ROOT / "skills.manifest.yaml.example"

# agentskills.io: lowercase alphanumerics + single hyphens, 1-64 chars.
PORTABLE_NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*\Z")
# Interpolation tokens that only SOME dialects expand; a portable body may
# not depend on any of them.
NON_PORTABLE_PLACEHOLDER_RE = re.compile(r"\$ARGUMENTS|\$\d|\{\{args\}\}")
# Names that would shadow a CLI built-in (verified 2026-07-17: Claude Code
# bundles /doctor and lets same-named user skills override it; OpenCode has
# /init and /review; Codex/Antigravity reserve their TUI commands).
RESERVED_BUILTIN_NAMES = {
    "doctor", "init", "review", "help", "clear", "compact", "skills",
    "agents", "model", "plan", "resume", "share", "undo", "redo",
}


def _frontmatter_and_body(name: str) -> tuple[dict, str]:
    text = (SKILLS_ROOT / name / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{name}: SKILL.md must open with YAML frontmatter"
    front, _, body = text[4:].partition("\n---\n")
    return yaml.safe_load(front), body


def test_every_starter_ships_a_valid_portable_skill():
    for name in STARTERS:
        skill_md = SKILLS_ROOT / name / "SKILL.md"
        assert skill_md.is_file(), f"missing vendored starter skill {skill_md}"
        front, body = _frontmatter_and_body(name)
        assert front.get("name") == name, (
            f"{name}: frontmatter name must equal the folder name "
            "(Antigravity and the agentskills spec require the match)"
        )
        assert PORTABLE_NAME_RE.fullmatch(name) and len(name) <= 64
        description = front.get("description")
        assert isinstance(description, str) and 1 <= len(description) <= 1024, (
            f"{name}: description is required (it drives listing and implicit "
            "invocation on every runtime) and capped at 1024 chars by the spec"
        )
        assert body.strip(), f"{name}: SKILL.md body must not be empty"


def test_starter_names_avoid_cli_builtin_collisions():
    for name in STARTERS:
        assert name not in RESERVED_BUILTIN_NAMES
        assert name.startswith("vault-"), (
            f"{name}: starters keep the vault- prefix so they can never "
            "shadow a CLI built-in or bundled skill"
        )


def test_starter_bodies_are_free_of_dialect_placeholders():
    for name in STARTERS:
        _, body = _frontmatter_and_body(name)
        hit = NON_PORTABLE_PLACEHOLDER_RE.search(body)
        assert hit is None, (
            f"{name}: body uses non-portable placeholder {hit.group(0)!r}; "
            "write it model-mediated (the text after the command is the request)"
        )


def test_example_manifest_registers_starters_as_core_commands():
    data = yaml.safe_load(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
    skills = (data or {}).get("skills") or {}
    for name in STARTERS:
        assert name in skills, f"{name}: missing from skills.manifest.yaml.example"
        spec = skills[name]
        assert spec.get("origin") == "vault"
        assert spec.get("exposure") == "core", (
            f"{name}: command skills need exposure core to reach the shared "
            "~/.agents/skills root (Codex + OpenCode discovery)"
        )
        assert {"claude", "antigravity", "opencode"} <= set(spec.get("targets", [])), (
            f"{name}: command skills target every runtime that needs a native "
            "view or a discoverability check"
        )
