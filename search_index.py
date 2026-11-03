#!/usr/bin/env python3
"""A persistent full-text index of every session, kept current in the background.

``SearchIndex`` owns one SQLite file holding each session's search document
(see search_text.py): the normalized text a query scans, and the source
chunks that snippets are cut from. Nothing is kept in memory between
searches; the OS file cache keeps the file warm, and a search over every
session reads it in well under a second.

One coordinator thread schedules the work. It sleeps until ``notify()``
reports sessions whose fingerprint differs from the stored one, then has
worker processes parse, extract and store them, most recent first, so neither
the parsing time nor the text passes through the server process (SQLite
serializes their writes). Searches never wait for it: they answer from what
is indexed, and ``status()`` reports progress.

This module never imports server.py. The server hands in ``load_session``
and a way to configure worker processes the way it is configured itself.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import common
import search_text

SCHEMA_VERSION = 1
DEFAULT_WORKERS = 4
# A transcript that is still being written is re-indexed at most this often;
# re-parsing a large live session after every append would waste the battery.
LIVE_REINDEX_SECONDS = 30.0
# Worker processes exit after this long without work, freeing their memory.
POOL_IDLE_SECONDS = 60.0
# After a worker crashes, a fresh pool is tried after this long, doubling with
# each crash in a row; after POOL_MAX_CRASHES the server parses in-process.
POOL_RETRY_SECONDS = 5.0
POOL_MAX_CRASHES = 3
# Recycle a worker after this many sessions (3.11+), so one huge parse
# doesn't keep a worker's memory high for the life of the pool.
_TASKS_PER_WORKER = 25

# User prompts should outrank generated/tool output without overwhelming a
# custom title selected specifically to identify the transcript.
USER_MSG_WEIGHT = 50
CUSTOM_TITLE_WEIGHT = 10_000
NATIVE_TITLE_WEIGHT = CUSTOM_TITLE_WEIGHT // 2

_USER, _REST = 0, 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS docs (
    file TEXT PRIMARY KEY,
    fingerprint REAL NOT NULL,
    user_chars INTEGER NOT NULL,
    user_norm BLOB NOT NULL,
    rest_norm BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    file TEXT NOT NULL,
    segment INTEGER NOT NULL,
    src_start INTEGER NOT NULL,
    norm_start INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (file, segment, src_start)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS chunks_by_norm ON chunks (file, segment, norm_start, src_start);
"""


def remove_index_files(path: Path) -> list[Path]:
    """Delete an index (and its write-ahead log); returns what was removed."""
    removed = []
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(path) + suffix)
        try:
            target.unlink()
            removed.append(target)
        except FileNotFoundError:
            pass
    return removed


def _connect(path: Path, **kwargs) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The index holds transcript text: owner-only, like the transcripts.
    # SQLite gives its -wal/-shm files the database file's permissions.
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
    os.chmod(path, 0o600)
    db = sqlite3.connect(path, timeout=30.0, **kwargs)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


def _store(db: sqlite3.Connection, file_id: str, fingerprint: float,
           doc: search_text.SessionDoc) -> None:
    rows = [
        (file_id, segment, c.src_start, c.norm_start, c.text)
        for segment, part in ((_USER, doc.user), (_REST, doc.rest))
        for c in part.chunks
    ]
    user_chars = sum(len(c.text) for c in doc.user.chunks)
    with db:
        db.execute("DELETE FROM chunks WHERE file = ?", (file_id,))
        db.execute(
            "INSERT OR REPLACE INTO docs VALUES (?, ?, ?, ?, ?)",
            (file_id, fingerprint, user_chars, doc.user.normalized, doc.rest.normalized),
        )
        db.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?)", rows)


def _index_session(db: sqlite3.Connection, load_session, file_id: str, fingerprint: float) -> None:
    """Parse, extract and store one session. A session that can't be read
    gets an empty document, so it isn't retried until it changes."""
    try:
        data = load_session(file_id)
    except Exception:  # noqa: BLE001 - one unreadable session must not stop indexing
        data = None
    _store(db, file_id, fingerprint, search_text.build_session_doc(data))


_WORKER_DBS: dict[str, sqlite3.Connection] = {}  # one connection per worker process


def _worker_index_session(path: str, load_session, file_id: str, fingerprint: float) -> None:
    """_index_session in a worker process, on that process's own connection."""
    db = _WORKER_DBS.get(path)
    if db is None:
        db = _WORKER_DBS[path] = _connect(Path(path))
    _index_session(db, load_session, file_id, fingerprint)


def title_segments(session: dict) -> tuple[str, str]:
    """Viewer-custom and native-title text, as distinct score tiers."""
    custom = session.get("custom_title") or ""
    native_titles = []
    # Sources with stored AI- or user-authored names. Codex's legacy `title`
    # is usually the first prompt, but newer versions also expose `ai_title`.
    if session.get("agent") in {"claude", "codex", "cursor", "opencode"}:
        for title in (
            session.get("original_title"),
            session.get("claude_title"),
            session.get("ai_title"),
        ):
            if title and title != custom and title not in native_titles:
                native_titles.append(title)
    return custom, "\n".join(native_titles)


class SearchIndex:
    def __init__(self, path, load_session, *, workers: int = DEFAULT_WORKERS,
                 worker_setup=None, live_reindex_seconds: float = LIVE_REINDEX_SECONDS):
        """``load_session(file_id)`` returns a parsed session; it must be a
        module-level function so worker processes can import it.
        ``worker_setup()``, if given, returns ``(initializer, initargs)`` for
        those processes. ``workers=0`` extracts in this process instead."""
        self.path = Path(path).expanduser()
        self._load_session = load_session
        self._workers = workers
        self._worker_setup = worker_setup
        self._live_delay = live_reindex_seconds

        self._lock = threading.Lock()        # guards the state below
        self._wanted: dict[str, float] = {}  # file -> fingerprint, latest session list
        self._order: list[str] = []          # wanted files, most recent first
        self._stored: dict[str, float] = {}  # file -> fingerprint in the database
        self._indexed_at: dict[str, float] = {}  # file -> monotonic time last written
        self._generation = 0
        self._error: str | None = None
        self._search_seq = 0

        self._write_lock = threading.Lock()  # one writer: coordinator or sync()
        self._wakeup = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._pool: ProcessPoolExecutor | None = None
        self._in_process = False      # workers can't run here: extract in this process
        self._pool_crashes = 0        # worker crashes in a row
        self._pool_retry_at = 0.0     # monotonic time a crashed pool may be retried
        self._pool_last_used = 0.0
        self._db: sqlite3.Connection | None = None
        try:
            self._open()
        except (OSError, sqlite3.Error) as e:
            self._error = f"could not open the search index: {e}"

    # ----- storage ---------------------------------------------------------
    def _open(self) -> None:
        # The writer connection is shared by the coordinator and sync()
        # callers; _write_lock keeps them from using it at the same time.
        try:
            self._db = _connect(self.path, check_same_thread=False)
            self._prepare(self._db)
        except sqlite3.DatabaseError:
            # Unreadable (corrupt, or not a database): it's only a cache.
            if self._db is not None:
                self._db.close()
            remove_index_files(self.path)
            self._db = _connect(self.path, check_same_thread=False)
            self._prepare(self._db)
        self._stored = dict(self._db.execute("SELECT file, fingerprint FROM docs"))

    def _prepare(self, db: sqlite3.Connection) -> None:
        """Create the schema, clearing documents built by another version."""
        version = f"{SCHEMA_VERSION}.{search_text.EXTRACTOR_VERSION}"
        with db:
            db.executescript(_SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            if row is None or row[0] != version:
                db.execute("DELETE FROM docs")
                db.execute("DELETE FROM chunks")
                db.execute("INSERT OR REPLACE INTO meta VALUES ('version', ?)", (version,))

    def _mark_indexed(self, file_id: str, fingerprint: float) -> None:
        with self._lock:
            self._stored[file_id] = fingerprint
            self._indexed_at[file_id] = time.monotonic()
            self._generation += 1

    def _prune(self, files: list[str]) -> None:
        with self._db:
            for file_id in files:
                self._db.execute("DELETE FROM docs WHERE file = ?", (file_id,))
                self._db.execute("DELETE FROM chunks WHERE file = ?", (file_id,))
        with self._lock:
            for file_id in files:
                self._stored.pop(file_id, None)
                self._indexed_at.pop(file_id, None)
            self._generation += 1

    # ----- keeping it current ----------------------------------------------
    def _set_wanted(self, sessions: list[dict]) -> bool:
        """Record the latest session list; True when the index lags it."""
        wanted = {s["file"]: float(s.get("mtime") or 0) for s in sessions if s.get("file")}
        order = sorted(wanted, key=wanted.__getitem__, reverse=True)
        with self._lock:
            self._wanted, self._order = wanted, order
            return (any(self._stored.get(f) != fp for f, fp in wanted.items())
                    or any(f not in wanted for f in self._stored))

    def notify(self, sessions: list[dict]) -> None:
        """The current session list (every session, every source). Cheap;
        call it whenever the list is refreshed."""
        if self._set_wanted(sessions) and self._db is not None and not self._closed:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="search-indexer", daemon=True)
                self._thread.start()
            self._wakeup.set()

    def sync(self, sessions: list[dict]) -> None:
        """Bring the index fully up to date in the calling thread."""
        self._set_wanted(sessions)
        if self._db is None:
            return
        while True:
            work, prune, _ = self._plan(live_delay=0)
            if not work and not prune:
                return
            with self._write_lock:
                if prune:
                    self._prune(prune)
                for file_id, fingerprint in work:
                    _index_session(self._db, self._load_session, file_id, fingerprint)
                    self._mark_indexed(file_id, fingerprint)

    def _plan(self, live_delay: float):
        """(sessions to index in priority order, files to drop, seconds until
        a deferred live session is due or None)."""
        now = time.monotonic()
        work, wake = [], None
        with self._lock:
            prune = [f for f in self._stored if f not in self._wanted]
            for file_id in self._order:
                fingerprint = self._wanted[file_id]
                stored = self._stored.get(file_id)
                if stored == fingerprint:
                    continue
                if stored is not None:  # indexed before, changed since: maybe still being written
                    due = self._indexed_at.get(file_id, float("-inf")) + live_delay
                    if now < due:
                        wake = due - now if wake is None else min(wake, due - now)
                        continue
                work.append((file_id, fingerprint))
        return work, prune, wake

    def _run(self) -> None:
        while not self._closed:
            self._wakeup.clear()
            try:
                work, prune, wake = self._plan(self._live_delay)
                retry_in = self._pool_retry_at - time.monotonic()
                if work and retry_in > 0:  # waiting out a worker crash, not parsing here
                    work = []
                    wake = retry_in if wake is None else min(wake, retry_in)
                if work or prune:
                    with self._lock:
                        self._error = None
                    with self._write_lock:
                        if prune:
                            self._prune(prune)
                        failed = self._index(work[:max(self._workers, 1) * 2]) if work else 0
                    if failed:
                        self._wakeup.wait(5.0)  # don't spin on a write that keeps failing
                    continue
            except Exception as e:  # noqa: BLE001 - report it, keep the thread alive
                with self._lock:
                    self._error = f"indexing failed: {e}"
                wake = 5.0
            if self._pool is not None:
                idle_left = self._pool_last_used + POOL_IDLE_SECONDS - time.monotonic()
                if idle_left <= 0:
                    self._stop_pool()
                else:
                    wake = idle_left if wake is None else min(wake, idle_left)
            self._wakeup.wait(wake)

    def _index(self, batch: list[tuple[str, float]]) -> int:
        """Index ``batch``; returns how many sessions couldn't be stored."""
        pool = self._executor()
        if pool is None:
            for file_id, fingerprint in batch:
                _index_session(self._db, self._load_session, file_id, fingerprint)
                self._mark_indexed(file_id, fingerprint)
            return 0
        futures = {
            pool.submit(_worker_index_session, str(self.path), self._load_session, f, fp): (f, fp)
            for f, fp in batch
        }
        failed = 0
        try:
            for future in as_completed(futures):
                file_id, fingerprint = futures[future]
                try:
                    future.result()
                except sqlite3.Error as e:  # e.g. locked too long; retried next pass
                    failed += 1
                    with self._lock:
                        self._error = f"could not store {Path(file_id).name}: {e}"
                    continue
                self._mark_indexed(file_id, fingerprint)
                self._pool_crashes = 0
        except BrokenProcessPool:
            # A worker died, perhaps on one huge transcript. Retry with a fresh
            # pool rather than parsing in the server, unless it keeps happening.
            self._stop_pool()
            self._pool_crashes += 1
            if self._pool_crashes >= POOL_MAX_CRASHES:
                self._in_process = True
                message = "search workers keep crashing; indexing in the viewer process instead"
            else:
                delay = POOL_RETRY_SECONDS * 2 ** (self._pool_crashes - 1)
                self._pool_retry_at = time.monotonic() + delay
                message = f"a search worker crashed; retrying in {delay:g} s"
            with self._lock:
                self._error = message
        except Exception:  # noqa: BLE001 - _index_session catches session errors itself
            # So workers can't run here at all (e.g. the loader can't be
            # pickled or imported by them): extract in-process from now on.
            self._in_process = True
            self._stop_pool()
        finally:
            self._pool_last_used = time.monotonic()
        return failed

    def _executor(self) -> ProcessPoolExecutor | None:
        if self._workers <= 0 or self._in_process:
            return None
        if self._pool is None:
            initializer, initargs = self._worker_setup() if self._worker_setup else (None, ())
            kwargs = {"max_tasks_per_child": _TASKS_PER_WORKER} if sys.version_info >= (3, 11) else {}
            try:
                self._pool = ProcessPoolExecutor(
                    max_workers=max(1, min(self._workers, os.cpu_count() or 1)),
                    mp_context=multiprocessing.get_context("spawn"),
                    initializer=initializer, initargs=initargs, **kwargs,
                )
            except (OSError, ValueError):
                self._in_process = True
                return None
        return self._pool

    def _stop_pool(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        self._closed = True
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._stop_pool()
        if self._db is not None:
            self._db.close()
            self._db = None

    def status(self) -> dict:
        """How far the index is behind the latest session list.

        ``generation`` changes whenever indexed content changes, so a client
        can re-run an open search only when its results could differ.
        """
        with self._lock:
            total = len(self._wanted)
            missing = sum(1 for f in self._wanted if f not in self._stored)
            updating = sum(1 for f, fp in self._wanted.items()
                           if f in self._stored and self._stored[f] != fp)
            state = "error" if self._error else ("building" if missing else "ready")
            return {"state": state, "indexed": total - missing, "total": total,
                    "updating": updating, "generation": self._generation, "error": self._error}

    # ----- searching -------------------------------------------------------
    def search(self, query: str, sessions: list[dict], *, wait: bool = False) -> list[dict] | None:
        """[{file, snippet, score, pos}] for sessions whose titles or content
        match ``query``, best first; None when a newer search replaced this one.

        Viewer custom-title hits rank first, native titles get half that
        weight, then user messages, then everything else. Among equal
        scores, an earlier first occurrence wins. Without ``wait``, sessions
        not indexed yet can still match by title.
        """
        q = common.normalize_search_text(query)
        if not q:
            return []
        if wait:
            self.sync(sessions)
        else:
            self.notify(sessions)
        with self._lock:
            self._search_seq += 1
            token = self._search_seq

        titles = {}
        for s in sessions:
            custom, native = title_segments(s)
            titles[s["file"]] = (custom, native, common.normalize_search_text(custom),
                                 common.normalize_search_text(native))
        out = []
        content = self._content_matches(q, titles, token) if self._db is not None else {}
        if content is None:
            return None
        for file_id, (custom, native, custom_low, native_low) in titles.items():
            c_custom = custom_low.count(q)
            c_native = native_low.count(q)
            c_user, c_rest, located = content.get(file_id, (0, 0, None))
            if not (c_custom or c_native or c_user or c_rest):
                continue
            if c_custom:
                snippet, idx = search_text.text_snippet(custom, custom_low, custom_low.find(q), len(q))
                pos = idx
            elif c_native:
                snippet, idx = search_text.text_snippet(native, native_low, native_low.find(q), len(q))
                pos = len(custom) + idx
            else:
                snippet, idx = located
                pos = len(custom) + len(native) + idx
            score = (c_custom * CUSTOM_TITLE_WEIGHT + c_native * NATIVE_TITLE_WEIGHT
                     + c_user * USER_MSG_WEIGHT + c_rest)
            out.append({"file": file_id, "snippet": snippet, "score": score, "pos": pos})
        out.sort(key=lambda m: (-m["score"], m["pos"]))
        return out

    def _content_matches(self, q: str, titles: dict, token: int) -> dict | None:
        """file -> (user count, rest count, (snippet, offset) or None), read
        from one consistent snapshot of the index. The snippet is built only
        when no title outranks the content match."""
        needle = q.encode("utf-8")
        try:
            db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        except sqlite3.Error as e:
            with self._lock:
                self._error = f"could not read the search index: {e}"
            return {}
        try:
            db.execute("BEGIN")
            found = {}
            rows = db.execute("SELECT file, user_chars, user_norm, rest_norm FROM docs")
            for file_id, user_chars, user_norm, rest_norm in rows:
                if token != self._search_seq:
                    return None
                if file_id not in titles:
                    continue
                c_user, c_rest = user_norm.count(needle), rest_norm.count(needle)
                if not (c_user or c_rest):
                    continue
                located = None
                _custom, _native, custom_low, native_low = titles[file_id]
                if q not in custom_low and q not in native_low:
                    if c_user:
                        located = self._locate(db, file_id, _USER, user_norm, needle, len(q))
                    else:
                        snippet, idx = self._locate(db, file_id, _REST, rest_norm, needle, len(q))
                        located = (snippet, user_chars + idx)
                found[file_id] = (c_user, c_rest, located)
            return found
        except sqlite3.Error as e:
            with self._lock:
                self._error = f"could not read the search index: {e}"
            return {}
        finally:
            db.close()

    @staticmethod
    def _locate(db, file_id, segment, normalized: bytes, needle: bytes, length: int):
        """Snippet for the first match in one segment, from the few chunks
        around it: the one holding the match, those up to where it ends, and
        one on each side for context."""
        index = normalized.find(needle)
        end = index + len(needle) - 1
        head = db.execute(
            "SELECT norm_start, src_start, text FROM chunks WHERE file = ? AND segment = ?"
            " AND norm_start <= ? ORDER BY norm_start DESC, src_start DESC LIMIT 2",
            (file_id, segment, index)).fetchall()[::-1]
        last = head[-1][1]
        tail = db.execute(
            "SELECT norm_start, src_start, text FROM chunks WHERE file = ? AND segment = ?"
            " AND src_start > ? AND norm_start <= ? ORDER BY src_start",
            (file_id, segment, last, end)).fetchall()
        if tail:
            last = tail[-1][1]
        after = db.execute(
            "SELECT norm_start, src_start, text FROM chunks WHERE file = ? AND segment = ?"
            " AND src_start > ? ORDER BY src_start LIMIT 1",
            (file_id, segment, last)).fetchall()
        chunks = [search_text.Chunk(*row) for row in head + tail + after]
        return search_text.segment_snippet(chunks, normalized, index, length)
