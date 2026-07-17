from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import hashlib
import os
import re
import subprocess
import time

from .config import Settings


NOTE_EXTENSIONS = {".md", ".markdown", ".mdown", ".mdx"}
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
TAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9/_-]+)")
HEADING_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
NON_WORD_RE = re.compile(r"[^a-z0-9]+")
ATX_HEADING_LINE_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*$")
ATX_CLOSING_HASHES_RE = re.compile(r"[ \t]+#+$")
FENCE_LINE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
SECTION_HEADING_ARG_RE = re.compile(r"(#{1,6})\s+(\S.*)$")


@dataclass(frozen=True, slots=True)
class NoteRecord:
    abs_path: Path
    rel_path: str
    title: str
    body: str
    full_content: str
    content_hash: str
    size_bytes: int
    modified_at: str
    truncated: bool
    tags: tuple[str, ...]
    raw_links: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NoteSection:
    heading: str
    text: str
    level: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class VaultIndex:
    records: tuple[NoteRecord, ...]
    by_relative_lower: dict[str, NoteRecord]
    by_absolute_lower: dict[str, NoteRecord]
    by_lookup: dict[str, tuple[NoteRecord, ...]]


def _normalize_lookup(value: str) -> str:
    lowered = value.lower().strip()
    lowered = lowered.removesuffix(".md")
    lowered = lowered.removesuffix(".markdown")
    lowered = lowered.removesuffix(".mdown")
    lowered = lowered.removesuffix(".mdx")
    return NON_WORD_RE.sub(" ", lowered).strip()


def _normalize_prefix(value: str) -> str:
    return value.replace("\\", "/").strip().lstrip("./").lstrip("/").lower()


def _prefix_matches(rel_path: str, prefix: str) -> bool:
    normalized_path = rel_path.replace("\\", "/").strip().lower()
    normalized_prefix = _normalize_prefix(prefix)
    if not normalized_prefix:
        return False
    return normalized_path == normalized_prefix or normalized_path.startswith(normalized_prefix)


def _read_text(path: Path, max_bytes: int) -> tuple[str, bool]:
    with path.open("rb") as handle:
        raw_bytes = handle.read(max_bytes + 1)
    truncated = len(raw_bytes) > max_bytes
    text = raw_bytes[:max_bytes].decode("utf-8", errors="ignore")
    return text, truncated


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_frontmatter(text: str) -> tuple[str, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return "", text
    return match.group(1), text[match.end() :]


def _extract_title(text: str, path: Path) -> str:
    _, content = _strip_frontmatter(text)
    heading_match = HEADING_RE.search(content)
    if heading_match:
        return heading_match.group(1).strip()

    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:120]

    return path.stem


def _extract_tags(text: str) -> tuple[str, ...]:
    frontmatter, content = _strip_frontmatter(text)
    tags: set[str] = set()

    for tag in TAG_RE.findall(content):
        tags.add(tag.strip().lower())

    tag_line_match = re.search(r"(?mi)^tags:\s*(.+)$", frontmatter)
    if tag_line_match:
        raw_tags = tag_line_match.group(1).strip().strip("[]")
        for tag in raw_tags.split(","):
            cleaned = tag.strip().strip("'\"").lower()
            if cleaned:
                tags.add(cleaned)

    return tuple(sorted(tags))


def _extract_links(text: str) -> tuple[str, ...]:
    links: list[str] = []

    for match in WIKILINK_RE.findall(text):
        target = match.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            links.append(target)

    for match in MARKDOWN_LINK_RE.findall(text):
        target = match.strip()
        if "://" in target or target.startswith(("mailto:", "#")):
            continue
        target = target.split("#", 1)[0].split("?", 1)[0].strip()
        if target:
            links.append(target)

    deduplicated: list[str] = []
    seen: set[str] = set()
    for link in links:
        key = link.lower()
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(link)
    return tuple(deduplicated)


def _parse_sections(full_text: str) -> tuple[NoteSection, ...]:
    """Split a note into ATX-heading sections.

    Frontmatter is never a section, a heading-looking line inside a fenced
    code block is never a boundary, and a section's span runs from its
    heading line up to the next heading of the same or a shallower level
    (deeper subsections stay inside their parent's span).
    """

    offset = 0
    frontmatter = FRONTMATTER_RE.match(full_text)
    if frontmatter:
        offset = frontmatter.end()

    headings: list[tuple[int, int, str]] = []
    fence_marker: str | None = None
    position = offset
    for line in full_text[offset:].splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        fence = FENCE_LINE_RE.match(stripped)
        if fence_marker is not None:
            if (
                fence
                and fence.group(1)[0] == fence_marker[0]
                and len(fence.group(1)) >= len(fence_marker)
                and not fence.group(2).strip()
            ):
                fence_marker = None
        elif fence:
            fence_marker = fence.group(1)
        else:
            heading = ATX_HEADING_LINE_RE.match(stripped)
            if heading:
                text = ATX_CLOSING_HASHES_RE.sub("", heading.group(2)).strip()
                headings.append((position, len(heading.group(1)), text))
        position += len(line)

    sections: list[NoteSection] = []
    for index, (start, level, text) in enumerate(headings):
        end = len(full_text)
        for next_start, next_level, _ in headings[index + 1 :]:
            if next_level <= level:
                end = next_start
                break
        sections.append(
            NoteSection(
                heading=f"{'#' * level} {text}",
                text=text,
                level=level,
                start=start,
                end=end,
            )
        )
    return tuple(sections)


def _match_section(sections: tuple[NoteSection, ...], section_heading: str) -> NoteSection:
    cleaned = " ".join(section_heading.strip().split())
    if not cleaned:
        raise ValueError("section_heading cannot be empty")

    level: int | None = None
    target = cleaned
    qualified = SECTION_HEADING_ARG_RE.fullmatch(cleaned)
    if qualified:
        level = len(qualified.group(1))
        target = qualified.group(2)

    def _norm(value: str) -> str:
        return " ".join(value.split())

    candidates = [
        section
        for section in sections
        if _norm(section.text) == _norm(target) and (level is None or section.level == level)
    ]

    if not candidates:
        available = ", ".join(section.heading for section in sections[:20])
        raise ValueError(f"Section not found: {section_heading!r}. Available headings: {available}")
    if len(candidates) > 1:
        if level is None and len({section.level for section in candidates}) > 1:
            options = ", ".join(section.heading for section in candidates[:5])
            raise ValueError(
                f"Ambiguous section heading {section_heading!r} ({options}). "
                "Qualify the level, e.g. '## title'."
            )
        raise ValueError(
            f"Heading {candidates[0].heading!r} appears {len(candidates)} times; "
            "section-level editing needs a unique heading. Use update_note instead."
        )
    return candidates[0]


def _make_snippet(text: str, query: str, terms: list[str], size: int = 240) -> str:
    lowered = text.lower()
    needle = query.lower()
    index = lowered.find(needle) if needle else -1
    if index < 0:
        for term in terms:
            index = lowered.find(term)
            if index >= 0:
                break
    if index < 0:
        snippet = text[:size]
        return snippet.strip()

    start = max(0, index - size // 3)
    end = min(len(text), start + size)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


class VaultService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cached_index: VaultIndex | None = None
        self._cache_expires_at = 0.0

    @property
    def root(self) -> Path:
        return self._settings.vault_root

    def note_count(self) -> int:
        return len(self._get_index().records)

    def get_start_here(self) -> dict[str, Any]:
        index = self._get_index()
        preferred_names = [
            self._settings.start_here_filename,
            "start-here.md",
            "README.md",
            "readme.md",
        ]

        for candidate in preferred_names:
            record = index.by_relative_lower.get(candidate.lower())
            if record:
                payload = self._note_payload(record)
                payload["related"] = self.list_related(record.rel_path, limit=5)["related"]
                return payload

        fallbacks = [
            record
            for record in index.records
            if "start" in _normalize_lookup(record.rel_path) or "readme" in record.rel_path.lower()
        ]
        if fallbacks:
            record = sorted(fallbacks, key=lambda item: item.rel_path.lower())[0]
            payload = self._note_payload(record)
            payload["related"] = self.list_related(record.rel_path, limit=5)["related"]
            return payload

        raise FileNotFoundError(
            f"No start note found under {self.root}. Expected {self._settings.start_here_filename}"
        )

    def read_note(self, note_ref: str) -> dict[str, Any]:
        record = self._resolve_note_ref(note_ref)
        return self._note_payload(record)

    def search_notes(self, query: str, limit: int | None = None) -> dict[str, Any]:
        cleaned_query = query.strip()
        if not cleaned_query:
            raise ValueError("query cannot be empty")

        normalized_query = cleaned_query.lower()
        terms = [term for term in NON_WORD_RE.split(normalized_query) if term]
        search_limit = self._normalize_limit(limit)

        matches: list[tuple[int, NoteRecord]] = []
        for record in self._get_index().records:
            path_lower = record.rel_path.lower()
            title_lower = record.title.lower()
            body_lower = record.body.lower()

            score = 0
            if normalized_query in title_lower:
                score += 40
            if normalized_query in path_lower:
                score += 25
            if normalized_query in body_lower:
                score += 12

            for term in terms:
                if term in title_lower:
                    score += 12
                if term in path_lower:
                    score += 8
                if term in record.tags:
                    score += 8
                occurrences = body_lower.count(term)
                score += min(occurrences, 5) * 2

            if score > 0:
                matches.append((score, record))

        matches.sort(key=lambda item: (-item[0], item[1].rel_path.lower()))

        results = []
        for score, record in matches[:search_limit]:
            results.append(
                {
                    "path": record.rel_path,
                    "title": record.title,
                    "score": score,
                    "tags": list(record.tags),
                    "modified_at": record.modified_at,
                    "snippet": _make_snippet(record.body, cleaned_query, terms),
                }
            )

        return {
            "query": cleaned_query,
            "limit": search_limit,
            "matches": results,
            "total_matches": len(matches),
        }

    def list_related(self, note_ref: str, limit: int | None = None) -> dict[str, Any]:
        search_limit = self._normalize_limit(limit)
        record = self._resolve_note_ref(note_ref)
        index = self._get_index()

        related: list[dict[str, Any]] = []
        seen_paths: set[str] = {record.rel_path.lower()}

        for raw_link in record.raw_links:
            target = self._resolve_note_ref(raw_link, base_note=record, allow_missing=True)
            if not target:
                continue
            lowered = target.rel_path.lower()
            if lowered in seen_paths:
                continue
            seen_paths.add(lowered)
            related.append(
                {
                    "path": target.rel_path,
                    "title": target.title,
                    "relationship": "outgoing_link",
                    "reason": f"Referenced from {record.rel_path}",
                }
            )
            if len(related) >= search_limit:
                return {"note": record.rel_path, "related": related[:search_limit]}

        for candidate in index.records:
            if candidate.rel_path == record.rel_path:
                continue
            for raw_link in candidate.raw_links:
                target = self._resolve_note_ref(raw_link, base_note=candidate, allow_missing=True)
                if not target or target.rel_path != record.rel_path:
                    continue
                lowered = candidate.rel_path.lower()
                if lowered in seen_paths:
                    break
                seen_paths.add(lowered)
                related.append(
                    {
                        "path": candidate.rel_path,
                        "title": candidate.title,
                        "relationship": "backlink",
                        "reason": f"Links to {record.rel_path}",
                    }
                )
                break
            if len(related) >= search_limit:
                return {"note": record.rel_path, "related": related[:search_limit]}

        if record.tags:
            for candidate in index.records:
                if candidate.rel_path == record.rel_path:
                    continue
                lowered = candidate.rel_path.lower()
                if lowered in seen_paths:
                    continue
                shared_tags = sorted(set(record.tags).intersection(candidate.tags))
                if not shared_tags:
                    continue
                seen_paths.add(lowered)
                related.append(
                    {
                        "path": candidate.rel_path,
                        "title": candidate.title,
                        "relationship": "shared_tag",
                        "reason": f"Shared tags: {', '.join(shared_tags[:3])}",
                    }
                )
                if len(related) >= search_limit:
                    break

        return {"note": record.rel_path, "related": related[:search_limit]}

    def create_note(self, note_path: str, content: str, message: str = "") -> dict[str, Any]:
        """Create a new Markdown note and commit it to the vault Git repo."""

        self._assert_write_enabled()
        target_path, rel_path = self._resolve_write_path(note_path)

        with self._write_lock():
            self._ensure_git_clean()
            if target_path.exists():
                raise FileExistsError(f"Note already exists: {rel_path}")
            self._write_note_file(target_path, content)
            return self._commit_note(rel_path, message or f"vault: create {rel_path}", action="created")

    def append_note(self, note_path: str, content: str, message: str = "") -> dict[str, Any]:
        """Append Markdown content to an existing note, or create it if missing, then commit."""

        self._assert_write_enabled()
        target_path, rel_path = self._resolve_write_path(note_path)

        with self._write_lock():
            self._ensure_git_clean()
            previous = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
            old_hash = _hash_text(previous) if previous else None
            clean_append = self._normalize_write_content(content)
            if previous:
                separator = "" if previous.endswith("\n\n") else "\n" if previous.endswith("\n") else "\n\n"
                new_content = f"{previous}{separator}{clean_append}"
            else:
                new_content = clean_append
            self._write_note_file(target_path, new_content, already_validated=True)
            result = self._commit_note(rel_path, message or f"vault: append {rel_path}", action="appended")
            result["old_content_hash"] = old_hash
            return result

    def update_note(
        self,
        note_ref: str,
        content: str,
        expected_hash: str,
        message: str = "",
    ) -> dict[str, Any]:
        """Replace an existing note only if expected_hash matches the current full content hash."""

        self._assert_write_enabled()
        record = self._resolve_note_ref(note_ref)
        if record is None:
            raise FileNotFoundError(f"Note not found: {note_ref}")
        target_path, rel_path = self._resolve_write_path(record.rel_path)

        with self._write_lock():
            self._ensure_git_clean()
            current = target_path.read_text(encoding="utf-8")
            current_hash = _hash_text(current)
            if expected_hash.strip().lower() != current_hash:
                raise ValueError(
                    "expected_hash does not match the current note. "
                    "Read the note again before updating."
                )
            self._write_note_file(target_path, content)
            result = self._commit_note(rel_path, message or f"vault: update {rel_path}", action="updated")
            result["old_content_hash"] = current_hash
            return result

    def update_section(
        self,
        note_ref: str,
        section_heading: str,
        content: str,
        expected_hash: str,
        message: str = "",
    ) -> dict[str, Any]:
        """Replace exactly one ATX-heading section when expected_hash matches
        that section's current hash (from read_note's `sections`).

        The compare-and-swap is per-section: edits landed elsewhere in the
        note since the read do not invalidate this write.
        """

        self._assert_write_enabled()
        record = self._resolve_note_ref(note_ref)
        target_path, rel_path = self._resolve_write_path(record.rel_path)

        with self._write_lock():
            self._ensure_git_clean()
            current = target_path.read_text(encoding="utf-8")
            sections = _parse_sections(current)
            if not sections:
                raise ValueError(
                    f"Note has no ATX headings to address: {rel_path}. Use update_note instead."
                )
            section = _match_section(sections, section_heading)
            span = current[section.start : section.end]
            span_hash = _hash_text(span)
            if expected_hash.strip().lower() != span_hash:
                raise ValueError(
                    f"expected_hash does not match the current content of section "
                    f"{section.heading!r}. Read the note again (read_note lists "
                    "per-section hashes) before updating."
                )

            new_span = self._validate_section_replacement(content, section)
            if section.end < len(current) and not new_span.endswith("\n\n"):
                new_span += "\n"
            new_content = f"{current[: section.start]}{new_span}{current[section.end :]}"
            self._write_note_file(target_path, new_content)
            result = self._commit_note(
                rel_path,
                message or f"vault: update section {section.text} in {rel_path}",
                action="updated_section",
            )
            result["section"] = section.heading
            result["old_section_hash"] = span_hash
            result["new_section_hash"] = _hash_text(new_span)
            return result

    def _validate_section_replacement(self, content: str, section: NoteSection) -> str:
        normalized = self._normalize_write_content(content)
        inner = _parse_sections(normalized)
        if not inner or normalized[: inner[0].start].strip():
            raise ValueError(
                "Replacement content must start with the section's ATX heading line "
                f"(level {section.level}, e.g. {section.heading!r})."
            )
        if inner[0].level != section.level:
            raise ValueError(
                f"Replacement heading level must stay {section.level} "
                f"(got level {inner[0].level}). Restructuring needs update_note."
            )
        for extra in inner[1:]:
            if extra.level <= section.level:
                raise ValueError(
                    f"Replacement content injects heading {extra.heading!r} at the same "
                    "or a shallower level, which would reshape the note beyond this "
                    "section. Use update_note for structural changes."
                )
        return normalized

    def recent_activity(self, limit: int | None = None) -> dict[str, Any]:
        search_limit = self._normalize_limit(limit)
        index = self._get_index()
        if self._settings.git_dir is not None:
            try:
                return self._recent_activity_git(index, search_limit)
            except Exception:
                pass
        return self._recent_activity_mtime(index, search_limit)

    def _recent_activity_git(self, index: VaultIndex, limit: int) -> dict[str, Any]:
        raw = self._run_git(
            [
                "-c",
                "core.quotePath=false",
                "log",
                f"--max-count={min(limit * 8, 300)}",
                "--name-only",
                "--pretty=format:\x01%H\x1f%cI\x1f%s",
            ],
            check=True,
        )
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        commit_date = ""
        commit_message = ""
        for line in raw.split("\n"):
            if line.startswith("\x01"):
                parts = line[1:].split("\x1f")
                commit_date = parts[1] if len(parts) > 1 else ""
                commit_message = parts[2] if len(parts) > 2 else ""
                continue
            rel_path = line.strip()
            if not rel_path or rel_path in seen:
                continue
            record = index.by_relative_lower.get(rel_path.lower())
            if record is None:
                continue
            seen.add(rel_path)
            items.append(
                {
                    "path": record.rel_path,
                    "title": record.title,
                    "modified_at": commit_date,
                    "message": commit_message,
                }
            )
            if len(items) >= limit:
                break
        return {"source": "git", "limit": limit, "items": items}

    def _recent_activity_mtime(self, index: VaultIndex, limit: int) -> dict[str, Any]:
        records = sorted(index.records, key=lambda item: item.modified_at, reverse=True)
        items = [
            {"path": record.rel_path, "title": record.title, "modified_at": record.modified_at}
            for record in records[:limit]
        ]
        return {"source": "mtime", "limit": limit, "items": items}

    def _get_index(self) -> VaultIndex:
        now = time.monotonic()
        if self._cached_index and now < self._cache_expires_at:
            return self._cached_index

        records: list[NoteRecord] = []
        for current_root, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in self._settings.ignored_dirs and not name.startswith(".")
            ]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                path = Path(current_root, filename)
                if path.suffix.lower() not in NOTE_EXTENSIONS:
                    continue
                rel_path = path.relative_to(self.root).as_posix()
                if not self._path_allowed(rel_path):
                    continue

                text, truncated = _read_text(path, self._settings.max_note_bytes)
                stat = path.stat()
                body = _strip_frontmatter(text)[1]
                records.append(
                    NoteRecord(
                        abs_path=path.resolve(),
                        rel_path=rel_path,
                        title=_extract_title(text, path),
                        body=body,
                        full_content=text,
                        content_hash=_hash_text(text),
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                        truncated=truncated,
                        tags=_extract_tags(text),
                        raw_links=_extract_links(body),
                    )
                )

        records.sort(key=lambda item: item.rel_path.lower())
        by_relative_lower = {record.rel_path.lower(): record for record in records}
        by_absolute_lower = {record.abs_path.as_posix().lower(): record for record in records}

        lookup: dict[str, list[NoteRecord]] = {}
        for record in records:
            keys = {
                _normalize_lookup(record.rel_path),
                _normalize_lookup(Path(record.rel_path).stem),
                _normalize_lookup(record.title),
            }
            for key in keys:
                if not key:
                    continue
                lookup.setdefault(key, []).append(record)

        index = VaultIndex(
            records=tuple(records),
            by_relative_lower=by_relative_lower,
            by_absolute_lower=by_absolute_lower,
            by_lookup={key: tuple(value) for key, value in lookup.items()},
        )
        self._cached_index = index
        self._cache_expires_at = now + self._settings.cache_ttl_seconds
        return index

    def _path_allowed(self, rel_path: str) -> bool:
        include_prefixes = self._settings.include_path_prefixes
        if include_prefixes and not any(_prefix_matches(rel_path, prefix) for prefix in include_prefixes):
            return False

        exclude_prefixes = self._settings.exclude_path_prefixes
        if exclude_prefixes and any(_prefix_matches(rel_path, prefix) for prefix in exclude_prefixes):
            return False

        return True

    def _note_payload(self, record: NoteRecord) -> dict[str, Any]:
        # Truncated reads would hash spans that do not exist on disk; those
        # notes fall back to whole-note update_note.
        sections = (
            []
            if record.truncated
            else [
                {
                    "heading": section.heading,
                    "level": section.level,
                    "content_hash": _hash_text(record.full_content[section.start : section.end]),
                }
                for section in _parse_sections(record.full_content)
            ]
        )
        return {
            "path": record.rel_path,
            "title": record.title,
            "tags": list(record.tags),
            "size_bytes": record.size_bytes,
            "modified_at": record.modified_at,
            "truncated": record.truncated,
            "content": record.body,
            "full_content": record.full_content,
            "content_hash": record.content_hash,
            "sections": sections,
        }

    def _normalize_limit(self, limit: int | None) -> int:
        if limit is None:
            return self._settings.default_search_limit
        return max(1, min(limit, self._settings.max_search_limit))

    def _resolve_note_ref(
        self,
        note_ref: str,
        *,
        base_note: NoteRecord | None = None,
        allow_missing: bool = False,
    ) -> NoteRecord | None:
        cleaned_ref = note_ref.strip()
        if not cleaned_ref:
            raise ValueError("note_ref cannot be empty")

        index = self._get_index()

        explicit_match = self._resolve_path_like_ref(cleaned_ref, index, base_note=base_note)
        if explicit_match:
            return explicit_match

        lookup_key = _normalize_lookup(cleaned_ref)
        candidates = list(index.by_lookup.get(lookup_key, ()))

        if not candidates:
            if allow_missing:
                return None
            raise FileNotFoundError(f"Note not found: {note_ref}")

        if len(candidates) > 1:
            candidate_paths = ", ".join(candidate.rel_path for candidate in candidates[:5])
            raise ValueError(f"Ambiguous note reference '{note_ref}'. Candidates: {candidate_paths}")

        return candidates[0]

    def _resolve_path_like_ref(
        self,
        note_ref: str,
        index: VaultIndex,
        *,
        base_note: NoteRecord | None = None,
    ) -> NoteRecord | None:
        cleaned_ref = note_ref.replace("\\", "/").split("#", 1)[0].split("?", 1)[0].strip()
        if not cleaned_ref:
            return None

        candidate_paths: list[Path] = []
        if base_note:
            candidate_paths.append((self.root / Path(base_note.rel_path).parent / cleaned_ref).resolve())
        candidate_paths.append((self.root / cleaned_ref.lstrip("/")).resolve())

        for candidate_path in candidate_paths:
            try:
                candidate_path.relative_to(self.root)
            except ValueError:
                continue

            variants = [candidate_path]
            if candidate_path.suffix.lower() not in NOTE_EXTENSIONS:
                variants.extend(candidate_path.with_suffix(extension) for extension in sorted(NOTE_EXTENSIONS))

            for variant in variants:
                record = index.by_absolute_lower.get(variant.as_posix().lower())
                if record:
                    return record

        return None

    def _assert_write_enabled(self) -> None:
        if not self._settings.write_enabled:
            raise PermissionError("Vault write tools are disabled for this MCP server.")

    @contextmanager
    def _write_lock(self):
        lock_path = Path("/tmp/vault-write.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w", encoding="utf-8") as handle:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _resolve_write_path(self, note_path: str) -> tuple[Path, str]:
        cleaned = note_path.replace("\\", "/").split("#", 1)[0].split("?", 1)[0].strip()
        if not cleaned:
            raise ValueError("note_path cannot be empty")
        if "\x00" in cleaned:
            raise ValueError("note_path cannot contain null bytes")
        if cleaned.startswith("/"):
            raise ValueError("note_path must be relative to the vault root")

        candidate = Path(cleaned)
        if any(part in {"", ".", ".."} or part.startswith(".") for part in candidate.parts):
            raise ValueError("note_path cannot contain hidden, empty, current, or parent path segments")
        if candidate.suffix:
            if candidate.suffix.lower() not in NOTE_EXTENSIONS:
                raise ValueError("Only Markdown note files can be written")
        else:
            candidate = candidate.with_suffix(".md")

        resolved = (self.root / candidate).resolve()
        try:
            rel_path = resolved.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise ValueError("note_path escapes the vault root") from exc

        if any(_prefix_matches(rel_path, prefix) for prefix in self._settings.write_exclude_path_prefixes):
            raise PermissionError(f"Writing to this vault path is blocked: {rel_path}")
        if not self._path_allowed(rel_path):
            raise PermissionError(f"Writing to this vault path is not allowed by prefix filters: {rel_path}")

        return resolved, rel_path

    def _normalize_write_content(self, content: str) -> str:
        if "\x00" in content:
            raise ValueError("content cannot contain null bytes")
        encoded = content.encode("utf-8")
        if len(encoded) > self._settings.max_write_bytes:
            raise ValueError(f"content exceeds MAX_WRITE_BYTES ({self._settings.max_write_bytes})")
        return content if content.endswith("\n") else f"{content}\n"

    def _write_note_file(self, target_path: Path, content: str, *, already_validated: bool = False) -> None:
        normalized = content if already_validated else self._normalize_write_content(content)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(normalized, encoding="utf-8")

    def _ensure_git_clean(self) -> None:
        status = self._run_git(["status", "--porcelain"], check=True)
        if status.strip():
            raise RuntimeError(
                "Vault Git working tree has uncommitted changes. "
                "Refusing MCP write until it is clean."
            )

    def _commit_note(self, rel_path: str, message: str, *, action: str) -> dict[str, Any]:
        safe_message = self._normalize_commit_message(message)
        self._run_git(["add", "--", rel_path], check=True)

        diff = self._run_git_raw(["diff", "--cached", "--quiet", "--", rel_path])
        if diff.returncode == 0:
            self._cached_index = None
            current_commit = self._run_git(["rev-parse", "HEAD"], check=True)
            current_text = (self.root / rel_path).read_text(encoding="utf-8")
            return {
                "path": rel_path,
                "action": action,
                "committed": False,
                "commit": current_commit,
                "content_hash": _hash_text(current_text),
                "message": safe_message,
            }
        if diff.returncode != 1:
            raise RuntimeError(diff.stderr.strip() or "git diff --cached failed")

        self._run_git(["commit", "-m", safe_message, "--", rel_path], check=True)
        commit = self._run_git(["rev-parse", "HEAD"], check=True)
        current_text = (self.root / rel_path).read_text(encoding="utf-8")
        self._cached_index = None
        return {
            "path": rel_path,
            "action": action,
            "committed": True,
            "commit": commit,
            "content_hash": _hash_text(current_text),
            "message": safe_message,
        }

    def _normalize_commit_message(self, message: str) -> str:
        cleaned = " ".join(message.strip().split())
        if not cleaned:
            cleaned = "vault: update note"
        return cleaned[:200]

    def _run_git(self, args: list[str], *, check: bool) -> str:
        result = self._run_git_raw(args)
        if check and result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result.stdout.strip()

    def _run_git_raw(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if self._settings.git_dir is None:
            raise RuntimeError("VAULT_GIT_DIR is not configured")

        env = os.environ.copy()
        env["HOME"] = env.get("HOME") or "/tmp"
        env["GIT_AUTHOR_NAME"] = self._settings.git_author_name
        env["GIT_AUTHOR_EMAIL"] = self._settings.git_author_email
        env["GIT_COMMITTER_NAME"] = self._settings.git_author_name
        env["GIT_COMMITTER_EMAIL"] = self._settings.git_author_email

        command = [
            "git",
            f"--work-tree={self.root}",
            f"--git-dir={self._settings.git_dir}",
            *args,
        ]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
