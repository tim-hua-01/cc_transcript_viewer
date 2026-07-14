#!/usr/bin/env python3
"""Helpers shared by the Claude Code, Codex, and Cursor parser modules.

Each parser used to carry its own copy of these (JSONL iteration, title
truncation, timestamp conversion, and — most significantly — an mtime-keyed
summary cache with dirty/generation bookkeeping). They live here once so the
three parsers stay small and can't drift apart.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path


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
