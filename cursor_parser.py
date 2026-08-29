#!/usr/bin/env python3
"""Cursor agent transcript parsing library.

Three sources:

1. **Cursor IDE** — the SQLite store at
   ``~/Library/Application Support/Cursor/User/globalStorage/state.vscdb``.
   Full fidelity: tool outputs, per-turn model (from user-bubble
   ``modelInfo``), thinking, timestamps, token counts.
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
from datetime import datetime
from pathlib import Path

import common
import cursor_binary

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
    "Shell": "Shell",
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

# Per-file summary cache for CLI JSONL / store.db, keyed on (mtime, size).
SUMMARY_CACHE = common.SummaryCache()
# session id -> store.db path, refreshed whenever we scan chats.
_CLI_STORE_INDEX: dict[str, Path] = {}
# Whole IDE session list, keyed by the DB file mtime so the 1s /api/sessions
# poll doesn't re-read 100+ composers every tick.
_LIST_CACHE: tuple[float, list] | None = None


def configure(
    db_path: Path | None = None,
    projects_dir: Path | None = None,
    chats_dir: Path | None = None,
) -> None:
    """Point the module at Cursor IDE DB / projects / chats directories."""
    global DB_PATH, PROJECTS_DIR, CHATS_DIR, _LIST_CACHE, _CLI_STORE_INDEX
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
    SUMMARY_CACHE.clear()
    _CLI_STORE_INDEX = {}


def _connect() -> sqlite3.Connection | None:
    """Read-only connection to the live DB (safe while Cursor is running)."""
    if not DB_PATH.exists():
        return None
    try:
        return common.connect_ro(DB_PATH)
    except sqlite3.Error:
        return None


_loads = common.loads_or_none
_iso_from_ms = common.iso_from_ms_or_none


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


# ---------------------------------------------------------------------------
# Session list
# ---------------------------------------------------------------------------
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


_NOTICE_PATTERN = re.compile(r"^\s*(?:<timestamp>.*?</timestamp>\s*)?<system_notification>", re.DOTALL)


def _synthetic_user_notice(text) -> dict | None:
    """If a user bubble is actually Cursor injecting a background shell/subagent
    result rather than a real prompt, return a ``{label, text}`` notice;
    otherwise ``None``. Mirrors Claude Code's task-notification convention
    (claude_parser._synthetic_user_notice): the model needs the full text, but
    it isn't something the person typed, so it renders as a notice, not a
    user turn."""
    if not isinstance(text, str) or not _NOTICE_PATTERN.match(text):
        return None
    return {"label": "Background task", "text": text.strip()}


def _first_user_text(conn: sqlite3.Connection, cid: str, headers: list) -> str:
    """First real user bubble's text (for conversations with no AI-generated name)."""
    for h in headers:
        if h.get("type") != 1:
            continue
        row = conn.execute(
            "select value from cursorDiskKV where key=?",
            (f"bubbleId:{cid}:{h.get('bubbleId')}",),
        ).fetchone()
        b = _loads(row[0]) if row else None
        text = (b.get("text") or "").strip() if b else ""
        if text and not _synthetic_user_notice(text):
            return b["text"]
    return ""


def _model_from_bubble(b: dict | None) -> str:
    """Per-turn model Cursor stores on user bubbles as ``modelInfo.modelName``."""
    if not isinstance(b, dict):
        return ""
    mi = b.get("modelInfo")
    if isinstance(mi, dict):
        return (mi.get("modelName") or "").strip()
    return ""


def _summary_from_composer(conn, cid: str, d: dict, db_mtime: float) -> dict:
    headers = d.get("fullConversationHeadersOnly") or []
    n_user = 0
    for h in headers:
        if h.get("type") != 1:
            continue
        row = conn.execute(
            "select value from cursorDiskKV where key=?",
            (f"bubbleId:{cid}:{h.get('bubbleId')}",),
        ).fetchone()
        text = (_loads(row[0]).get("text") or "").strip() if row else ""
        if text and not _synthetic_user_notice(text):
            n_user += 1
    n_tool = sum(1 for h in headers if (h.get("grouping") or {}).get("toolFormerTool") is not None)
    n_assistant = sum(
        1
        for h in headers
        if h.get("type") == 2 and (h.get("grouping") or {}).get("toolFormerTool") is None
    )
    model = (d.get("modelConfig") or {}).get("modelName") or ""
    title = d.get("name") or common.short_title(_first_user_text(conn, cid, headers)) or "(untitled session)"
    created = d.get("createdAt")
    updated = d.get("lastUpdatedAt") or created
    mtime = (updated / 1000) if updated else db_mtime
    return common.make_summary(
        agent="cursor",
        id=cid,
        file=SESSION_SCHEME + cid,
        title=title,
        cwd=_composer_cwd(d),
        first_ts=_iso_from_ms(created),
        last_ts=_iso_from_ms(updated),
        n_user=n_user,
        n_assistant=n_assistant,
        n_tool=n_tool,
        n_records=len(headers),
        model=model,
        mtime=mtime,
    )


def _list_db_sessions() -> list[dict]:
    """IDE conversation summaries from state.vscdb."""
    global _LIST_CACHE
    st = common.safe_stat(DB_PATH)
    if st is None:
        return []
    if _LIST_CACHE and _LIST_CACHE[0] == st.st_mtime:
        return _LIST_CACHE[1]

    conn = _connect()
    if conn is None:
        return []
    sessions: list[dict] = []
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
                sessions.append(_summary_from_composer(conn, cid, d, st.st_mtime))
            except (sqlite3.Error, ValueError, TypeError):
                continue
    finally:
        conn.close()

    _LIST_CACHE = (st.st_mtime, sessions)
    return sessions


def list_sessions() -> list[dict]:
    """Flat list of Cursor session summaries (IDE DB + CLI store.db + JSONL).

    When the same conversation exists in more than one store, preference is
    IDE ``state.vscdb`` > CLI ``store.db`` > JSONL export — but the losing
    records still contribute structural metadata (subagent linkage, titles)
    to the preferred one.
    """
    sessions_by_id: dict[str, dict] = {}
    for summary in _list_db_sessions():
        if summary.get("id"):
            sessions_by_id[summary["id"]] = summary

    # Prefer rich per-chat store.db over the lossy JSONL export. Store metadata
    # also carries the newer subagent hierarchy, so inspect duplicates too.
    for summary in _iter_cli_store_summaries():
        preferred = sessions_by_id.get(summary.get("id"))
        if preferred is not None:
            if summary.get("is_subagent"):
                for field in ("is_subagent", "subagent_type", "parent_id", "root_parent_id"):
                    preferred[field] = summary.get(field)
                if not preferred.get("title") or preferred.get("title") == "New Agent":
                    preferred["title"] = summary.get("title")
            continue
        sessions_by_id[summary["id"]] = summary

    # JSONL paths encode the subagent hierarchy, even when the same conversation
    # is also present in the IDE DB or a rich CLI store.db.  Do not skip those
    # duplicates: retain the preferred record, but copy its structural linkage.
    for summary in _iter_cli_summaries():
        preferred = sessions_by_id.get(summary.get("id"))
        if preferred is not None:
            if summary.get("is_subagent"):
                for field in ("is_subagent", "subagent_type", "parent_id"):
                    preferred[field] = summary.get(field)
            continue
        sessions_by_id[summary["id"]] = summary

    # Sub-agent grouping in the unified server is keyed by the parent record's
    # `file`. A subagent discovered from JSONL may have a cursordb:/cursorcli:
    # parent, so resolve the link after all preferred records are selected.
    for summary in sessions_by_id.values():
        if not summary.get("is_subagent") or not summary.get("parent_id"):
            continue
        parent = sessions_by_id.get(summary["parent_id"])
        if parent is not None:
            summary["parent_file"] = parent.get("file") or ""

    return list(sessions_by_id.values())


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
        # Newer Cursor leaves the JSON result's `files` list empty; the real
        # match list lives only in the toolCallBinary protobuf.
        if not text or text == "(no matches)":
            files = cursor_binary.glob_files(tf)
            if files:
                text = "\n".join(files)
    elif raw_name == "ripgrep_raw_search":
        inp = {"pattern": args.get("pattern") or args.get("query") or "", "path": args.get("path") or ""}
        text, is_error = _format_generic(result, is_error)
        # Grep results are almost never in the JSON result — decode the
        # toolCallBinary protobuf (fall back to additionalData stats).
        if not text:
            text = cursor_binary.grep_result_text(tf)
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

    # Last resort for completed calls whose result was never written to JSON
    # (Cursor is migrating tool results into the binary record — grep first,
    # await and others already affected): pull readable strings out of the
    # binary's result section. Skip edit_file (rendered as a diff from content
    # snapshots) and todo_write (input-only), where this would only add noise.
    if not text and status == "completed" and raw_name not in ("edit_file_v2", "todo_write"):
        text = cursor_binary.generic_result_text(tf)

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


def _parse_ms(ts) -> int | None:
    """A bubble createdAt (ISO-8601 string or epoch-ms) → epoch-ms."""
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str) and ts:
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _recovered_inserts(headers: list, bubbles: dict) -> dict[int, list[dict]]:
    """Assistant text bubbles Cursor's checkpoint rebuilds dropped, keyed by
    the header index to insert them before (len(headers) = append at end).

    cursor-agent threads are periodically rebuilt from the server-side
    conversation: every kept bubble is re-created (fresh bubbleId, the rebuild
    time as createdAt) and older generations become rows no header references.
    A text bubble that never registered server-side (no serverBubbleId — seen
    with gpt plan-mode clarifying-question turns) is silently dropped by the
    rebuild and survives only as such an orphan row; Cursor's own UI loses it.

    An orphan whose text is a substring of the kept transcript is stream
    debris or a superseded generation, not a lost message — matched against
    the header-order concatenation because rebuilds sometimes re-split one
    streamed message into adjacent bubbles. Placement uses the only genuine
    timestamps that survive a rebuild: the orphan's own createdAt and the
    start/end stamps inside kept tool calls' binary envelopes. A turn-final
    text belongs directly after the last tool that finished before it was
    streamed, so it goes just before the next user message after that anchor.
    """
    header_ids = {h.get("bubbleId") for h in headers}
    kept_texts = []
    for h in headers:
        text = (bubbles.get(h.get("bubbleId")) or {}).get("text") or ""
        if text.strip():
            kept_texts.append(text)
    joined = "".join(kept_texts)

    by_text: dict[str, dict] = {}
    for bid, b in bubbles.items():
        if bid in header_ids or b.get("type") != 2 or b.get("toolFormerData"):
            continue
        text = b.get("text") or ""
        if not text.strip() or text in joined or _parse_ms(b.get("createdAt")) is None:
            continue
        prev = by_text.get(text)  # rebuilds can leave several copies: keep the original
        if prev is None or _parse_ms(b["createdAt"]) < _parse_ms(prev["createdAt"]):
            by_text[text] = b
    # a partial stream snapshot of another lost message is not its own message
    lost = [
        b for t, b in by_text.items()
        if not any(t != other and t in other for other in by_text)
    ]
    if not lost:
        return {}

    anchors = []  # (header index, epoch-ms), in header order
    for i, h in enumerate(headers):
        tf = (bubbles.get(h.get("bubbleId")) or {}).get("toolFormerData")
        if isinstance(tf, dict):
            start_ms, end_ms = cursor_binary.call_times_ms(tf)
            if end_ms or start_ms:
                anchors.append((i, end_ms or start_ms))

    inserts: dict[int, list[dict]] = {}
    for b in sorted(lost, key=lambda b: _parse_ms(b["createdAt"])):
        t = _parse_ms(b["createdAt"])
        before = [i for i, ms in anchors if ms <= t]
        after = [i for i, ms in anchors if ms > t]
        if before:
            pos = next(
                (j for j in range(before[-1] + 1, len(headers)) if headers[j].get("type") == 1),
                len(headers),
            )
            if after:  # never push past chronologically later tool activity
                pos = min(pos, after[0])
        else:
            pos = after[0] if after else len(headers)
        inserts.setdefault(pos, []).append(b)
    return inserts


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
        # Session config is only the *currently selected* model. Per-turn model
        # lives on user bubbles as modelInfo.modelName and applies to the
        # assistant replies that follow until the next user turn overrides it.
        session_model = (d.get("modelConfig") or {}).get("modelName") or ""
        current_model = session_model
        models_seen: list[str] = []
        models_seen_set: set[str] = set()

        events: list[dict] = []
        recovered = _recovered_inserts(headers, bubbles)

        def emit_recovered(pos: int):
            for rb in recovered.get(pos, ()):
                if current_model and current_model not in models_seen_set:
                    models_seen_set.add(current_model)
                    models_seen.append(current_model)
                events.append(
                    {
                        "kind": "assistant",
                        "ts": rb.get("createdAt"),
                        "model": current_model,
                        "blocks": [{"type": "text", "text": rb["text"]}],
                        "is_sidechain": False,
                        "recovered": True,
                    }
                )

        # Each Cursor bubble (a thinking block, a text reply, or a tool call) is
        # emitted as its own event — like Claude Code renders each message as a
        # separate turn — rather than collapsing a whole turn into one box.
        for hi, h in enumerate(headers):
            emit_recovered(hi)
            b = bubbles.get(h.get("bubbleId"))
            if not b:
                continue
            btype = b.get("type")
            ts = h.get("createdAt") or b.get("createdAt")

            bubble_model = _model_from_bubble(b)
            if bubble_model:
                current_model = bubble_model

            if btype == 1:  # user
                text = b.get("text") or ""
                if not text.strip():
                    continue
                notice = _synthetic_user_notice(text)
                if notice:
                    events.append({"kind": "notice", "ts": ts, "is_sidechain": False, **notice})
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
            if current_model and current_model not in models_seen_set:
                models_seen_set.add(current_model)
                models_seen.append(current_model)
            events.append(
                {
                    "kind": "assistant",
                    "ts": ts,
                    "model": current_model,
                    "blocks": blocks,
                    "is_sidechain": False,
                }
            )
        emit_recovered(len(headers))

        cwd = _composer_cwd(d)
        meta = {}
        if cwd:
            meta["cwd"] = cwd
        # Prefer the last turn's model; fall back to session config.
        meta_model = current_model or session_model
        if meta_model:
            meta["model"] = meta_model
        if models_seen:
            meta["models"] = models_seen
        return {
            "agent": "cursor",
            "id": composer_id,
            "title": d.get("name") or common.short_title(_first_user_text(conn, composer_id, headers)) or "(untitled session)",
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


def _normalize_cli_tool(name: str, args: dict) -> dict:
    """Map a CLI tool_use onto the canonical frontend tool shape."""
    raw = name or "tool"
    canon = _CLI_TOOL_NAME_MAP.get(raw, raw)
    args = args if isinstance(args, dict) else {}
    inp: dict = {}

    if raw == "Shell":
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


def _cli_transcript_context(path: Path) -> dict:
    """Where a CLI JSONL transcript sits: its cwd, and sub-agent parentage.

    Sub-agents live at …/agent-transcripts/<parent-id>/subagents/<id>.jsonl,
    regular sessions at …/agent-transcripts/<id>/<id>.jsonl.
    """
    is_subagent = path.parent.name == "subagents"
    if is_subagent:
        project_dir = path.parent.parent.parent.parent
        parent_id = path.parent.parent.name
        parent_file = path.parent.parent / f"{parent_id}.jsonl"
    else:
        project_dir = path.parent.parent.parent
        parent_id = ""
        parent_file = None
    return {
        "is_subagent": is_subagent,
        "cwd": cwd_for_project_dir(project_dir) if project_dir else "",
        "parent_id": parent_id,
        "parent_file": parent_file,
    }


def _apply_cli_subagent_fields(out: dict, ctx: dict) -> None:
    """Stamp sub-agent linkage onto a summary or full parse, in place."""
    if not ctx["is_subagent"]:
        return
    out["is_subagent"] = True
    out["subagent_type"] = "cursor-cli"
    if ctx["parent_file"] is not None:
        try:
            out["parent_file"] = str(ctx["parent_file"].resolve())
        except OSError:
            out["parent_file"] = str(ctx["parent_file"])
        out["parent_id"] = ctx["parent_id"]


def _cli_session_summary_uncached(path: Path) -> dict:
    records = list(common.iter_jsonl(path))
    n_user = n_assistant = n_tool = 0
    first_user = ""

    for rec in records:
        role = rec.get("role")
        content = (rec.get("message") or {}).get("content")
        if role == "user":
            cleaned = _clean_cli_user_text(common.content_text(content))
            if cleaned:
                n_user += 1
                if not first_user:
                    first_user = cleaned
        elif role == "assistant":
            n_assistant += 1
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        n_tool += 1

    ctx = _cli_transcript_context(path)
    st = common.safe_stat(path)
    summary = common.make_summary(
        agent="cursor",
        cursor_source="cli-jsonl",
        id=path.stem,
        file=str(path.resolve()),
        title=common.short_title(first_user) or "(untitled session)",
        cwd=ctx["cwd"],
        n_user=n_user,
        n_assistant=n_assistant,
        n_tool=n_tool,
        n_records=len(records),
        mtime=st.st_mtime if st else 0,
    )
    _apply_cli_subagent_fields(summary, ctx)
    return summary


def cli_session_summary(path: Path) -> dict:
    """Lightweight metadata for one CLI JSONL transcript, cached by file identity."""
    return common.cached_summary(
        SUMMARY_CACHE, str(path), common.file_identity(path),
        lambda: _cli_session_summary_uncached(path),
    )


def _iter_cli_summaries():
    for path in _cli_paths():
        try:
            yield cli_session_summary(path)
        except (OSError, ValueError):
            continue


def parse_cli_session(path: Path) -> dict | None:
    """Full structured parse of a Cursor CLI agent-transcripts JSONL file."""
    if not path.exists():
        return None
    records = list(common.iter_jsonl(path))
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

    ctx = _cli_transcript_context(path)
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
        "title": common.short_title(first_user) or "(untitled session)",
        "meta": {"cwd": ctx["cwd"]} if ctx["cwd"] else {},
        "events": events,
        "n_records": len(records),
    }
    _apply_cli_subagent_fields(out, ctx)
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


_JSON_DECODER = json.JSONDecoder()


def _extract_json_objects(data: bytes) -> list:
    """Pull the JSON objects out of a blob (plain JSON or binary wrapper).

    Real messages are valid UTF-8 JSON between stretches of binary framing.
    Try a real (C-speed) parse at each '{' and jump over whatever parses;
    a '{' that doesn't start valid JSON is framing noise and the scan hops to
    the next candidate. This replaced a hand-rolled Python brace balancer that
    took ~20s over a big chat's store.db; surrogateescape keeps invalid bytes
    representable, and a parse that covered any is re-checked and dropped,
    matching the strict json.loads(bytes) behavior of the original.
    """
    text = data.decode("utf-8", errors="surrogateescape")
    scan = _JSON_DECODER.scan_once  # the C scanner; unlike raw_decode, a miss
    # raises a cheap StopIteration instead of building a JSONDecodeError whose
    # constructor counts newlines over the whole prefix (quadratic on framing).
    out = []
    n = len(text)
    pos = text.find("{")
    while pos != -1:
        nxt = text[pos + 1] if pos + 1 < n else ""
        # A JSON object continues with a key, '}', or whitespace — anything
        # else is framing noise, not worth handing to the scanner.
        if nxt == '"' or nxt == "}" or nxt.isspace():
            try:
                obj, end = scan(text, pos)
            except (StopIteration, ValueError):
                pass
            else:
                try:
                    text[pos:end].encode("utf-8")
                except UnicodeEncodeError:
                    pass
                else:
                    out.append(obj)
                    pos = text.find("{", end)
                    continue
        pos = text.find("{", pos + 1)
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
            yield obj


_store_user_text = common.content_text


def _store_subagent_fields(meta: dict) -> dict:
    """Normalize Cursor CLI's store.db subagentInfo metadata."""
    info = meta.get("subagentInfo")
    if not isinstance(info, dict) or not info.get("parentAgentId"):
        return {}
    return {
        "is_subagent": True,
        "subagent_type": info.get("typeName") or "cursor-cli",
        "parent_id": info["parentAgentId"],
        "root_parent_id": info.get("rootParentAgentId") or info["parentAgentId"],
    }


def _store_prompt_text(text: str) -> str:
    """Remove an injected reminder while retaining task text after it."""
    return _clean_cli_user_text(_CLI_SYSTEM_REMINDER_RE.sub("", text or ""))


def _store_subagent_title(text: str) -> str:
    """Prefer the task section over generic subagent runner instructions."""
    task = re.search(r"(?:^|\s)##\s+Task\s+(.+)", text or "", re.DOTALL | re.IGNORECASE)
    return _clean_cli_user_text(task.group(1) if task else text)


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


def _open_store(path: Path) -> sqlite3.Connection | None:
    """Read-only connection to one CLI store.db, or None if unopenable."""
    if not path.exists():
        return None
    try:
        return common.connect_ro(path)
    except sqlite3.Error:
        return None


def _store_header(conn: sqlite3.Connection, path: Path) -> dict:
    """The identity fields the summary and the full parse both read."""
    meta = _read_store_meta(conn)
    side = _read_sidecar_meta(path)
    return {
        "meta": meta,
        "side": side,
        "session_id": meta.get("agentId") or path.parent.name,
        "model": meta.get("lastUsedModel") or "",
        "title": (meta.get("name") or side.get("title") or "").strip(),
        "cwd": (side.get("cwd") or "").strip(),
        "subagent_fields": _store_subagent_fields(meta),
    }


def _store_title(title: str, first_user: str, subagent_fields: dict) -> str:
    """Store sessions are often unnamed ('New Agent'); derive a title."""
    if subagent_fields and (not title or title == "New Agent"):
        task_title = _store_subagent_title(first_user)
        return common.short_title(
            f"[{subagent_fields['subagent_type']}] {task_title or first_user or '(sub-agent)'}"
        )
    if not title:
        return common.short_title(first_user) or "(untitled session)"
    return title


def _store_content_fingerprint(conn: sqlite3.Connection, path: Path) -> list:
    """Cheap content identity for one store.db: blob count, last rowid, total
    payload bytes, the raw meta row, and the sidecar meta.json identity.

    Costs a few milliseconds, versus ~1.5s for the full blob scan a summary
    needs — so a store whose mtime was touched without a real change (another
    process, a backup tool, a cache fingerprint format change) revalidates
    cheaply instead of forcing the scan.
    """
    try:
        n, last, total = conn.execute(
            "SELECT count(*), coalesce(max(rowid), 0), coalesce(sum(length(data)), 0) FROM blobs"
        ).fetchone()
    except sqlite3.Error:
        n = last = total = -1
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='0'").fetchone()
        meta_raw = row[0] if row else b""
        if isinstance(meta_raw, bytes):
            meta_raw = meta_raw.decode("utf-8", errors="ignore")
    except sqlite3.Error:
        meta_raw = ""
    sidecar = common.file_identity(path.parent / "meta.json")
    return [n, last, total, str(meta_raw), list(sidecar) if sidecar else None]


def _cli_store_summary_uncached(conn: sqlite3.Connection, path: Path) -> dict | None:
    header = _store_header(conn, path)
    meta, side = header["meta"], header["side"]
    subagent_fields = header["subagent_fields"]
    created = meta.get("createdAt") or side.get("createdAtMs")
    updated = side.get("updatedAtMs") or created

    n_user = n_assistant = n_tool = 0
    first_user = ""
    for msg in _iter_store_role_messages(conn):
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            text = _store_user_text(content)
            cleaned = _store_prompt_text(text)
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

    summary = common.make_summary(
        agent="cursor",
        cursor_source="cli",
        id=header["session_id"],
        file=CLI_SESSION_SCHEME + header["session_id"],
        title=_store_title(header["title"], first_user, subagent_fields),
        cwd=header["cwd"],
        first_ts=_iso_from_ms(created),
        last_ts=_iso_from_ms(updated),
        n_user=n_user,
        n_assistant=n_assistant,
        n_tool=n_tool,
        n_records=n_user + n_assistant + n_tool,
        model=header["model"],
        mtime=_store_db_mtime(path),
    )
    summary.update(subagent_fields)
    return summary


def cli_store_summary(path: Path) -> dict | None:
    """Lightweight metadata for one CLI store.db.

    Two-level cache: the fast fingerprint is the store file's (mtime, size);
    when that misses, a cheap in-database content fingerprint is compared
    before paying for the full blob scan. Fingerprints are stored as
    ``[fast, content]`` so either level can validate an entry.
    """
    key = str(path)
    ident = common.file_identity(path)
    if ident is None:
        return None
    fast = list(ident)
    entry = SUMMARY_CACHE.peek(key)
    if entry and isinstance(entry[0], list) and len(entry[0]) == 2 and entry[0][0] == fast:
        return entry[1]

    conn = _open_store(path)
    if conn is None:
        return None
    try:
        content = _store_content_fingerprint(conn, path)
        if entry and isinstance(entry[0], list) and len(entry[0]) == 2 and entry[0][1] == content:
            # Only the mtime moved; the store's content is unchanged. Reuse the
            # summary, but refresh its recency from the file (a full recompute
            # would have picked the new mtime up, and the sidebar sorts by it)
            # along with the fast fingerprint for the next poll.
            summary = dict(entry[1])
            summary["mtime"] = _store_db_mtime(path)
            SUMMARY_CACHE.put(key, [fast, content], summary)
            return summary
        summary = _cli_store_summary_uncached(conn, path)
        if summary is not None:
            SUMMARY_CACHE.put(key, [fast, content], summary)
        return summary
    finally:
        conn.close()


def _iter_cli_store_summaries():
    for path in _cli_store_paths():
        try:
            summary = cli_store_summary(path)
        except (OSError, ValueError, sqlite3.Error):
            continue
        if summary:
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
    conn = _open_store(path)
    if conn is None:
        return None
    try:
        header = _store_header(conn, path)
        session_id = header["session_id"]
        model = header["model"]
        cwd = header["cwd"]
        subagent_fields = header["subagent_fields"]

        messages = list(_iter_store_role_messages(conn))

        # First pass: collect tool results by toolCallId.
        results: dict[str, dict] = {}
        for msg in messages:
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
        for msg in messages:
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
                    text = _CLI_SYSTEM_REMINDER_RE.sub("", text)
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

        title = _store_title(header["title"], first_user, subagent_fields)
        out_meta = {}
        if cwd:
            out_meta["cwd"] = cwd
        if model:
            out_meta["model"] = model
        out = {
            "agent": "cursor",
            "cursor_source": "cli",
            "id": session_id,
            "title": title,
            "meta": out_meta,
            "events": events,
            "n_records": len(messages),
        }
        out.update(subagent_fields)
        return out
    finally:
        conn.close()


def parse_cli_store_by_id(session_id: str) -> dict | None:
    """Parse a CLI session addressed as ``cursorcli:<id>``."""
    path = find_cli_store(session_id)
    if path is None:
        return None
    return parse_cli_store(path)
