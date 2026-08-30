#!/usr/bin/env python3
"""Export opencode sessions as Codex-format rollout JSONL.

opencode keeps a complete transcript in its own SQLite database, so unlike the
Cursor exporter this is a format conversion rather than a recovery operation:
nothing here has to be dug out of a content-addressed blob chain. What it buys
you is that Codex-aware tooling can read an opencode session, and that the
provider's *encrypted reasoning* travels with it.

That reasoning sits inline on the part, in the per-provider metadata bag:

    part.metadata.<providerID>.reasoning_details[]
        {"type": "reasoning.summary",   "summary": "<one token>", …}   ← noise
        {"type": "reasoning.encrypted", "id": "rs_…", "data": "<opaque>",
         "format": "xai-responses-v1"}                                 ← this

The summary entries are a token-by-token copy of text the part already holds
whole; the encrypted entry is the real chain of thought, which only the
provider can read. A reasoning part's ``text`` is therefore a *summary* of
reasoning the transcript does not contain, and the pair maps cleanly onto a
Codex ``reasoning`` response_item: ``summary[0].summary_text`` plus
``encrypted_content``.

**The blobs are not necessarily OpenAI's.** Codex rollouts carry OpenAI
Responses-API reasoning items, but opencode records whatever its provider
returned and stamps the shape in ``format`` — grok through OpenRouter writes
``xai-responses-v1``, which is a faithful record but is *not* replayable
against the OpenAI Responses API. The formats seen in a session are reported
on export and recorded in ``session_meta.opencode.reasoning_formats`` so an
export can't be mistaken for an OpenAI-replayable one.

Mapping: the opencode session → ``session_meta`` (with any per-message
``system`` prompt as ``base_instructions``); each user turn → ``turn_context``
+ ``message`` + a ``user_message`` mirror; reasoning parts → ``reasoning``;
assistant text → ``message`` + an ``agent_message`` mirror; each tool part →
``function_call`` *and* ``function_call_output``, since opencode fuses the call
and its result into one record where Codex keeps two. Tool names and arguments
stay opencode's own (``read``/``filePath``), not renamed to Codex's — the
export records what the model was actually sent.

Unlike Cursor, every part carries its own clock, so timestamps are exact
rather than inferred from neighbouring records.

Usage::

    python3 codex_export/opencode_to_codex.py --list
    python3 codex_export/opencode_to_codex.py --out ~/opencode-rollouts
    python3 codex_export/opencode_to_codex.py --session <session-id> --out -
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

try:
    from .codex_rollout import as_text, iso, line, output_path, write_rollout
except ImportError:  # run directly as a script, not imported as a package
    from codex_rollout import as_text, iso, line, output_path, write_rollout

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

ORIGINATOR = "opencode"
EXPORTER_VERSION = "opencode-to-codex/1"

# Reasoning-detail entry that holds the provider's opaque blob, as opposed to
# the token-by-token `reasoning.summary` fragments beside it.
ENCRYPTED_DETAIL = "reasoning.encrypted"


# ---------------------------------------------------------------------------
# Store access
# ---------------------------------------------------------------------------
def open_db(path: Path) -> sqlite3.Connection:
    """Read-only connection — safe to run while opencode is open."""
    conn = sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _loads(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def iter_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every session row, oldest first."""
    return conn.execute("select * from session order by time_created").fetchall()


def session_turns(conn: sqlite3.Connection, session_id: str) -> list[tuple[dict, list[dict]]]:
    """(message, parts) for one session, in the order opencode wrote them."""
    parts_by_message: dict[str, list[dict]] = {}
    for row in conn.execute(
        "select message_id, data from part where session_id = ? order by id", (session_id,)
    ):
        part = _loads(row["data"])
        if isinstance(part, dict):
            parts_by_message.setdefault(row["message_id"], []).append(part)
    turns = []
    for row in conn.execute(
        "select id, data from message where session_id = ? order by time_created, id",
        (session_id,),
    ):
        message = _loads(row["data"])
        if isinstance(message, dict):
            turns.append((message, parts_by_message.get(row["id"], [])))
    return turns


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------
def encrypted_reasoning(part: dict) -> dict | None:
    """The provider's opaque reasoning item on this part, if it recorded one."""
    for provider in (part.get("metadata") or {}).values():
        if not isinstance(provider, dict):
            continue
        for detail in provider.get("reasoning_details") or []:
            if isinstance(detail, dict) and detail.get("type") == ENCRYPTED_DETAIL:
                return detail
    return None


def reasoning_formats(turns: list[tuple[dict, list[dict]]]) -> list[str]:
    """Distinct provider formats of the encrypted blobs in a session.

    Only reasoning parts are consulted: opencode stamps the same provider
    metadata onto every part written during a step, so the identical ``rs_…``
    entry also rides along on that step's tool parts.
    """
    formats = []
    for _message, parts in turns:
        for part in parts:
            if part.get("type") != "reasoning":
                continue
            detail = encrypted_reasoning(part)
            fmt = (detail or {}).get("format")
            if fmt and fmt not in formats:
                formats.append(fmt)
    return formats


def _reasoning_payload(part: dict) -> dict:
    text = part.get("text") or ""
    payload: dict = {"type": "reasoning"}
    detail = encrypted_reasoning(part)
    if detail and detail.get("id"):
        payload["id"] = detail["id"]
    payload["summary"] = [{"type": "summary_text", "text": text}] if text.strip() else []
    if detail and detail.get("data"):
        payload["encrypted_content"] = detail["data"]
    return payload


# ---------------------------------------------------------------------------
# Parts → response_items
# ---------------------------------------------------------------------------
def _user_payload(parts: list[dict]) -> dict:
    content = []
    for part in parts:
        kind = part.get("type")
        if kind == "text":
            content.append({"type": "input_text", "text": part.get("text") or ""})
        elif kind == "file":
            url = part.get("url") or ""
            if str(part.get("mime", "")).startswith("image/") and url:
                content.append({"type": "input_image", "image_url": url})
            else:
                content.append({
                    "type": "input_text",
                    "text": f"[attachment: {part.get('filename') or part.get('mime') or 'file'}]",
                })
    return {"type": "message", "role": "user", "content": content}


def _function_call_payload(part: dict) -> dict:
    state = part.get("state") or {}
    return {
        "type": "function_call",
        "id": part.get("id") or "",
        "name": part.get("tool") or "tool",
        "arguments": json.dumps(state.get("input") or {}, ensure_ascii=False),
        "call_id": part.get("callID") or "",
    }


def _function_output_payload(part: dict, output_mode: str) -> dict | None:
    """The tool's result, or None when opencode never recorded an outcome."""
    state = part.get("state") or {}
    status = state.get("status")
    if status == "completed":
        text = as_text(state.get("output"))
    elif status == "error":
        text = as_text(state.get("error"))
    else:
        return None  # pending / running: the call was made, nothing came back
    output = text if output_mode == "string" else [{"type": "input_text", "text": text}]
    return {
        "type": "function_call_output",
        "call_id": part.get("callID") or "",
        "output": output,
    }


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------
def _part_start(part: dict, fallback: str) -> str:
    """When the part began — opencode times every one of them."""
    time = part.get("time")
    if part.get("type") == "tool":
        time = (part.get("state") or {}).get("time")
    if isinstance(time, dict):
        return iso(time.get("start")) or fallback
    return fallback


def _part_end(part: dict, fallback: str) -> str:
    time = (part.get("state") or {}).get("time") if part.get("type") == "tool" else part.get("time")
    if isinstance(time, dict):
        return iso(time.get("end")) or iso(time.get("start")) or fallback
    return fallback


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
def _model_id(model) -> str:
    """opencode names this field `modelID` on messages but `id` on the session row."""
    if not isinstance(model, dict):
        return ""
    return model.get("modelID") or model.get("id") or ""


def _model_of(message: dict) -> str:
    model = message.get("model")
    if isinstance(model, dict):  # user messages carry {providerID, modelID}
        return _model_id(model)
    return message.get("modelID") or ""


def build_rollout(
    session: sqlite3.Row | dict,
    turns: list[tuple[dict, list[dict]]],
    *,
    tool_output: str = "list",
    event_msgs: bool = True,
) -> list[dict]:
    """Codex rollout records for one opencode session."""
    session = dict(session)
    session_id = session["id"]
    start = iso(session.get("time_created")) or iso(0)
    cwd = session.get("directory") or ""
    session_model = _loads(session.get("model")) or {}
    model = _model_id(session_model)
    provider = session_model.get("providerID") or ""

    meta = {
        "session_id": session_id,
        "id": session_id,
        "timestamp": start,
        "cwd": cwd,
        "originator": ORIGINATOR,
        "cli_version": EXPORTER_VERSION,
        "source": "opencode",
        "thread_source": "user",
        "model_provider": provider,
        "model": model,
        "opencode": {
            "session_id": session_id,
            "slug": session.get("slug") or "",
            "title": session.get("title") or "",
            "version": session.get("version") or "",
            "agent": session.get("agent") or "",
            "parent_id": session.get("parent_id") or None,
            # Which provider's reasoning shape the encrypted blobs are in.
            # Not OpenAI unless it says so — see the module docstring.
            "reasoning_formats": reasoning_formats(turns),
        },
    }
    system_prompt = next(
        (m.get("system") for m, _p in turns if isinstance(m.get("system"), str) and m["system"]),
        "",
    )
    if system_prompt:
        meta["base_instructions"] = {"text": system_prompt}
    lines = [line("session_meta", start, meta)]

    for message, parts in turns:
        created = iso((message.get("time") or {}).get("created")) or start
        turn_model = _model_of(message) or model

        if message.get("role") == "user":
            # Synthetic text is injected context (a background task's result
            # fed back in), not something the human typed.
            prompt = "\n".join(
                p.get("text") or "" for p in parts
                if p.get("type") == "text" and not p.get("synthetic") and not p.get("ignored")
            ).strip()
            if prompt:
                lines.append(line("turn_context", created, {
                    "cwd": cwd,
                    "model": turn_model,
                    "summary": session.get("title") or None,
                }))
            content_parts = [p for p in parts if p.get("type") in ("text", "file")]
            if content_parts:
                lines.append(line("response_item", created, _user_payload(content_parts)))
            if event_msgs and prompt:
                lines.append(line("event_msg", created, {
                    "type": "user_message", "message": prompt, "kind": "plain",
                }))
            continue

        for part in parts:
            kind = part.get("type")
            stamp = _part_start(part, created)
            if kind == "reasoning":
                if part.get("text") or encrypted_reasoning(part):
                    lines.append(line("response_item", stamp, _reasoning_payload(part)))
            elif kind == "text":
                text = part.get("text") or ""
                if not text:
                    continue
                lines.append(line("response_item", stamp, {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }))
                if event_msgs:
                    lines.append(line("event_msg", stamp, {
                        "type": "agent_message", "message": text,
                    }))
            elif kind == "tool":
                lines.append(line("response_item", stamp, _function_call_payload(part)))
                output = _function_output_payload(part, tool_output)
                if output is not None:
                    lines.append(line("response_item", _part_end(part, stamp), output))

        # A failed turn is recorded only here — there is no response_item
        # carrying it — so it survives --no-event-msg.
        error = message.get("error")
        if error:
            name = error.get("name") if isinstance(error, dict) else ""
            detail = (error.get("data") or {}).get("message") if isinstance(error, dict) else ""
            lines.append(line("event_msg", created, {
                "type": "error", "message": f"{name}: {detail}" if detail else as_text(error),
            }))

    return lines


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def export_session(
    conn: sqlite3.Connection,
    session: sqlite3.Row | dict,
    *,
    tool_output: str = "list",
    event_msgs: bool = True,
) -> tuple[list[dict], dict]:
    """Rollout records plus a small report about what was converted."""
    session = dict(session)
    turns = session_turns(conn, session["id"])
    lines = build_rollout(session, turns, tool_output=tool_output, event_msgs=event_msgs)
    reasoning = sum(
        1 for rec in lines
        if rec["type"] == "response_item" and rec["payload"].get("type") == "reasoning"
    )
    encrypted = sum(
        1 for rec in lines
        if rec["type"] == "response_item" and rec["payload"].get("encrypted_content")
    )
    calls = sum(
        1 for rec in lines
        if rec["type"] == "response_item" and rec["payload"].get("type") == "function_call"
    )
    outputs = sum(
        1 for rec in lines
        if rec["type"] == "response_item" and rec["payload"].get("type") == "function_call_output"
    )
    report = {
        "id": session["id"],
        "title": session.get("title") or "",
        "model": _model_id(_loads(session.get("model"))),
        "messages": len(turns),
        "records": len(lines),
        "reasoning": reasoning,
        "encrypted": encrypted,
        "formats": reasoning_formats(turns),
        # A call whose result never landed — the session was interrupted, or
        # is still running right now.
        "unfinished_calls": calls - outputs,
    }
    return lines, report


def select_sessions(
    conn: sqlite3.Connection,
    *,
    session_ids: set[str] | None = None,
    with_encrypted: bool = False,
) -> list[sqlite3.Row]:
    out = []
    for row in iter_sessions(conn):
        if session_ids is not None and row["id"] not in session_ids:
            continue
        if with_encrypted and not reasoning_formats(session_turns(conn, row["id"])):
            continue
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="opencode.db")
    parser.add_argument("--out", help="output directory, or - for stdout")
    parser.add_argument("--session", action="append", default=[], help="session id (repeatable)")
    parser.add_argument(
        "--with-encrypted",
        action="store_true",
        help="only sessions whose reasoning carries an encrypted blob",
    )
    parser.add_argument("--list", action="store_true", help="list matching sessions and exit")
    parser.add_argument(
        "--layout", choices=("codex", "flat"), default="codex", help="YYYY/MM/DD dirs, or flat"
    )
    parser.add_argument(
        "--tool-output",
        choices=("list", "string"),
        default="list",
        help="function_call_output shape (Codex ≥0.14x writes a list)",
    )
    parser.add_argument(
        "--no-event-msg",
        action="store_true",
        help="drop the user_message/agent_message mirrors (turn errors are kept: "
             "they have no response_item to mirror)",
    )
    args = parser.parse_args(argv)

    db = Path(args.db).expanduser()
    if not db.exists():
        print(f"no opencode database at {db}", file=sys.stderr)
        return 2
    conn = open_db(db)
    try:
        sessions = select_sessions(
            conn,
            session_ids=set(args.session) or None,
            with_encrypted=args.with_encrypted,
        )
        if not sessions:
            print("no matching sessions", file=sys.stderr)
            return 1

        if args.list:
            for row in sessions:
                formats = reasoning_formats(session_turns(conn, row["id"]))
                model = _model_id(_loads(row["model"]))
                print(
                    f"{row['id']}  {model:<28} "
                    f"{','.join(formats) if formats else '---':<20} {row['title'] or ''}"
                )
            return 0

        if not args.out:
            parser.error("--out is required (use - for stdout)")

        to_stdout = args.out == "-"
        out_dir = None if to_stdout else Path(args.out).expanduser()
        total = {"sessions": 0, "records": 0, "encrypted": 0}
        formats_seen: list[str] = []
        for row in sessions:
            lines, report = export_session(
                conn, row, tool_output=args.tool_output, event_msgs=not args.no_event_msg
            )
            if not lines[1:]:
                print(f"skip {row['id']}: no messages", file=sys.stderr)
                continue
            total["sessions"] += 1
            total["records"] += report["records"]
            total["encrypted"] += report["encrypted"]
            for fmt in report["formats"]:
                if fmt not in formats_seen:
                    formats_seen.append(fmt)
            if to_stdout:
                for record in lines:
                    print(json.dumps(record, ensure_ascii=False))
                continue
            path = output_path(out_dir, row["id"], lines[0]["timestamp"], args.layout)
            write_rollout(path, lines)
            print(
                f"{path}  {report['records']} records, {report['reasoning']} reasoning "
                f"({report['encrypted']} encrypted"
                + (f", {','.join(report['formats'])}" if report["formats"] else "")
                + ")"
                + (
                    f"  [{report['unfinished_calls']} tool calls without a result]"
                    if report["unfinished_calls"]
                    else ""
                )
            )
        if not to_stdout:
            print(
                f"\n{total['sessions']} sessions, {total['records']} records, "
                f"{total['encrypted']} encrypted reasoning items"
            )
            non_openai = [f for f in formats_seen if not f.startswith("openai")]
            if non_openai:
                print(
                    f"note: reasoning blobs are {', '.join(non_openai)} — a faithful record, "
                    "but not replayable against the OpenAI Responses API"
                )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
