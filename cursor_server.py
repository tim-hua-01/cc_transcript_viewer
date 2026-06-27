#!/usr/bin/env python3
"""Cursor agent transcript parsing library.

Reads Cursor's real conversation store — the SQLite DB at
``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`` — rather
than the lossy ``~/.cursor/projects/.../agent-transcripts/*.jsonl`` export. The
JSONL export drops tool *outputs*, the model, thinking text, timestamps, and
token counts; all of those live in the DB, keyed by the same conversation UUID.

The DB stores each conversation ("composer") as:
  - ``composerData:<id>``        — metadata: name, model, cwd, ordered bubble list
  - ``bubbleId:<id>:<bubbleId>`` — one message ("bubble"): user text, assistant
                                   text, a thinking block, or a tool call+result
  - ``composer.content.<hash>``  — raw file snapshots referenced by edit results
                                   (used to reconstruct before/after diffs)

This module is imported by server.py (the unified transcript browser). It emits
events in the Claude Code shape (kind user/assistant with `blocks`) so the
existing frontend renders them without special-casing, and normalizes Cursor's
tool names/inputs onto the canonical ones the frontend already formats nicely.

Sessions are addressed by the synthetic id ``cursordb:<composerId>`` (there is no
backing file). Call configure(db_path) to point at a different state.vscdb.
"""

from __future__ import annotations

import difflib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# macOS default. (Linux: ~/.config/Cursor/...; Windows: %APPDATA%/Cursor/...)
DEFAULT_DB_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Cursor"
    / "User"
    / "globalStorage"
    / "state.vscdb"
)

DB_PATH = DEFAULT_DB_PATH

# Session ids are "cursordb:<composerId>" — there's no file on disk. server.py
# routes any session whose id starts with this scheme to parse_session_by_id().
SESSION_SCHEME = "cursordb:"

# Cursor's internal tool names -> the canonical names the frontend already
# formats (so Cursor tools render with the same nice views as Claude/Codex).
_TOOL_NAME_MAP = {
    "run_terminal_command_v2": "Bash",
    "read_file_v2": "Read",
    "edit_file_v2": "Edit",
    "glob_file_search": "Glob",
    "ripgrep_raw_search": "Grep",
    "read_lints": "ReadLints",
    "todo_write": "TodoWrite",
    "delete_file": "Delete",
    "await": "AwaitShell",
    "semantic_search_full": "SemanticSearch",
    "web_search": "WebSearch",
}


def configure(db_path: Path | None) -> None:
    """Point the module at a Cursor state.vscdb (or its parent Cursor dir)."""
    global DB_PATH
    if db_path is None:
        DB_PATH = DEFAULT_DB_PATH
        return
    p = Path(db_path).expanduser()
    # Accept either the .vscdb directly or a Cursor app-support dir.
    if p.is_dir():
        cand = p / "User" / "globalStorage" / "state.vscdb"
        DB_PATH = cand if cand.exists() else p / "state.vscdb"
    else:
        DB_PATH = p


def _connect() -> sqlite3.Connection | None:
    """Read-only connection to the live DB (safe while Cursor is running)."""
    if not DB_PATH.exists():
        return None
    try:
        return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None


def _safe_stat():
    try:
        return DB_PATH.stat()
    except OSError:
        return None


def _loads(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _iso_from_ms(ms) -> str | None:
    if ms in (None, 0):
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _composer_cwd(d: dict) -> str:
    wsi = (d.get("workspaceIdentifier") or {}).get("uri") or {}
    if wsi.get("fsPath"):
        return wsi["fsPath"]
    if wsi.get("path"):
        return wsi["path"]
    repos = d.get("trackedGitRepos") or []
    if repos and isinstance(repos[0], dict) and repos[0].get("repoPath"):
        return repos[0]["repoPath"]
    return ""


def _short_title(text: str, n: int = 100) -> str:
    text = " ".join((text or "").split())
    return text[:n] + ("…" if len(text) > n else "")


# ---------------------------------------------------------------------------
# Session list
# ---------------------------------------------------------------------------
# Cache the whole grouped list, keyed by the DB file mtime so the 1s /api/sessions
# poll doesn't re-read 100+ composers every tick.
_LIST_CACHE: tuple[float, list] | None = None


def _bubble_rows(conn: sqlite3.Connection, cid: str):
    """All bubbles for a composer, as {bubbleId: parsed}."""
    out = {}
    for key, value in conn.execute(
        "select key, value from cursorDiskKV where key like ?", (f"bubbleId:{cid}:%",)
    ):
        b = _loads(value)
        if b is not None:
            out[key.rsplit(":", 1)[-1]] = b
    return out


def _first_user_text(conn: sqlite3.Connection, cid: str, headers: list) -> str:
    """First user bubble's text (for conversations with no AI-generated name)."""
    for h in headers:
        if h.get("type") != 1:
            continue
        row = conn.execute(
            "select value from cursorDiskKV where key=?",
            (f"bubbleId:{cid}:{h.get('bubbleId')}",),
        ).fetchone()
        b = _loads(row[0]) if row else None
        if b and (b.get("text") or "").strip():
            return b["text"]
    return ""


def _summary_from_composer(conn, cid: str, d: dict, db_mtime: float) -> dict:
    headers = d.get("fullConversationHeadersOnly") or []
    n_user = sum(1 for h in headers if h.get("type") == 1)
    n_tool = sum(1 for h in headers if (h.get("grouping") or {}).get("toolFormerTool") is not None)
    n_assistant = sum(
        1
        for h in headers
        if h.get("type") == 2 and (h.get("grouping") or {}).get("toolFormerTool") is None
    )
    model = (d.get("modelConfig") or {}).get("modelName") or ""
    title = d.get("name") or _short_title(_first_user_text(conn, cid, headers)) or "(untitled session)"
    created = d.get("createdAt")
    updated = d.get("lastUpdatedAt") or created
    mtime = (updated / 1000) if updated else db_mtime
    return {
        "agent": "cursor",
        "id": cid,
        "file": SESSION_SCHEME + cid,
        "title": title,
        "cwd": _composer_cwd(d),
        "git_branch": "",
        "version": "",
        "first_ts": _iso_from_ms(created),
        "last_ts": _iso_from_ms(updated),
        "n_user": n_user,
        "n_assistant": n_assistant,
        "n_tool": n_tool,
        "n_web": 0,
        "n_records": len(headers),
        "model": model,
        "models": [model] if model else [],
        "mtime": mtime,
    }


def list_sessions() -> list[dict]:
    """Conversations grouped by cwd (mirrors codex_server.list_sessions)."""
    global _LIST_CACHE
    st = _safe_stat()
    if st is None:
        return []
    if _LIST_CACHE and _LIST_CACHE[0] == st.st_mtime:
        return _LIST_CACHE[1]

    conn = _connect()
    if conn is None:
        return []
    projects: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "select key, value from cursorDiskKV where key like 'composerData:%'"
        ).fetchall()
        for key, value in rows:
            d = _loads(value)
            if not isinstance(d, dict):
                continue
            cid = key[len("composerData:"):]
            # Skip empty draft composers (no messages yet) — they're just noise.
            if not (d.get("fullConversationHeadersOnly") or []):
                continue
            try:
                summary = _summary_from_composer(conn, cid, d, st.st_mtime)
            except (sqlite3.Error, ValueError, TypeError):
                continue
            key_cwd = summary.get("cwd") or "(unknown project)"
            group = projects.setdefault(
                key_cwd, {"dir": key_cwd, "path": key_cwd, "sessions": [], "last_mtime": 0}
            )
            group["sessions"].append(summary)
            group["last_mtime"] = max(group["last_mtime"], summary["mtime"])
    finally:
        conn.close()

    out = list(projects.values())
    for group in out:
        group["sessions"].sort(key=lambda s: s["mtime"], reverse=True)
    out.sort(key=lambda p: p["last_mtime"], reverse=True)
    _LIST_CACHE = (st.st_mtime, out)
    return out


# ---------------------------------------------------------------------------
# Full conversation parse
# ---------------------------------------------------------------------------
def _resolve_content(conn, content_id) -> str | None:
    """Fetch a composer.content.<hash> snapshot (raw file text)."""
    if not content_id:
        return None
    key = content_id if str(content_id).startswith("composer.content.") else f"composer.content.{content_id}"
    row = conn.execute("select value from cursorDiskKV where key=?", (key,)).fetchone()
    if not row:
        return None
    # Stored as raw text; older entries may be JSON-wrapped.
    val = row[0]
    parsed = _loads(val)
    if isinstance(parsed, dict):
        return parsed.get("content") or parsed.get("text") or val
    return val


def _tool_args(tf: dict) -> dict:
    # `params` is usually a dict but sometimes a JSON-encoded string; `rawArgs`
    # is the fallback (also JSON text). Coerce whichever we get into a dict.
    for candidate in (tf.get("params"), tf.get("rawArgs")):
        if isinstance(candidate, dict) and candidate:
            return candidate
        if isinstance(candidate, str) and candidate.strip():
            parsed = _loads(candidate)
            if isinstance(parsed, dict) and parsed:
                return parsed
    return {}


def _normalize_tool(conn, tf: dict) -> dict:
    """Build a Claude-shape tool_use block (name, input, result) from Cursor's
    toolFormerData, mapping names/inputs onto the canonical frontend renderers."""
    raw_name = tf.get("name") or str(tf.get("tool") or "tool")
    name = _TOOL_NAME_MAP.get(raw_name, raw_name)
    args = _tool_args(tf)
    result = tf.get("result")
    if isinstance(result, str):
        result = _loads(result) if result.strip().startswith(("{", "[")) else result
    status = (tf.get("status") or "").lower()

    inp: dict = {}
    text = ""
    is_error = status == "error"

    if raw_name == "run_terminal_command_v2":
        inp = {"command": args.get("command") or "", "description": args.get("description") or ""}
        if isinstance(result, dict):
            text = result.get("output") or ""
            ec = result.get("exitCode")
            if ec not in (None, 0):
                text = (text + f"\n[exit code {ec}]").strip()
                is_error = True
            if result.get("error"):
                is_error = True
    elif raw_name == "read_file_v2":
        inp = {"file_path": args.get("targetFile") or args.get("effectiveUri") or ""}
        if isinstance(result, dict):
            text = result.get("contents") or ""
    elif raw_name == "edit_file_v2":
        path = args.get("relativeWorkspacePath") or args.get("targetFile") or ""
        before = after = None
        if isinstance(result, dict):
            before = _resolve_content(conn, result.get("beforeContentId"))
            after = _resolve_content(conn, result.get("afterContentId"))
        inp = {"file_path": path}
        if before is None and after is None:
            text = "(diff snapshot not available)"
        else:
            # Cursor stores full before/after file snapshots, not a diff. Compute a
            # real unified diff so the viewer shows changed hunks, not the whole file.
            inp["patch"] = _unified_diff(before or "", after or "", path)
    elif raw_name == "glob_file_search":
        inp = {"pattern": args.get("globPattern") or "", "path": args.get("targetDirectory") or ""}
        text = _format_glob(result)
    elif raw_name == "ripgrep_raw_search":
        inp = {"pattern": args.get("pattern") or args.get("query") or "", "path": args.get("path") or ""}
        text, is_error = _format_generic(result, is_error)
    elif raw_name == "read_lints":
        inp = {"paths": args.get("paths") or ([args["path"]] if args.get("path") else [])}
        text, is_error = _format_generic(result, is_error)
    elif raw_name == "todo_write":
        inp = {"todos": args.get("todos") or []}
    elif raw_name == "delete_file":
        inp = {"path": args.get("relativeWorkspacePath") or args.get("path") or ""}
    elif raw_name == "semantic_search_full":
        inp = {
            "query": args.get("query") or "",
            "target_directories": args.get("targetDirectories") or args.get("target_directories") or [],
        }
        text, is_error = _format_generic(result, is_error)
    elif raw_name == "web_search":
        inp = {"search_term": args.get("searchTerm") or args.get("query") or ""}
        text, is_error = _format_generic(result, is_error)
    elif raw_name == "await":
        inp = args or {}
        text, is_error = _format_generic(result, is_error)
    else:
        inp = args or {}
        text, is_error = _format_generic(result, is_error)

    return {
        "type": "tool_use",
        "id": tf.get("toolCallId"),
        "name": name,
        "input": inp,
        "result": {"is_error": is_error, "text": text, "images": []},
    }


def _unified_diff(before: str, after: str, path: str) -> str:
    """A unified diff of two full-file snapshots (changed hunks only)."""
    if before == after:
        return "(no textual change)"
    label = path or "file"
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=label,
        tofile=label,
        lineterm="",
        n=3,
    )
    return "\n".join(diff)


def _format_glob(result) -> str:
    if not isinstance(result, dict):
        return _format_generic(result, False)[0]
    files = []
    for d in result.get("directories") or []:
        if not isinstance(d, dict):
            continue
        base = d.get("absPath") or ""
        for f in d.get("files") or []:
            rel = f.get("relPath") if isinstance(f, dict) else str(f)
            files.append(f"{base}/{rel}" if base and rel else (rel or base))
    return "\n".join(files) if files else "(no matches)"


def _format_generic(result, is_error: bool) -> tuple[str, bool]:
    if result is None:
        return "", is_error
    if isinstance(result, str):
        return result, is_error
    if isinstance(result, dict):
        if result.get("error"):
            e = result["error"]
            return (e if isinstance(e, str) else json.dumps(e, indent=2)), True
        if "output" in result and len(result) <= 4:
            return str(result.get("output") or ""), is_error
        if not result:
            return "(no output)", is_error
        return json.dumps(result, indent=2), is_error
    return json.dumps(result, indent=2), is_error


def parse_session_by_id(composer_id: str) -> dict | None:
    """Full structured parse of one Cursor conversation, in the Claude Code shape."""
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "select value from cursorDiskKV where key=?", (f"composerData:{composer_id}",)
        ).fetchone()
        if not row:
            return None
        d = _loads(row[0]) or {}
        headers = d.get("fullConversationHeadersOnly") or []
        bubbles = _bubble_rows(conn, composer_id)
        model = (d.get("modelConfig") or {}).get("modelName") or ""

        events: list[dict] = []

        # Each Cursor bubble (a thinking block, a text reply, or a tool call) is
        # emitted as its own event — like Claude Code renders each message as a
        # separate turn — rather than collapsing a whole turn into one box.
        for h in headers:
            b = bubbles.get(h.get("bubbleId"))
            if not b:
                continue
            btype = b.get("type")
            ts = h.get("createdAt") or b.get("createdAt")

            if btype == 1:  # user
                text = b.get("text") or ""
                if not text.strip():
                    continue
                events.append(
                    {"kind": "user", "ts": ts, "blocks": [{"type": "text", "text": text}], "is_sidechain": False}
                )
                continue

            # assistant-side bubble: thinking, text, and/or a tool call
            blocks = []
            thinking = (b.get("thinking") or {}).get("text") if isinstance(b.get("thinking"), dict) else None
            if thinking and thinking.strip():
                blocks.append({"type": "thinking", "text": thinking})
            if (b.get("text") or "").strip():
                blocks.append({"type": "text", "text": b["text"]})
            tf = b.get("toolFormerData")
            if isinstance(tf, dict):
                blocks.append(_normalize_tool(conn, tf))
            if not blocks:
                continue
            events.append(
                {"kind": "assistant", "ts": ts, "model": model, "blocks": blocks, "is_sidechain": False}
            )

        cwd = _composer_cwd(d)
        meta = {}
        if cwd:
            meta["cwd"] = cwd
        if model:
            meta["model"] = model
        return {
            "agent": "cursor",
            "id": composer_id,
            "title": d.get("name") or _short_title(_first_user_text(conn, composer_id, headers)) or "(untitled session)",
            "meta": meta,
            "events": events,
            "n_records": len(headers),
        }
    finally:
        conn.close()
