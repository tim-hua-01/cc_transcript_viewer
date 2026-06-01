#!/usr/bin/env python3
"""Codex transcript browser.

A zero-dependency local web app for browsing Codex session transcripts stored
under ~/.codex/sessions. Run it and open the printed URL.

Usage:
    python codex_server.py [--port 3133] [--codex-home PATH]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_CODEX_HOME = Path.home() / ".codex"

# Set by main() so handlers can reach it.
CODEX_HOME = DEFAULT_CODEX_HOME
SESSIONS_DIR = DEFAULT_CODEX_HOME / "sessions"
ARCHIVED_SESSIONS_DIR = DEFAULT_CODEX_HOME / "archived_sessions"
STATE_DB = DEFAULT_CODEX_HOME / "state_5.sqlite"
MAX_INLINE_IMAGE_CHARS = 2_000_000


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


def _first_user_message(records: list[dict]) -> str:
    for rec in records:
        if rec.get("type") != "event_msg":
            continue
        payload = rec.get("payload") or {}
        if payload.get("type") == "user_message" and payload.get("message"):
            return " ".join(str(payload["message"]).split())[:200]
    return ""


def _thread_id_from_path(path: Path) -> str:
    stem = path.stem
    if stem.startswith("rollout-"):
        parts = stem.split("-")
        if len(parts) >= 8:
            return "-".join(parts[-5:])
    return stem


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
    title = row.get("title") or row.get("preview") or _first_user_message(records) or "(untitled session)"
    cwd = row.get("cwd") or meta.get("cwd") or ""
    updated_ms = row.get("updated_at_ms") or (row.get("updated_at") * 1000 if row.get("updated_at") else None)
    created_ms = row.get("created_at_ms") or (row.get("created_at") * 1000 if row.get("created_at") else None)

    return {
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


def _tool_summary(name: str, args) -> str:
    if not isinstance(args, dict):
        return str(args)[:200]
    if name in {"exec_command", "shell"}:
        return str(args.get("cmd") or "")[:200]
    if name == "write_stdin":
        return str(args.get("session_id") or "")[:80]
    if name == "apply_patch":
        return "patch"
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

    for rec in records:
        payload = rec.get("payload") or {}
        if rec.get("type") == "session_meta":
            meta.update(payload)
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
                tool_outputs[call_id] = {
                    "output": normalized["text"],
                    "images": normalized["images"],
                    "raw": normalized["raw"],
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
                events.append(
                    _event_payload(
                        "user",
                        ts,
                        {
                            "text": payload.get("message") or "",
                            "images": _safe_images(payload.get("images") or []),
                            "local_images": payload.get("local_images") or [],
                            "text_elements": payload.get("text_elements") or [],
                        },
                    )
                )
            elif pt == "agent_message":
                events.append(
                    _event_payload(
                        "assistant",
                        ts,
                        {
                            "text": payload.get("message") or "",
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

    if not title:
        title = _first_user_message(records)

    return {
        "id": meta.get("id") or _thread_id_from_path(path),
        "title": title or "(untitled session)",
        "meta": meta,
        "events": events,
        "n_records": len(records),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quieter logs
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/" or route == "/index.html":
            self._send_file(STATIC_DIR / "codex_index.html", "text/html; charset=utf-8")
            return
        if route == "/codex_app.js":
            self._send_file(STATIC_DIR / "codex_app.js", "application/javascript; charset=utf-8")
            return
        if route == "/codex_style.css":
            self._send_file(STATIC_DIR / "codex_style.css", "text/css; charset=utf-8")
            return
        if route == "/style.css":
            self._send_file(STATIC_DIR / "style.css", "text/css; charset=utf-8")
            return

        if route == "/api/local-image":
            qs = parse_qs(parsed.query)
            path_arg = qs.get("path", [""])[0]
            if not path_arg:
                self._send_json({"error": "missing path param"}, status=400)
                return
            target = Path(path_arg).expanduser().resolve()
            content_type = mimetypes.guess_type(str(target))[0] or ""
            if not content_type.startswith("image/"):
                self._send_json({"error": "not an image"}, status=400)
                return
            self._send_file(target, content_type)
            return

        if route == "/api/sessions":
            try:
                self._send_json({"projects": list_sessions()})
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/session":
            qs = parse_qs(parsed.query)
            file_arg = qs.get("file", [""])[0]
            if not file_arg:
                self._send_json({"error": "missing file param"}, status=400)
                return
            target = Path(file_arg).expanduser().resolve()
            sessions_root = SESSIONS_DIR.resolve()
            archived_root = ARCHIVED_SESSIONS_DIR.resolve()
            in_sessions = target == sessions_root or sessions_root in target.parents
            in_archived = (
                ARCHIVED_SESSIONS_DIR.exists()
                and (target == archived_root or archived_root in target.parents)
            )
            # Security: only allow files under Codex transcript roots.
            if not in_sessions and not in_archived:
                self._send_json({"error": "forbidden"}, status=403)
                return
            if not target.exists():
                self._send_json({"error": "not found"}, status=404)
                return
            try:
                self._send_json(parse_session(target))
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
            return

        self.send_error(404)


def main():
    global CODEX_HOME, SESSIONS_DIR, ARCHIVED_SESSIONS_DIR, STATE_DB
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=3133)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--codex-home", type=Path, default=DEFAULT_CODEX_HOME)
    args = ap.parse_args()

    CODEX_HOME = args.codex_home.expanduser()
    SESSIONS_DIR = CODEX_HOME / "sessions"
    ARCHIVED_SESSIONS_DIR = CODEX_HOME / "archived_sessions"
    STATE_DB = CODEX_HOME / "state_5.sqlite"

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print("Codex transcript browser")
    print(f"  codex home:   {CODEX_HOME}")
    print(f"  sessions dir: {SESSIONS_DIR}")
    print(f"  archived dir: {ARCHIVED_SESSIONS_DIR}")
    print(f"  serving at:   {url}")
    print("  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
