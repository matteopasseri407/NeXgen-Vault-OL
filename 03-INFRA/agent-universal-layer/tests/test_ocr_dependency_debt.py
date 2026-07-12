"""Regression tests for the OCR dependency-debt hardening pass (2026-07-12).

Covers three findings from the security audit:
  A (LOW): 03-INFRA/deploy/ocr/api/Dockerfile's `FROM python:3.12-slim` had
    no version pin at all -- a floating tag could silently change what
    actually gets built. Pinned to the explicit patch version
    (python:3.12.13-slim) that tag resolves to today, verified live
    against the Docker Registry HTTP API. A full `@sha256:<digest>` pin is
    a stronger final step, deliberately deferred: this repo's anti-leak
    pre-commit hook reads its pattern file from the maintainer's private
    vault (out of this package's scope to touch) and has no allowlist
    entry yet for a bare 40+ char hex digest, even a public, non-secret
    one -- see the Dockerfile's header comment for the exact follow-up.
  B (LOW): .github/workflows/ci.yml applied pip-audit's --ignore-vuln
    exception (meant only for the OCR stack's tracked starlette debt) to
    EVERY requirements*.txt the repo-wide find|xargs sweep discovered, not
    just the OCR one.
  DEBT: fastapi==0.115.6 pinned `starlette<0.42.0` (8 open CVEs, no patched
    starlette release satisfies that pin). Bumped to fastapi==0.139.0
    (-> starlette>=1.3.1) and validated for real, 2026-07-12: a live
    TestClient round-trip through the real (non-stubbed) RapidOCR engine
    succeeded on /health and /ocr, and pip-audit against the bumped pins
    reports zero known vulnerabilities (Docker itself is not available in
    this test environment, matching the sandboxing note in
    test_deploy_compose_pins.py).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO / "03-INFRA" / "deploy" / "ocr" / "api" / "Dockerfile"
REQUIREMENTS = REPO / "03-INFRA" / "deploy" / "ocr" / "api" / "requirements.txt"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"

# Versions that actually fix the advisories fastapi==0.115.6's
# `starlette<0.42.0` pin left open (verified on osv.dev, 2026-07-12):
#   PYSEC-2026-1941 / CVE-2025-54121  -> fixed 0.47.2
#   PYSEC-2026-1942 / CVE-2025-62727  -> fixed 0.49.1
#   PYSEC-2026-161  / CVE-2026-48710  -> fixed 1.0.1
#   CVE-2026-48817                    -> fixed 1.1.0
#   CVE-2026-48818                    -> fixed 1.1.0
#   PYSEC-2026-248  / CVE-2026-54282  -> fixed 1.3.0
#   PYSEC-2026-249  / CVE-2026-54283  -> fixed 1.3.1
MIN_FASTAPI = (0, 139, 0)
IGNORED_STARLETTE_VULN_IDS = (
    "PYSEC-2026-161",
    "PYSEC-2026-249",
    "PYSEC-2026-248",
    "PYSEC-2026-1942",
    "PYSEC-2026-1941",
    "CVE-2026-48818",
    "CVE-2026-48817",
)


def test_dockerfile_base_image_is_pinned_to_an_explicit_version():
    content = DOCKERFILE.read_text(encoding="utf-8")
    from_lines = [line for line in content.splitlines() if line.strip().startswith("FROM ")]
    assert from_lines, "Dockerfile has no FROM line"
    assert len(from_lines) == 1, f"expected exactly one FROM line, got {from_lines!r}"
    from_line = from_lines[0].strip()
    assert from_line != "FROM python:3.12-slim", "base image still uses the bare, floating 3.12-slim tag"
    match = re.match(r"^FROM python:(\d+)\.(\d+)\.(\d+)-slim(@sha256:[0-9a-f]{64})?$", from_line)
    assert match, (
        f"expected FROM python:<major>.<minor>.<patch>-slim[@sha256:<digest>], got {from_line!r}"
    )
    assert (int(match.group(1)), int(match.group(2))) == (3, 12), (
        f"expected the 3.12 line, got {from_line!r}"
    )


def test_requirements_fastapi_pin_clears_the_known_starlette_advisories():
    text = REQUIREMENTS.read_text(encoding="utf-8")
    match = re.search(r"^fastapi==([0-9]+\.[0-9]+\.[0-9]+)\s*$", text, re.MULTILINE)
    assert match, "fastapi is not pinned with == in requirements.txt"
    pinned = tuple(int(part) for part in match.group(1).split("."))
    assert pinned >= MIN_FASTAPI, (
        f"fastapi=={match.group(1)} is older than {'.'.join(map(str, MIN_FASTAPI))}, "
        "the first release whose starlette upper bound can actually satisfy a "
        "patched starlette (>=1.3.1)"
    )


def _load_ci_workflow() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _invokes_pip_audit(run: str) -> bool:
    """True if `run` actually invokes the pip-audit command somewhere
    (directly, or as an xargs/pipe target), not merely `pip install
    pip-audit`."""
    without_install = run.replace("pip install pip-audit", "")
    return "pip-audit" in without_install


def test_dependency_audit_job_has_two_pip_audit_steps():
    steps = _load_ci_workflow()["jobs"]["dependency-audit"]["steps"]
    audit_steps = [s for s in steps if _invokes_pip_audit(s.get("run", ""))]
    assert len(audit_steps) == 2, (
        f"expected exactly 2 pip-audit-invoking steps (OCR-scoped + everything "
        f"else), found {len(audit_steps)}: {[s.get('name') for s in audit_steps]!r}"
    )


def test_ignore_vuln_flags_only_ever_target_the_ocr_requirements_file():
    """The bug this regression guards: a single find|xargs sweep used to
    apply --ignore-vuln to every requirements*.txt discovered, OCR or not.
    Any step that mentions --ignore-vuln must be scoped to exactly the OCR
    path and must NOT run a repo-wide find/xargs discovery sweep."""
    steps = _load_ci_workflow()["jobs"]["dependency-audit"]["steps"]
    for step in steps:
        run = step.get("run", "")
        if "--ignore-vuln" not in run:
            continue
        assert "03-INFRA/deploy/ocr/api/requirements.txt" in run, (
            f"step {step.get('name')!r} uses --ignore-vuln but isn't scoped "
            "to the OCR requirements.txt path"
        )
        assert "find " not in run, (
            f"step {step.get('name')!r} uses --ignore-vuln together with a "
            "repo-wide find sweep -- exactly the bug this test guards against"
        )


def test_non_ocr_sweep_step_never_uses_ignore_vuln_and_excludes_ocr_path():
    steps = _load_ci_workflow()["jobs"]["dependency-audit"]["steps"]
    sweep_steps = [s for s in steps if "find " in s.get("run", "") and "pip-audit" in s.get("run", "")]
    assert sweep_steps, "expected a find-based pip-audit sweep step for non-OCR requirements files"
    for step in sweep_steps:
        run = step["run"]
        assert "--ignore-vuln" not in run, (
            f"step {step.get('name')!r} sweeps requirements*.txt but still carries "
            "--ignore-vuln flags -- those must stay confined to the OCR-only step"
        )
        assert "-prune" in run and "03-INFRA/deploy/ocr/api/requirements.txt" in run, (
            f"step {step.get('name')!r} must explicitly exclude the OCR requirements.txt "
            "from its own discovery sweep (it's already covered by the OCR-scoped step)"
        )


def test_ocr_step_pip_audit_currently_needs_no_ignore_vuln_flags():
    """Documents the intended end state once the fastapi bump lands: with
    fastapi pinned above MIN_FASTAPI, none of the previously-ignored IDs
    should still be necessary as a suppression in CI."""
    steps = _load_ci_workflow()["jobs"]["dependency-audit"]["steps"]
    ocr_steps = [
        s for s in steps
        if "03-INFRA/deploy/ocr/api/requirements.txt" in s.get("run", "") and "pip-audit" in s.get("run", "")
    ]
    assert ocr_steps, "expected a pip-audit step scoped to the OCR requirements.txt"
    for step in ocr_steps:
        for vuln_id in IGNORED_STARLETTE_VULN_IDS:
            assert vuln_id not in step["run"], (
                f"step {step.get('name')!r} still ignores {vuln_id}, but the fastapi "
                "pin in requirements.txt should already clear it -- either the bump "
                "regressed or this leftover suppression should be removed"
            )
