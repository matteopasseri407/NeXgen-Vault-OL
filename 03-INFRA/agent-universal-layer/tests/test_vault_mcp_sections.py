"""Behavioral tests for section-level editing in the bundled vault-library
MCP server (03-INFRA/deploy/vault-mcp/).

update_section is the additive, fail-closed sibling of update_note: it
replaces exactly one ATX-heading section under a per-section
compare-and-swap hash, so a concurrent edit to a DIFFERENT section no
longer invalidates the write. update_note keeps its whole-note semantics
untouched.

vault.py is stdlib-only, so these run the real VaultService against a
throwaway Git-backed vault without the MCP runtime; CI's vault-mcp-smoke
job exercises the same tool over streamable-http against the container.
The write path serializes through fcntl, which does not exist on Windows —
matching the Linux-only container, the whole module is POSIX-only.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import hashlib
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="vault-mcp write path uses fcntl (POSIX-only, like the container)",
)

REPO = Path(__file__).resolve().parents[3]
VAULT_MCP_SRC = REPO / "03-INFRA" / "deploy" / "vault-mcp" / "src"
if str(VAULT_MCP_SRC) not in sys.path:
    sys.path.insert(0, str(VAULT_MCP_SRC))

# Importing the bundled package must not drop __pycache__ into the deployable
# component — test_deploy_vault_mcp's leftover check would (rightly) fail.
sys.dont_write_bytecode = True

from vault_mcp_server.config import Settings  # noqa: E402
from vault_mcp_server.vault import VaultService  # noqa: E402

NOTE = "02-PROJECTS/sample.md"

SAMPLE = (
    "---\n"
    "tags:\n"
    "  - test\n"
    "---\n"
    "\n"
    "# Sample note\n"
    "\n"
    "intro paragraph\n"
    "\n"
    "## Alpha\n"
    "\n"
    "alpha body\n"
    "\n"
    "### Alpha sub\n"
    "\n"
    "sub body\n"
    "\n"
    "## Beta\n"
    "\n"
    "```bash\n"
    "## not a heading\n"
    "echo hi\n"
    "```\n"
    "\n"
    "beta body\n"
    "\n"
    "## Gamma\n"
    "\n"
    "gamma body\n"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _span(text: str, start_marker: str, end_marker: str | None = None) -> str:
    start = text.index(start_marker)
    return text[start : text.index(end_marker)] if end_marker else text[start:]


def _git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@localhost",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@localhost",
    }
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return result.stdout.strip()


def make_settings(root: Path, git_dir: Path | None, **overrides) -> Settings:
    values = dict(
        vault_root=root,
        vault_token=None,
        write_enabled=True,
        git_dir=git_dir,
        git_author_name="Vault MCP Test",
        git_author_email="vault-mcp-test@localhost",
        host="127.0.0.1",
        port=8081,
        mcp_path="/mcp",
        health_path="/healthz",
        stateless_http=True,
        json_response=True,
        allowed_origins=(),
        ignored_dirs=(".git", ".obsidian"),
        max_note_bytes=1_000_000,
        cache_ttl_seconds=0,
        default_search_limit=10,
        max_search_limit=25,
        start_here_filename="00-START-HERE.md",
        include_path_prefixes=(),
        exclude_path_prefixes=(),
        write_exclude_path_prefixes=("99-SECRETS", ".git"),
        max_write_bytes=262144,
        semantic_url=None,
        semantic_enabled=False,
        semantic_max_limit=5,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "02-PROJECTS").mkdir(parents=True)
    (root / "00-START-HERE.md").write_text("# Start\n\nhub\n", encoding="utf-8")
    (root / NOTE).write_text(SAMPLE, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    git_dir = root / ".git"
    service = VaultService(make_settings(root, git_dir))
    return SimpleNamespace(
        root=root,
        git_dir=git_dir,
        path=root / NOTE,
        service=service,
    )


def _section_hashes(service: VaultService) -> dict[str, str]:
    return {
        section["heading"]: section["content_hash"]
        for section in service.read_note(NOTE)["sections"]
    }


# --- read side: sections are addressable -------------------------------------


def test_read_note_lists_sections_with_per_section_hashes(vault):
    payload = vault.service.read_note(NOTE)
    sections = payload["sections"]

    assert [section["heading"] for section in sections] == [
        "# Sample note",
        "## Alpha",
        "### Alpha sub",
        "## Beta",
        "## Gamma",
    ], "fenced '## not a heading' must not appear, frontmatter is not a section"
    assert [section["level"] for section in sections] == [1, 2, 3, 2, 2]

    by_heading = {section["heading"]: section["content_hash"] for section in sections}
    assert by_heading["## Beta"] == _sha(_span(SAMPLE, "## Beta", "## Gamma"))
    assert by_heading["## Gamma"] == _sha(_span(SAMPLE, "## Gamma"))
    assert by_heading["# Sample note"] == _sha(_span(SAMPLE, "# Sample note"))


def test_sections_are_omitted_for_truncated_notes(vault):
    service = VaultService(make_settings(vault.root, vault.git_dir, max_note_bytes=64))
    payload = service.read_note(NOTE)
    assert payload["truncated"] is True
    assert payload["sections"] == [], (
        "hashes computed over a truncated read would never match the file; "
        "oversized notes must fall back to update_note"
    )


# --- write side: surgical replace under section-level CAS ---------------------


def test_update_section_replaces_exactly_one_span_and_commits(vault):
    commits_before = int(_git(vault.root, "rev-list", "--count", "HEAD"))
    hashes = _section_hashes(vault.service)

    result = vault.service.update_section(
        NOTE,
        "## Beta",
        "## Beta\n\nnew beta body\n",
        hashes["## Beta"],
        message="test: rewrite beta",
    )

    assert result["action"] == "updated_section"
    assert result["committed"] is True
    assert result["section"] == "## Beta"
    assert result["old_section_hash"] == hashes["## Beta"]

    new_span = "## Beta\n\nnew beta body\n\n"
    expected = SAMPLE.replace(_span(SAMPLE, "## Beta", "## Gamma"), new_span)
    assert vault.path.read_text(encoding="utf-8") == expected, (
        "every byte outside the addressed section must survive untouched, and "
        "the seam before the next heading keeps a blank line"
    )
    assert result["new_section_hash"] == _sha(new_span)

    after = _section_hashes(vault.service)
    assert after["## Beta"] == result["new_section_hash"], "read/write hash round-trip"
    assert after["## Gamma"] == hashes["## Gamma"]
    assert after["## Alpha"] == hashes["## Alpha"]
    assert int(_git(vault.root, "rev-list", "--count", "HEAD")) == commits_before + 1


def test_update_section_rejects_stale_hash_and_leaves_note_untouched(vault):
    commits_before = int(_git(vault.root, "rev-list", "--count", "HEAD"))
    with pytest.raises(ValueError, match="expected_hash"):
        vault.service.update_section(NOTE, "## Beta", "## Beta\n\nx\n", "0" * 64)
    assert vault.path.read_text(encoding="utf-8") == SAMPLE
    assert int(_git(vault.root, "rev-list", "--count", "HEAD")) == commits_before


def test_update_section_tolerates_concurrent_edit_of_another_section(vault):
    hashes = _section_hashes(vault.service)

    # Another session lands a whole-note update touching only Gamma...
    payload = vault.service.read_note(NOTE)
    vault.service.update_note(
        NOTE,
        payload["full_content"].replace("gamma body", "gamma body v2"),
        payload["content_hash"],
    )

    # ...and the Alpha hash taken BEFORE that edit still authorizes the write.
    result = vault.service.update_section(
        NOTE,
        "## Alpha",
        "## Alpha\n\nalpha v2\n\n### Alpha sub\n\nsub body\n",
        hashes["## Alpha"],
    )
    assert result["committed"] is True

    text = vault.path.read_text(encoding="utf-8")
    assert "alpha v2" in text
    assert "gamma body v2" in text, "the concurrent edit must not be rolled back"


def test_update_section_fails_closed_on_missing_and_duplicate_headings(vault):
    vault.service.create_note(
        "01-NOTES/dups.md",
        "# Dups\n\n## Dup\n\na\n\n### Dup\n\nb\n\n## Twice\n\nx\n\n## Twice\n\ny\n",
    )

    with pytest.raises(ValueError, match="Section not found") as excinfo:
        vault.service.update_section("01-NOTES/dups.md", "## Missing", "## Missing\n\nz\n", "0" * 64)
    assert "## Dup" in str(excinfo.value), "the error must list the available headings"

    with pytest.raises(ValueError, match="[Aa]mbiguous"):
        vault.service.update_section("01-NOTES/dups.md", "Dup", "## Dup\n\nz\n", "0" * 64)

    # The level-qualified form disambiguates: resolution succeeds and the
    # failure moves on to the (deliberately stale) hash guard.
    with pytest.raises(ValueError, match="expected_hash"):
        vault.service.update_section("01-NOTES/dups.md", "### Dup", "### Dup\n\nz\n", "0" * 64)

    with pytest.raises(ValueError, match="update_note"):
        vault.service.update_section("01-NOTES/dups.md", "## Twice", "## Twice\n\nz\n", "0" * 64)


def test_update_section_never_matches_fenced_pseudo_headings(vault):
    with pytest.raises(ValueError, match="Section not found"):
        vault.service.update_section(
            NOTE, "## not a heading", "## not a heading\n\nx\n", "0" * 64
        )


def test_update_section_rejects_structure_breaking_replacements(vault):
    hashes = _section_hashes(vault.service)
    bad_contents = (
        "no heading on the first line\n",
        "### Beta\n\nwrong level\n",
        "## Beta\n\nok\n\n## Sneaky sibling\n\nwould split the section\n",
        "## Beta\n\nok\n\n# Shallower\n\nwould reshape the whole note\n",
    )
    for bad in bad_contents:
        with pytest.raises(ValueError):
            vault.service.update_section(NOTE, "## Beta", bad, hashes["## Beta"])
    assert vault.path.read_text(encoding="utf-8") == SAMPLE


def test_update_section_allows_renaming_at_the_same_level(vault):
    hashes = _section_hashes(vault.service)
    result = vault.service.update_section(
        NOTE, "## Gamma", "## Gamma (renamed)\n\ngamma body\n", hashes["## Gamma"]
    )
    assert result["committed"] is True
    text = vault.path.read_text(encoding="utf-8")
    assert "## Gamma (renamed)" in text
    assert text.endswith("gamma body\n"), "last section: no forced blank line at EOF"


def test_update_section_requires_write_enabled(vault):
    readonly = VaultService(make_settings(vault.root, vault.git_dir, write_enabled=False))
    with pytest.raises(PermissionError):
        readonly.update_section(NOTE, "## Beta", "## Beta\n\nx\n", "0" * 64)
