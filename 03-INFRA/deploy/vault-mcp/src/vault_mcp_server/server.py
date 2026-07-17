from __future__ import annotations

from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Any
import json
import urllib.request
from urllib.parse import urlparse, urlencode

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
import uvicorn

from . import __version__
from .config import Settings
from .vault import VaultService


def _extract_token(headers: Headers) -> str:
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return headers.get("x-vault-token", "").strip()


def _build_transport_security(settings: Settings) -> TransportSecuritySettings:
    allowed_hosts = {
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
    }
    allowed_origins = {
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    }

    for origin in settings.allowed_origins:
        cleaned_origin = origin.strip()
        if not cleaned_origin:
            continue

        allowed_origins.add(cleaned_origin)
        parsed = urlparse(cleaned_origin)
        if parsed.netloc:
            allowed_hosts.add(parsed.netloc)
            if ":" not in parsed.netloc:
                allowed_hosts.add(f"{parsed.netloc}:*")

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(allowed_hosts),
        allowed_origins=sorted(allowed_origins),
    )


class McpSecurityMiddleware:
    def __init__(self, app: Any, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not self._is_mcp_path(path):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin and not self._origin_allowed(origin, headers):
            response = JSONResponse(
                {"error": "origin_not_allowed", "detail": "Origin header is not allowed for this MCP endpoint."},
                status_code=403,
            )
            await response(scope, receive, send)
            return

        if self.settings.vault_token:
            token = _extract_token(headers)
            if not token or not compare_digest(token, self.settings.vault_token):
                response = JSONResponse(
                    {"error": "unauthorized", "detail": "Missing or invalid bearer token."},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)

    def _is_mcp_path(self, path: str) -> bool:
        mcp_path = self.settings.mcp_path.rstrip("/") or "/"
        return path == mcp_path or path.startswith(f"{mcp_path}/")

    def _origin_allowed(self, origin: str, headers: Headers) -> bool:
        if origin in self.settings.allowed_origins:
            return True

        parsed = urlparse(origin)
        if not parsed.scheme or not parsed.netloc:
            return False

        host_candidates = {
            headers.get("host", ""),
            headers.get("x-forwarded-host", ""),
        }
        host_candidates = {value for value in host_candidates if value}

        expected_origins = {f"https://{host}" for host in host_candidates}
        expected_origins.update(f"http://{host}" for host in host_candidates)

        forwarded_proto = headers.get("x-forwarded-proto")
        forwarded_host = headers.get("x-forwarded-host")
        if forwarded_proto and forwarded_host:
            expected_origins.add(f"{forwarded_proto}://{forwarded_host}")

        return origin in expected_origins


def _call_semantic(settings: Settings, query: str, limit: int) -> dict[str, Any]:
    base = (settings.semantic_url or "").rstrip("/")
    url = f"{base}/search?{urlencode({'q': query, 'k': limit})}"
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create_server(settings: Settings) -> tuple[FastMCP, VaultService]:
    vault = VaultService(settings)
    server_name = "markdown-vault-git-backed" if settings.write_enabled else "markdown-vault-readonly"
    instructions = (
        "Git-backed MCP server for Markdown vaults. "
        "Use get_start_here first, then search_notes/read_note/list_related. "
        "Write tools are enabled for trusted clients only. "
        "Use create_note for new notes, append_note for additive updates, update_note with expected_hash "
        "for replacing whole notes, and update_section with a per-section hash (from read_note's `sections`) "
        "for surgical single-section edits. Every write is committed to Git."
        if settings.write_enabled
        else (
            "Read-only MCP server for Markdown vaults. "
            "Use get_start_here first, then search_notes/read_note/list_related. "
            "Never assume write access."
        )
    )
    mcp = FastMCP(
        name=server_name,
        instructions=instructions,
        stateless_http=settings.stateless_http,
        json_response=settings.json_response,
        transport_security=_build_transport_security(settings),
    )
    mcp.settings.streamable_http_path = settings.mcp_path

    @mcp.tool()
    def get_start_here() -> dict[str, Any]:
        """Return the vault entry note, usually 00-START-HERE.md."""

        return vault.get_start_here()

    @mcp.tool()
    def read_note(note_ref: str) -> dict[str, Any]:
        """Read a note by relative path, filename, or unique stem."""

        return vault.read_note(note_ref)

    @mcp.tool()
    def search_notes(query: str, limit: int = 10) -> dict[str, Any]:
        """Search note titles, paths, tags, and body text."""

        return vault.search_notes(query=query, limit=limit)

    @mcp.tool()
    def list_related(note_ref: str, limit: int = 10) -> dict[str, Any]:
        """List outgoing links, backlinks, and tag-based related notes."""

        return vault.list_related(note_ref=note_ref, limit=limit)

    @mcp.tool()
    def recent_activity(limit: int = 15) -> dict[str, Any]:
        """List recently changed notes (path, title, last-change date, and commit message when available). Use to resume context: what was touched recently in the vault."""

        return vault.recent_activity(limit=limit)

    if settings.semantic_enabled and settings.semantic_url:

        @mcp.tool()
        def semantic_search(query: str, limit: int = 5) -> dict[str, Any]:
            """Search the vault by meaning/concept (hybrid semantic + keyword), powered by the embedding sidecar. Use when search_notes misses or you do not know the exact words."""
            capped = max(1, min(limit, settings.semantic_max_limit))
            try:
                data = _call_semantic(settings, query, capped)
            except Exception as exc:  # sidecar down -> never break the MCP itself
                return {"error": "semantic_unavailable", "detail": repr(exc)}
            # if this instance has a path whitelist, filter results through it
            prefixes = settings.include_path_prefixes
            if prefixes and isinstance(data, dict) and isinstance(data.get("results"), list):
                data["results"] = [
                    r for r in data["results"]
                    if any(str(r.get("path", "")).startswith(p) for p in prefixes)
                ]
            return data

    if settings.write_enabled:

        @mcp.tool()
        def create_note(note_path: str, content: str, message: str = "") -> dict[str, Any]:
            """Create a new Markdown note and commit it to Git. Refuses to overwrite existing notes."""

            return vault.create_note(note_path=note_path, content=content, message=message)

        @mcp.tool()
        def append_note(note_path: str, content: str, message: str = "") -> dict[str, Any]:
            """Append Markdown content to a note, or create it if missing, then commit it to Git."""

            return vault.append_note(note_path=note_path, content=content, message=message)

        @mcp.tool()
        def update_note(note_ref: str, content: str, expected_hash: str, message: str = "") -> dict[str, Any]:
            """Replace an existing note only when expected_hash matches the current full content hash."""

            return vault.update_note(
                note_ref=note_ref,
                content=content,
                expected_hash=expected_hash,
                message=message,
            )

        @mcp.tool()
        def update_section(
            note_ref: str,
            section_heading: str,
            content: str,
            expected_hash: str,
            message: str = "",
        ) -> dict[str, Any]:
            """Replace ONE section of a note (its ATX heading plus body and subsections) when expected_hash matches that section's current hash from read_note's `sections`. Prefer this over update_note for surgical edits: smaller diffs, and concurrent edits to other sections stay valid."""

            return vault.update_section(
                note_ref=note_ref,
                section_heading=section_heading,
                content=content,
                expected_hash=expected_hash,
                message=message,
            )

    return mcp, vault


def create_app() -> Any:
    settings = Settings.from_env()
    mcp, vault = create_server(settings)

    async def homepage(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "name": "markdown-vault-git-backed" if settings.write_enabled else "markdown-vault-readonly",
                "version": __version__,
                "transport": "streamable-http",
                "mcp_path": settings.mcp_path,
                "health_path": settings.health_path,
                "vault_root": str(settings.vault_root),
                "note_count": vault.note_count(),
                "read_only": not settings.write_enabled,
                "write_enabled": settings.write_enabled,
                "authentication": "bearer" if settings.vault_token else "none",
            }
        )

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "note_count": vault.note_count(),
                "mcp_path": settings.mcp_path,
            }
        )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        async with mcp.session_manager.run():
            yield

    routes = [Route("/", endpoint=homepage), Route(settings.health_path, endpoint=health), Mount("/", app=mcp.streamable_http_app())]
    app: Any = Starlette(routes=routes, lifespan=lifespan)
    app = McpSecurityMiddleware(app, settings)

    if settings.allowed_origins:
        app = CORSMiddleware(
            app,
            allow_origins=list(settings.allowed_origins),
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Accept", "X-Vault-Token", "Mcp-Session-Id"],
            expose_headers=["Mcp-Session-Id"],
        )

    return app


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        create_app(),
        host=settings.host,
        port=settings.port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
