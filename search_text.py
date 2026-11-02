#!/usr/bin/env python3
"""What a session contributes to full-text search, and how a match maps back
to the source text.

Pure functions, no I/O: search_index.py stores what these build. A session's
searchable text has two segments, its user messages (ranked higher) and
everything else. Each segment is kept twice: normalized (what queries match
against, as UTF-8 bytes) and as source text (what snippets are cut from, with
its original typography), split into chunks so a snippet never has to
re-normalize everything before a match.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

import common

# Bump when extraction or normalization changes what a session's searchable
# text is; search_index drops documents built by another version.
EXTRACTOR_VERSION = 1

# Source chunks end at a newline once they reach this many characters, so a
# snippet normalizes at most about this much text. A single longer line stays
# whole.
CHUNK_CHARS = 64 * 1024

# Input text from all tool shapes, including file edits and nested exec calls.
_TOOL_INPUT_SEARCH_KEYS = (
    "cmd", "command", "file_path", "pattern", "query", "prompt", "description", "content", "url",
    "old_string", "new_string", "old_str", "new_str", "old_text", "new_text",
    "patch", "patch_text", "diff",
)


def event_text(ev: dict) -> list[str]:
    """All searchable text from a single event (both agent shapes)."""
    parts: list[str] = []

    def add(x):
        if x and isinstance(x, str):
            parts.append(x)

    def add_input(inp):
        if isinstance(inp, str):
            add(inp)
        elif isinstance(inp, dict):
            for k in _TOOL_INPUT_SEARCH_KEYS:
                add(inp.get(k))
            for call in inp.get("calls") or []:
                if isinstance(call, dict):
                    add_input(call.get("input"))
            for edit in inp.get("edits") or []:
                add_input(edit)

    if ev.get("blocks"):  # block shape (Claude Code / Cursor / opencode)
        for b in ev["blocks"]:
            add(b.get("text"))
            if b.get("type") == "tool_use":
                add_input(b.get("input"))
                res = b.get("result") or {}
                if isinstance(res, dict):
                    add(res.get("text"))
    else:  # Codex shape
        add(ev.get("text"))
        add(ev.get("summary"))
        add(ev.get("query"))
        add_input(ev.get("input"))
        res = ev.get("result")
        if isinstance(res, dict):
            add(res.get("output"))
        act = ev.get("action")
        if isinstance(act, dict):
            for q in act.get("queries") or []:
                add(q)
        request = ev.get("request")
        if isinstance(request, dict):
            add(request.get("tool"))
            add(request.get("cwd"))
            add(request.get("justification"))
            command = request.get("command")
            if isinstance(command, str):
                add(command)
            elif isinstance(command, list):
                for part in command:
                    add(part)
    return parts


def session_segments(data: dict) -> tuple[str, str]:
    """Split a parsed session into (all user messages, everything else).

    User messages are scored above generated/tool content. Image blobs are
    skipped throughout.
    """
    user: list[str] = []
    rest: list[str] = []
    cwd = (data.get("meta") or {}).get("cwd")
    if cwd:
        rest.append(cwd)

    def add_event(ev: dict) -> None:
        if ev.get("kind") == "instructions" and ev.get("role") == "system":
            return  # the agent's boilerplate system prompt would match everywhere
        if ev.get("kind") == "branch":
            for group in ev.get("groups", []) or []:
                for ge in group:
                    add_event(ge)
        elif ev.get("kind") == "user":
            user.extend(event_text(ev))
        else:
            rest.extend(event_text(ev))

    for ev in data.get("events", []) or []:
        add_event(ev)
    return "\n".join(user), "\n".join(rest)


# ---------------------------------------------------------------------------
# Search documents
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Chunk:
    norm_start: int  # byte offset in the segment's normalized text
    src_start: int   # character offset in the segment's source text
    text: str        # source text


@dataclass(frozen=True)
class Segment:
    normalized: bytes
    chunks: tuple[Chunk, ...]


@dataclass(frozen=True)
class SessionDoc:
    user: Segment
    rest: Segment


def _source_chunks(text: str, size: int):
    start = 0
    while start < len(text):
        cut = text.find("\n", start + size - 1)
        end = len(text) if cut < 0 else cut + 1
        yield start, text[start:end]
        start = end


def build_segment(text: str, chunk_chars: int = CHUNK_CHARS) -> Segment:
    """Normalize ``text`` chunk by chunk.

    Chunks end just after a newline, and normalization collapses a newline
    with the whitespace around it into one space, so joining the non-empty
    chunk normalizations with spaces equals normalizing the whole text.
    """
    parts: list[bytes] = []
    chunks: list[Chunk] = []
    offset = 0
    for src_start, chunk in _source_chunks(text, chunk_chars):
        folded = common.normalize_search_text(chunk).encode("utf-8")
        if folded and parts:
            offset += 1  # the joining space
        chunks.append(Chunk(offset, src_start, chunk))
        if folded:
            parts.append(folded)
            offset += len(folded)
    return Segment(b" ".join(parts), tuple(chunks))


def build_session_doc(data: dict | None) -> SessionDoc:
    user, rest = session_segments(data) if data else ("", "")
    return SessionDoc(build_segment(user), build_segment(rest))


# ---------------------------------------------------------------------------
# Mapping a match back to its source
# ---------------------------------------------------------------------------
def _source_offset(line: str, offset: int) -> int:
    """Smallest prefix of ``line`` whose normalization reaches ``offset``
    characters."""
    lo, hi = 0, len(line)
    while lo < hi:
        mid = (lo + hi) // 2
        if len(common.normalize_search_text(line[:mid])) < offset:
            lo = mid + 1
        else:
            hi = mid
    return lo


def source_span(text: str, index: int, length: int) -> tuple[int, int]:
    """Source [start, end) of the match at normalized character ``index``.

    Line boundaries are safe normalization boundaries, so only lines up to
    the match are normalized, and only the matching lines need a
    character-level search.
    """
    idx = end = None
    source_cursor = normalized_cursor = 0
    for line in text.splitlines(keepends=True):
        folded = common.normalize_search_text(line)
        if folded:
            line_end = normalized_cursor + len(folded)
            if idx is None and normalized_cursor <= index < line_end:
                idx = source_cursor + _source_offset(line, index - normalized_cursor + 1) - 1
            last = index + length - 1
            if normalized_cursor <= last < line_end:
                end = source_cursor + _source_offset(line, last - normalized_cursor + 1)
                break
            normalized_cursor = line_end + 1  # collapsed line separator
        source_cursor += len(line)
    # Defensive fallback for unusual Unicode line-separator behavior.
    if idx is None or end is None:
        idx = _source_offset(text, index + 1) - 1
        end = _source_offset(text, index + length)
    return idx, end


def format_snippet(text: str, idx: int, end: int, offset: int = 0) -> str:
    """The context around text[idx:end], single-spaced, with a leading
    ellipsis when it doesn't start at the beginning of the segment.
    ``offset`` is where ``text`` starts within the segment."""
    start = max(0, idx - 50)
    snippet = " ".join(text[start:end + 70].split())
    return ("…" + snippet) if offset + start > 0 else snippet


def text_snippet(text: str, normalized: str, index: int, length: int) -> tuple[str, int]:
    """(snippet, source offset) for a match in a short in-memory text such as
    a title; ``index`` and ``length`` are in normalized characters."""
    if len(text) == len(normalized) and text.lower() == normalized:
        idx, end = index, index + length
    else:
        idx, end = source_span(text, index, length)
    return format_snippet(text, idx, end), idx


def segment_snippet(chunks: list[Chunk], normalized: bytes, index: int,
                    length: int) -> tuple[str, int]:
    """(snippet, source offset in the whole segment) for the match at byte
    ``index`` of a segment's normalized text, ``length`` characters long.

    ``chunks`` must include the chunk containing the match and every chunk
    after it up to the one where the match ends; one more on each side gives
    the snippet its full context.
    """
    starts = [c.norm_start for c in chunks]
    first = max(bisect.bisect_right(starts, index) - 1, 0)
    head = chunks[first]
    local = len(normalized[head.norm_start:index].decode("utf-8"))
    tail = "".join(c.text for c in chunks[first:])
    idx, end = source_span(tail, local, length)
    before = chunks[first - 1].text if first else ""
    text = before + tail
    snippet = format_snippet(text, len(before) + idx, len(before) + end,
                             offset=head.src_start - len(before))
    return snippet, head.src_start + idx
