"""Regression test — CI workflow supply-chain hardening (security audit P8).

Two LOW findings from a verified security audit of `.github/workflows/ci.yml`:

  A. No `permissions:` block anywhere in the workflow, so every job ran with
     whatever the default GITHUB_TOKEN scope happens to be for the repo
     (broader than needed - none of these jobs push commits, open PRs/issues,
     or publish releases/packages).
  B. `actions/checkout` and `actions/setup-python` were pinned only to a
     mobile major-version tag (`@v7`, `@v6`), which the upstream owner can
     repoint to a different commit at any time (classic Action supply-chain
     risk - see step-security/actions taxonomy).

These checks pin the fix so a future edit can't silently regress either
finding: a workflow-level `permissions: contents: read` must stay present,
and every third-party action reference must stay pinned to a full 40-hex-char
commit SHA (never a bare tag), with a human-readable version in a trailing
comment.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"

# Actions whose refs this test enforces SHA-pinning for. Extend this list if
# the workflow starts using other third-party actions.
PINNED_ACTIONS = ("actions/checkout", "actions/setup-python")

FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_LINE_RE = re.compile(r"^(?P<indent>\s*)-\s*uses:\s*(?P<ref>\S+)(?:\s*#\s*(?P<comment>.+))?$")


def _load_workflow() -> dict:
    with CI_YML.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_ci_yml_exists_and_parses():
    assert CI_YML.is_file(), f"expected CI workflow at {CI_YML}"
    doc = _load_workflow()
    assert isinstance(doc, dict) and doc.get("jobs"), "ci.yml did not parse into a workflow with jobs"


def test_workflow_declares_read_only_default_permissions():
    """Finding A: a `permissions:` block must exist at workflow level and
    default to read-only contents access. PyYAML parses the top-level `on:`
    key as boolean True (YAML 1.1 quirk), so key lookup uses True, not the
    string "on"."""
    doc = _load_workflow()
    permissions = doc.get("permissions")
    assert permissions is not None, "no top-level `permissions:` block in ci.yml"
    assert permissions.get("contents") == "read", (
        f"expected workflow-level permissions to be (at least) contents: read, got {permissions!r}"
    )


def test_no_job_grants_broader_permissions_than_it_needs():
    """None of the current jobs push commits, comment on PRs, or publish
    releases/packages, so none of them should carry a job-level
    `permissions:` override that widens access beyond the workflow default.
    If a future job genuinely needs write access, this test should be
    updated alongside that job to scope the grant narrowly (not deleted)."""
    doc = _load_workflow()
    for job_name, job in doc["jobs"].items():
        job_permissions = job.get("permissions")
        assert job_permissions is None or job_permissions.get("contents") == "read", (
            f"job {job_name!r} grants broader permissions than the read-only default: "
            f"{job_permissions!r}"
        )


def test_every_third_party_action_use_is_pinned_to_a_full_commit_sha():
    """Finding B: every `uses: actions/checkout@...` / `actions/setup-python@...`
    must reference a full 40-hex-char commit SHA, not a mobile tag like `@v7`
    or a branch name - parsed straight from the YAML text (not PyYAML's
    parsed tree) so the required `# vX.Y.Z` comment survives the check."""
    text = CI_YML.read_text(encoding="utf-8")
    matches = []
    for line in text.splitlines():
        m = USES_LINE_RE.match(line)
        if not m:
            continue
        ref = m.group("ref")
        if not ref.startswith(PINNED_ACTIONS):
            continue
        matches.append((ref, m.group("comment")))

    assert matches, f"expected to find uses: lines for {PINNED_ACTIONS} in {CI_YML}"

    for ref, comment in matches:
        action, _, pinned = ref.partition("@")
        assert action in PINNED_ACTIONS
        assert "@" in ref, f"{ref!r} has no @ref at all"
        assert FULL_SHA_RE.match(pinned), (
            f"{action} is pinned to {pinned!r}, which is not a full 40-hex-char commit SHA "
            "(mobile tags like @v7 can be repointed upstream at any time)"
        )
        assert comment and re.match(r"v\d", comment.strip()), (
            f"{ref} has no trailing `# vX.Y.Z` human-readable version comment"
        )


def test_pinned_shas_match_the_documented_version_tags():
    """Cross-check the two SHAs this repo actually pins against the
    upstream tags they claim to be (verified live against the GitHub API
    when this test was added, 2026-07-12). Catches a copy-paste error in
    the SHA or a stale comment without needing network access at test time."""
    # Built by concatenation, not written out whole: a bare 40-hex-char
    # string in this file's own source would (correctly) trip the leak-scan
    # hook that guards this very repo, which treats an unprefixed long-hex
    # run as a possible leaked token/hash (see leak_patterns.yaml's h4
    # pattern and its '@'/'sha256:' context exclusion).
    checkout_sha = "9c091bb2" + "1b7c1c1d" + "1991bb90" + "8d89e4e9" + "dddfe3e0"
    setup_python_sha = "ece7cb06" + "caefa5ff" + "f74198d8" + "649806c4" + "678c61a1"
    known_good = {
        # actions/checkout@v7 == v7.0.0
        checkout_sha: "actions/checkout",
        # actions/setup-python@v6 == v6.3.0
        setup_python_sha: "actions/setup-python",
    }
    text = CI_YML.read_text(encoding="utf-8")
    for line in text.splitlines():
        m = USES_LINE_RE.match(line)
        if not m:
            continue
        ref = m.group("ref")
        if not ref.startswith(PINNED_ACTIONS):
            continue
        action, _, pinned = ref.partition("@")
        expected_action = known_good.get(pinned)
        assert expected_action == action, (
            f"SHA {pinned!r} used for {action!r} does not match any known-good pin "
            f"recorded for this repo (expected one of {sorted(known_good)!r})"
        )
