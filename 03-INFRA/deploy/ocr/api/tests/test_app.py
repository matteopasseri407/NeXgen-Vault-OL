"""Regression tests for the vault-ocr FastAPI service (../app.py).

Covers the NX-02 audit fix (2026-07-10):
  - requirements.txt pins python-multipart / Pillow above the versions that
    fix known OSV advisories.
  - POST /ocr rejects image formats outside an explicit allowlist.
  - POST /ocr enforces MAX_BYTES by reading the upload in bounded chunks
    and aborting as soon as the running total exceeds the limit, instead
    of buffering the whole body first (await file.read()) and checking
    the size afterwards.

Covers the auth hardening pass (2026-07-12, finding B): POST /ocr gates on
an optional VAULT_OCR_TOKEN bearer token, defaulting to WARN-and-allow when
unset for backward compatibility with existing local/tunnel-only deploys.

rapidocr/onnxruntime are intentionally NOT installed for these tests (too
heavy for a unit-test run): a stub module satisfies app.py's top-level
`from rapidocr import RapidOCR`, and get_engine() is monkeypatched with a
light fake per test.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

API_DIR = Path(__file__).resolve().parents[1]
APP_PATH = API_DIR / "app.py"
REQUIREMENTS_PATH = API_DIR / "requirements.txt"

# Minimum versions that actually fix the advisories the audit flagged
# (verified on osv.dev / pypi.org 2026-07-12):
#   python-multipart < 0.0.27 -> GHSA-pp6c-gr5w-3c5g (unbounded part headers, DoS)
#   python-multipart < 0.0.30 -> GHSA-5rvq-cxj2-64vf (quadratic separator scan, DoS)
#   Pillow < 12.2.0            -> GHSA-whj4-6x5x-4v2j (FITS gzip decompression bomb)
#                                  and GHSA-pwv6-vv43-88gr (PSD tile OOB write)
MIN_PYTHON_MULTIPART = (0, 0, 30)
MIN_PILLOW = (12, 2, 0)


def _pinned_version(requirements_text: str, package: str) -> tuple[int, ...]:
    match = re.search(rf"^{re.escape(package)}==([0-9][0-9.]*)\s*$", requirements_text, re.MULTILINE)
    assert match, f"{package} is not pinned with == in requirements.txt"
    return tuple(int(part) for part in match.group(1).split("."))


def test_python_multipart_pin_fixes_known_advisories():
    text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    assert _pinned_version(text, "python-multipart") >= MIN_PYTHON_MULTIPART


def test_pillow_pin_fixes_known_advisories():
    text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    assert _pinned_version(text, "Pillow") >= MIN_PILLOW


def _install_rapidocr_stub() -> None:
    """app.py does `from rapidocr import RapidOCR` at import time. The real
    package pulls in onnxruntime and is too heavy to install for unit
    tests; a stub satisfies the import so tests can monkeypatch
    get_engine() instead of ever constructing a real engine."""
    if "rapidocr" in sys.modules:
        return
    stub = types.ModuleType("rapidocr")

    class RapidOCR:  # pragma: no cover - never actually called in tests
        def __call__(self, *args, **kwargs):
            raise AssertionError("real RapidOCR engine must never run in tests")

    stub.RapidOCR = RapidOCR
    sys.modules["rapidocr"] = stub


def load_app_module():
    """Fresh copy of app.py per test, so one test's monkeypatches (ENGINE
    cache, get_engine, MAX_BYTES) never leak into another."""
    _install_rapidocr_stub()
    spec = importlib.util.spec_from_file_location(f"vault_ocr_app_under_test_{id(object())}", APP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def app_module():
    return load_app_module()


class FakeOCRResult:
    def __init__(self):
        self.txts = ["hola vault"]
        self.scores = [0.987]
        self.boxes = [[[0.0, 0.0], [10.0, 0.0], [10.0, 5.0], [0.0, 5.0]]]
        self.elapse = 0.01


class FakeEngine:
    def __call__(self, data: bytes, text_score=None):
        return FakeOCRResult()


@pytest.fixture
def client(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "get_engine", lambda: FakeEngine())
    return TestClient(app_module.app)


def _image_bytes(fmt: str, size=(10, 10), color=(200, 50, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format=fmt)
    return buf.getvalue()


def test_valid_allowlisted_format_is_accepted(client):
    response = client.post(
        "/ocr",
        files={"file": ("page.png", _image_bytes("PNG"), "image/png")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["image"]["format"] == "PNG"
    assert body["markdown"] == "hola vault"


def test_format_outside_allowlist_is_rejected(client):
    response = client.post(
        "/ocr",
        files={"file": ("page.gif", _image_bytes("GIF"), "image/gif")},
    )
    assert response.status_code == 415
    assert "GIF" in response.json()["detail"]


def test_oversized_upload_is_rejected_end_to_end(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "get_engine", lambda: FakeEngine())
    monkeypatch.setattr(app_module, "MAX_BYTES", 100)
    client = TestClient(app_module.app)

    payload = _image_bytes("PNG", size=(200, 200))  # comfortably over 100 bytes once encoded
    assert len(payload) > 100
    response = client.post("/ocr", files={"file": ("big.png", payload, "image/png")})
    assert response.status_code == 413


class HugeFakeUpload:
    """Minimal double for fastapi.UploadFile: serves an effectively
    unbounded stream in fixed windows and records how many bytes were ever
    handed out. Regression guard for the ordering bug: the old handler did
    `data = await file.read()` (no size argument) BEFORE ever comparing
    against MAX_BYTES, forcing a full read (and full buffering) of an
    oversized upload before rejecting it. This proves the replacement
    stops pulling from the stream shortly after crossing the limit,
    instead of draining it to the end."""

    def __init__(self, total_size: int):
        self._remaining = total_size
        self.bytes_served = 0
        self.read_calls = 0

    async def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        n = self._remaining if size in (-1, None) else min(size, self._remaining)
        if n <= 0:
            return b""
        self._remaining -= n
        self.bytes_served += n
        return b"\xff" * n


def test_read_upload_within_limit_stops_reading_early(app_module):
    max_bytes = 1000
    total_size = 10_000_000  # ~10,000x max_bytes: a full drain would never finish this test quickly
    fake = HugeFakeUpload(total_size)

    async def run():
        with pytest.raises(app_module.HTTPException) as excinfo:
            await app_module.read_upload_within_limit(fake, max_bytes)
        return excinfo.value

    exc = asyncio.run(run())

    assert exc.status_code == 413
    # Proves early interruption, not just the final response: only a
    # handful of chunk-sized reads happened, not enough to drain the
    # "huge" stream.
    assert fake.bytes_served <= max_bytes + app_module.READ_CHUNK_BYTES
    assert fake.bytes_served < total_size
    assert fake.read_calls < 50


def test_read_upload_within_limit_returns_full_body_when_within_limit(app_module):
    fake = HugeFakeUpload(500)

    async def run():
        return await app_module.read_upload_within_limit(fake, 1000)

    data = asyncio.run(run())
    assert data == b"\xff" * 500
    assert fake.bytes_served == 500


# --- Auth (finding B): POST /ocr gates on an optional VAULT_OCR_TOKEN. ------


def load_app_module_with_token(monkeypatch, token: str | None) -> Any:
    """Loads a fresh app.py with VAULT_OCR_TOKEN set (or explicitly unset)
    BEFORE import, since app.py reads it into a module-level constant at
    import time -- setting it after the fact on the loaded module would not
    exercise the same code path a real process start does."""
    if token is None:
        monkeypatch.delenv("VAULT_OCR_TOKEN", raising=False)
    else:
        monkeypatch.setenv("VAULT_OCR_TOKEN", token)
    return load_app_module()


@pytest.fixture
def client_no_token(monkeypatch):
    """Default/backward-compatible posture: VAULT_OCR_TOKEN unset."""
    from fastapi.testclient import TestClient

    mod = load_app_module_with_token(monkeypatch, None)
    monkeypatch.setattr(mod, "get_engine", lambda: FakeEngine())
    return TestClient(mod.app)


@pytest.fixture
def client_with_token(monkeypatch):
    """VAULT_OCR_TOKEN set: the API now requires a matching bearer token."""
    from fastapi.testclient import TestClient

    mod = load_app_module_with_token(monkeypatch, "s3cr3t-test-token")
    monkeypatch.setattr(mod, "get_engine", lambda: FakeEngine())
    return TestClient(mod.app)


def test_ocr_without_token_configured_accepts_unauthenticated_requests(client_no_token):
    """Backward compatibility: an operator who never set VAULT_OCR_TOKEN
    (every deploy before this hardening pass, and any local/tunnel-only
    deploy today) must keep working exactly as before."""
    response = client_no_token.post(
        "/ocr",
        files={"file": ("page.png", _image_bytes("PNG"), "image/png")},
    )
    assert response.status_code == 200


def test_ocr_without_token_configured_logs_warning_once(client_no_token, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="vault_ocr_api"):
        for _ in range(3):
            client_no_token.post(
                "/ocr",
                files={"file": ("page.png", _image_bytes("PNG"), "image/png")},
            )
    warnings = [r for r in caplog.records if "VAULT_OCR_TOKEN is not set" in r.message]
    assert len(warnings) == 1, "expected exactly one warning, not one per request"


def test_ocr_with_token_configured_rejects_missing_header(client_with_token):
    response = client_with_token.post(
        "/ocr",
        files={"file": ("page.png", _image_bytes("PNG"), "image/png")},
    )
    assert response.status_code == 401


def test_ocr_with_token_configured_rejects_wrong_token(client_with_token):
    response = client_with_token.post(
        "/ocr",
        files={"file": ("page.png", _image_bytes("PNG"), "image/png")},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_ocr_with_token_configured_accepts_correct_bearer_token(client_with_token):
    response = client_with_token.post(
        "/ocr",
        files={"file": ("page.png", _image_bytes("PNG"), "image/png")},
        headers={"Authorization": "Bearer s3cr3t-test-token"},
    )
    assert response.status_code == 200


def test_health_endpoint_never_requires_a_token(client_with_token):
    """/health stays open even with VAULT_OCR_TOKEN set: it backs the
    container's own Docker healthcheck (see ../../docker-compose.yml),
    which does not send an Authorization header."""
    response = client_with_token.get("/health")
    assert response.status_code == 200
