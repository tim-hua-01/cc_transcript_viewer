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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

DEFAULT_CODEX_HOME = Path.home() / ".codex"

# Defaults; override via configure().
CODEX_HOME = DEFAULT_CODEX_HOME
SESSIONS_DIR = DEFAULT_CODEX_HOME / "sessions"
ARCHIVED_SESSIONS_DIR = DEFAULT_CODEX_HOME / "archived_sessions"
STATE_DB = DEFAULT_CODEX_HOME / "state_5.sqlite"
MAX_INLINE_IMAGE_CHARS = 2_000_000


def configure(codex_home: Path) -> None:
    """Point the module at a Codex home directory."""
    global CODEX_HOME, SESSIONS_DIR, ARCHIVED_SESSIONS_DIR, STATE_DB
    CODEX_HOME = Path(codex_home).expanduser()
    SESSIONS_DIR = CODEX_HOME / "sessions"
    ARCHIVED_SESSIONS_DIR = CODEX_HOME / "archived_sessions"
    STATE_DB = CODEX_HOME / "state_5.sqlite"


def _iter_records(path: Path):
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _safe_stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return None


def _parse_json_string(value):
    if not isinstance(value, str):
        return value if value is not None else {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}


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
            text = " ".join(str(payload["message"]).split())
            return text[:100] + ("…" if len(text) > 100 else "")
    return ""


def _short_title(text: str, n: int = 100) -> str:
    text = " ".join((text or "").split())
    return text[:n] + ("…" if len(text) > n else "")


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


def _guardian_turn_metadata(events: list[dict]) -> dict:
    """Collapse generic Codex bookkeeping for one guardian review turn."""
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


def _fold_guardian_metadata(events: list[dict]) -> list[dict]:
    """Attach status/context/token records to their guardian request."""
    metadata_kinds = {"status", "context", "tokens", "raw"}
    out = []
    pending: list[dict] = []
    current_request = None

    def finish() -> None:
        nonlocal current_request, pending
        if current_request is not None:
            metadata = _guardian_turn_metadata(pending)
            if metadata:
                current_request["metadata"] = metadata
        current_request = None
        pending = []

    for ev in events:
        if ev.get("kind") in metadata_kinds:
            if ev.get("kind") == "status" and ev.get("status") == "started" and current_request:
                finish()
            pending.append(ev)
            if ev.get("kind") == "status" and ev.get("status") in {"complete", "aborted"}:
                finish()
            continue
        if ev.get("kind") == "guardian_request":
            if current_request is not None:
                finish()
            current_request = ev
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


def _iso_from_ms(ms) -> str:
    if ms is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def session_summary(path: Path, thread_row: dict | None = None) -> dict:
    records = list(_iter_records(path))
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

    st = _safe_stat(path)
    row = thread_row or {}
    title = _first_user_message(records) or row.get("title") or row.get("preview") or "(untitled session)"
    cwd = row.get("cwd") or meta.get("cwd") or ""
    updated_ms = row.get("updated_at_ms") or (row.get("updated_at") * 1000 if row.get("updated_at") else None)
    created_ms = row.get("created_at_ms") or (row.get("created_at") * 1000 if row.get("created_at") else None)

    subagent_fields = _subagent_fields(meta)
    if subagent_fields.get("subagent_type") == "guardian":
        title = "Approval reviews"
    elif subagent_fields:
        title = _short_title(f"[{subagent_fields['subagent_type']}] {title}")

    summary = {
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
        "first_ts": first_ts or _iso_from_ms(created_ms),
        "last_ts": last_ts or _iso_from_ms(updated_ms),
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
    projects: dict[str, dict] = {}
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

    for path in sorted(paths):
        try:
            resolved = path.resolve()
            row = rows_by_path.get(str(resolved)) or rows_by_path.get(str(path))
            summary = session_summary(resolved, row)
        except (OSError, ValueError):
            continue
        key = summary.get("cwd") or "(unknown project)"
        group = projects.setdefault(
            key,
            {"dir": key, "path": key, "sessions": [], "last_mtime": 0},
        )
        group["sessions"].append(summary)
        group["last_mtime"] = max(group["last_mtime"], summary["mtime"])

    out = list(projects.values())
    for group in out:
        group["sessions"].sort(key=lambda s: s["mtime"], reverse=True)
    out.sort(key=lambda p: p["last_mtime"], reverse=True)
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
    st = _safe_stat(resolved)
    return {
        "kind": "local",
        "src": "/api/local-image?path=" + quote(str(resolved), safe=""),
        "path": str(resolved),
        "bytes": st.st_size if st else 0,
        "content_type": content_type,
    }


def _normalize_tool_output(output, name: str = "", args=None) -> dict:
    normalized = {"text": "", "images": [], "raw": None}
    args = args if isinstance(args, dict) else {}
    local_image = None
    if name == "view_image":
        local_image = _local_image_payload(args.get("path"))
        if local_image:
            normalized["images"].append(local_image)

    if output is None:
        return normalized
    if isinstance(output, str):
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
    records = list(_iter_records(path))
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
        elif rec.get("type") == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
            call_id = payload.get("call_id")
            if call_id:
                raw_args = payload.get("arguments") if payload.get("type") == "function_call" else payload.get("input")
                tool_calls[call_id] = {
                    "name": payload.get("name") or "tool",
                    "input": _parse_json_string(raw_args),
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
                    "is_error": bool(payload.get("is_error")),
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
                    events.append(
                        _event_payload(
                            "user",
                            ts,
                            {
                                "text": text,
                                "images": _safe_images(payload.get("images") or []),
                                "local_images": payload.get("local_images") or [],
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
            args = _parse_json_string(payload.get("arguments") if pt == "function_call" else payload.get("input"))
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
        events = _fold_guardian_metadata(events)

    title = _first_user_message(records) or title
    if is_guardian:
        title = "Approval reviews"
    elif subagent_fields:
        title = _short_title(f"[{subagent_fields['subagent_type']}] {title}")
    meta.pop("base_instructions", None)  # surfaced as an instructions event instead

    data = {
        "id": meta.get("id") or _thread_id_from_path(path),
        "title": title or "(untitled session)",
        "meta": meta,
        "events": events,
        "n_records": len(records),
    }
    data.update(subagent_fields)
    return data
