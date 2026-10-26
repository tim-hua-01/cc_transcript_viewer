"""On-disk format of Mica, and the read-only side the viewer uses.

Layout (everything below is written only by the capture daemon):

    <mica>/
      FORMAT                 "mica 1"
      status.json            daemon heartbeat, rewritten every few seconds
      index.json             key -> short summary of every captured file
      events.jsonl           append-only log of every event, all files
      files/<key>/
        record.json          full state of one captured transcript file
        events.jsonl         append-only events for this file only
        g0000.chain          hash chain over the bytes of generation g0000
        g0000/<source>/<relpath>
                             captured bytes, laid out like the source tree
        g0001/...            a later generation, started after a divergence

A *generation* is one append-only capture. The daemon never rewrites captured
bytes: when the live file stops being an extension of what was captured
(truncated, rewritten, replaced), the current generation is closed and a new
one starts from the file's new contents. The oldest generation therefore holds
the earliest content the store ever saw.

Generation files keep the source's relative path (``<project>/<session>.jsonl``
for Claude Code, ``YYYY/MM/DD/rollout-*.jsonl`` for Codex) so the viewer's
parsers, which read session ids and sub-agent links from path components, can
render a store copy exactly like the live file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

FORMAT_TEXT = "mica 1\n"
DEFAULT_STORE_DIR = Path("/Library/Mica")

FORMAT_FILE = "FORMAT"
STATUS_FILE = "status.json"
INDEX_FILE = "index.json"
EVENTS_FILE = "events.jsonl"
FILES_DIR = "files"
RECORD_FILE = "record.json"

# Event types the daemon writes. Changes it flags on the transcript:
# truncated, rewritten, replaced, deleted, recreated, unreadable. Expected
# changes: moved, inode_changed, readable_again. Store-wide events, with no
# transcript key: daemon_started, capture_gap (the daemon was not running),
# access_lost / access_restored (a whole source folder), and unreadable for a
# file Mica could never open, so has no capture of.
STORE_EVENTS_SHOWN = frozenset({"capture_gap", "access_lost", "access_restored", "unreadable"})

# A heartbeat older than this means the daemon is not running.
HEARTBEAT_STALE_SECONDS = 30

# The daemon normally uses well under 1% of one core; above this the viewer
# shows a warning, since something (a huge tree, a bug) is making it work hard.
CPU_WARN_PERCENT = 2.0

_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_GEN_RE = re.compile(r"^g\d{4,}$")

# How many differing lines a comparison reports individually.
DIFF_SAMPLE_LINES = 200
DIFF_PREVIEW_CHARS = 240
DIFF_TEXT_CHARS = 600


# ---------------------------------------------------------------------------
# Small shared helpers (used by both the daemon and the viewer)
# ---------------------------------------------------------------------------
def iso_utc(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, tz=timezone.utc).isoformat()


def parse_iso(ts) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def valid_key(key) -> bool:
    return isinstance(key, str) and bool(_KEY_RE.match(key)) and key not in (".", "..")


def valid_gen(gen_id) -> bool:
    return isinstance(gen_id, str) and bool(_GEN_RE.match(gen_id))


def gen_id(n: int) -> str:
    return f"g{n:04d}"


def chain_seed(key: str, gen: str) -> str:
    """Starting head of a generation's hash chain."""
    return hashlib.sha256(f"mica-v1:{key}:{gen}".encode("utf-8")).hexdigest()


def chain_hasher(head: str):
    """A sha256 object primed with the previous head; feed it the next chunk.

    head_i = sha256(head_{i-1} || chunk_i), so every head commits to all bytes
    captured before it, and chunks can be streamed without holding them.
    """
    h = hashlib.sha256()
    h.update(bytes.fromhex(head))
    return h


def atomic_write_json(path: Path, obj) -> None:
    """Replace ``path`` with ``obj`` as JSON so readers never see a partial file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def read_json(path: Path):
    try:
        with open(path, "rb") as fh:
            return json.loads(fh.read())
    except (OSError, ValueError):
        return None


def read_jsonl(path: Path) -> list:
    out = []
    try:
        with open(path, "rb") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _flags(entry: dict) -> list:
    """Every flag the daemon recorded, as recorded."""
    flags = entry.get("flags")
    return [str(f) for f in flags] if isinstance(flags, list) else []


def _clip(text: str, n: int) -> str:
    return text[:n] + ("…" if len(text) > n else "")


def _record_text(rec: dict) -> str:
    """The human-readable part of one transcript record (message text, tool
    calls and results, thinking), for showing a changed line in a diff.
    Covers both record shapes: Claude Code's ``message.content`` blocks and
    Codex's ``payload``."""
    parts: list = []

    def walk(c):
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for item in c:
                walk(item)
        elif isinstance(c, dict):
            kind = c.get("type")
            if kind == "tool_use":
                inp = c.get("input")
                main = next((inp[k] for k in ("command", "file_path", "pattern", "query", "url", "prompt")
                             if isinstance(inp, dict) and isinstance(inp.get(k), str)), None)
                parts.append(f"[{c.get('name') or 'tool'}] " + (main if main is not None else json.dumps(inp, ensure_ascii=False)))
            elif kind == "tool_result":
                parts.append("[result] ")
                walk(c.get("content"))
            elif kind == "thinking":
                parts.append("(thinking) " + str(c.get("thinking") or ""))
            else:
                for field in ("text", "output_text", "input_text"):
                    if isinstance(c.get(field), str):
                        parts.append(c[field])
                        break

    msg = rec.get("message")
    payload = rec.get("payload")
    if isinstance(msg, dict):
        walk(msg.get("content"))
    elif isinstance(payload, dict):
        if payload.get("name"):
            parts.append(f"[{payload['name']}] ")
        for field in ("content", "message", "text", "arguments", "output", "summary"):
            if payload.get(field):
                walk(payload[field])
                break
    elif "content" in rec:
        walk(rec.get("content"))
    return " ".join(" ".join(parts).split())


def _line_preview(line: bytes) -> dict:
    """A short, display-only description of one JSONL line: who/what it is,
    when, its readable text, and the start of the raw JSON."""
    raw = line.decode("utf-8", errors="replace")
    info: dict = {"preview": _clip(raw, DIFF_PREVIEW_CHARS)}
    try:
        rec = json.loads(raw)
    except ValueError:
        return info
    if isinstance(rec, dict):
        for field in ("type", "timestamp", "uuid"):
            if isinstance(rec.get(field), str):
                info[field] = rec[field]
        payload = rec.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("type"), str):
            info.setdefault("subtype", payload["type"])
        msg = rec.get("message")
        role = msg.get("role") if isinstance(msg, dict) else None
        if not role and isinstance(payload, dict):
            role = payload.get("role")
        if isinstance(role, str):
            info["role"] = role
        text = _record_text(rec)
        if text:
            info["text"] = _clip(text, DIFF_TEXT_CHARS)
    return info


def _split_lines(data: bytes) -> list:
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def diff_lines(captured: bytes, live: bytes) -> dict:
    """Line-level summary of how a live file differs from its captured bytes.

    Transcripts are JSONL, so a line is one record. ``missing`` are captured
    records absent from the live file; ``extra`` are live records the capture
    never saw (which includes legitimate appends made after a divergence).
    """
    cap_lines = _split_lines(captured)
    live_lines = _split_lines(live)
    common = 0
    for a, b in zip(cap_lines, live_lines):
        if a != b:
            break
        common += 1
    live_counts = Counter(live_lines)
    cap_counts = Counter(cap_lines)
    missing, extra = [], []
    for n, line in enumerate(cap_lines, 1):
        if live_counts[line] > 0:
            live_counts[line] -= 1
        else:
            missing.append((n, line))
    for n, line in enumerate(live_lines, 1):
        if cap_counts[line] > 0:
            cap_counts[line] -= 1
        else:
            extra.append((n, line))
    return {
        "captured_lines": len(cap_lines),
        "live_lines": len(live_lines),
        "common_prefix_lines": common,
        "first_diff_line": common + 1 if (missing or extra or len(cap_lines) != len(live_lines)) else None,
        "missing_count": len(missing),
        "extra_count": len(extra),
        "missing": [dict(line=n, **_line_preview(line)) for n, line in missing[:DIFF_SAMPLE_LINES]],
        "extra": [dict(line=n, **_line_preview(line)) for n, line in extra[:DIFF_SAMPLE_LINES]],
    }


def first_difference(a_path: Path, b_path: Path, length: int, chunk: int = 1 << 20):
    """Offset of the first differing byte within ``length`` bytes, or None if
    the first ``length`` bytes of both files are identical. A file shorter
    than ``length`` differs at its end."""
    with open(a_path, "rb") as fa, open(b_path, "rb") as fb:
        offset = 0
        while offset < length:
            n = min(chunk, length - offset)
            a, b = fa.read(n), fb.read(n)
            if a != b:
                for i, (x, y) in enumerate(zip(a, b)):
                    if x != y:
                        return offset + i
                return offset + min(len(a), len(b))
            offset += n
    return None


# ---------------------------------------------------------------------------
# Read-only access for the viewer
# ---------------------------------------------------------------------------
# How long StoreReader trusts its last look at index.json. A sidebar poll asks
# about every session, and each question would otherwise stat the file again.
INDEX_RECHECK_SECONDS = 0.25


class StoreReader:
    """Read-only view of a store directory. Never writes anything."""

    def __init__(self, root):
        self.root = Path(root).expanduser()
        self.files_dir = self.root / FILES_DIR
        self._index_fp = None
        self._index_checked = float("-inf")
        self._index: dict = {}
        self._by_path: dict = {}
        self._realpaths: dict = {}
        self._records: dict = {}
        self._events_fp = None
        self._store_events: list = []

    # ----- mica-level state ------------------------------------------------
    def available(self) -> bool:
        try:
            with open(self.root / FORMAT_FILE, "r", encoding="utf-8") as fh:
                return fh.read().startswith("mica ")
        except OSError:
            return False

    def status(self) -> dict:
        """Heartbeat plus counts, shaped for the viewer's status line."""
        hb = read_json(self.root / STATUS_FILE)
        hb = hb if isinstance(hb, dict) else {}
        last = parse_iso(hb.get("last_poll"))
        age = None if last is None else max(0.0, time.time() - last)
        index = self.index()
        flagged = sum(1 for e in index.values() if _flags(e))
        cpu = hb.get("cpu_percent")
        cpu = cpu if isinstance(cpu, (int, float)) and not isinstance(cpu, bool) else None
        return {
            "enabled": True,
            "root": str(self.root),
            "running": age is not None and age < HEARTBEAT_STALE_SECONDS,
            "heartbeat_age": age,
            "started": hb.get("started"),
            "sources": hb.get("sources") or {},
            "n_files": len(index),
            "n_flagged": flagged,
            "cpu_percent": cpu,
            "cpu_warn": cpu is not None and cpu > CPU_WARN_PERCENT,
            "store_events": self.store_events(),
        }

    def store_events(self) -> list:
        """Store-wide gaps and failures (not tied to one captured transcript),
        re-read only when the log changes."""
        path = self.root / EVENTS_FILE
        try:
            st = path.stat()
            fp = (st.st_mtime_ns, st.st_size)
        except OSError:
            fp = None
        if fp != self._events_fp:
            events = read_jsonl(path) if fp else []
            self._store_events = [e for e in events if isinstance(e, dict) and not e.get("key")
                                  and e.get("type") in STORE_EVENTS_SHOWN]
            self._events_fp = fp
        return self._store_events

    def index(self) -> dict:
        """key -> summary, reloaded only when index.json changes (checked at
        most every INDEX_RECHECK_SECONDS)."""
        now = time.monotonic()
        if now - self._index_checked < INDEX_RECHECK_SECONDS:
            return self._index
        self._index_checked = now
        path = self.root / INDEX_FILE
        try:
            st = path.stat()
            fp = (st.st_mtime_ns, st.st_size)
        except OSError:
            fp = None
        if fp != self._index_fp:
            data = read_json(path) if fp else None
            files = data.get("files") if isinstance(data, dict) else None
            self._index = {k: v for k, v in (files or {}).items() if valid_key(k) and isinstance(v, dict)}
            self._by_path = {}
            for key, entry in self._index.items():
                path_value = entry.get("path")
                if isinstance(path_value, str):
                    # The live file currently at a path wins over older
                    # (deleted) records that once lived there.
                    prior = self._by_path.get(path_value)
                    if prior is None or entry.get("status") == "active":
                        self._by_path[path_value] = key
            self._index_fp = fp
        return self._index

    def key_for_path(self, path: str) -> str | None:
        """Mica key for a live transcript path, if the store tracks it."""
        index = self.index()
        key = self._by_path.get(path)
        if key is None:
            real = self._realpaths.get(path)
            if real is None:
                real = os.path.realpath(path)
                self._realpaths[path] = real
            key = self._by_path.get(real)
        return key if key in index else None

    # ----- per-file state ---------------------------------------------------
    def record(self, key: str) -> dict | None:
        """One file's record, re-read only when record.json changes (the
        sidebar asks about deleted files on every poll)."""
        if not valid_key(key):
            return None
        path = self.files_dir / key / RECORD_FILE
        try:
            st = path.stat()
        except OSError:
            return None
        fp = (st.st_mtime_ns, st.st_size)
        cached = self._records.get(key)
        if cached and cached[0] == fp:
            return cached[1]
        data = read_json(path)
        if not isinstance(data, dict):
            return None
        self._records[key] = (fp, data)
        return data

    def events(self, key: str) -> list:
        if not valid_key(key):
            return []
        return read_jsonl(self.files_dir / key / EVENTS_FILE)

    def generation_path(self, key: str, gen: dict) -> Path | None:
        mirror = gen.get("mirror") if isinstance(gen, dict) else None
        if not valid_key(key) or not isinstance(mirror, str):
            return None
        target = (self.files_dir / key / mirror).resolve()
        base = (self.files_dir / key).resolve()
        if base not in target.parents:
            return None
        return target

    def locate(self, path) -> tuple | None:
        """(key, generation dict, record) when ``path`` is a captured
        generation file inside this mica, else None."""
        try:
            target = Path(path).resolve()
            rel = target.relative_to(self.files_dir.resolve())
        except (OSError, ValueError):
            return None
        parts = rel.parts
        if len(parts) < 3 or not valid_key(parts[0]) or not valid_gen(parts[1]):
            return None
        record = self.record(parts[0])
        if record is None:
            return None
        for gen in record.get("generations") or []:
            if gen.get("id") == parts[1] and self.generation_path(parts[0], gen) == target:
                return parts[0], gen, record
        return None

    # ----- the viewer's questions ------------------------------------------
    def badge(self, key: str) -> dict:
        """Compact per-session mica state for the sidebar, from the index
        only (no file reads)."""
        entry = self.index().get(key) or {}
        flags = _flags(entry)
        state = entry.get("status") or "active"
        return {"key": key, "state": state, "flags": flags, "generations": entry.get("gens", 1)}

    def best_generation(self, record: dict) -> dict | None:
        """The generation to show for a file with no live copy: the largest
        capture (the earliest one on ties), since that holds the most history."""
        gens = [g for g in record.get("generations") or [] if isinstance(g, dict)]
        if not gens:
            return None
        return max(gens, key=lambda g: (g.get("size") or 0, -gens.index(g)))

    def _changes(self, key: str, gens: list, live_path) -> list:
        """What each recorded change did: a line diff between every closed
        capture and what came next (the following capture, or the live file
        when no new capture was started, e.g. after the file was emptied).
        A deletion has nothing after it, so it has no diff."""
        changes = []
        for i, gen in enumerate(gens):
            if not gen.get("closed") or gen.get("close_reason") == "deleted":
                continue
            before = self.generation_path(key, gen)
            if i + 1 < len(gens):
                after, after_id = self.generation_path(key, gens[i + 1]), gens[i + 1].get("id")
            elif live_path and Path(live_path).is_file():
                after, after_id = Path(live_path), "live"
            else:
                continue
            try:
                with open(before, "rb") as fh:
                    old = fh.read()
                with open(after, "rb") as fh:
                    new = fh.read()
            except (OSError, TypeError):
                continue
            change = {"from": gen.get("id"), "to": after_id, "reason": gen.get("close_reason"), "at": gen.get("closed")}
            change.update(diff_lines(old, new))
            changes.append(change)
        return changes

    def compare(self, key: str, live_path) -> dict:
        """Full comparison of a live transcript with its capture.

        state:
          verified  live file is byte-identical to the current generation
          ahead     live file extends the capture (newest bytes not yet copied)
          modified  live file is shorter than, or differs from, the capture
          deleted   no live file; the store still has the capture
        """
        record = self.record(key)
        if record is None:
            return {"state": "untracked"}
        gens = [g for g in record.get("generations") or [] if isinstance(g, dict)]
        result = {
            "key": key,
            "source": record.get("source"),
            "parser": record.get("parser"),
            "path": record.get("path"),
            "first_seen": record.get("first_seen"),
            "preexisting": bool(record.get("preexisting")),
            "flags": _flags(record),
            "recreated_from": record.get("recreated_from"),
            "generations": [],
            "events": self.events(key),
        }
        old_key = record.get("recreated_from")
        old = self.record(old_key) if old_key else None
        old_gen = self.best_generation(old) if old else None
        old_path = self.generation_path(old_key, old_gen) if old_gen else None
        if old_path:
            result["recreated_from_file"] = str(old_path)
        result["changes"] = self._changes(key, gens, live_path)
        for g in gens:
            gpath = self.generation_path(key, g)
            result["generations"].append({
                "id": g.get("id"),
                "reason": g.get("reason"),
                "started": g.get("started"),
                "closed": g.get("closed"),
                "close_reason": g.get("close_reason"),
                "size": g.get("size"),
                "file": str(gpath) if gpath else None,
            })

        live = Path(live_path) if live_path else None
        live_exists = live is not None and live.is_file()
        if not live_exists:
            result["state"] = "deleted"
            return result

        current = gens[-1] if gens and not gens[-1].get("closed") else None
        if current is None:
            # Emptied and nothing written since: compare against the last capture.
            current = gens[-1] if gens else None
        if current is None:
            result["state"] = "untracked"
            return result
        cap_path = self.generation_path(key, current)
        try:
            cap_size = cap_path.stat().st_size if cap_path else 0
            live_size = live.stat().st_size
        except OSError:
            result["state"] = "untracked"
            return result
        result["compared_generation"] = current.get("id")
        result["captured_size"] = cap_size
        result["live_size"] = live_size
        diff_at = first_difference(cap_path, live, min(cap_size, live_size))
        if diff_at is None and live_size >= cap_size and not current.get("closed"):
            result["state"] = "verified" if live_size == cap_size else "ahead"
            return result
        result["state"] = "modified"
        result["first_diff_offset"] = diff_at if diff_at is not None else min(cap_size, live_size)
        with open(cap_path, "rb") as fh:
            captured = fh.read()
        with open(live, "rb") as fh:
            live_bytes = fh.read()
        result["diff"] = diff_lines(captured, live_bytes)
        return result
