#!/usr/bin/env python3
"""Parser for opencode transcripts.

opencode keeps everything in one SQLite database (``opencode.db``, by default
under ``~/.local/share/opencode``). There are no per-session transcript files,
so — like Cursor's IDE conversations — sessions are addressed by the synthetic
id ``opencode:<sessionID>`` rather than by path.

Storage layout (opencode ≥ 1.18):

- ``session`` — one row per conversation. ``parent_id`` is set on sub-agent
  sessions spawned by the ``task`` tool.
- ``message`` — one row per turn; ``data`` is the JSON message record, either
  ``role: "user"`` or ``role: "assistant"``.
- ``part`` — the actual content, one row per part, ``data`` holding a
  discriminated union on ``type``: text, reasoning, tool, file, agent, subtask,
  step-start, step-finish, snapshot, patch, retry, compaction.

Tool calls live on the assistant turn (``type: "tool"`` parts carrying their own
result in ``state``), so this parser emits the *block* event shape that
claude_parser and cursor_parser use, not Codex's flat shape.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import common

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
DB_PATH = DEFAULT_DB_PATH

# Sessions have no transcript file, so `file` ids are synthetic.
SESSION_SCHEME = "opencode:"

# Per-session sidebar summaries, keyed by session id and invalidated by the
# session's own `time_updated` plus its message/part counts.
SUMMARY_CACHE = common.SummaryCache()

# Whole-list cache keyed on the database's on-disk identity, so the 1s
# /api/sessions poll does no SQL at all while opencode is idle.
_LIST_CACHE: tuple[tuple, list] | None = None

# opencode names its tools in lowercase and its tool *arguments* in camelCase.
# The frontend's formatToolInput() already lowercases names, so the names line
# up as-is; the argument keys do not, and are renamed here to the canonical
# Claude Code vocabulary (the same normalization cursor_parser does).
_INPUT_KEY_ALIASES = {
    "read": {"filePath": "file_path"},
    "write": {"filePath": "file_path"},
    "edit": {
        "filePath": "file_path",
        "oldString": "old_string",
        "newString": "new_string",
        "replaceAll": "replace_all",
    },
    "grep": {"include": "glob"},
    "apply_patch": {"patchText": "patch"},
    "lsp": {"filePath": "file_path"},
}

def configure(db_path: Path | None = None) -> None:
    """Point the module at an opencode database (or back at the default)."""
    global DB_PATH, _LIST_CACHE
    if db_path is None:
        DB_PATH = DEFAULT_DB_PATH
    else:
        path = Path(db_path).expanduser()
        # Accept either the .db directly or the opencode data directory.
        DB_PATH = path / "opencode.db" if path.is_dir() else path
    _LIST_CACHE = None
    SUMMARY_CACHE.clear()


def _connect() -> sqlite3.Connection | None:
    """Read-only connection to the live DB (safe while opencode is running)."""
    if not DB_PATH.exists():
        return None
    try:
        return common.connect_ro(DB_PATH, row_factory=sqlite3.Row)
    except sqlite3.Error:
        return None


def _db_identity() -> tuple | None:
    """Change fingerprint for the database, write-ahead log included.

    opencode runs the database in WAL mode: a live session's writes land in
    ``opencode.db-wal`` and leave the main file's mtime untouched for minutes at
    a time. Keying the list cache on the main file alone would therefore serve a
    stale sidebar for the whole of an in-progress session.
    """
    identity = common.file_identity(DB_PATH)
    if identity is None:
        return None
    return identity + (common.file_identity(DB_PATH.with_name(DB_PATH.name + "-wal")),)


_loads = common.loads_or_none
_iso_from_ms = common.iso_from_ms_or_none


def _model_name(model) -> str:
    """'openrouter/x-ai/grok-4.6' from a {providerID, modelID} record."""
    if not isinstance(model, dict):
        return ""
    model_id = model.get("modelID") or model.get("id") or ""
    provider = model.get("providerID") or ""
    if model_id and provider:
        return f"{provider}/{model_id}"
    return model_id or provider or ""


# ---------------------------------------------------------------------------
# Session list
# ---------------------------------------------------------------------------
# opencode titles a session before the model has named it; those placeholders
# are replaced by the first user prompt.
def _is_placeholder_title(title: str) -> bool:
    return not title or title.startswith("New session - ")


def _session_counts(conn: sqlite3.Connection, session_ids: list[str]) -> dict[str, dict]:
    """Message/part tallies for the given sessions, in two grouped queries."""
    counts = {sid: {"n_user": 0, "n_assistant": 0, "n_tool": 0, "n_web": 0, "n_records": 0}
              for sid in session_ids}
    if not session_ids:
        return counts
    marks = ",".join("?" * len(session_ids))
    for row in conn.execute(
        f"select session_id, json_extract(data, '$.role') role, count(*) n "
        f"from message where session_id in ({marks}) group by 1, 2",
        session_ids,
    ):
        entry = counts.get(row["session_id"])
        if entry is None:
            continue
        if row["role"] == "user":
            entry["n_user"] = row["n"]
        elif row["role"] == "assistant":
            entry["n_assistant"] = row["n"]
        entry["n_records"] += row["n"]
    for row in conn.execute(
        f"select session_id, json_extract(data, '$.tool') tool, count(*) n "
        f"from part where session_id in ({marks}) "
        f"and json_extract(data, '$.type') = 'tool' group by 1, 2",
        session_ids,
    ):
        entry = counts.get(row["session_id"])
        if entry is None:
            continue
        entry["n_tool"] += row["n"]
        if row["tool"] in ("webfetch", "websearch"):
            entry["n_web"] += row["n"]
    return counts


def _first_user_text(conn: sqlite3.Connection, session_id: str) -> str:
    """Text of the earliest user prompt, for sessions the model never titled."""
    row = conn.execute(
        "select p.data from part p join message m on m.id = p.message_id "
        "where p.session_id = ? and json_extract(m.data, '$.role') = 'user' "
        "and json_extract(p.data, '$.type') = 'text' "
        "order by m.time_created, m.id, p.id limit 1",
        (session_id,),
    ).fetchone()
    part = _loads(row["data"]) if row else None
    return (part or {}).get("text") or ""


def _summary(conn: sqlite3.Connection, row: sqlite3.Row, counts: dict) -> dict:
    session_id = row["id"]
    title = row["title"] or ""
    if _is_placeholder_title(title):
        title = common.short_title(_first_user_text(conn, session_id)) or "(untitled session)"
    model = _model_name(_loads(row["model"]))
    created, updated = row["time_created"], row["time_updated"] or row["time_created"]
    summary = common.make_summary(
        agent="opencode",
        id=session_id,
        file=SESSION_SCHEME + session_id,
        title=title,
        cwd=row["directory"] or "",
        version=row["version"] or "",
        first_ts=_iso_from_ms(created),
        last_ts=_iso_from_ms(updated),
        n_user=counts["n_user"],
        n_assistant=counts["n_assistant"],
        n_tool=counts["n_tool"],
        n_web=counts["n_web"],
        n_records=counts["n_records"],
        model=model,
        mtime=(updated / 1000) if updated else 0,
    )
    if row["parent_id"]:
        summary["is_subagent"] = True
        summary["parent_id"] = row["parent_id"]
        summary["parent_file"] = SESSION_SCHEME + row["parent_id"]
        # The agent a sub-agent session runs as ('explore', 'plan', …).
        summary["subagent_type"] = row["agent"] or ""
    return summary


def list_sessions() -> list[dict]:
    """Summaries for every opencode session in the database, unsorted."""
    global _LIST_CACHE
    identity = _db_identity()
    if identity is None:
        return []
    if _LIST_CACHE and _LIST_CACHE[0] == identity:
        return _LIST_CACHE[1]

    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "select id, parent_id, title, directory, version, model, agent, "
            "time_created, time_updated from session"
        ).fetchall()
        # Only re-tally sessions whose stored fingerprint went stale; an idle
        # session's counts are reused straight from the persisted cache.
        stale = [r for r in rows if SUMMARY_CACHE.get(r["id"], [r["time_updated"]]) is None]
        counts = _session_counts(conn, [r["id"] for r in stale])
        sessions = []
        for row in rows:
            cached = SUMMARY_CACHE.get(row["id"], [row["time_updated"]])
            if cached is None:
                cached = _summary(conn, row, counts[row["id"]])
                SUMMARY_CACHE.put(row["id"], [row["time_updated"]], cached)
            sessions.append(dict(cached))
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    _LIST_CACHE = (identity, sessions)
    return sessions


# ---------------------------------------------------------------------------
# Full conversation parse
# ---------------------------------------------------------------------------
def _normalize_input(tool: str, raw) -> dict:
    """Rename opencode's camelCase arguments to the canonical tool vocabulary."""
    if not isinstance(raw, dict):
        return {"input": raw} if raw not in (None, "") else {}
    aliases = _INPUT_KEY_ALIASES.get(tool)
    if not aliases:
        return dict(raw)
    return {aliases.get(key, key): value for key, value in raw.items()}


def _attachment_images(attachments) -> list[str]:
    """data: URIs for the image attachments a completed tool returned."""
    out = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        url = att.get("url") or ""
        if str(att.get("mime", "")).startswith("image/") and url.startswith("data:"):
            out.append(url)
    return out


def _tool_block(part: dict) -> dict:
    """A `tool_use` block from a tool part, with its result already attached."""
    tool = part.get("tool") or "tool"
    state = part.get("state") or {}
    status = state.get("status")
    block = {
        "type": "tool_use",
        "id": part.get("callID") or part.get("id") or "",
        "name": tool,
        "input": _normalize_input(tool, state.get("input")),
        "result": None,
    }
    if status == "completed":
        block["result"] = {
            "text": state.get("output") or "",
            "images": _attachment_images(state.get("attachments")),
            "is_error": False,
        }
    elif status == "error":
        block["result"] = {
            "text": state.get("error") or "",
            "images": [],
            "is_error": True,
        }
    else:
        # pending/running: opencode has recorded the call but no outcome yet.
        block["status"] = status or "pending"
    # The `task` tool records the sub-agent session it spawned; carry the link
    # so the viewer can point at that session.
    child = (state.get("metadata") or {}).get("sessionId")
    if tool == "task" and child:
        block["child_session_id"] = child
        block["child_file"] = SESSION_SCHEME + child
    return block


def _has_encrypted_reasoning(part: dict) -> bool:
    """Whether the provider kept an opaque reasoning blob beside the summary.

    Reasoning parts carry a per-provider ``metadata`` bag whose
    ``reasoning_details`` list holds both the token-by-token summary and, for
    models that return one, a ``reasoning.encrypted`` entry: an ``rs_…`` id
    plus opaque ``data`` that only the provider can read. The visible thinking
    text is then a *summary* of a chain of thought the transcript does not
    contain, which is worth saying rather than leaving implied.
    """
    for provider in (part.get("metadata") or {}).values():
        if not isinstance(provider, dict):
            continue
        for detail in provider.get("reasoning_details") or []:
            if isinstance(detail, dict) and detail.get("type") == "reasoning.encrypted":
                return True
    return False


def _step_tokens(part: dict) -> dict:
    """Flattened token counts from a step-finish part."""
    tokens = part.get("tokens") or {}
    cache = tokens.get("cache") or {}
    return {
        "input": tokens.get("input"),
        "output": tokens.get("output"),
        "reasoning": tokens.get("reasoning"),
        "cache_read": cache.get("read"),
        "cache_write": cache.get("write"),
    }


def _file_block(part: dict) -> dict | None:
    """An image block for an attached image, else None (handled as a notice)."""
    url = part.get("url") or ""
    if str(part.get("mime", "")).startswith("image/") and url.startswith("data:"):
        return {"type": "image", "data_uri": url}
    return None


def _error_text(error) -> str:
    """Readable one-liner for a message-level or retry-part error record."""
    if not isinstance(error, dict):
        return str(error or "")
    name = error.get("name") or "Error"
    data = error.get("data") or {}
    message = data.get("message") if isinstance(data, dict) else ""
    return f"{name}: {message}" if message else name


def _user_events(parts: list[dict], ts) -> list[dict]:
    """Events for one user turn: the prompt itself plus any injected records."""
    events: list[dict] = []
    blocks: list[dict] = []
    for part in parts:
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text") or ""
            if not text:
                continue
            if part.get("synthetic") or part.get("ignored"):
                # Not something the user typed: a background-task result fed
                # back in, or text recorded but withheld from the model.
                events.append({
                    "kind": "notice",
                    "ts": ts,
                    "label": "Ignored" if part.get("ignored") else "Injected",
                    "text": text,
                })
                continue
            blocks.append({"type": "text", "text": text})
        elif ptype == "file":
            block = _file_block(part)
            if block is not None:
                blocks.append(block)
            else:
                events.append({
                    "kind": "attachment",
                    "ts": ts,
                    "att_type": "file",
                    "filename": part.get("filename") or "",
                    "mime": part.get("mime") or "",
                })
        elif ptype == "subtask":
            events.append({
                "kind": "instructions",
                "ts": ts,
                "role": "user",
                "label": f"Subtask → @{part.get('agent') or 'agent'}"
                         + (f" ({part['description']})" if part.get("description") else ""),
                "text": part.get("prompt") or "",
            })
        elif ptype == "compaction":
            events.append(_compaction_event(part, ts))
        # `agent` parts only mark the @-mention span inside the prompt text.

    if blocks:
        events.insert(0, {"kind": "user", "ts": ts, "blocks": blocks})
    return events


def _compaction_event(part: dict, ts) -> dict:
    overflow = bool(part.get("overflow"))
    return {
        "kind": "system",
        "ts": ts,
        "subtype": "compact_boundary",
        "text": "Context compacted" + (" (context overflow)" if overflow else ""),
        "compaction": {
            "trigger": "auto" if part.get("auto") else "manual",
            "overflow": overflow,
        },
    }


def _assistant_events(message: dict, parts: list[dict], ts) -> list[dict]:
    """Events for one assistant turn, with tool calls folded into its blocks."""
    events: list[dict] = []
    blocks: list[dict] = []
    steps: list[dict] = []
    patched_files: list[str] = []

    for part in parts:
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text") or ""
            if text:
                blocks.append({"type": "text", "text": text})
        elif ptype == "reasoning":
            text = part.get("text") or ""
            if text:
                blocks.append({
                    "type": "thinking",
                    "text": text,
                    "has_encrypted": _has_encrypted_reasoning(part),
                })
        elif ptype == "tool":
            blocks.append(_tool_block(part))
        elif ptype == "step-finish":
            steps.append(part)
        elif ptype == "patch":
            patched_files.extend(part.get("files") or [])
        elif ptype == "compaction":
            events.append(_compaction_event(part, ts))
        elif ptype == "retry":
            events.append({
                "kind": "notice",
                "ts": _iso_from_ms((part.get("time") or {}).get("created")) or ts,
                "label": f"Retry (attempt {part.get('attempt')})",
                "text": _error_text(part.get("error")),
            })
        # step-start/snapshot are pure bookkeeping.

    error = message.get("error")
    if error:
        events.append({
            "kind": "notice", "ts": ts, "label": "Error", "text": _error_text(error),
        })

    if not blocks:
        return events

    tokens = message.get("tokens") or {}
    cache = tokens.get("cache") or {}
    event = {
        "kind": "assistant",
        "ts": ts,
        "model": _model_name({
            "providerID": message.get("providerID"), "modelID": message.get("modelID"),
        }),
        "blocks": blocks,
        "usage": {
            "input": tokens.get("input"),
            "output": tokens.get("output"),
            "reasoning": tokens.get("reasoning"),
            "cache_read": cache.get("read"),
            "cache_write": cache.get("write"),
        },
    }
    metadata = {}
    if message.get("cost"):
        metadata["cost_usd"] = message["cost"]
    if message.get("finish"):
        metadata["finish"] = message["finish"]
    if message.get("variant"):
        metadata["variant"] = message["variant"]
    if message.get("agent"):
        metadata["agent"] = message["agent"]
    if patched_files:
        metadata["patched_files"] = patched_files
    if len(steps) > 1:
        metadata["steps"] = [_step_tokens(step) for step in steps]
    if metadata:
        event["turn_metadata"] = metadata
    events.insert(0, event)
    return events


def parse_session_by_id(session_id: str) -> dict | None:
    """Full parse of one opencode session addressed as ``opencode:<id>``."""
    conn = _connect()
    if conn is None:
        return None
    try:
        row = conn.execute("select * from session where id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        messages = conn.execute(
            "select id, data from message where session_id = ? order by time_created, id",
            (session_id,),
        ).fetchall()
        parts_by_message: dict[str, list[dict]] = {}
        for part_row in conn.execute(
            "select message_id, data from part where session_id = ? order by id",
            (session_id,),
        ):
            part = _loads(part_row["data"])
            if isinstance(part, dict):
                parts_by_message.setdefault(part_row["message_id"], []).append(part)
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    events: list[dict] = []
    models: list[str] = []
    for message_row in messages:
        message = _loads(message_row["data"])
        if not isinstance(message, dict):
            continue
        parts = parts_by_message.get(message_row["id"], [])
        ts = _iso_from_ms((message.get("time") or {}).get("created"))
        if message.get("role") == "user":
            events.extend(_user_events(parts, ts))
        else:
            events.extend(_assistant_events(message, parts, ts))
            model = _model_name({
                "providerID": message.get("providerID"), "modelID": message.get("modelID"),
            })
            if model and model not in models:
                models.append(model)

    title = row["title"] or ""
    if _is_placeholder_title(title):
        first = ""
        for event in events:
            if event.get("kind") == "user":
                first = next((b["text"] for b in event["blocks"] if b["type"] == "text"), "")
                break
        title = common.short_title(first) or "(untitled session)"

    meta = {}
    if row["directory"]:
        meta["cwd"] = row["directory"]
    session_model = _model_name(_loads(row["model"]))
    if session_model or models:
        meta["model"] = session_model or models[0]
    if row["version"]:
        meta["version"] = row["version"]
    if row["agent"]:
        meta["source"] = row["agent"]
    if row["cost"]:
        meta["cost_usd"] = row["cost"]

    out = {
        "agent": "opencode",
        "id": session_id,
        "title": title,
        "meta": meta,
        "events": events,
        "models": models,
        "n_records": len(messages),
    }
    if row["parent_id"]:
        out["is_subagent"] = True
        out["parent_id"] = row["parent_id"]
        out["parent_file"] = SESSION_SCHEME + row["parent_id"]
        out["subagent_type"] = row["agent"] or ""
    return out
