#!/usr/bin/env python3
"""Check Firecrawl search end to end, with a small success cache."""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_QUERY = '"Example Domain" site:example.com'


def read_cached_success(path: Path, ttl_seconds: int, now: float) -> dict[str, Any] | None:
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        checked_at = float(cached["checked_at"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if cached.get("ok") is True and 0 <= now - checked_at < ttl_seconds:
        return cached
    return None


def extract_urls(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data")
    if isinstance(data, dict):
        rows = data.get("web", [])
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return [
        row["url"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("url"), str)
    ]


def is_expected_result(url: str) -> bool:
    try:
        hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return hostname == "example.com" or hostname.endswith(".example.com")


def write_success(path: Path, now: float, result_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {"ok": True, "checked_at": now, "result_count": result_count},
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(path)


def probe(api_url: str, api_key: str, timeout: float) -> tuple[bool, int, str]:
    body = json.dumps({"query": DEFAULT_QUERY, "limit": 1}).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/v2/search",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return False, 0, f"request failed: {type(exc).__name__}"

    if not isinstance(payload, dict) or payload.get("success") is not True:
        return False, 0, "Firecrawl returned an unsuccessful response"
    urls = extract_urls(payload)
    if not any(is_expected_result(url) for url in urls):
        return False, len(urls), "search returned no result from example.com"
    return True, len(urls), "ok"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Firecrawl search end to end and cache successful probes."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("FIRECRAWL_API_URL", "http://127.0.0.1:33002"),
    )
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--ttl-seconds", type=int, default=86400)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = time.time()
    if not args.force:
        cached = read_cached_success(args.cache, args.ttl_seconds, now)
        if cached:
            print(
                json.dumps(
                    {
                        "status": "cached",
                        "result_count": int(cached.get("result_count", 0)),
                    },
                    separators=(",", ":"),
                )
            )
            return 0

    api_key = os.environ.get("FIRECRAWL_API_KEY", "local-self-hosted")
    ok, result_count, detail = probe(args.url, api_key, args.timeout)
    if not ok:
        print(
            json.dumps(
                {"status": "failed", "result_count": result_count, "detail": detail},
                separators=(",", ":"),
            )
        )
        return 1

    write_success(args.cache, now, result_count)
    print(
        json.dumps(
            {"status": "live", "result_count": result_count},
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
