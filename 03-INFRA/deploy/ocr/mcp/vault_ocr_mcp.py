#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


API_URL = os.environ.get("VAULT_OCR_API_URL", "http://127.0.0.1:33003").rstrip("/")
MAX_LOCAL_BYTES = int(os.environ.get("VAULT_OCR_MAX_LOCAL_BYTES", "15728640"))
SERVER_NAME = "vault-ocr"
SERVER_VERSION = "1.0.0"
LOG_PATH = os.environ.get("VAULT_OCR_MCP_LOG")
FRAMING = "headers"


def debug(event: str, **fields: Any) -> None:
    if not LOG_PATH:
        return
    payload = {"event": event, **fields}
    try:
        with open(Path(LOG_PATH).expanduser(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


def read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            debug("stdin_eof")
            return None
        if line in (b"\r\n", b"\n"):
            break
        stripped = line.strip()
        if not headers and stripped.startswith((b"{", b"[")):
            global FRAMING
            FRAMING = "jsonl"
            req = json.loads(stripped.decode("utf-8"))
            if isinstance(req, dict):
                debug("recv_json_line", method=req.get("method"), id=req.get("id"), has_params="params" in req)
            else:
                debug("recv_json_line_non_object", payload_type=type(req).__name__)
            return req
        key, _, value = line.decode("ascii", errors="replace").partition(":")
        headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        debug("empty_message", headers=headers)
        return None
    req = json.loads(sys.stdin.buffer.read(length).decode("utf-8"))
    if isinstance(req, dict):
        debug("recv", method=req.get("method"), id=req.get("id"), has_params="params" in req)
    else:
        debug("recv_non_object", payload_type=type(req).__name__)
    return req


def write_message(payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if FRAMING == "jsonl":
        sys.stdout.buffer.write(data + b"\n")
    else:
        sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    debug("send", id=payload.get("id"), has_result="result" in payload, has_error="error" in payload)


def result(req_id: Any, value: Any) -> None:
    write_message({"jsonrpc": "2.0", "id": req_id, "result": value})


def error(req_id: Any, code: int, message: str) -> None:
    write_message({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def text_content(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def request_json(url: str, timeout: int = 20) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def read_local_image(path: Path) -> bytes:
    """Checks size via stat() before touching file content: a naive
    read_bytes()-then-check lets a 10GB file (or /dev/zero) get fully loaded
    into RAM before the limit is even consulted, OOM-killing the MCP
    process. stat() is O(1) regardless of file size."""
    size = path.stat().st_size
    if size > MAX_LOCAL_BYTES:
        raise ValueError(f"image too large: {size} bytes > {MAX_LOCAL_BYTES}")
    return path.read_bytes()


def safe_multipart_filename(name: str) -> str:
    """Escapes quotes/backslashes and strips CR/LF before embedding a
    filename in a multipart Content-Disposition line: unescaped, a name
    containing '"' breaks out of the filename attribute, and one containing
    \\r\\n can inject extra multipart headers/parts into the request body."""
    name = name.replace("\\", "\\\\").replace('"', '\\"')
    return name.replace("\r", "").replace("\n", "")


def multipart_request(path: Path, data: bytes, min_confidence: float, timeout: int = 120) -> dict[str, Any]:
    boundary = "----vault-ocr-" + uuid.uuid4().hex
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    filename = safe_multipart_filename(path.name)
    parts: list[bytes] = []
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8")
        + data
        + b"\r\n"
    )
    parts.append(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="min_confidence"\r\n\r\n'
            f"{min_confidence}\r\n"
        ).encode("utf-8")
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    req = urllib.request.Request(
        f"{API_URL}/ocr",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OCR API HTTP {exc.code}: {detail}") from exc


def extract_image(args: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(args.get("image_path", ""))).expanduser()
    min_conf = float(args.get("min_confidence", 0.0) or 0.0)
    include_lines = bool(args.get("include_lines", False))
    if not path.is_file():
        raise FileNotFoundError(f"image_path not found: {path}")
    data = read_local_image(path)
    digest = hashlib.sha256(data).hexdigest()
    payload = multipart_request(path, data, min_conf)
    lines = payload.get("lines", [])
    body = [
        f"# OCR Result: {path.name}",
        "",
        f"- sha256: `{payload.get('sha256') or digest}`",
        f"- size: {payload.get('image', {}).get('width')}x{payload.get('image', {}).get('height')} {payload.get('image', {}).get('format')}",
        f"- lines: {payload.get('line_count', 0)}",
        f"- avg_confidence: {payload.get('avg_confidence')}",
        f"- engine: {payload.get('engine')} ({payload.get('elapsed_sec')}s)",
        "",
        "## Text",
        "",
        payload.get("markdown") or "",
    ]
    if include_lines:
        body.extend(["", "## Lines JSON", "", "```json", json.dumps(lines, ensure_ascii=False, indent=2), "```"])
    body.extend(
        [
            "",
            "Persistence rule: if this text should be saved, write it through vault-library. The OCR tool does not write to the vault.",
        ]
    )
    return {"content": [text_content("\n".join(body))]}


def extract_batch(args: dict[str, Any]) -> dict[str, Any]:
    paths = args.get("image_paths") or []
    if not isinstance(paths, list) or not paths:
        raise ValueError("image_paths must be a non-empty array")
    min_conf = float(args.get("min_confidence", 0.0) or 0.0)
    chunks = []
    for item in paths:
        path = Path(str(item)).expanduser()
        try:
            data = read_local_image(path)
            payload = multipart_request(path, data, min_conf)
            chunks.append(
                "\n".join(
                    [
                        f"## {path}",
                        f"- sha256: `{payload.get('sha256')}`",
                        f"- lines: {payload.get('line_count', 0)}",
                        f"- avg_confidence: {payload.get('avg_confidence')}",
                        "",
                        payload.get("markdown") or "",
                    ]
                )
            )
        except Exception as exc:
            chunks.append(f"## {path}\n\nERROR: {exc}")
    chunks.append("\nPersistence rule: save durable text through vault-library, not through OCR.")
    return {"content": [text_content("\n\n".join(chunks))]}


TOOLS = [
    {
        "name": "ocr_healthcheck",
        "description": "Check whether the remote RapidOCR API is reachable through the local tunnel.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "ocr_extract_image",
        "description": "Extract printed or screen text from one local image. Does not write to the vault.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Local path to a PNG/JPG/WebP image."},
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0},
                "include_lines": {"type": "boolean", "default": False},
            },
            "required": ["image_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ocr_extract_batch",
        "description": "Extract text from multiple local images sequentially. Does not write to the vault.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "image_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0},
            },
            "required": ["image_paths"],
            "additionalProperties": False,
        },
    },
]


def handle(req: dict[str, Any]) -> None:
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        protocol = (req.get("params") or {}).get("protocolVersion") or "2024-11-05"
        result(
            req_id,
            {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "tools/list":
        result(req_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "ocr_healthcheck":
                payload = request_json(f"{API_URL}/health")
                result(req_id, {"content": [text_content(json.dumps(payload, ensure_ascii=False, indent=2))]})
            elif name == "ocr_extract_image":
                result(req_id, extract_image(args))
            elif name == "ocr_extract_batch":
                result(req_id, extract_batch(args))
            else:
                raise ValueError(f"unknown tool: {name}")
        except Exception as exc:
            result(req_id, {"content": [text_content(f"OCR tool error: {exc}")], "isError": True})
        return
    if req_id is not None:
        error(req_id, -32601, f"method not found: {method}")


def main() -> int:
    while True:
        req = read_message()
        if req is None:
            return 0
        handle(req)


if __name__ == "__main__":
    raise SystemExit(main())
