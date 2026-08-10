#!/usr/bin/env python3
"""The event contract between the parsers and the frontend.

Every parser (claude_parser, codex_parser, cursor_parser) emits a session as
``{"agent", "id", "title", "meta", "events", ...}`` where ``events`` is a flat
list of dicts, each with a ``kind``. The frontend's renderEvent() dispatches on
``kind``; this module is the single written-down description of what each kind
carries, plus a validator the test suite runs over every parser's output so
the contract can't silently drift.

Two message shapes exist for ``user``/``assistant`` events:

- **Block shape** (Claude Code and Cursor): a ``blocks`` list whose items are
  ``{"type": "text"|"thinking"|"image"|"tool_use", ...}``. Tool calls ride
  inside the assistant turn, with their result attached on the block.
- **Flat shape** (Codex): plain ``text`` on the event; reasoning and tool
  calls arrive as separate top-level events (``reasoning``, ``tool``).

Everything the frontend renders must be reachable from one of the kinds below.
``ts`` (an ISO timestamp or null) may appear on any event; extra fields beyond
the documented ones are allowed (the validator checks presence and types of
the load-bearing ones, not exhaustiveness).

Kinds
-----
- ``user``        — a real user prompt. Block or flat shape; flat shape may
                    carry ``images``/``local_images`` payloads.
- ``assistant``   — a model turn. Block or flat shape; may carry
                    ``turn_metadata`` (folded Codex bookkeeping) and ``usage``.
- ``reasoning``   — Codex reasoning summary: ``text``, ``has_encrypted``.
- ``tool``        — Codex tool call: ``name``, ``input``, ``summary``,
                    ``result`` (dict or null).
- ``web_search`` / ``web_call`` — a web search: ``query`` and/or ``action``,
                    plus ``results`` when the transcript recorded the hits.
- ``instructions``— injected system prompt/context: ``role``, ``label``,
                    ``text``.
- ``system``      — system record; ``subtype == "compact_boundary"`` carries
                    a ``compaction`` stats dict.
- ``notice``      — a system-injected message recorded as a user record but
                    not a real prompt: ``label``, ``text``.
- ``attachment``  — Claude Code attachment (hook output, re-attached file,
                    queued command…): ``att_type`` plus type-specific fields.
- ``guardian_request`` / ``guardian_decision`` — Codex guardian review turns.
- ``status`` / ``context`` / ``tokens`` — Codex turn bookkeeping (usually
                    folded into ``turn_metadata`` instead of standalone).
- ``raw``         — unrecognized record, preserved verbatim: ``record_type``,
                    ``payload``.
- ``branch``      — abandoned conversation branches folded at a fork:
                    ``groups`` (list of lists of events), ``count``.
"""

from __future__ import annotations

# Every kind a parser may emit. The frontend must have a renderEvent() case
# for each of these (test_event_schema checks app.js against this set).
KINDS = {
    "user", "assistant", "reasoning", "tool", "web_search", "web_call",
    "instructions", "system", "notice", "attachment",
    "guardian_request", "guardian_decision",
    "status", "context", "tokens", "raw", "branch",
}

BLOCK_TYPES = {"text", "thinking", "image", "tool_use"}


def _check(errors: list, cond: bool, where: str, message: str) -> None:
    if not cond:
        errors.append(f"{where}: {message}")


def _validate_block(b, where: str, errors: list) -> None:
    if not isinstance(b, dict):
        errors.append(f"{where}: block is not a dict")
        return
    btype = b.get("type")
    _check(errors, btype in BLOCK_TYPES, where, f"unknown block type {btype!r}")
    if btype in ("text", "thinking"):
        _check(errors, isinstance(b.get("text"), str), where, "text block needs str 'text'")
    elif btype == "image":
        _check(errors, isinstance(b.get("data_uri", ""), str), where, "'data_uri' must be str")
    elif btype == "tool_use":
        _check(errors, "name" in b, where, "tool_use block needs 'name'")
        _check(errors, "input" in b, where, "tool_use block needs 'input'")
        result = b.get("result")
        if result is not None:
            _check(errors, isinstance(result, dict), where, "tool result must be dict or null")
            if isinstance(result, dict):
                _check(errors, "text" in result, where, "tool result needs 'text'")
                _check(errors, isinstance(result.get("images", []), list), where,
                       "tool result 'images' must be a list")


def validate_event(ev, where: str = "event") -> list[str]:
    """Return a list of contract violations for one event ('' problems == valid)."""
    errors: list[str] = []
    if not isinstance(ev, dict):
        return [f"{where}: event is not a dict"]
    kind = ev.get("kind")
    if kind not in KINDS:
        return [f"{where}: unknown kind {kind!r}"]
    where = f"{where}[{kind}]"

    if kind in ("user", "assistant"):
        if "blocks" in ev:
            blocks = ev.get("blocks")
            _check(errors, isinstance(blocks, list) and blocks, where, "'blocks' must be a non-empty list")
            for i, b in enumerate(blocks if isinstance(blocks, list) else []):
                _validate_block(b, f"{where}.blocks[{i}]", errors)
        else:
            _check(errors, isinstance(ev.get("text"), str), where,
                   "flat-shape event needs str 'text'")
    elif kind == "reasoning":
        _check(errors, isinstance(ev.get("text"), str), where, "needs str 'text'")
        _check(errors, isinstance(ev.get("has_encrypted"), bool), where, "needs bool 'has_encrypted'")
    elif kind == "tool":
        _check(errors, isinstance(ev.get("name"), str), where, "needs str 'name'")
        _check(errors, "input" in ev, where, "needs 'input'")
        _check(errors, "result" in ev, where, "needs 'result' (may be null)")
        _check(errors, isinstance(ev.get("summary", ""), str), where, "'summary' must be str")
    elif kind in ("web_search", "web_call"):
        _check(errors, "query" in ev or "action" in ev, where, "needs 'query' or 'action'")
    elif kind == "instructions":
        for field in ("role", "label", "text"):
            _check(errors, isinstance(ev.get(field), str), where, f"needs str '{field}'")
    elif kind == "system":
        _check(errors, isinstance(ev.get("text"), str), where, "needs str 'text'")
        if ev.get("subtype") == "compact_boundary":
            _check(errors, isinstance(ev.get("compaction"), dict), where,
                   "compact_boundary needs dict 'compaction'")
    elif kind == "notice":
        _check(errors, isinstance(ev.get("label"), str), where, "needs str 'label'")
        _check(errors, isinstance(ev.get("text"), str), where, "needs str 'text'")
    elif kind == "attachment":
        _check(errors, "att_type" in ev, where, "needs 'att_type'")
    elif kind == "guardian_request":
        _check(errors, isinstance(ev.get("request"), dict), where, "needs dict 'request'")
    elif kind == "guardian_decision":
        _check(errors, ev.get("outcome") in ("allow", "deny"), where,
               "'outcome' must be allow|deny")
    elif kind == "status":
        _check(errors, ev.get("status") in ("started", "complete", "aborted"), where,
               f"unknown status {ev.get('status')!r}")
    elif kind == "context":
        _check(errors, "turn_id" in ev, where, "needs 'turn_id'")
    elif kind == "tokens":
        _check(errors, isinstance(ev.get("usage"), dict), where, "needs dict 'usage'")
    elif kind == "raw":
        _check(errors, "record_type" in ev, where, "needs 'record_type'")
        _check(errors, "payload" in ev, where, "needs 'payload'")
    elif kind == "branch":
        groups = ev.get("groups")
        _check(errors, isinstance(groups, list) and groups, where, "'groups' must be a non-empty list")
        _check(errors, isinstance(ev.get("count"), int), where, "needs int 'count'")
        for gi, group in enumerate(groups if isinstance(groups, list) else []):
            if not isinstance(group, list):
                errors.append(f"{where}.groups[{gi}]: group is not a list")
                continue
            for ei, sub in enumerate(group):
                errors.extend(validate_event(sub, f"{where}.groups[{gi}][{ei}]"))
    return errors


# Fields every sidebar summary must carry, for every agent and source.
REQUIRED_SUMMARY_FIELDS = {
    "agent", "id", "file", "title", "cwd", "mtime",
    "n_user", "n_assistant", "n_tool", "n_records",
}


def validate_summary(summary, where: str = "summary") -> list[str]:
    errors: list[str] = []
    if not isinstance(summary, dict):
        return [f"{where}: summary is not a dict"]
    missing = REQUIRED_SUMMARY_FIELDS - set(summary)
    _check(errors, not missing, where, f"missing required fields {sorted(missing)}")
    _check(errors, summary.get("agent") in ("claude", "codex", "cursor"), where,
           f"unknown agent {summary.get('agent')!r}")
    return errors


def validate_session(data, where: str = "session") -> list[str]:
    """Validate a full parse_session()-style payload."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"{where}: session is not a dict"]
    _check(errors, data.get("agent") in ("claude", "codex", "cursor"), where,
           f"unknown agent {data.get('agent')!r}")
    _check(errors, isinstance(data.get("id"), str) and data.get("id"), where, "needs str 'id'")
    _check(errors, isinstance(data.get("title"), str), where, "needs str 'title'")
    _check(errors, isinstance(data.get("meta"), dict), where, "needs dict 'meta'")
    events = data.get("events")
    _check(errors, isinstance(events, list), where, "needs list 'events'")
    for i, ev in enumerate(events if isinstance(events, list) else []):
        errors.extend(validate_event(ev, f"{where}.events[{i}]"))
    return errors
