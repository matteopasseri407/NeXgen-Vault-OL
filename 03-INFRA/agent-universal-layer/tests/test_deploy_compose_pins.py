"""Regression tests for the Cloud-Server deploy profile's reproducibility.

Covers the NX-07 audit findings for 03-INFRA/deploy/: pinned (non-"latest")
image tags with coherent healthchecks in the three docker-compose.yml
files, and a POSIX-correct, Compose-v2 bootstrap-vps.sh.

Docker itself is not available in this test environment (no daemon, no
registry access), so compose validity is checked with PyYAML rather than
`docker compose config` — CI adds that step separately.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest
import yaml


REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "03-INFRA" / "deploy"
COMPOSE_FILES = {
    "n8n": DEPLOY / "n8n" / "docker-compose.yml",
    "ocr": DEPLOY / "ocr" / "docker-compose.yml",
    "firecrawl": DEPLOY / "firecrawl" / "docker-compose.yml",
}
BOOTSTRAP = DEPLOY / "bootstrap-vps.sh"

# A real, explicit version tag: at least major.minor, optionally
# .patch/-suffix (e.g. 2.29.10, 1.0.0, 8.2.7-alpine). Rejects "latest" and
# other floating tags.
VERSION_TAG = re.compile(r"^\d+(\.\d+){1,2}(-[A-Za-z0-9][A-Za-z0-9.]*)?$")

# Narrow, documented exception: verified 2026-07-12 that ghcr.io/firecrawl/
# playwright-service publishes NO version-numbered tag at all (only
# latest/linux-amd64/buildcache variants) -- there is no floating-vs-pinned
# choice to make here, "latest" is the only tag that exists. Real
# reproducibility for this one service has to come from a pinned sha256
# digest (documented in the compose file's header comment), not a tag.
FLOATING_TAG_EXCEPTIONS = {("firecrawl", "firecrawl-playwright")}


def _load_compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _image_tag(image_ref: str) -> str:
    """Extracts the tag from an image reference, including the
    ${VAR:-repo/image:tag} form used by every service here."""
    tag = image_ref.rsplit(":", 1)[-1]
    return tag.rstrip("}")


def test_all_compose_files_load_with_pyyaml():
    for name, path in COMPOSE_FILES.items():
        assert path.is_file(), f"{name}: compose file missing at {path}"
        data = _load_compose(path)
        assert "services" in data, f"{name}: no top-level services key"
        assert data["services"], f"{name}: services block is empty"


def test_no_compose_image_uses_a_latest_tag():
    for name, path in COMPOSE_FILES.items():
        data = _load_compose(path)
        for service, cfg in data["services"].items():
            if (name, service) in FLOATING_TAG_EXCEPTIONS:
                continue
            image = cfg.get("image")
            assert image, f"{name}/{service}: no image key"
            tag = _image_tag(image)
            assert tag != "latest", f"{name}/{service}: image pinned to :latest ({image!r})"
            assert VERSION_TAG.match(tag), (
                f"{name}/{service}: tag {tag!r} does not look like an explicit "
                f"version (image={image!r})"
            )


def test_floating_tag_exception_is_actually_floating_and_documented():
    """The exception list must stay narrow: each entry really has no
    versioned tag upstream (still `latest` today) and the compose file
    documents why, so the exception doesn't silently rot into a plain
    unpinned image nobody explains."""
    for name, service in FLOATING_TAG_EXCEPTIONS:
        data = _load_compose(COMPOSE_FILES[name])
        image = data["services"][service]["image"]
        assert _image_tag(image) == "latest", (
            f"{name}/{service} is listed as a floating-tag exception but its "
            f"tag is {_image_tag(image)!r}, not 'latest' -- either upstream "
            f"started publishing versions (remove the exception and pin one) "
            f"or this entry is stale"
        )
        content = COMPOSE_FILES[name].read_text(encoding="utf-8")
        assert "no versioned tags" in content or "NO versioned tags" in content, (
            f"{name}/{service}'s floating tag must stay explained in the "
            f"compose file's header comment"
        )


def test_every_service_has_a_coherent_healthcheck():
    required_keys = {"test", "interval", "timeout", "retries"}
    for name, path in COMPOSE_FILES.items():
        data = _load_compose(path)
        for service, cfg in data["services"].items():
            healthcheck = cfg.get("healthcheck")
            assert healthcheck, f"{name}/{service}: no healthcheck"
            missing = required_keys - healthcheck.keys()
            assert not missing, f"{name}/{service}: healthcheck missing {missing}"
            test = healthcheck["test"]
            assert isinstance(test, list) and test, f"{name}/{service}: empty healthcheck test"


def test_bootstrap_vps_has_a_bash_shebang():
    first_line = BOOTSTRAP.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/usr/bin/env bash", f"unexpected shebang line: {first_line!r}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bits are not the Windows permission model.")
def test_bootstrap_vps_is_executable():
    mode = BOOTSTRAP.stat().st_mode
    assert mode & stat.S_IXUSR, "bootstrap-vps.sh must be executable"


def test_bootstrap_vps_has_strict_mode():
    content = BOOTSTRAP.read_text(encoding="utf-8")
    assert "set -euo pipefail" in content


def test_bootstrap_vps_uses_compose_v2_not_legacy():
    content = BOOTSTRAP.read_text(encoding="utf-8")
    # The original bug: `docker-compose -f <file> up ...` (legacy v1 CLI).
    # Matched narrowly on "-f" so this doesn't flag prose that merely
    # mentions the legacy command name (e.g. an unsupported-tool message)
    # or the unrelated `docker-compose.yml` filenames.
    legacy_invocations = re.findall(r"docker-compose\s+-f\b", content)
    assert not legacy_invocations, f"found legacy docker-compose invocation(s): {legacy_invocations}"
    assert re.search(r"docker compose\s+-f\b", content), "expected at least one `docker compose -f` (v2) invocation"
    assert re.search(r"n8n/docker-compose\.yml", content)
    assert re.search(r"firecrawl/docker-compose\.yml", content)
    assert re.search(r"ocr/docker-compose\.yml", content)
