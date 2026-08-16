#!/usr/bin/env python3
"""Export Cursor's GPT sessions as Codex-format rollout JSONL.

Cursor's IDE store keeps two representations of a conversation. The bubbles
(``bubbleId:<composer>:<bubble>``) are the *rendered* transcript — thinking
text with an empty signature, tool results shaped for the UI. Alongside them
Cursor also keeps the exact provider-format message array it sends to the
model, content-addressed out of line:

    composerData:<id>.conversationState  → protobuf, repeated 32-byte sha256
    agentKv:blob:<sha256>                → one message, as sent to the provider

For OpenAI models those blobs are the real thing: reasoning items carry the
``rs_…`` id and the ``gAAAAA…`` ``encrypted_content`` in ``signature`` (a
JSON-encoded copy of the provider's reasoning item), tool calls carry the
``call_…`` id plus the ``fc_…`` item id, and tool results carry their output.

This module converts those messages into Codex rollout JSONL — the same
``{timestamp, type, payload}`` lines Codex writes under ``~/.codex/sessions``:

    session_meta      once, with cwd/model/title and Cursor's system prompt
                      in ``base_instructions``
    turn_context      once per user turn, with the model used for that turn
    response_item     message / reasoning / function_call / function_call_output
    event_msg         user_message / agent_message mirrors (as Codex writes),
                      so the rollout renders in Codex-aware viewers

Reasoning maps 1:1 — ``summary[0].summary_text`` is Cursor's thinking text and
``encrypted_content`` is the provider's opaque blob, so an exported turn can be
replayed against the Responses API.

Usage::

    python3 cursor_to_codex.py --list
    python3 cursor_to_codex.py --out ~/cursor-rollouts
    python3 cursor_to_codex.py --session <composer-id> --out -
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sqlite3
import sys
from pathlib import Path

import cursor_binary
from codex_rollout import (
    as_text as _as_text,
    iso as _iso,
    line as _line,
    output_path,
    rollout_filename,
    write_rollout,
)

DEFAULT_DB_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "Cursor"
    / "User"
    / "globalStorage"
    / "state.vscdb"
)

# Which composers count as "GPT" by default, matched against the composer's
# model name. Cursor's own models (composer-*), Claude, Grok and GLM all store
# a provider-opaque reasoning signature instead of OpenAI's rs_/encrypted pair.
DEFAULT_MODEL_REGEX = r"(?i)^(gpt|o[1-4]\b|codex)"

ORIGINATOR = "cursor-ide"
EXPORTER_VERSION = "cursor-to-codex/1"

# Cursor wraps the human's prompt in a large context envelope (open files,
# rules, skills, git status). Codex splits the same way: the response_item
# keeps the envelope, the event_msg mirror carries what the user typed.
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL | re.IGNORECASE)

# Leading bytes → image subtype, for Cursor's raw Uint8Array image blocks.
_IMAGE_MAGIC = (
    ("ffd8ff", "jpeg"),
    ("89504e47", "png"),
    ("47494638", "gif"),
    ("52494646", "webp"),
    ("424d", "bmp"),
)


# ---------------------------------------------------------------------------
# Store access
# ---------------------------------------------------------------------------
def open_db(path: Path) -> sqlite3.Connection:
    """Read-only connection — safe to run while Cursor is open."""
    return sqlite3.connect(f"file:{Path(path).expanduser()}?mode=ro", uri=True)


def _loads(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def iter_composers(conn: sqlite3.Connection):
    """(composer_id, composerData) for every conversation in the store."""
    for key, value in conn.execute(
        "select key, value from cursorDiskKV where key like 'composerData:%'"
    ):
        data = _loads(value)
        if isinstance(data, dict):
            yield key.split(":", 1)[1], data


def composer_model(composer: dict) -> str:
    return ((composer.get("modelConfig") or {}).get("modelName") or "").strip()


def composer_cwd(composer: dict) -> str:
    uri = (composer.get("workspaceIdentifier") or {}).get("uri") or {}
    if uri.get("fsPath"):
        return uri["fsPath"]
    if uri.get("path"):
        return uri["path"]
    repos = composer.get("trackedGitRepos") or []
    if repos and isinstance(repos[0], dict) and repos[0].get("repoPath"):
        return repos[0]["repoPath"]
    return ""


def session_messages(conn: sqlite3.Connection, composer: dict) -> tuple[list[dict], int]:
    """Provider-format messages for a composer, plus the unresolved blob count."""
    messages: list[dict] = []
    missing = 0
    for digest in cursor_binary.conversation_state_hashes(composer.get("conversationState") or ""):
        row = conn.execute(
            "select value from cursorDiskKV where key=?", (f"agentKv:blob:{digest}",)
        ).fetchone()
        obj = _loads(row[0]) if row else None
        if isinstance(obj, dict) and obj.get("role"):
            messages.append(obj)
        else:
            missing += 1
    return messages, missing


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------
def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def has_openai_reasoning(messages: list[dict]) -> bool:
    """True when reasoning blocks carry an OpenAI ``rs_…``/encrypted item."""
    for message in messages:
        for block in _blocks(message):
            if block.get("type") != "reasoning":
                continue
            item = _reasoning_item(block)
            if item and item.get("encrypted_content"):
                return True
    return False


def _reasoning_item(block: dict) -> dict | None:
    """Cursor stashes the provider's reasoning item, JSON-encoded, in ``signature``."""
    signature = block.get("signature")
    if not isinstance(signature, str) or not signature.startswith("{"):
        return None
    item = _loads(signature)
    return item if isinstance(item, dict) else None


def message_model(message: dict) -> str:
    """Per-message model — Cursor can switch models mid-conversation."""
    options = message.get("providerOptions")
    if isinstance(options, dict):
        cursor_options = options.get("cursor")
        if isinstance(cursor_options, dict):
            name = cursor_options.get("modelName")
            if isinstance(name, str) and name:
                return name
    for block in _blocks(message):
        options = block.get("providerOptions")
        if isinstance(options, dict):
            cursor_options = options.get("cursor")
            if isinstance(cursor_options, dict) and cursor_options.get("modelName"):
                return cursor_options["modelName"]
    return ""


def _message_text(message: dict) -> str:
    return "\n".join(
        b.get("text") or "" for b in _blocks(message) if b.get("type") == "text"
    )


def user_query_text(message: dict) -> str:
    """What the human actually typed, or "" for a context-only user message."""
    match = _USER_QUERY_RE.search(_message_text(message))
    return match.group(1).strip() if match else ""


def _split_call_id(raw) -> tuple[str, str]:
    """Cursor joins the provider call id and item id with a newline."""
    if not isinstance(raw, str):
        return "", ""
    call_id, _, item_id = raw.partition("\n")
    return call_id.strip(), item_id.strip()


def _image_data_url(image) -> str | None:
    """Cursor stores pasted images as ``{__type: Uint8Array, hex: …}``."""
    if isinstance(image, str):
        return image if image.startswith("data:") else None
    if not isinstance(image, dict):
        return None
    hex_data = image.get("hex")
    if isinstance(hex_data, str) and hex_data:
        try:
            raw = bytes.fromhex(hex_data)
        except ValueError:
            return None
        subtype = next(
            (name for magic, name in _IMAGE_MAGIC if hex_data.lower().startswith(magic)), "png"
        )
        return f"data:image/{subtype};base64," + base64.b64encode(raw).decode("ascii")
    for key in ("base64", "base64Data", "data"):
        value = image.get(key)
        if isinstance(value, str) and value:
            return value if value.startswith("data:") else f"data:image/png;base64,{value}"
    return None


def _tool_call_times(composer: dict) -> dict[str, str]:
    """toolCallId → bubble creation time, the only per-item clock Cursor keeps."""
    times: dict[str, str] = {}
    for header in composer.get("fullConversationHeadersOnly") or []:
        grouping = header.get("grouping") or {}
        call_id = grouping.get("toolCallId")
        stamp = _iso(header.get("createdAt"))
        if isinstance(call_id, str) and call_id and stamp:
            times.setdefault(call_id, stamp)
    return times


def _user_bubble_times(composer: dict) -> list[str]:
    return [
        stamp
        for stamp in (
            _iso(h.get("createdAt"))
            for h in composer.get("fullConversationHeadersOnly") or []
            if h.get("type") == 1
        )
        if stamp
    ]


def _message_times(messages: list[dict], composer: dict, start: str) -> list[str]:
    """One timestamp per message: exact where a bubble matches, carried forward otherwise.

    Provider messages themselves are untimed — only bubbles are. Two anchors
    tie them together: tool traffic matches a bubble by toolCallId (older
    composers don't record one), and the n-th prompt matches the n-th user
    bubble. Prompts are aligned from the end because a compacted
    conversationState keeps the most recent turns, not the first ones.
    Anything between two anchors inherits the last known time.
    """
    tool_times = _tool_call_times(composer)
    stamps: list[str | None] = [None] * len(messages)
    for i, message in enumerate(messages):
        for block in _blocks(message):
            raw = block.get("toolCallId")
            if isinstance(raw, str) and raw in tool_times:
                stamps[i] = tool_times[raw]
                break
    prompts = [
        i for i, m in enumerate(messages) if m.get("role") == "user" and user_query_text(m)
    ]
    for i, stamp in zip(reversed(prompts), reversed(_user_bubble_times(composer))):
        stamps[i] = stamp
    last = start
    for i, stamp in enumerate(stamps):
        if stamp:
            last = stamp
        else:
            stamps[i] = last
    return [s or start for s in stamps]


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------
def _user_payload(message: dict) -> dict:
    content = []
    for block in _blocks(message):
        kind = block.get("type")
        if kind == "text":
            content.append({"type": "input_text", "text": block.get("text") or ""})
        elif kind == "image":
            url = _image_data_url(block.get("image"))
            if url:
                content.append({"type": "input_image", "image_url": url})
            else:
                content.append({"type": "input_text", "text": "[unsupported image]"})
        else:
            content.append({"type": "input_text", "text": _as_text(block)})
    payload = {"type": "message", "role": "user", "content": content}
    if isinstance(message.get("id"), str):
        payload["id"] = message["id"]
    return payload


def _reasoning_payload(block: dict) -> dict:
    text = block.get("text") or ""
    payload: dict = {"type": "reasoning"}
    item = _reasoning_item(block)
    if item and item.get("id"):
        payload["id"] = item["id"]
    payload["summary"] = [{"type": "summary_text", "text": text}] if text.strip() else []
    if item:
        if item.get("encrypted_content"):
            payload["encrypted_content"] = item["encrypted_content"]
        if item.get("content"):
            payload["content"] = item["content"]
    elif isinstance(block.get("signature"), str) and block["signature"]:
        # Non-OpenAI providers store one opaque string instead of an item.
        payload["encrypted_content"] = block["signature"]
    return payload


def _redacted_reasoning_payload(block: dict) -> dict:
    return {
        "type": "reasoning",
        "summary": [],
        "encrypted_content": block.get("data") or "",
    }


def _function_call_payload(block: dict) -> dict:
    call_id, item_id = _split_call_id(block.get("toolCallId"))
    args = block.get("args")
    payload: dict = {"type": "function_call"}
    if item_id:
        payload["id"] = item_id
    payload["name"] = block.get("toolName") or "tool"
    payload["arguments"] = args if isinstance(args, str) else json.dumps(args or {}, ensure_ascii=False)
    payload["call_id"] = call_id
    return payload


def _function_output_payload(block: dict, output_mode: str) -> dict:
    call_id, _ = _split_call_id(block.get("toolCallId"))
    parts: list[dict] = []
    experimental = block.get("experimental_content")
    if isinstance(experimental, list):
        for part in experimental:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                parts.append({"type": "input_text", "text": part.get("text") or ""})
            elif part.get("type") == "image":
                url = _image_data_url(part.get("image") or part.get("data"))
                if url:
                    parts.append({"type": "input_image", "image_url": url})
    if not parts:
        parts = [{"type": "input_text", "text": _as_text(block.get("result"))}]
    if output_mode == "string":
        output = "\n".join(p["text"] for p in parts if p.get("type") == "input_text")
    else:
        output = parts
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _turn_models(messages: list[dict], default: str) -> list[str]:
    """Model in force at each index — the next assistant reply's, if it names one."""
    models = [default] * len(messages)
    current = default
    for i in range(len(messages) - 1, -1, -1):
        current = message_model(messages[i]) or current
        models[i] = current
    return models


def build_rollout(
    composer_id: str,
    composer: dict,
    messages: list[dict],
    *,
    tool_output: str = "list",
    event_msgs: bool = True,
) -> list[dict]:
    """Codex rollout records for one Cursor conversation."""
    start = _iso(composer.get("createdAt")) or _iso(composer.get("lastUpdatedAt")) or _iso(0)
    stamps = _message_times(messages, composer, start)
    cwd = composer_cwd(composer)
    model = composer_model(composer)
    system_prompt = next(
        (
            _as_text(m.get("content"))
            for m in messages
            if m.get("role") == "system"
        ),
        "",
    )

    meta = {
        "session_id": composer_id,
        "id": composer_id,
        "timestamp": start,
        "cwd": cwd,
        "originator": ORIGINATOR,
        "cli_version": EXPORTER_VERSION,
        "source": "cursor",
        "thread_source": "user",
        "model_provider": "openai",
        "model": model,
        "cursor": {
            "composer_id": composer_id,
            "model": model,
            "title": composer.get("name") or "",
            "last_updated_at": _iso(composer.get("lastUpdatedAt")),
        },
    }
    if system_prompt:
        meta["base_instructions"] = {"text": system_prompt}
    lines = [_line("session_meta", start, meta)]

    turn_models = _turn_models(messages, model)
    for index, (message, stamp) in enumerate(zip(messages, stamps)):
        role = message.get("role")
        if role == "system":
            continue  # carried in session_meta.base_instructions

        if role == "user":
            # Only a message carrying a prompt starts a turn; the rest are
            # context injections Cursor sends in the user slot.
            if user_query_text(message):
                lines.append(
                    _line(
                        "turn_context",
                        stamp,
                        {
                            "cwd": cwd,
                            "model": turn_models[index],
                            "summary": composer.get("name") or None,
                        },
                    )
                )
            payload = _user_payload(message)
            lines.append(_line("response_item", stamp, payload))
            # Mirror only real prompts: context-only user messages stay
            # response_items, which is how Codex-aware readers tell the
            # human's turn from an injected envelope.
            prompt = user_query_text(message)
            if event_msgs and prompt:
                lines.append(
                    _line(
                        "event_msg",
                        stamp,
                        {"type": "user_message", "message": prompt, "kind": "plain"},
                    )
                )
            continue

        if role == "assistant":
            texts = [
                b.get("text") or "" for b in _blocks(message) if b.get("type") == "text"
            ]
            emitted_text = False
            for block in _blocks(message):
                kind = block.get("type")
                if kind == "reasoning":
                    lines.append(_line("response_item", stamp, _reasoning_payload(block)))
                elif kind == "redacted-reasoning":
                    lines.append(_line("response_item", stamp, _redacted_reasoning_payload(block)))
                elif kind == "text":
                    if emitted_text:
                        continue
                    emitted_text = True
                    payload = {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": t} for t in texts],
                    }
                    if isinstance(message.get("id"), str):
                        payload["id"] = message["id"]
                    lines.append(_line("response_item", stamp, payload))
                    if event_msgs:
                        lines.append(
                            _line(
                                "event_msg",
                                stamp,
                                {"type": "agent_message", "message": "\n".join(texts)},
                            )
                        )
                elif kind == "tool-call":
                    lines.append(_line("response_item", stamp, _function_call_payload(block)))
            continue

        if role == "tool":
            for block in _blocks(message):
                if block.get("type") == "tool-result":
                    lines.append(
                        _line("response_item", stamp, _function_output_payload(block, tool_output))
                    )
            continue

    return lines


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def select_sessions(
    conn: sqlite3.Connection, *, model_regex: str | None, session_ids: set[str] | None
) -> list[tuple[str, dict]]:
    pattern = re.compile(model_regex) if model_regex else None
    out = []
    for composer_id, composer in iter_composers(conn):
        if session_ids is not None and composer_id not in session_ids:
            continue
        if not (composer.get("fullConversationHeadersOnly") or []):
            continue  # draft with no turns
        if pattern and not pattern.search(composer_model(composer)):
            continue
        out.append((composer_id, composer))
    out.sort(key=lambda pair: pair[1].get("createdAt") or 0)
    return out


def export_session(
    conn: sqlite3.Connection,
    composer_id: str,
    composer: dict,
    *,
    tool_output: str = "list",
    event_msgs: bool = True,
) -> tuple[list[dict], dict]:
    """Rollout records plus a small report about what was converted."""
    messages, missing = session_messages(conn, composer)
    lines = build_rollout(
        composer_id, composer, messages, tool_output=tool_output, event_msgs=event_msgs
    )
    reasoning = sum(
        1
        for line in lines
        if line["type"] == "response_item" and line["payload"].get("type") == "reasoning"
    )
    encrypted = sum(
        1
        for line in lines
        if line["type"] == "response_item" and line["payload"].get("encrypted_content")
    )
    prompts = sum(1 for m in messages if m.get("role") == "user" and user_query_text(m))
    bubble_turns = len(_user_bubble_times(composer))
    report = {
        "id": composer_id,
        "title": composer.get("name") or "",
        "model": composer_model(composer),
        "messages": len(messages),
        "missing_blobs": missing,
        "records": len(lines),
        "reasoning": reasoning,
        "encrypted": encrypted,
        "openai_reasoning": has_openai_reasoning(messages),
        # conversationState holds the live context window: if Cursor compacted
        # the thread, the oldest turns are gone from it (the bubbles still
        # have them, without the encrypted reasoning).
        "dropped_turns": max(0, bubble_turns - prompts),
    }
    return lines, report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="Cursor state.vscdb")
    parser.add_argument("--out", help="output directory, or - for stdout")
    parser.add_argument("--session", action="append", default=[], help="composer id (repeatable)")
    parser.add_argument(
        "--model-regex",
        default=DEFAULT_MODEL_REGEX,
        help=f"model filter, default {DEFAULT_MODEL_REGEX!r}",
    )
    parser.add_argument("--all-models", action="store_true", help="export every model, not just GPT")
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
        "--no-event-msg", action="store_true", help="response_items only, no event_msg mirrors"
    )
    args = parser.parse_args(argv)

    if not Path(args.db).expanduser().exists():
        print(f"no Cursor store at {args.db}", file=sys.stderr)
        return 2
    conn = open_db(args.db)
    sessions = select_sessions(
        conn,
        model_regex=None if args.all_models else args.model_regex,
        session_ids=set(args.session) or None,
    )
    if not sessions:
        print("no matching sessions", file=sys.stderr)
        return 1

    if args.list:
        for composer_id, composer in sessions:
            messages, missing = session_messages(conn, composer)
            print(
                f"{composer_id}  {composer_model(composer):<20} "
                f"{len(messages):>5} msgs  {'enc' if has_openai_reasoning(messages) else '---'}"
                f"{'  MISSING ' + str(missing) if missing else ''}  {composer.get('name') or ''}"
            )
        return 0

    if not args.out:
        parser.error("--out is required (use - for stdout)")

    to_stdout = args.out == "-"
    out_dir = None if to_stdout else Path(args.out).expanduser()
    total = {"sessions": 0, "records": 0, "encrypted": 0, "missing": 0}
    for composer_id, composer in sessions:
        lines, report = export_session(
            conn,
            composer_id,
            composer,
            tool_output=args.tool_output,
            event_msgs=not args.no_event_msg,
        )
        if not lines[1:]:
            print(f"skip {composer_id}: no messages in conversationState", file=sys.stderr)
            continue
        total["sessions"] += 1
        total["records"] += report["records"]
        total["encrypted"] += report["encrypted"]
        total["missing"] += report["missing_blobs"]
        if to_stdout:
            for line in lines:
                print(json.dumps(line, ensure_ascii=False))
            continue
        path = output_path(out_dir, composer_id, lines[0]["timestamp"], args.layout)
        write_rollout(path, lines)
        print(
            f"{path}  {report['records']} records, {report['reasoning']} reasoning "
            f"({report['encrypted']} encrypted)"
            + (f"  [{report['missing_blobs']} blobs missing]" if report["missing_blobs"] else "")
            + (
                f"  [compacted: {report['dropped_turns']} earlier turns not in "
                "conversationState]"
                if report["dropped_turns"]
                else ""
            )
        )
    if not to_stdout:
        print(
            f"\n{total['sessions']} sessions, {total['records']} records, "
            f"{total['encrypted']} encrypted reasoning items"
            + (f", {total['missing']} unresolved blobs" if total["missing"] else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
