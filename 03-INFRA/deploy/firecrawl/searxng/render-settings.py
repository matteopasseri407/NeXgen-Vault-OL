#!/usr/bin/env python3
"""Render the private SearXNG settings file without logging its secrets."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def render_settings(destination: Path) -> None:
    api_key = required_env("BRAVE_SEARCH_API_KEY")
    secret_key = required_env("FIRECRAWL_SEARXNG_SECRET")

    content = f"""\
use_default_settings: true

server:
  secret_key: {json.dumps(secret_key)}
  base_url: "http://firecrawl-searxng:8080/"
  limiter: false
  public_instance: false

search:
  formats:
    - html
    - json

engines:
  - name: braveapi
    engine: braveapi
    inactive: false
    api_key: {json.dumps(api_key)}
    results_per_page: 20
"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(destination)


def main() -> int:
    destination = Path(
        sys.argv[1] if len(sys.argv) > 1 else "/etc/searxng/settings.yml"
    )
    render_settings(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
