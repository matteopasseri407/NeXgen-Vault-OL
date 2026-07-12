from __future__ import annotations

import hashlib
import io
import logging
import os
import secrets
import time
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from PIL import Image
from rapidocr import RapidOCR


MAX_BYTES = int(os.getenv("VAULT_OCR_MAX_BYTES", "15728640"))
READ_CHUNK_BYTES = 65536
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
ENGINE: RapidOCR | None = None

# Bearer-token gate, extending the pattern already used by n8n-mcp/
# vault-library to this service. Optional by design: this API has
# historically had no auth at all and is normally reached only via an SSH
# tunnel bound to 127.0.0.1 (see ../../README.md), so requiring a token
# unconditionally would break every existing local/tunnel-only deploy the
# moment this ships. If VAULT_OCR_TOKEN is unset or empty, requests are
# still accepted but a warning is logged once (not once per request) so an
# operator notices the gap. Set VAULT_OCR_TOKEN (see .env.example) to
# actually require the header.
VAULT_OCR_TOKEN = os.getenv("VAULT_OCR_TOKEN", "").strip()
_logger = logging.getLogger("vault_ocr_api")
_warned_no_token = False


async def require_ocr_token(authorization: str | None = Header(default=None)) -> None:
    global _warned_no_token
    if not VAULT_OCR_TOKEN:
        if not _warned_no_token:
            _logger.warning(
                "VAULT_OCR_TOKEN is not set: the OCR API is accepting requests "
                "without authentication. Set VAULT_OCR_TOKEN in .env to require "
                "a bearer token (see .env.example)."
            )
            _warned_no_token = True
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(presented, VAULT_OCR_TOKEN):
        raise HTTPException(status_code=401, detail="invalid bearer token")


app = FastAPI(
    title="Vault OCR API",
    version="1.0.0",
    description="RapidOCR extraction service for the user's agent layer. It never writes to the vault.",
)


def get_engine() -> RapidOCR:
    global ENGINE
    if ENGINE is None:
        ENGINE = RapidOCR()
    return ENGINE


async def read_upload_within_limit(file: UploadFile, max_bytes: int) -> bytes:
    """Reads an UploadFile in bounded chunks, aborting as soon as the
    running total exceeds max_bytes instead of buffering the whole body
    first. Keeps a hostile client from forcing a full read (and full
    memory allocation) of an oversized upload before it gets rejected."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"image too large: exceeds {max_bytes} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def validate_image(data: bytes) -> dict[str, Any]:
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format or "unknown"
            width, height = img.size
            img.verify()
    except Exception as exc:
        raise HTTPException(status_code=415, detail=f"unsupported or invalid image: {exc}") from exc
    if fmt not in ALLOWED_IMAGE_FORMATS:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported image format: {fmt}. allowed: {sorted(ALLOWED_IMAGE_FORMATS)}",
        )
    return {"format": fmt, "width": width, "height": height}


def box_to_list(box: Any) -> list[list[float]]:
    if hasattr(box, "tolist"):
        box = box.tolist()
    return [[float(x), float(y)] for x, y in box]


def markdown_from_lines(lines: list[dict[str, Any]]) -> str:
    if not lines:
        return ""
    return "\n".join(line["text"] for line in lines)


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "engine": "rapidocr",
        "engine_loaded": ENGINE is not None,
        "max_bytes": MAX_BYTES,
    }


@app.post("/ocr")
async def ocr(
    file: UploadFile = File(...),
    min_confidence: float = Form(0.0),
    _auth: None = Depends(require_ocr_token),
) -> dict[str, Any]:
    data = await read_upload_within_limit(file, MAX_BYTES)
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    if min_confidence < 0 or min_confidence > 1:
        raise HTTPException(status_code=400, detail="min_confidence must be between 0 and 1")

    image_meta = validate_image(data)
    digest = hashlib.sha256(data).hexdigest()
    started = time.perf_counter()
    result = get_engine()(data, text_score=min_confidence or None)
    elapsed = time.perf_counter() - started

    txts_raw = getattr(result, "txts", None)
    scores_raw = getattr(result, "scores", None)
    boxes_raw = getattr(result, "boxes", None)
    txts = list(txts_raw) if txts_raw is not None else []
    scores = list(scores_raw) if scores_raw is not None else []
    boxes = list(boxes_raw) if boxes_raw is not None else []
    lines: list[dict[str, Any]] = []
    for idx, text in enumerate(txts):
        score = float(scores[idx]) if idx < len(scores) else None
        box = box_to_list(boxes[idx]) if idx < len(boxes) else None
        lines.append(
            {
                "index": idx,
                "text": str(text),
                "confidence": score,
                "box": box,
            }
        )

    avg_confidence = None
    if scores:
        avg_confidence = round(sum(float(s) for s in scores) / len(scores), 5)

    return {
        "status": "ok",
        "filename": file.filename,
        "sha256": digest,
        "bytes": len(data),
        "image": image_meta,
        "engine": "rapidocr",
        "elapsed_sec": round(elapsed, 4),
        "engine_elapsed_sec": round(float(getattr(result, "elapse", elapsed) or elapsed), 4),
        "line_count": len(lines),
        "avg_confidence": avg_confidence,
        "markdown": markdown_from_lines(lines),
        "lines": lines,
    }
