#!/usr/bin/env python3
"""Decode Cursor's ``toolFormerData.toolCallBinary`` protobuf records.

Newer Cursor versions are migrating tool results out of the JSON ``result``
field and into ``toolCallBinary`` — a base64-encoded protobuf blob. Grep
results have essentially always lived there (548 of 559 observed grep bubbles
have no JSON result), newer glob results leave the JSON ``files`` list empty,
and ``await`` results were never in JSON at all. This module decodes just
enough of the protobuf *wire format* (no .proto schema, no dependency) to get
the content back.

Reverse-engineered envelope, shared by every tool call observed:

    toolCallBinary = {
        f<N>: {            # per-tool oneof: grep=5, glob=4, await=42, …
            f1: <request echo>,
            f2: <result>,
        },
        f57: call ids, f59/f60: start/end epoch-ms,
    }

Known result layouts:

    grep result:  f1 → f4(workspace){ f1: root path,
                    f2(container){ f3{f1: file}   # "content" output mode
                                   f2: file } }   # "files_with_matches" mode
                  file = { f1: relative path,
                           repeated f2: { f1: line number, f2: line text } }
    glob result:  f1(dir){ f2: absPath, repeated f3: relPath, f4: count }

For every other tool, :func:`generic_result_text` walks the result section and
returns its readable strings — best effort, but strictly better than showing
"(no output)" for a call that did produce one.
"""

from __future__ import annotations

import base64
import binascii

_GENERIC_MAX_LINES = 300


# ---------------------------------------------------------------------------
# Wire-format reader
# ---------------------------------------------------------------------------
def _pb_fields(data: bytes) -> list[tuple[int, int, object]]:
    """Parse one protobuf message into (field, wire, value) triples.

    Values are ints (wire 0) or raw bytes (wires 1/2/5). Raises ValueError on
    malformed input — callers treat that as "not a message"."""
    out: list[tuple[int, int, object]] = []
    i = 0

    def varint(i):
        val = shift = 0
        while True:
            if i >= len(data):
                raise ValueError("truncated varint")
            b = data[i]
            i += 1
            val |= (b & 0x7F) << shift
            if not b & 0x80:
                return val, i
            shift += 7

    while i < len(data):
        key, i = varint(i)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError("field 0")
        if wire == 0:
            val, i = varint(i)
            out.append((field, 0, val))
        elif wire == 2:
            ln, i = varint(i)
            if i + ln > len(data):
                raise ValueError("length overrun")
            out.append((field, 2, data[i:i + ln]))
            i += ln
        elif wire == 1:
            out.append((field, 1, data[i:i + 8]))
            i += 8
        elif wire == 5:
            out.append((field, 5, data[i:i + 4]))
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out


def _pb_msgs(fields, num: int) -> list[list]:
    """All wire-2 values of field `num` that parse as messages."""
    out = []
    for field, wire, val in fields:
        if field == num and wire == 2:
            try:
                out.append(_pb_fields(val))
            except ValueError:
                continue
    return out


def _pb_strs(fields, num: int) -> list[str]:
    out = []
    for field, wire, val in fields:
        if field == num and wire == 2:
            try:
                out.append(val.decode("utf-8"))
            except UnicodeDecodeError:
                continue
    return out


def _pb_int(fields, num: int):
    for field, wire, val in fields:
        if field == num and wire == 0:
            return val
    return None


def _decode(tf: dict) -> list | None:
    """toolCallBinary → parsed top-level fields, or None."""
    raw = tf.get("toolCallBinary")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        blob = base64.b64decode(raw, validate=True)
        return _pb_fields(blob)
    except (ValueError, binascii.Error):
        return None


def _result_sections(top) -> list[list]:
    """The result (f2) messages of every per-tool envelope in the record.

    Envelopes are the low-numbered message fields; f57+ carry call ids and
    timestamps, never tool payloads."""
    out = []
    for field, wire, val in top:
        if wire != 2 or field >= 50:
            continue
        try:
            envelope = _pb_fields(val)
        except ValueError:
            continue
        out.extend(_pb_msgs(envelope, 2))
    return out


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------
def _grep_file_lines(file_msg) -> list[str]:
    """Render one grep file entry: its path, then `  <line no>: <text>`."""
    out = []
    paths = _pb_strs(file_msg, 1)
    if paths:
        out.append(paths[0])
    for line_msg in _pb_msgs(file_msg, 2):
        no = _pb_int(line_msg, 1)
        texts = _pb_strs(line_msg, 2)
        if texts and no is not None:
            out.append(f"  {no}: {texts[0]}")
        elif texts:
            out.append(f"  {texts[0]}")
    return out


def _grep_binary_text(tf: dict) -> str:
    """Reconstruct ripgrep output from toolCallBinary; '' if undecodable."""
    top = _decode(tf)
    if top is None:
        return ""
    lines: list[str] = []
    for wrapper in _result_sections(top):
        for result in _pb_msgs(wrapper, 1):
            for workspace in _pb_msgs(result, 4):
                for container in _pb_msgs(workspace, 2):
                    # content mode nests the file under f3; the
                    # files_with_matches mode carries it directly at f2.
                    for entry in _pb_msgs(container, 3):
                        for file_msg in _pb_msgs(entry, 1):
                            lines.extend(_grep_file_lines(file_msg))
                    for file_msg in _pb_msgs(container, 2):
                        lines.extend(_grep_file_lines(file_msg))
    return "\n".join(lines)


def _grep_stats_summary(additional: dict) -> str:
    """Human summary from additionalData when match content isn't recoverable."""
    if not isinstance(additional, dict):
        return ""
    total = additional.get("totalMatches")
    if total is None:
        return ""
    if not total:
        return "(no matches)"
    files = additional.get("totalFiles")
    lines = [f"{total} matches in {files} files" if files is not None else f"{total} matches"]
    for top in additional.get("topFiles") or []:
        if isinstance(top, dict) and top.get("uri"):
            count = top.get("matchCount")
            lines.append(f"{top['uri']} ({count} matches)" if count is not None else str(top["uri"]))
    return "\n".join(lines)


def grep_result_text(tf: dict) -> str:
    """Best available grep output: decoded binary, then stats, then ''."""
    additional = tf.get("additionalData") or {}
    text = _grep_binary_text(tf)
    if text:
        if additional.get("isPruned") and additional.get("totalMatches"):
            text += (
                f"\n[shown output pruned by Cursor: {additional['totalMatches']} matches"
                f" in {additional.get('totalFiles')} files total]"
            )
        return text
    return _grep_stats_summary(additional)


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------
def glob_files(tf: dict) -> list[str]:
    """File paths from a glob toolCallBinary; [] if undecodable."""
    top = _decode(tf)
    if top is None:
        return []
    files: list[str] = []
    for wrapper in _result_sections(top):
        for dir_msg in _pb_msgs(wrapper, 1):
            bases = _pb_strs(dir_msg, 2)
            base = bases[0] if bases else ""
            for rel in _pb_strs(dir_msg, 3):
                files.append(f"{base}/{rel}" if base and rel else (rel or base))
    return files


# ---------------------------------------------------------------------------
# Generic fallback for every other tool
# ---------------------------------------------------------------------------
def _collect_strings(fields, depth: int, out: list[str]) -> None:
    for field, wire, val in fields:
        if wire != 2 or not val:
            continue
        try:
            nested = _pb_fields(val)
        except ValueError:
            nested = None
        if nested is not None and len(val) > 1:
            _collect_strings(nested, depth + 1, out)
            continue
        try:
            text = val.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text.strip() and len(text) >= 2:
            out.append(text)


def generic_result_text(tf: dict) -> str:
    """Readable strings from the result section of any tool's binary record.

    Best effort — protobuf without a schema can't distinguish every string
    from a nested message — but for a completed call whose JSON result was
    never written, recovered content beats "(no output)"."""
    top = _decode(tf)
    if top is None:
        return ""
    lines: list[str] = []
    for wrapper in _result_sections(top):
        _collect_strings(wrapper, 0, lines)
    if len(lines) > _GENERIC_MAX_LINES:
        lines = lines[:_GENERIC_MAX_LINES] + [f"… ({len(lines) - _GENERIC_MAX_LINES} more lines)"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conversation state (agentKv blob chain)
# ---------------------------------------------------------------------------
def conversation_state_hashes(state: str) -> list[str]:
    """Blob hashes from ``composerData.conversationState``, in message order.

    Cursor keeps the exact provider-format message array (the one it sends to
    the model, complete with OpenAI ``encrypted_content`` reasoning items) out
    of line: the composer stores only a protobuf whose repeated field 1 holds
    one 32-byte sha256 per message, each addressing an ``agentKv:blob:<hex>``
    row in the same ``cursorDiskKV`` table. The value is base64, prefixed with
    a literal ``~``.
    """
    if not isinstance(state, str) or not state:
        return []
    if state.startswith("~"):
        state = state[1:]
    try:
        raw = base64.b64decode(state)
    except (binascii.Error, ValueError):
        return []
    try:
        fields = _pb_fields(raw)
    except ValueError:
        return []
    return [
        val.hex()
        for field, wire, val in fields
        if field == 1 and wire == 2 and isinstance(val, bytes) and len(val) == 32
    ]
