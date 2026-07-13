#!/usr/bin/env python3
"""Cursor agent transcript parsing library.

Three sources:

1. **Cursor IDE** — the SQLite store at
   ``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb``.
   Full fidelity: tool outputs, model, thinking, timestamps, token counts.
   Sessions are addressed by the synthetic id ``cursordb:<composerId>``.

2. **Cursor CLI (canonical)** — per-chat SQLite at
   ``~/.cursor/chats/<workspace-md5>/<session-uuid>/store.db``.
   Full fidelity for CLI sessions (tool results, model, reasoning). Sessions are
   addressed by ``cursorcli:<sessionId>``. Sibling ``meta.json`` carries cwd/title.

3. **Cursor CLI (lossy export)** — JSONL under
   ``~/.cursor/projects/<project>/agent-transcripts/<uuid>/<uuid>.jsonl``.
   Used only when no ``store.db`` exists for that UUID. No tool outputs.

When the same UUID exists in more than one place, preference is
IDE ``state.vscdb`` > CLI ``store.db`` > JSONL.

The IDE DB stores each conversation ("composer") as:
  - ``composerData:<id>``        — metadata: name, model, cwd, ordered bubble list
  - ``bubbleId:<id>:<bubbleId>`` — one message ("bubble"): user text, assistant
                                   text, a thinking block, or a tool call+result
  - ``composer.content.<hash>``  — raw file snapshots referenced by edit results
                                   (used to reconstruct before/after diffs)

This module is imported by server.py (the unified transcript browser). It emits
events in the Claude Code shape (kind user/assistant with `blocks`) so the
existing frontend renders them without special-casing, and normalizes Cursor's
tool names/inputs onto the canonical ones the frontend already formats nicely.

Call ``configure(db_path, projects_dir=…, chats_dir=…)`` to retarget stores.
"""

from __future__ import annotations

import difflib
import json
import re
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
DEFAULT_PROJECTS_DIR = Path.home() / ".cursor" / "projects"
DEFAULT_CHATS_DIR = Path.home() / ".cursor" / "chats"

DB_PATH = DEFAULT_DB_PATH
PROJECTS_DIR = DEFAULT_PROJECTS_DIR
CHATS_DIR = DEFAULT_CHATS_DIR

# Session ids are "cursordb:<composerId>" — there's no file on disk. server.py
# routes any session whose id starts with this scheme to parse_session_by_id().
SESSION_SCHEME = "cursordb:"
# CLI store.db sessions — also synthetic (path looked up under CHATS_DIR).
CLI_SESSION_SCHEME = "cursorcli:"

# Cursor IDE internal tool names -> the canonical names the frontend already
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

# Cursor CLI agent tool names (store.db / agent-transcripts JSONL).
_CLI_TOOL_NAME_MAP = {
    "Shell": "Shell",  # frontend already formats lowercase "shell"
    "Read": "Read",
    "ReadFile": "Read",
    "StrReplace": "Edit",
    "Write": "Write",
    "Delete": "Delete",
    "Glob": "Glob",
    "Grep": "Grep",
    "rg": "Grep",
    "TodoWrite": "TodoWrite",
    "ReadLints": "ReadLints",
    "SemanticSearch": "SemanticSearch",
    "WebSearch": "WebSearch",
    "AwaitShell": "AwaitShell",
    "Await": "AwaitShell",
    "Task": "Task",
    "ApplyPatch": "Edit",
}

_CLI_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL | re.IGNORECASE)
_CLI_TIMESTAMP_RE = re.compile(r"<timestamp>.*?</timestamp>\s*", re.DOTALL | re.IGNORECASE)
_CLI_SYSTEM_REMINDER_RE = re.compile(
    r"<system_reminder>\s*(.*?)\s*</system_reminder>", re.DOTALL | re.IGNORECASE
)

# Per-file summary cache for CLI JSONL / store.db (mtime, size) -> summary dict.
_CLI_SUMMARY_CACHE: dict[str, tuple[float, int, dict]] = {}
# session id -> store.db path, refreshed whenever we scan chats.
_CLI_STORE_INDEX: dict[str, Path] = {}


def configure(
    db_path: Path | None = None,
    projects_dir: Path | None = None,
    chats_dir: Path | None = None,
) -> None:
    """Point the module at Cursor IDE DB / projects / chats directories."""
    global DB_PATH, PROJECTS_DIR, CHATS_DIR, _LIST_CACHE, _CLI_SUMMARY_CACHE, _CLI_STORE_INDEX
    if db_path is None:
        DB_PATH = DEFAULT_DB_PATH
    else:
        p = Path(db_path).expanduser()
        # Accept either the .vscdb directly or a Cursor app-support dir.
        if p.is_dir():
            cand = p / "User" / "globalStorage" / "state.vscdb"
            DB_PATH = cand if cand.exists() else p / "state.vscdb"
        else:
            DB_PATH = p
    if projects_dir is None:
        PROJECTS_DIR = DEFAULT_PROJECTS_DIR
    else:
        PROJECTS_DIR = Path(projects_dir).expanduser()
    if chats_dir is None:
        CHATS_DIR = DEFAULT_CHATS_DIR
    else:
        CHATS_DIR = Path(chats_dir).expanduser()
    _LIST_CACHE = None
    _CLI_SUMMARY_CACHE = {}
    _CLI_STORE_INDEX = {}


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


def _list_db_sessions() -> list[dict]:
    """IDE conversations from state.vscdb, grouped by cwd."""
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


def list_sessions() -> list[dict]:
    """Conversations grouped by cwd (IDE DB + CLI store.db + JSONL orphans)."""
    projects: dict[str, dict] = {}
    for group in _list_db_sessions():
        key_cwd = group.get("path") or group.get("dir") or "(unknown project)"
        projects[key_cwd] = {
            "dir": key_cwd,
            "path": key_cwd,
            "sessions": list(group.get("sessions") or []),
            "last_mtime": group.get("last_mtime") or 0,
        }

    known_ids = {
        s["id"]
        for group in projects.values()
        for s in group["sessions"]
        if s.get("id")
    }

    # Prefer rich per-chat store.db over the lossy JSONL export.
    for summary in _iter_cli_store_summaries(skip_ids=known_ids):
        known_ids.add(summary["id"])
        key_cwd = summary.get("cwd") or "(unknown project)"
        group = projects.setdefault(
            key_cwd, {"dir": key_cwd, "path": key_cwd, "sessions": [], "last_mtime": 0}
        )
        group["sessions"].append(summary)
        group["last_mtime"] = max(group["last_mtime"], summary.get("mtime") or 0)

    for summary in _iter_cli_summaries(skip_ids=known_ids):
        key_cwd = summary.get("cwd") or "(unknown project)"
        group = projects.setdefault(
            key_cwd, {"dir": key_cwd, "path": key_cwd, "sessions": [], "last_mtime": 0}
        )
        group["sessions"].append(summary)
        group["last_mtime"] = max(group["last_mtime"], summary.get("mtime") or 0)

    out = list(projects.values())
    for group in out:
        group["sessions"].sort(key=lambda s: s.get("mtime") or 0, reverse=True)
    out.sort(key=lambda p: p.get("last_mtime") or 0, reverse=True)
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


# ---------------------------------------------------------------------------
# Cursor CLI / agent-transcripts JSONL
# ---------------------------------------------------------------------------
def is_cli_transcript(path: Path) -> bool:
    """True when ``path`` sits under ``PROJECTS_DIR/.../agent-transcripts/``."""
    try:
        resolved = path.resolve()
        root = PROJECTS_DIR.resolve()
    except OSError:
        return False
    if resolved == root or root not in resolved.parents:
        return False
    return "agent-transcripts" in resolved.parts


def decode_project_dir(dirname: str) -> str:
    """Best-effort cwd from a ``~/.cursor/projects/<dirname>`` folder name.

    Cursor encodes paths by replacing both ``/`` and ``_`` with ``-``, so a
    naive dash→slash rewrite is wrong (``aisafety_githubs`` becomes
    ``aisafety/githubs``). Prefer resolving against the live filesystem; fall
    back to a readable slash path when that fails.
    """
    if not dirname or dirname.isdigit():
        return ""
    if dirname.startswith("var-folders-") or dirname.startswith("tmp"):
        return ""
    parts = [p for p in dirname.split("-") if p]
    if not parts:
        return ""
    resolved = _resolve_dashed_path(parts)
    if resolved:
        return resolved
    path = "/".join(parts)
    if parts[0] in {"Users", "home"}:
        path = "/" + path
    return path


def _resolve_dashed_path(parts: list[str]) -> str:
    """Greedily match dashed path segments against real directories on disk."""
    if not parts or parts[0] not in {"Users", "home"}:
        return ""
    current = Path("/") / parts[0]
    if not current.is_dir():
        return ""
    i = 1
    while i < len(parts):
        try:
            children = {c.name: c for c in current.iterdir() if c.is_dir()}
        except OSError:
            break
        matched = None
        # Prefer longer matches so ``cc_transcript_viewer`` wins over ``cc``.
        for j in range(len(parts), i, -1):
            for candidate in (
                "_".join(parts[i:j]),
                "-".join(parts[i:j]),
                "/".join(parts[i:j]),  # unlikely as a single dir name
            ):
                if candidate in children:
                    matched = children[candidate]
                    i = j
                    break
            if matched is not None:
                break
        if matched is None:
            break
        current = matched
    if i != len(parts):
        return ""
    return str(current)


def cwd_for_project_dir(project_dir: Path) -> str:
    """Resolve a project folder to a cwd via worker.log, then filesystem decode."""
    log = project_dir / "worker.log"
    if log.is_file():
        try:
            # Prefer the most recent workspacePath= mention.
            text = log.read_text(encoding="utf-8", errors="ignore")
            matches = re.findall(r"workspacePath=(\S+)", text)
            if matches:
                return matches[-1]
        except OSError:
            pass
    return decode_project_dir(project_dir.name)


def _clean_cli_user_text(text: str) -> str:
    """Strip Cursor CLI wrapper tags; prefer the inner ``<user_query>`` body."""
    if not text:
        return ""
    text = _CLI_TIMESTAMP_RE.sub("", text)
    m = _CLI_USER_QUERY_RE.search(text)
    if m:
        return m.group(1).strip()
    # Drop other simple XML-ish wrappers the CLI sometimes wraps prompts in.
    text = re.sub(r"</?[a-zA-Z][a-zA-Z0-9_-]*>", " ", text)
    return " ".join(text.split())


def _iter_cli_records(path: Path):
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _normalize_cli_tool(name: str, args: dict) -> dict:
    """Map a CLI tool_use onto the canonical frontend tool shape."""
    raw = name or "tool"
    canon = _CLI_TOOL_NAME_MAP.get(raw, raw)
    args = args if isinstance(args, dict) else {}
    inp: dict = {}

    if raw in {"Shell"}:
        inp = {
            "command": args.get("command") or "",
            "description": args.get("description") or "",
        }
        if args.get("working_directory"):
            inp["workdir"] = args["working_directory"]
    elif raw in {"Read", "ReadFile"}:
        inp = {"file_path": args.get("path") or args.get("file_path") or ""}
        if args.get("offset") is not None:
            inp["offset"] = args["offset"]
        if args.get("limit") is not None:
            inp["limit"] = args["limit"]
    elif raw in {"StrReplace", "ApplyPatch"}:
        inp = {
            "file_path": args.get("path") or args.get("file_path") or "",
            "old_string": args.get("old_string") or "",
            "new_string": args.get("new_string") or "",
        }
        if args.get("replace_all"):
            inp["replace_all"] = args["replace_all"]
        if args.get("patch"):
            inp["patch"] = args["patch"]
    elif raw == "Write":
        inp = {
            "file_path": args.get("path") or args.get("file_path") or "",
            "content": args.get("contents") if "contents" in args else args.get("content") or "",
        }
    elif raw == "Delete":
        inp = {"path": args.get("path") or ""}
    elif raw == "Glob":
        inp = {
            "pattern": args.get("glob_pattern") or args.get("pattern") or "",
            "path": args.get("target_directory") or args.get("path") or "",
        }
    elif raw in {"Grep", "rg"}:
        inp = {
            "pattern": args.get("pattern") or "",
            "path": args.get("path") or "",
            "output_mode": args.get("output_mode") or "",
        }
        if args.get("-i"):
            inp["-i"] = args["-i"]
    elif raw in {"AwaitShell", "Await"}:
        inp = {
            "shell_id": args.get("shell_id") or args.get("task_id"),
            "pattern": args.get("pattern") or "",
            "block_until_ms": args.get("block_until_ms"),
        }
    elif raw == "TodoWrite":
        inp = {"todos": args.get("todos") or []}
    elif raw == "ReadLints":
        inp = {"paths": args.get("paths") or ([args["path"]] if args.get("path") else [])}
    elif raw == "SemanticSearch":
        inp = {
            "query": args.get("query") or "",
            "target_directories": args.get("target_directories") or [],
            "num_results": args.get("num_results"),
        }
    elif raw == "WebSearch":
        inp = {
            "search_term": args.get("search_term") or args.get("query") or "",
            "explanation": args.get("explanation") or "",
        }
    elif raw == "Task":
        inp = {
            "description": args.get("description") or "",
            "prompt": args.get("prompt") or "",
            "subagent_type": args.get("subagent_type") or "",
        }
    else:
        inp = args

    return {
        "type": "tool_use",
        "id": None,
        "name": canon,
        "input": inp,
        # CLI JSONL never records tool outputs.
        "result": {"is_error": False, "text": "", "images": [], "missing": True},
    }


def _cli_paths() -> list[Path]:
    """All agent-transcript JSONL files under PROJECTS_DIR."""
    if not PROJECTS_DIR.exists():
        return []
    out: list[Path] = []
    try:
        for proj in PROJECTS_DIR.iterdir():
            if not proj.is_dir():
                continue
            root = proj / "agent-transcripts"
            if not root.is_dir():
                continue
            # Main sessions: <uuid>/<uuid>.jsonl
            out.extend(root.glob("*/*.jsonl"))
            # Sub-agents: <uuid>/subagents/<id>.jsonl
            out.extend(root.glob("*/subagents/*.jsonl"))
    except OSError:
        return []
    return out


def _cli_session_summary_uncached(path: Path) -> dict:
    records = list(_iter_cli_records(path))
    is_subagent = path.parent.name == "subagents"
    n_user = n_assistant = n_tool = 0
    first_user = ""

    for rec in records:
        role = rec.get("role")
        if role == "user":
            content = (rec.get("message") or {}).get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            cleaned = _clean_cli_user_text(text)
            if cleaned:
                n_user += 1
                if not first_user:
                    first_user = cleaned
        elif role == "assistant":
            n_assistant += 1
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        n_tool += 1

    if is_subagent:
        # …/agent-transcripts/<parent-id>/subagents/<id>.jsonl
        project_dir = path.parent.parent.parent.parent
        parent_id = path.parent.parent.name
        parent_file = path.parent.parent / f"{parent_id}.jsonl"
    else:
        # …/agent-transcripts/<id>/<id>.jsonl
        project_dir = path.parent.parent.parent
        parent_id = ""
        parent_file = None

    cwd = cwd_for_project_dir(project_dir) if project_dir else ""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0

    summary = {
        "agent": "cursor",
        "cursor_source": "cli-jsonl",
        "id": path.stem,
        "file": str(path.resolve()),
        "title": _short_title(first_user) or "(untitled session)",
        "cwd": cwd,
        "git_branch": "",
        "version": "",
        "first_ts": None,
        "last_ts": None,
        "n_user": n_user,
        "n_assistant": n_assistant,
        "n_tool": n_tool,
        "n_web": 0,
        "n_records": len(records),
        "model": "",
        "models": [],
        "mtime": mtime,
    }
    if is_subagent:
        summary["is_subagent"] = True
        summary["subagent_type"] = "cursor-cli"
        if parent_file is not None:
            try:
                summary["parent_file"] = str(parent_file.resolve())
            except OSError:
                summary["parent_file"] = str(parent_file)
            summary["parent_id"] = parent_id
    return summary


def cli_session_summary(path: Path) -> dict:
    """Lightweight metadata for one CLI JSONL transcript, cached by mtime/size."""
    global _CLI_SUMMARY_CACHE
    key = str(path)
    try:
        st = path.stat()
        identity = (st.st_mtime, st.st_size)
    except OSError:
        identity = None
    if identity is not None:
        cached = _CLI_SUMMARY_CACHE.get(key)
        if cached and cached[:2] == identity:
            return cached[2]
    summary = _cli_session_summary_uncached(path)
    if identity is not None:
        _CLI_SUMMARY_CACHE[key] = (identity[0], identity[1], summary)
    return summary


def _iter_cli_summaries(skip_ids: set[str] | None = None):
    skip = skip_ids or set()
    for path in _cli_paths():
        if path.stem in skip:
            continue
        try:
            yield cli_session_summary(path)
        except (OSError, ValueError):
            continue


def parse_cli_session(path: Path) -> dict | None:
    """Full structured parse of a Cursor CLI agent-transcripts JSONL file."""
    if not path.exists():
        return None
    records = list(_iter_cli_records(path))
    is_subagent = path.parent.name == "subagents"
    events: list[dict] = []

    for rec in records:
        role = rec.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = (rec.get("message") or {}).get("content")
        blocks: list[dict] = []
        if isinstance(content, str):
            text = _clean_cli_user_text(content) if role == "user" else content
            if text.strip():
                blocks.append({"type": "text", "text": text})
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    text = b.get("text") or ""
                    if role == "user":
                        text = _clean_cli_user_text(text)
                    if text.strip():
                        blocks.append({"type": "text", "text": text})
                elif btype == "tool_use":
                    blocks.append(_normalize_cli_tool(b.get("name") or "tool", b.get("input") or {}))
                elif btype == "thinking" and (b.get("thinking") or "").strip():
                    blocks.append({"type": "thinking", "text": b["thinking"]})
        if not blocks:
            continue
        events.append(
            {
                "kind": role,
                "ts": None,
                "model": "",
                "blocks": blocks,
                "is_sidechain": False,
            }
        )

    if is_subagent:
        project_dir = path.parent.parent.parent.parent
        parent_id = path.parent.parent.name
        parent_file = path.parent.parent / f"{parent_id}.jsonl"
    else:
        project_dir = path.parent.parent.parent
        parent_id = ""
        parent_file = None

    cwd = cwd_for_project_dir(project_dir) if project_dir else ""
    first_user = ""
    for ev in events:
        if ev["kind"] == "user":
            for b in ev["blocks"]:
                if b.get("type") == "text" and b.get("text"):
                    first_user = b["text"]
                    break
            if first_user:
                break

    out = {
        "agent": "cursor",
        "cursor_source": "cli-jsonl",
        "id": path.stem,
        "title": _short_title(first_user) or "(untitled session)",
        "meta": {"cwd": cwd} if cwd else {},
        "events": events,
        "n_records": len(records),
    }
    if is_subagent:
        out["is_subagent"] = True
        out["subagent_type"] = "cursor-cli"
        if parent_file is not None:
            try:
                out["parent_file"] = str(parent_file.resolve())
            except OSError:
                out["parent_file"] = str(parent_file)
            out["parent_id"] = parent_id
    return out


# ---------------------------------------------------------------------------
# Cursor CLI store.db (canonical rich store under ~/.cursor/chats)
# ---------------------------------------------------------------------------
def _read_store_meta(conn: sqlite3.Connection) -> dict:
    """Decode meta.key='0' (hex-encoded JSON, or plain JSON)."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='0'").fetchone()
    except sqlite3.Error:
        return {}
    if not row or not row[0]:
        return {}
    raw = row[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    raw = str(raw).strip()
    try:
        if re.fullmatch(r"[0-9a-fA-F]+", raw):
            return json.loads(bytes.fromhex(raw).decode("utf-8"))
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _read_sidecar_meta(store_path: Path) -> dict:
    """Optional meta.json next to store.db (cwd, title, timestamps)."""
    side = store_path.parent / "meta.json"
    if not side.is_file():
        return {}
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _extract_json_objects(data: bytes) -> list:
    """Pull balanced JSON objects out of a blob (plain JSON or binary wrapper)."""
    out = []
    i = 0
    n = len(data)
    while i < n:
        if data[i] != 0x7B:  # '{'
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            c = data[j]
            if in_str:
                if esc:
                    esc = False
                elif c == 0x5C:  # '\\'
                    esc = True
                elif c == 0x22:  # '"'
                    in_str = False
            else:
                if c == 0x22:
                    in_str = True
                elif c == 0x7B:
                    depth += 1
                elif c == 0x7D:  # '}'
                    depth -= 1
                    if depth == 0:
                        try:
                            out.append(json.loads(data[i : j + 1]))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
                        i = j
                        break
        i += 1
    return out


def _iter_store_role_messages(conn: sqlite3.Connection):
    """Yield role messages in blob rowid order, deduped by content fingerprint."""
    try:
        rows = conn.execute("SELECT rowid, data FROM blobs ORDER BY rowid")
    except sqlite3.Error:
        return
    seen: set[str] = set()
    for rowid, data in rows:
        if not isinstance(data, (bytes, bytearray)):
            if isinstance(data, str):
                data = data.encode("utf-8", errors="ignore")
            else:
                continue
        for obj in _extract_json_objects(bytes(data)):
            if not isinstance(obj, dict):
                continue
            role = obj.get("role")
            if role not in {"user", "assistant", "tool"}:
                continue
            key = json.dumps(obj, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            yield rowid, obj


def _store_user_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        )
    return ""


def _cli_store_paths() -> list[Path]:
    """All store.db files under CHATS_DIR."""
    global _CLI_STORE_INDEX
    if not CHATS_DIR.exists():
        _CLI_STORE_INDEX = {}
        return []
    out: list[Path] = []
    index: dict[str, Path] = {}
    try:
        for ws in CHATS_DIR.iterdir():
            if not ws.is_dir():
                continue
            for sess in ws.iterdir():
                if not sess.is_dir():
                    continue
                db = sess / "store.db"
                if db.is_file():
                    out.append(db)
                    index[sess.name] = db
    except OSError:
        _CLI_STORE_INDEX = {}
        return []
    _CLI_STORE_INDEX = index
    return out


def find_cli_store(session_id: str) -> Path | None:
    """Resolve a CLI session id to its store.db path."""
    if not session_id:
        return None
    cached = _CLI_STORE_INDEX.get(session_id)
    if cached and cached.is_file():
        return cached
    _cli_store_paths()  # refresh index
    cached = _CLI_STORE_INDEX.get(session_id)
    return cached if cached and cached.is_file() else None


def _store_db_mtime(path: Path) -> float:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return 0.0
    for side in (path.parent / "store.db-wal", path.parent / "meta.json"):
        try:
            mtime = max(mtime, side.stat().st_mtime)
        except OSError:
            pass
    return mtime


def _cli_store_summary_uncached(path: Path) -> dict | None:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        meta = _read_store_meta(conn)
        side = _read_sidecar_meta(path)
        session_id = meta.get("agentId") or path.parent.name
        model = meta.get("lastUsedModel") or ""
        title = (meta.get("name") or side.get("title") or "").strip()
        cwd = (side.get("cwd") or "").strip()
        created = meta.get("createdAt") or side.get("createdAtMs")
        updated = side.get("updatedAtMs") or created

        n_user = n_assistant = n_tool = 0
        first_user = ""
        for _rowid, msg in _iter_store_role_messages(conn):
            role = msg.get("role")
            content = msg.get("content")
            if role == "user":
                text = _store_user_text(content)
                cleaned = _clean_cli_user_text(text)
                if cleaned and "<user_info>" not in text:
                    n_user += 1
                    if not first_user:
                        first_user = cleaned
                elif _CLI_SYSTEM_REMINDER_RE.search(text) and not _CLI_USER_QUERY_RE.search(text):
                    pass  # mode notices don't count as user prompts
            elif role == "assistant":
                n_assistant += 1
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool-call":
                            n_tool += 1
            elif role == "tool":
                pass
        if not title:
            title = _short_title(first_user) or "(untitled session)"

        mtime = _store_db_mtime(path)
        return {
            "agent": "cursor",
            "cursor_source": "cli",
            "id": session_id,
            "file": CLI_SESSION_SCHEME + session_id,
            "title": title,
            "cwd": cwd,
            "git_branch": "",
            "version": "",
            "first_ts": _iso_from_ms(created),
            "last_ts": _iso_from_ms(updated),
            "n_user": n_user,
            "n_assistant": n_assistant,
            "n_tool": n_tool,
            "n_web": 0,
            "n_records": n_user + n_assistant + n_tool,
            "model": model,
            "models": [model] if model else [],
            "mtime": mtime,
        }
    finally:
        conn.close()


def cli_store_summary(path: Path) -> dict | None:
    """Lightweight metadata for one CLI store.db, cached by mtime/size."""
    global _CLI_SUMMARY_CACHE
    key = str(path)
    try:
        st = path.stat()
        identity = (st.st_mtime, st.st_size)
    except OSError:
        identity = None
    if identity is not None:
        cached = _CLI_SUMMARY_CACHE.get(key)
        if cached and cached[:2] == identity:
            return cached[2]
    summary = _cli_store_summary_uncached(path)
    if summary is not None and identity is not None:
        _CLI_SUMMARY_CACHE[key] = (identity[0], identity[1], summary)
    return summary


def _iter_cli_store_summaries(skip_ids: set[str] | None = None):
    skip = skip_ids or set()
    for path in _cli_store_paths():
        try:
            summary = cli_store_summary(path)
        except (OSError, ValueError, sqlite3.Error):
            continue
        if not summary or summary.get("id") in skip:
            continue
        yield summary


def _tool_result_text(result) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("output", "text", "content", "stdout"):
            if isinstance(result.get(key), str):
                return result[key]
        return json.dumps(result, indent=2)
    return str(result)


def parse_cli_store(path: Path) -> dict | None:
    """Full structured parse of a Cursor CLI store.db into Claude-shaped events."""
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    try:
        meta = _read_store_meta(conn)
        side = _read_sidecar_meta(path)
        session_id = meta.get("agentId") or path.parent.name
        model = meta.get("lastUsedModel") or ""
        title = (meta.get("name") or side.get("title") or "").strip()
        cwd = (side.get("cwd") or "").strip()

        messages = list(_iter_store_role_messages(conn))

        # First pass: collect tool results by toolCallId.
        results: dict[str, dict] = {}
        for _rowid, msg in messages:
            if msg.get("role") != "tool":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool-result":
                    continue
                call_id = block.get("toolCallId") or msg.get("id")
                if not call_id:
                    continue
                results[call_id] = {
                    "is_error": bool(block.get("is_error") or block.get("isError")),
                    "text": _tool_result_text(block.get("result")),
                    "images": [],
                }

        events: list[dict] = []
        first_user = ""
        for _rowid, msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "tool":
                continue

            if role == "user":
                text = _store_user_text(content)
                if not text.strip():
                    continue
                # Skip the giant injected user_info preamble.
                if "<user_info>" in text and not _CLI_USER_QUERY_RE.search(text):
                    continue
                reminder = _CLI_SYSTEM_REMINDER_RE.search(text)
                if reminder and not _CLI_USER_QUERY_RE.search(text):
                    events.append(
                        {
                            "kind": "notice",
                            "label": "System reminder",
                            "text": reminder.group(1).strip() or text.strip(),
                            "is_sidechain": False,
                        }
                    )
                    continue
                cleaned = _clean_cli_user_text(text)
                if not cleaned:
                    continue
                if not first_user:
                    first_user = cleaned
                events.append(
                    {
                        "kind": "user",
                        "ts": None,
                        "blocks": [{"type": "text", "text": cleaned}],
                        "is_sidechain": False,
                    }
                )
                continue

            # assistant
            blocks: list[dict] = []
            if isinstance(content, str):
                if content.strip():
                    blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "reasoning":
                        thinking = block.get("text") or ""
                        # Still emit when only a signature exists so the UI can
                        # show "thinking (not recorded)" rather than hide the turn.
                        if thinking.strip() or block.get("signature"):
                            blocks.append({"type": "thinking", "text": thinking})
                    elif btype == "text" and (block.get("text") or "").strip():
                        blocks.append({"type": "text", "text": block["text"]})
                    elif btype == "tool-call":
                        name = block.get("toolName") or block.get("name") or "tool"
                        args = block.get("args") or block.get("input") or {}
                        if isinstance(args, str):
                            parsed = _loads(args)
                            args = parsed if isinstance(parsed, dict) else {"raw": args}
                        if not isinstance(args, dict):
                            args = {"value": args}
                        tool = _normalize_cli_tool(name, args)
                        call_id = block.get("toolCallId") or block.get("id")
                        if call_id:
                            tool["id"] = call_id
                            if call_id in results:
                                tool["result"] = results[call_id]
                                # Clear the "missing" flag from the JSONL normalizer.
                                tool["result"].pop("missing", None)
                        blocks.append(tool)
            if not blocks:
                continue
            # Drop duplicate near-identical assistant turns that store.db often
            # records twice in a row (partial then final).
            if events and events[-1].get("kind") == "assistant":
                prev = events[-1].get("blocks") or []
                if json.dumps(prev, sort_keys=True) == json.dumps(blocks, sort_keys=True):
                    continue
            events.append(
                {
                    "kind": "assistant",
                    "ts": None,
                    "model": model,
                    "blocks": blocks,
                    "is_sidechain": False,
                }
            )

        if not title:
            title = _short_title(first_user) or "(untitled session)"
        out_meta = {}
        if cwd:
            out_meta["cwd"] = cwd
        if model:
            out_meta["model"] = model
        return {
            "agent": "cursor",
            "cursor_source": "cli",
            "id": session_id,
            "title": title,
            "meta": out_meta,
            "events": events,
            "n_records": len(messages),
        }
    finally:
        conn.close()


def parse_cli_store_by_id(session_id: str) -> dict | None:
    """Parse a CLI session addressed as ``cursorcli:<id>``."""
    path = find_cli_store(session_id)
    if path is None:
        return None
    return parse_cli_store(path)
