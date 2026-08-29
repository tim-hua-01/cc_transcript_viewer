#!/usr/bin/env python3
"""Helpers shared by the Claude Code, Codex, Cursor, and opencode parsers.

Each parser used to carry its own copy of these (JSONL iteration, JSON/SQLite
access, title truncation, timestamp conversion, the sidebar-summary shape, and
— most significantly — an mtime-keyed summary cache with dirty/generation
bookkeeping). They live here once so the parsers stay small and can't drift
apart. `server.py` and `export_html.py` also share the route constant below.
"""

from __future__ import annotations

import json
import mimetypes
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

# The endpoint that serves transcript-referenced images. codex_parser builds
# URLs on it, server.py routes it, and export_html.py rewrites it into data:
# URIs — one constant so a rename can't silently break image embedding.
LOCAL_IMAGE_ROUTE = "/api/local-image"


def iter_jsonl(path: Path):
    """Yield parsed records from a JSONL file, skipping blank/corrupt lines."""
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def short_title(text: str, n: int = 100) -> str:
    """First ~n characters of a message, single-spaced, with an ellipsis if cut."""
    text = " ".join((text or "").split())
    return text[:n] + ("…" if len(text) > n else "")


def iso_from_ms(ms) -> str:
    """ISO-8601 UTC string from epoch milliseconds; '' when missing/invalid.

    0 is treated as missing (Cursor uses it as a null timestamp)."""
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def content_text(content) -> str:
    """Concatenated text of a message content (a string, or a list of typed
    blocks as Claude Code, Cursor, and opencode store them). Ignores non-text
    blocks; '' when there is no text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        )
    return ""


def image_mime(path) -> str:
    """The file's guessed MIME type when it is an image, else ''.

    Image-typed files are the only transcript-referenced files the viewer will
    serve (/api/local-image) or embed (single-file export) — one rule, shared
    by both, so they can't diverge.
    """
    mime = mimetypes.guess_type(str(path))[0] or ""
    return mime if mime.startswith("image/") else ""


def iso_from_ms_or_none(ms) -> str | None:
    """Like iso_from_ms, but None (not '') when missing — what the
    database-backed parsers have always emitted."""
    return iso_from_ms(ms) or None


def loads_or_none(value):
    """json.loads for a value that may be str, bytes, None, or junk;
    None on any failure."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def connect_ro(path, row_factory=None, timeout: float = 2.0) -> sqlite3.Connection:
    """Read-only SQLite connection — safe while the owning app is running.

    Raises sqlite3.Error like sqlite3.connect; callers that want None-on-failure
    wrap it themselves.
    """
    conn = sqlite3.connect(
        f"file:{Path(path).expanduser()}?mode=ro", uri=True, timeout=timeout
    )
    if row_factory is not None:
        conn.row_factory = row_factory
    return conn


def safe_stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def file_identity(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) identity used to key summary caches; None if unstatable."""
    st = safe_stat(path)
    if st is None:
        return None
    return st.st_mtime_ns, st.st_size


def cached_summary(cache: "SummaryCache", key: str, fingerprint, compute):
    """The one cache-wrapper pattern every parser uses: return the cached
    summary when the fingerprint still matches, otherwise compute and store.

    ``fingerprint`` is typically ``file_identity(path)`` (None skips the cache
    entirely); a None result from ``compute`` is returned but never cached.
    """
    if fingerprint is not None:
        hit = cache.get(key, fingerprint)
        if hit is not None:
            return hit
    summary = compute()
    if summary is not None and fingerprint is not None:
        cache.put(key, fingerprint, summary)
    return summary


def make_summary(
    *,
    agent: str,
    id: str,
    file: str,
    title: str,
    cwd: str = "",
    git_branch: str = "",
    version: str = "",
    first_ts=None,
    last_ts=None,
    n_user: int = 0,
    n_assistant: int = 0,
    n_tool: int = 0,
    n_web: int = 0,
    n_records: int = 0,
    model: str = "",
    mtime: float = 0,
    **extra,
) -> dict:
    """One sidebar-summary entry, in the shape the frontend and search expect.

    This is the contract every parser's ``list_sessions()`` rows satisfy
    (machine-checked by event_schema.REQUIRED_SUMMARY_FIELDS); per-agent extras
    (``ai_title``, ``cursor_source``, ``tokens_used``, sub-agent fields, …)
    ride along via ``**extra``.
    """
    summary = {
        "agent": agent,
        "id": id,
        "file": file,
        "title": title,
        "cwd": cwd,
        "git_branch": git_branch,
        "version": version,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "n_user": n_user,
        "n_assistant": n_assistant,
        "n_tool": n_tool,
        "n_web": n_web,
        "n_records": n_records,
        "model": model,
        "mtime": mtime,
    }
    summary.update(extra)
    return summary


class SummaryCache:
    """Thread-safe key → summary cache invalidated by a caller-chosen fingerprint.

    The fingerprint is a tuple of JSON-serializable scalars — typically
    ``(mtime_ns, size)``, optionally extended with extra invalidation state
    (e.g. Codex appends a SQLite-row signature). A cached summary is returned
    only when the stored fingerprint matches exactly.

    Persistence: ``snapshot()`` returns ``(generation, {key: [fingerprint,
    summary]})`` atomically; after writing it out, call ``mark_saved(gen)`` —
    the dirty flag clears only if no ``put()`` happened in between, so a
    concurrent update is never lost.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._data: dict[str, tuple[list, dict]] = {}
        self._dirty = False
        self._generation = 0

    def get(self, key: str, fingerprint) -> dict | None:
        fp = list(fingerprint)
        with self._lock:
            entry = self._data.get(key)
            if entry and entry[0] == fp:
                return entry[1]
        return None

    def peek(self, key: str):
        """(fingerprint, summary) for a key regardless of fingerprint match,
        or None. For callers with their own staleness logic (e.g. a cheap
        second-level content check when the fast fingerprint misses)."""
        with self._lock:
            return self._data.get(key)

    def put(self, key: str, fingerprint, summary: dict) -> None:
        with self._lock:
            self._data[key] = (list(fingerprint), summary)
            self._dirty = True
            self._generation += 1

    @property
    def dirty(self) -> bool:
        with self._lock:
            return self._dirty

    def load(self, data: dict) -> None:
        """Replace contents from a persisted ``snapshot()`` payload, best effort."""
        with self._lock:
            self._data.clear()
            for key, value in (data or {}).items():
                try:
                    fingerprint, summary = value
                except (TypeError, ValueError):
                    continue
                if isinstance(fingerprint, list) and isinstance(summary, dict):
                    self._data[str(key)] = (fingerprint, summary)
            self._dirty = False
            self._generation = 0

    def snapshot(self) -> tuple[int, dict]:
        """Atomic (generation, persistable-dict) pair for saving."""
        with self._lock:
            return self._generation, {
                key: [fingerprint, summary]
                for key, (fingerprint, summary) in self._data.items()
            }

    def mark_saved(self, generation: int) -> None:
        with self._lock:
            if self._generation == generation:
                self._dirty = False

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._dirty = False
            self._generation = 0
