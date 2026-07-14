#!/usr/bin/env python3
"""Codex transcript parsing library.

Reads Codex session transcripts stored under ~/.codex/sessions. This module is
imported by server.py (the unified Claude Code + Codex transcript browser); it
exposes list_sessions() / parse_session() and the helpers they need.

Call configure(codex_home) once at startup to point it at a Codex home other
than the default ~/.codex.
"""

from __future__ import annotations

import json
import mimetypes
import re
import sqlite3
from pathlib import Path
from urllib.parse import quote

import common

DEFAULT_CODEX_HOME = Path.home() / ".codex"

# Defaults; override via configure().
CODEX_HOME = DEFAULT_CODEX_HOME
SESSIONS_DIR = DEFAULT_CODEX_HOME / "sessions"
ARCHIVED_SESSIONS_DIR = DEFAULT_CODEX_HOME / "archived_sessions"
STATE_DB = DEFAULT_CODEX_HOME / "state_5.sqlite"
MAX_INLINE_IMAGE_CHARS = 2_000_000
SUMMARY_CACHE = common.SummaryCache()


def configure(codex_home: Path) -> None:
    """Point the module at a Codex home directory."""
    global CODEX_HOME, SESSIONS_DIR, ARCHIVED_SESSIONS_DIR, STATE_DB
    CODEX_HOME = Path(codex_home).expanduser()
    SESSIONS_DIR = CODEX_HOME / "sessions"
    ARCHIVED_SESSIONS_DIR = CODEX_HOME / "archived_sessions"
    STATE_DB = CODEX_HOME / "state_5.sqlite"
    SUMMARY_CACHE.clear()


def _thread_signature(thread_row: dict | None) -> str:
    """Stable fingerprint for SQLite metadata that can change without the JSONL."""
    row = thread_row or {}
    fields = (
        "id", "title", "preview", "cwd", "updated_at", "updated_at_ms",
        "created_at", "created_at_ms", "archived", "model", "reasoning_effort",
        "tokens_used", "source", "thread_source", "cli_version", "model_provider",
    )
    return json.dumps([row.get(field) for field in fields], sort_keys=True, default=str)


def _parse_json_string(value):
    if not isinstance(value, str):
        return value if value is not None else {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}


def _mask_js_literals(source: str) -> str:
    """Blank JS strings/comments while preserving offsets for structural scans."""
    chars = list(source)
    i = 0
    quote = None
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if quote:
            chars[i] = " "
            if ch == "\\":
                if i + 1 < len(chars):
                    chars[i + 1] = " "
                    i += 2
                    continue
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in {'"', "'", "`"}:
            quote = ch
            chars[i] = " "
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < len(chars) and chars[i] != "\n":
                chars[i] = " "
                i += 1
            continue
        if ch == "/" and nxt == "*":
            chars[i] = chars[i + 1] = " "
            i += 2
            while i < len(chars):
                if chars[i] == "*" and i + 1 < len(chars) and chars[i + 1] == "/":
                    chars[i] = chars[i + 1] = " "
                    i += 2
                    break
                chars[i] = " "
                i += 1
            continue
        i += 1
    return "".join(chars)


class _JSLiteralParser:
    """Parse the JSON-like literal subset used in generated tool calls."""

    def __init__(self, source: str, start: int = 0):
        self.source = source
        self.pos = start

    def parse(self):
        self._space()
        value = self._value()
        return value, self.pos

    def _space(self) -> None:
        while self.pos < len(self.source):
            if self.source[self.pos].isspace():
                self.pos += 1
                continue
            if self.source.startswith("//", self.pos):
                end = self.source.find("\n", self.pos + 2)
                self.pos = len(self.source) if end < 0 else end + 1
                continue
            if self.source.startswith("/*", self.pos):
                end = self.source.find("*/", self.pos + 2)
                if end < 0:
                    raise ValueError("unterminated comment")
                self.pos = end + 2
                continue
            break

    def _value(self):
        self._space()
        if self.pos >= len(self.source):
            raise ValueError("missing value")
        ch = self.source[self.pos]
        if ch == "{":
            return self._object()
        if ch == "[":
            return self._array()
        if ch in {'"', "'", "`"}:
            return self._string()
        number = re.match(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?", self.source[self.pos:])
        if number:
            raw = number.group(0)
            self.pos += len(raw)
            return float(raw) if any(c in raw for c in ".eE") else int(raw)
        ident = self._identifier()
        if ident == "true":
            return True
        if ident == "false":
            return False
        if ident in {"null", "undefined"}:
            return None
        raise ValueError("non-literal expression")

    def _identifier(self) -> str:
        match = re.match(r"[A-Za-z_$][\w$]*", self.source[self.pos:])
        if not match:
            raise ValueError("expected identifier")
        value = match.group(0)
        self.pos += len(value)
        return value

    def _string(self) -> str:
        quote_char = self.source[self.pos]
        self.pos += 1
        out = []
        escapes = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0"}
        while self.pos < len(self.source):
            ch = self.source[self.pos]
            self.pos += 1
            if ch == quote_char:
                return "".join(out)
            if quote_char == "`" and ch == "$" and self.source.startswith("{", self.pos):
                raise ValueError("template expression")
            if ch != "\\":
                out.append(ch)
                continue
            if self.pos >= len(self.source):
                raise ValueError("unterminated escape")
            escaped = self.source[self.pos]
            self.pos += 1
            if escaped in escapes:
                out.append(escapes[escaped])
            elif escaped == "x" and self.pos + 2 <= len(self.source):
                out.append(chr(int(self.source[self.pos:self.pos + 2], 16)))
                self.pos += 2
            elif escaped == "u" and self.pos + 4 <= len(self.source):
                out.append(chr(int(self.source[self.pos:self.pos + 4], 16)))
                self.pos += 4
            elif escaped in "\r\n":
                if escaped == "\r" and self.pos < len(self.source) and self.source[self.pos] == "\n":
                    self.pos += 1
            else:
                out.append(escaped)
        raise ValueError("unterminated string")

    def _object(self) -> dict:
        self.pos += 1
        value = {}
        while True:
            self._space()
            if self.pos < len(self.source) and self.source[self.pos] == "}":
                self.pos += 1
                return value
            key = self._string() if self.source[self.pos] in {'"', "'", "`"} else self._identifier()
            self._space()
            if self.pos >= len(self.source) or self.source[self.pos] != ":":
                raise ValueError("expected colon")
            self.pos += 1
            value[key] = self._value()
            self._space()
            if self.pos < len(self.source) and self.source[self.pos] == ",":
                self.pos += 1
                continue
            if self.pos >= len(self.source) or self.source[self.pos] != "}":
                raise ValueError("expected object end")

    def _array(self) -> list:
        self.pos += 1
        value = []
        while True:
            self._space()
            if self.pos < len(self.source) and self.source[self.pos] == "]":
                self.pos += 1
                return value
            value.append(self._value())
            self._space()
            if self.pos < len(self.source) and self.source[self.pos] == ",":
                self.pos += 1
                continue
            if self.pos >= len(self.source) or self.source[self.pos] != "]":
                raise ValueError("expected array end")


def _parse_js_literal(source: str, start: int):
    return _JSLiteralParser(source, start).parse()


def _exec_orchestration(source: str) -> dict:
    """Recover generated `tools.name(JSON)` calls from a Codex exec program."""
    masked = _mask_js_literals(source)
    constants = {}
    for match in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", masked):
        start = match.end()
        try:
            value, _ = _parse_js_literal(source, start)
        except (ValueError, TypeError):
            continue
        constants[match.group(1)] = value

    calls = []
    for match in re.finditer(r"\btools\.([A-Za-z_$][\w$]*)\s*\(", masked):
        start = match.end()
        try:
            value, _ = _parse_js_literal(source, start)
        except (ValueError, TypeError):
            ident = re.match(r"([A-Za-z_$][\w$]*)", masked[start:])
            value = constants.get(ident.group(1)) if ident else None
        calls.append({"name": match.group(1), "input": value})
    return {"code": source, "calls": calls}


def _normalize_tool_input(name: str, raw_value):
    parsed = _parse_json_string(raw_value)
    if name != "exec":
        return parsed
    source = parsed.get("raw") if isinstance(parsed, dict) else None
    return _exec_orchestration(source) if isinstance(source, str) else parsed


def _response_user_media(payload: dict) -> tuple[str, list[str]]:
    """Visible prompt text and embedded image URLs from a response_item message."""
    texts = []
    images = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "input_image" and block.get("image_url"):
            images.append(block["image_url"])
        elif block.get("type") in {"input_text", "text"}:
            text = block.get("text") or ""
            stripped = text.strip()
            if stripped == "</image>" or (stripped.startswith("<image ") and stripped.endswith(">")):
                continue
            if text:
                texts.append(text)
    return "\n".join(texts), images


def _extract_text_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("input_text") or block.get("output_text")
                if text:
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _instruction_label(role: str, text: str) -> str:
    """Human label for an injected instruction/context message."""
    head = (text or "").lstrip()
    if head.startswith("<environment_context>"):
        return "Environment context"
    if head.startswith("<user_instructions>"):
        return "User instructions (AGENTS.md)"
    if role == "developer":
        return "Developer instructions"
    if role == "system":
        return "System prompt"
    return "Context"


def _base_instructions_text(value) -> str:
    """Codex stores the base system prompt in session_meta.base_instructions,
    usually as {"text": ...} (sometimes a JSON-encoded string)."""
    if isinstance(value, dict):
        return value.get("text") or ""
    if isinstance(value, str):
        parsed = _parse_json_string(value)
        if isinstance(parsed, dict) and "text" in parsed:
            return parsed.get("text") or ""
        return value
    return ""


def _first_user_message(records: list[dict]) -> str:
    for rec in records:
        if rec.get("type") != "event_msg":
            continue
        payload = rec.get("payload") or {}
        if payload.get("type") == "user_message" and payload.get("message"):
            return common.short_title(str(payload["message"]))
    return ""


def _thread_id_from_path(path: Path) -> str:
    stem = path.stem
    if stem.startswith("rollout-"):
        parts = stem.split("-")
        if len(parts) >= 8:
            return "-".join(parts[-5:])
    return stem


def _subagent_fields(meta: dict) -> dict:
    """Normalize Codex subagent identity and parent linkage from session_meta."""
    source = meta.get("source")
    if isinstance(source, str):
        source = _parse_json_string(source)
    subagent = source.get("subagent") if isinstance(source, dict) else None
    if meta.get("thread_source") != "subagent" and not subagent:
        return {}
    subtype = ""
    if isinstance(subagent, str):
        subtype = subagent
    elif isinstance(subagent, dict):
        subtype = subagent.get("other") or subagent.get("type") or subagent.get("name") or ""
    parent_id = meta.get("parent_thread_id") or meta.get("parent_id") or ""
    parent_file = ""
    if parent_id and re.fullmatch(r"[A-Za-z0-9-]+", str(parent_id)):
        for root in (SESSIONS_DIR, ARCHIVED_SESSIONS_DIR):
            try:
                match = next(root.glob(f"**/rollout-*{parent_id}.jsonl"), None)
            except OSError:
                match = None
            if match:
                parent_file = str(match.resolve())
                break
    return {
        "is_subagent": True,
        "subagent_type": subtype or "subagent",
        "parent_id": parent_id,
        "parent_file": parent_file,
    }


def _guardian_request(text: str) -> dict | None:
    """Extract the structured planned action from a guardian review prompt."""
    marker = "Planned action JSON:"
    start = text.rfind(marker)
    if start < 0:
        return None
    start = text.find("{", start + len(marker))
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _guardian_decision(text: str) -> dict | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("outcome") not in {"allow", "deny"}:
        return None
    return value


def _turn_metadata(events: list[dict]) -> dict:
    """Collapse generic Codex bookkeeping for one model turn."""
    meta: dict = {}
    raw_types = []
    for ev in events:
        kind = ev.get("kind")
        if kind == "context":
            for key in ("turn_id", "model", "effort", "approval_policy", "sandbox_policy", "summary"):
                if ev.get(key) is not None:
                    meta[key] = ev[key]
        elif kind == "status":
            if ev.get("turn_id"):
                meta["turn_id"] = ev["turn_id"]
            for key in ("context_window", "collaboration_mode", "duration_ms", "time_to_first_token_ms"):
                if ev.get(key) is not None:
                    meta[key] = ev[key]
            if ev.get("reason"):
                meta["completion_reason"] = ev["reason"]
        elif kind == "tokens":
            meta["usage"] = ev.get("usage") or {}
            if ev.get("context_window") is not None:
                meta["context_window"] = ev["context_window"]
            if ev.get("rate_limits") is not None:
                meta["rate_limits"] = ev["rate_limits"]
        elif kind == "raw" and ev.get("record_type"):
            raw_types.append(ev["record_type"])
    if raw_types:
        meta["record_types"] = sorted(set(raw_types))
    return meta


def _fold_turn_metadata(
    events: list[dict],
    *,
    anchor_kinds: set[str],
    metadata_kinds: set[str] | None = None,
    prefer_first: bool = False,
    field: str = "turn_metadata",
) -> list[dict]:
    """Attach turn bookkeeping to a semantic event instead of separate cards."""
    metadata_kinds = metadata_kinds or {"status", "context", "tokens"}
    out = []
    pending: list[dict] = []
    anchors: list[dict] = []

    def finish() -> None:
        nonlocal anchors, pending
        if anchors:
            metadata = _turn_metadata(pending)
            if metadata:
                target = anchors[0] if prefer_first else anchors[-1]
                target[field] = metadata
        anchors = []
        pending = []

    for ev in events:
        if ev.get("kind") in metadata_kinds:
            if ev.get("kind") == "status" and ev.get("status") == "started" and anchors:
                finish()
            pending.append(ev)
            if ev.get("kind") == "status" and ev.get("status") in {"complete", "aborted"}:
                finish()
            continue
        if ev.get("kind") in anchor_kinds:
            anchors.append(ev)
        out.append(ev)
    finish()
    return out


def _read_thread_rows() -> dict[str, dict]:
    if not STATE_DB.exists():
        return {}
    query = """
        select id, rollout_path, created_at, updated_at, created_at_ms, updated_at_ms,
               source, model_provider, cwd, title, tokens_used, archived,
               cli_version, first_user_message, model, reasoning_effort,
               thread_source, preview
        from threads
    """
    rows: dict[str, dict] = {}
    try:
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(query):
                d = dict(row)
                if d.get("rollout_path"):
                    rows[str(Path(d["rollout_path"]).expanduser())] = d
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return rows


def session_summary(path: Path, thread_row: dict | None = None) -> dict:
    """Lightweight metadata for a Codex session, cached by file identity plus a
    fingerprint of the SQLite thread row (which can change without the JSONL)."""
    st = common.safe_stat(path)
    identity = (st.st_mtime_ns, st.st_size) if st else (0, 0)
    fingerprint = (identity[0], identity[1], _thread_signature(thread_row))
    cached = SUMMARY_CACHE.get(str(path), fingerprint)
    if cached is not None:
        return cached

    summary = _session_summary_uncached(path, thread_row)
    SUMMARY_CACHE.put(str(path), fingerprint, summary)
    return summary


def _session_summary_uncached(path: Path, thread_row: dict | None = None) -> dict:
    records = list(common.iter_jsonl(path))
    meta = {}
    first_ts = None
    last_ts = None
    n_user = n_assistant = n_tool = n_reasoning = n_web = 0
    model = ""

    for rec in records:
        ts = rec.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        typ = rec.get("type")
        payload = rec.get("payload") or {}
        if typ == "session_meta":
            meta.update(payload)
        elif typ == "turn_context":
            model = model or payload.get("model", "")
        elif typ == "event_msg":
            pt = payload.get("type")
            if pt == "user_message":
                n_user += 1
            elif pt == "agent_message":
                n_assistant += 1
            elif pt == "agent_reasoning":
                n_reasoning += 1
            elif pt == "web_search_end":
                n_web += 1
        elif typ == "response_item":
            pt = payload.get("type")
            if pt in {"function_call", "custom_tool_call"}:
                n_tool += 1
            elif pt == "reasoning":
                n_reasoning += 1
            elif pt == "web_search_call":
                n_web += 1

    st = common.safe_stat(path)
    row = thread_row or {}
    title = _first_user_message(records) or row.get("title") or row.get("preview") or "(untitled session)"
    cwd = row.get("cwd") or meta.get("cwd") or ""
    updated_ms = row.get("updated_at_ms") or (row.get("updated_at") * 1000 if row.get("updated_at") else None)
    created_ms = row.get("created_at_ms") or (row.get("created_at") * 1000 if row.get("created_at") else None)

    subagent_fields = _subagent_fields(meta)
    if subagent_fields.get("subagent_type") == "guardian":
        title = "Approval reviews"
    elif subagent_fields:
        title = common.short_title(f"[{subagent_fields['subagent_type']}] {title}")

    summary = {
        "agent": "codex",
        "id": row.get("id") or meta.get("id") or _thread_id_from_path(path),
        "file": str(path),
        "title": title,
        "cwd": cwd,
        "source": row.get("source") or meta.get("source") or meta.get("originator") or "",
        "thread_source": row.get("thread_source") or meta.get("thread_source") or "",
        "version": row.get("cli_version") or meta.get("cli_version") or "",
        "model_provider": row.get("model_provider") or meta.get("model_provider") or "",
        "model": row.get("model") or model,
        "reasoning_effort": row.get("reasoning_effort") or "",
        "tokens_used": row.get("tokens_used") or 0,
        "first_ts": first_ts or common.iso_from_ms(created_ms),
        "last_ts": last_ts or common.iso_from_ms(updated_ms),
        "n_user": n_user,
        "n_assistant": n_assistant,
        "n_tool": n_tool,
        "n_reasoning": n_reasoning,
        "n_web": n_web,
        "n_records": len(records),
        "mtime": st.st_mtime if st else 0,
        "archived": bool(row.get("archived", 0)),
    }
    summary.update(subagent_fields)
    return summary


def list_sessions() -> list[dict]:
    """Flat list of Codex session summaries (live + archived + DB-referenced)."""
    rows_by_path = _read_thread_rows()
    paths = set()

    if SESSIONS_DIR.exists():
        paths.update(SESSIONS_DIR.glob("**/rollout-*.jsonl"))
    if ARCHIVED_SESSIONS_DIR.exists():
        paths.update(ARCHIVED_SESSIONS_DIR.glob("**/rollout-*.jsonl"))
    for p in rows_by_path:
        path = Path(p).expanduser()
        if path.exists():
            paths.add(path)

    out: list[dict] = []
    for path in sorted(paths):
        try:
            resolved = path.resolve()
            row = rows_by_path.get(str(resolved)) or rows_by_path.get(str(path))
            out.append(session_summary(resolved, row))
        except (OSError, ValueError):
            continue
    return out


def _summary_text(summary) -> str:
    if isinstance(summary, str):
        return summary
    if isinstance(summary, list):
        parts = []
        for item in summary:
            if isinstance(item, dict):
                parts.append(item.get("text") or item.get("summary") or json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if summary:
        return str(summary)
    return ""


def _patch_text(args) -> str:
    """Codex sends apply_patch bodies as a raw patch string (not JSON)."""
    if isinstance(args, str):
        return args
    if isinstance(args, dict):
        for key in ("input", "patch", "raw"):
            val = args.get(key)
            if isinstance(val, str):
                return val
    return ""


def _patch_files(text: str) -> list[str]:
    files = []
    for line in (text or "").splitlines():
        for marker in ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: "):
            if line.startswith(marker):
                files.append(line[len(marker):].strip())
    return files


def _tool_summary(name: str, args) -> str:
    if name == "apply_patch":
        files = _patch_files(_patch_text(args))
        return ", ".join(files)[:200] if files else "patch"
    if name == "exec" and isinstance(args, dict):
        calls = args.get("calls") or []
        names = [call.get("name") for call in calls if isinstance(call, dict) and call.get("name")]
        return " · ".join(names)[:200] if names else "orchestration code"
    if not isinstance(args, dict):
        return str(args)[:200]
    if name in {"exec_command", "shell"}:
        return str(args.get("cmd") or "")[:200]
    if name == "write_stdin":
        return str(args.get("session_id") or "")[:80]
    if name == "parallel":
        uses = args.get("tool_uses") or []
        return f"{len(uses)} tool calls"
    if "query" in args:
        return str(args["query"])[:200]
    if "path" in args:
        return str(args["path"])[:200]
    if "file" in args:
        return str(args["file"])[:200]
    if "session_id" in args:
        return "session: " + str(args["session_id"])[:80]
    if args:
        key = next(iter(args))
        return f"{key}: {str(args[key]).splitlines()[0][:160]}"
    return ""


def _event_payload(kind: str, ts: str | None, payload: dict) -> dict:
    out = {"kind": kind, "ts": ts}
    out.update(payload)
    return out


def _safe_images(images, allow_large: bool = False) -> list[dict]:
    out = []
    if not isinstance(images, list):
        return out
    for item in images:
        if isinstance(item, str):
            if allow_large or len(item) <= MAX_INLINE_IMAGE_CHARS:
                out.append({"kind": "inline", "src": item, "bytes": len(item)})
            else:
                out.append({"kind": "omitted", "bytes": len(item), "reason": "inline image too large"})
        elif isinstance(item, dict):
            text = json.dumps(item)
            if allow_large or len(text) <= MAX_INLINE_IMAGE_CHARS:
                out.append({"kind": "object", "value": item, "bytes": len(text)})
            else:
                out.append({"kind": "omitted", "bytes": len(text), "reason": "image object too large"})
        else:
            out.append({"kind": "unknown", "value": str(item)[:500]})
    return out


def _local_image_payload(path_value) -> dict | None:
    if not isinstance(path_value, str) or not path_value:
        return None
    path = Path(path_value).expanduser()
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.exists() or not resolved.is_file():
        return None
    content_type = mimetypes.guess_type(str(resolved))[0] or ""
    if not content_type.startswith("image/"):
        return None
    st = common.safe_stat(resolved)
    return {
        "kind": "local",
        "src": "/api/local-image?path=" + quote(str(resolved), safe=""),
        "path": str(resolved),
        "bytes": st.st_size if st else 0,
        "content_type": content_type,
    }


def _unwrap_exec_text(text: str) -> dict | None:
    candidate = text.rpartition("Output:")[2].strip() if "Output:" in text else text.strip()
    try:
        inner = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(inner, dict) or "output" not in inner:
        return None
    metadata = {
        key: inner[key]
        for key in (
            "session_id", "chunk_id", "exit_code", "wall_time_seconds",
            "original_token_count", "output_hint",
        )
        if inner.get(key) is not None
    }
    return {
        "text": str(inner.get("output") or ""),
        "metadata": metadata,
        "is_error": bool(inner.get("exit_code") is not None and inner.get("exit_code") != 0),
    }


def _normalize_tool_output(output, name: str = "", args=None) -> dict:
    normalized = {"text": "", "images": [], "raw": None, "metadata": {}, "is_error": False}
    args = args if isinstance(args, dict) else {}
    local_image = None
    if name == "view_image":
        local_image = _local_image_payload(args.get("path"))
        if local_image:
            normalized["images"].append(local_image)

    if output is None:
        return normalized
    if isinstance(output, str):
        if name == "exec":
            unwrapped = _unwrap_exec_text(output)
            if unwrapped:
                normalized.update(unwrapped)
                return normalized
        normalized["text"] = output
        return normalized
    if isinstance(output, list):
        texts = []
        images = list(normalized["images"])
        raw_remainder = []
        for item in output:
            if isinstance(item, str):
                texts.append(item)
                continue
            if not isinstance(item, dict):
                raw_remainder.append(item)
                continue
            item_type = item.get("type")
            image_url = item.get("image_url") or item.get("url")
            if item_type in {"input_image", "image"} and image_url:
                # Prefer the local file for view_image. If it is unavailable, fall back to
                # the embedded payload even when it is large.
                if not local_image:
                    images.extend(_safe_images([image_url], allow_large=True))
            elif item_type in {"text", "output_text", "input_text"}:
                texts.append(item.get("text") or "")
            else:
                raw_remainder.append(item)
        normalized["text"] = "\n".join(t for t in texts if t)
        normalized["images"] = images
        if name == "exec":
            unwrapped = _unwrap_exec_text(normalized["text"])
            if unwrapped:
                normalized.update(unwrapped)
        if raw_remainder:
            normalized["raw"] = raw_remainder
        return normalized
    if isinstance(output, dict):
        image_url = output.get("image_url") or output.get("url")
        if output.get("type") in {"input_image", "image"} and image_url:
            if not local_image:
                normalized["images"] = _safe_images([image_url], allow_large=True)
        elif output.get("text"):
            normalized["text"] = output["text"]
        else:
            normalized["raw"] = output
        return normalized
    normalized["text"] = str(output)
    return normalized


def parse_session(path: Path) -> dict:
    records = list(common.iter_jsonl(path))
    meta = {}
    title = ""
    turn_contexts: dict[str, dict] = {}
    tool_calls: dict[str, dict] = {}
    tool_outputs: dict[str, dict] = {}
    web_searches: dict[str, dict] = {}
    # Normalized text of every real user prompt (event_msg user_message). Used to
    # tell injected context (environment_context, AGENTS.md) apart from the prompt
    # when a `message` response_item repeats the user's turn.
    user_event_texts: set[str] = set()
    user_image_fallbacks: dict[str, list[list[dict]]] = {}

    for rec in records:
        payload = rec.get("payload") or {}
        if rec.get("type") == "session_meta":
            meta.update(payload)
        elif rec.get("type") == "event_msg" and payload.get("type") == "user_message":
            msg = payload.get("message")
            if msg:
                user_event_texts.add(" ".join(str(msg).split()))
        elif rec.get("type") == "turn_context":
            turn_id = payload.get("turn_id")
            if turn_id:
                turn_contexts[turn_id] = payload
        elif (
            rec.get("type") == "response_item"
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            visible_text, image_urls = _response_user_media(payload)
            if image_urls:
                key = " ".join(visible_text.split())
                user_image_fallbacks.setdefault(key, []).append(
                    _safe_images(image_urls, allow_large=True)
                )
        elif rec.get("type") == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
            call_id = payload.get("call_id")
            if call_id:
                raw_args = payload.get("arguments") if payload.get("type") == "function_call" else payload.get("input")
                name = payload.get("name") or "tool"
                tool_calls[call_id] = {
                    "name": name,
                    "input": _normalize_tool_input(name, raw_args),
                    "status": payload.get("status"),
                    "type": payload.get("type"),
                }
        elif rec.get("type") == "response_item" and payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            call_id = payload.get("call_id")
            if call_id:
                call = tool_calls.get(call_id, {})
                normalized = _normalize_tool_output(
                    payload.get("output"),
                    name=call.get("name", ""),
                    args=call.get("input"),
                )
                # A patch_apply_end event may have already recorded the richer
                # diff (changes); don't let the terse call output clobber it.
                existing = tool_outputs.get(call_id)
                raw = normalized["raw"]
                if raw is None and existing and existing.get("raw"):
                    raw = existing["raw"]
                tool_outputs[call_id] = {
                    "output": normalized["text"] or (existing or {}).get("output", ""),
                    "images": normalized["images"],
                    "raw": raw,
                    "metadata": normalized.get("metadata") or {},
                    "is_error": bool(payload.get("is_error")) or normalized.get("is_error", False),
                }
        elif rec.get("type") == "event_msg" and payload.get("type") == "patch_apply_end":
            call_id = payload.get("call_id")
            if call_id:
                changes = payload.get("changes")
                raw = {"changes": changes} if changes else None
                tool_outputs[call_id] = {
                    "output": "\n".join(x for x in [payload.get("stdout"), payload.get("stderr")] if x),
                    "images": [],
                    "raw": raw,
                    "is_error": not bool(payload.get("success", True)),
                }
        elif rec.get("type") == "event_msg" and payload.get("type") == "web_search_end":
            call_id = payload.get("call_id")
            if call_id:
                web_searches[call_id] = payload

    row = _read_thread_rows().get(str(path))
    if row:
        title = row.get("title") or row.get("preview") or ""
        meta.update(
            {
                "cwd": row.get("cwd") or meta.get("cwd"),
                "source": row.get("source") or meta.get("source"),
                "model_provider": row.get("model_provider") or meta.get("model_provider"),
                "version": row.get("cli_version") or meta.get("cli_version"),
                "tokens_used": row.get("tokens_used"),
                "model": row.get("model"),
                "reasoning_effort": row.get("reasoning_effort"),
            }
        )

    subagent_fields = _subagent_fields(meta)
    is_guardian = subagent_fields.get("subagent_type") == "guardian"
    events = []
    recent_reasoning: list[tuple[str, str]] = []

    def append_reasoning(ts: str | None, text: str, has_encrypted: bool) -> None:
        normalized = " ".join((text or "").split())
        if normalized:
            for prev_ts, prev_text in recent_reasoning[-8:]:
                if prev_text == normalized:
                    return
            recent_reasoning.append((ts or "", normalized))
        events.append(
            _event_payload(
                "reasoning",
                ts,
                {
                    "text": text,
                    "has_encrypted": has_encrypted,
                },
            )
        )

    for rec in records:
        typ = rec.get("type")
        ts = rec.get("timestamp")
        payload = rec.get("payload") or {}

        if typ == "session_meta":
            base = _base_instructions_text(payload.get("base_instructions"))
            if base:
                events.append(
                    _event_payload(
                        "instructions",
                        ts,
                        {"role": "system", "label": "Base instructions", "text": base},
                    )
                )
            continue

        if typ == "turn_context":
            events.append(
                _event_payload(
                    "context",
                    ts,
                    {
                        "turn_id": payload.get("turn_id"),
                        "cwd": payload.get("cwd"),
                        "model": payload.get("model"),
                        "effort": payload.get("effort"),
                        "approval_policy": payload.get("approval_policy"),
                        "sandbox_policy": payload.get("sandbox_policy"),
                        "summary": payload.get("summary"),
                    },
                )
            )
            continue

        if typ == "compacted":
            replacement = payload.get("replacement_history") or []
            compaction_items = [
                item for item in replacement
                if isinstance(item, dict) and item.get("type") == "compaction"
            ]
            readable_summary = payload.get("message") or ""
            if not readable_summary:
                readable_summary = "\n\n".join(
                    text for text in (
                        _extract_text_content(item.get("content"))
                        or _summary_text(item.get("summary"))
                        for item in compaction_items
                    )
                    if text
                )
            events.append(
                _event_payload(
                    "system",
                    ts,
                    {
                        "subtype": "compact_boundary",
                        "text": readable_summary,
                        "compaction": {
                            "source": "codex",
                            "window_number": payload.get("window_number"),
                            "previous_window_id": payload.get("previous_window_id"),
                            "window_id": payload.get("window_id"),
                            "replacement_items": len(replacement),
                            "summary_encrypted": bool(compaction_items)
                            and not bool(readable_summary)
                            and any(item.get("encrypted_content") for item in compaction_items),
                        },
                    },
                )
            )
            continue

        if typ == "world_state":
            if (
                events
                and events[-1].get("kind") == "system"
                and events[-1].get("subtype") == "compact_boundary"
            ):
                events[-1]["metadata"] = {"world_state": payload}
                continue

        if typ == "event_msg":
            pt = payload.get("type")
            if pt == "user_message":
                text = payload.get("message") or ""
                request = _guardian_request(text) if is_guardian else None
                if request is not None:
                    events.append(
                        _event_payload(
                            "guardian_request",
                            ts,
                            {"request": request, "context": text},
                        )
                    )
                else:
                    key = " ".join(text.split())
                    fallback_groups = user_image_fallbacks.get(key) or []
                    fallback_images = fallback_groups.pop(0) if fallback_groups else []
                    local_paths = payload.get("local_images") or []
                    images = []
                    unavailable_paths = []
                    if local_paths:
                        for i, local_path in enumerate(local_paths):
                            local = _local_image_payload(local_path)
                            if local:
                                images.append(local)
                            elif i < len(fallback_images):
                                images.append(fallback_images[i])
                            else:
                                unavailable_paths.append(local_path)
                        images.extend(fallback_images[len(local_paths):])
                    else:
                        images = _safe_images(payload.get("images") or []) or fallback_images
                    events.append(
                        _event_payload(
                            "user",
                            ts,
                            {
                                "text": text,
                                "images": images,
                                "local_images": unavailable_paths,
                                "text_elements": payload.get("text_elements") or [],
                            },
                        )
                    )
            elif pt == "agent_message":
                text = payload.get("message") or ""
                decision = _guardian_decision(text) if is_guardian else None
                if decision is not None:
                    events.append(_event_payload("guardian_decision", ts, decision))
                else:
                    events.append(
                        _event_payload(
                            "assistant",
                            ts,
                            {
                                "text": text,
                                "phase": payload.get("phase"),
                                "memory_citation": payload.get("memory_citation"),
                            },
                        )
                    )
            elif pt == "agent_reasoning":
                append_reasoning(ts, payload.get("text") or "", False)
            elif pt == "task_started":
                ctx = turn_contexts.get(payload.get("turn_id"), {})
                events.append(
                    _event_payload(
                        "status",
                        ts,
                        {
                            "status": "started",
                            "turn_id": payload.get("turn_id"),
                            "model": ctx.get("model"),
                            "context_window": payload.get("model_context_window"),
                            "collaboration_mode": payload.get("collaboration_mode_kind"),
                        },
                    )
                )
            elif pt == "task_complete":
                events.append(
                    _event_payload(
                        "status",
                        ts,
                        {
                            "status": "complete",
                            "turn_id": payload.get("turn_id"),
                            "duration_ms": payload.get("duration_ms"),
                            "time_to_first_token_ms": payload.get("time_to_first_token_ms"),
                        },
                    )
                )
            elif pt == "turn_aborted":
                events.append(
                    _event_payload(
                        "status",
                        ts,
                        {
                            "status": "aborted",
                            "turn_id": payload.get("turn_id"),
                            "reason": payload.get("reason"),
                            "duration_ms": payload.get("duration_ms"),
                        },
                    )
                )
            elif pt == "token_count":
                info = payload.get("info") or {}
                usage = info.get("total_token_usage") or {}
                events.append(
                    _event_payload(
                        "tokens",
                        ts,
                        {
                            "usage": usage,
                            "context_window": info.get("model_context_window"),
                            "rate_limits": payload.get("rate_limits"),
                        },
                    )
                )
            elif pt == "web_search_end":
                events.append(
                    _event_payload(
                        "web_search",
                        ts,
                        {
                            "call_id": payload.get("call_id"),
                            "query": payload.get("query"),
                            "action": payload.get("action"),
                        },
                    )
                )
            continue

        if typ != "response_item":
            if typ:
                events.append(_event_payload("raw", ts, {"record_type": typ, "payload": payload}))
            continue

        pt = payload.get("type")
        if pt == "reasoning":
            text = _extract_text_content(payload.get("content")) or _summary_text(payload.get("summary"))
            append_reasoning(ts, text, bool(payload.get("encrypted_content")))
        elif pt in {"function_call", "custom_tool_call"}:
            name = payload.get("name") or "tool"
            args = _normalize_tool_input(
                name,
                payload.get("arguments") if pt == "function_call" else payload.get("input"),
            )
            if name == "apply_patch":
                args = _patch_text(args) or args
            call_id = payload.get("call_id")
            result = tool_outputs.get(call_id)
            events.append(
                _event_payload(
                    "tool",
                    ts,
                    {
                        "id": call_id,
                        "name": name,
                        "input": args,
                        "summary": _tool_summary(name, args),
                        "result": result,
                        "status": payload.get("status"),
                        "tool_record_type": pt,
                    },
                )
            )
        elif pt == "message":
            # Real user/assistant turns arrive as event_msg; the `message`
            # response_items repeat them plus carry the injected system prompt
            # (developer/system) and context (environment_context, AGENTS.md),
            # which is otherwise never surfaced. Emit only the latter.
            role = payload.get("role") or ""
            text = _extract_text_content(payload.get("content"))
            if text.strip() and (
                role in {"developer", "system"}
                or (role == "user" and " ".join(text.split()) not in user_event_texts)
            ):
                events.append(
                    _event_payload(
                        "instructions",
                        ts,
                        {"role": role, "label": _instruction_label(role, text), "text": text},
                    )
                )
        elif pt == "web_search_call":
            action = payload.get("action") or {}
            call_id = payload.get("call_id")
            matched = web_searches.get(call_id, {})
            events.append(
                _event_payload(
                    "web_call",
                    ts,
                    {
                        "status": payload.get("status"),
                        "query": matched.get("query") or action.get("query"),
                        "action": action,
                    },
                )
            )

    if is_guardian:
        events = _fold_turn_metadata(
            events,
            anchor_kinds={"guardian_request"},
            metadata_kinds={"status", "context", "tokens", "raw"},
            prefer_first=True,
            field="metadata",
        )
    else:
        events = _fold_turn_metadata(events, anchor_kinds={"user", "assistant"})

    title = _first_user_message(records) or title
    if is_guardian:
        title = "Approval reviews"
    elif subagent_fields:
        title = common.short_title(f"[{subagent_fields['subagent_type']}] {title}")
    meta.pop("base_instructions", None)  # surfaced as an instructions event instead

    data = {
        "agent": "codex",
        "id": meta.get("id") or _thread_id_from_path(path),
        "title": title or "(untitled session)",
        "meta": meta,
        "events": events,
        "n_records": len(records),
    }
    data.update(subagent_fields)
    return data
