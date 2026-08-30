#!/usr/bin/env python3
"""Claude Code transcript parsing library.

Reads Claude Code session transcripts stored under ~/.claude/projects
(including per-session sub-agent files under
``<project>/<session-id>/subagents/``). This module is imported by server.py
(the unified transcript browser); it exposes list_sessions() /
parse_session() and the helpers they need.

Call configure(projects_dir) once at startup to point it at a projects
directory other than the default ~/.claude/projects.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import common

DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Default; override via configure().
PROJECTS_DIR = DEFAULT_PROJECTS_DIR

SUMMARY_CACHE = common.SummaryCache()
_PARALLEL_SCAN_THRESHOLD = 32

# Record types the transcript deliberately does not render: session-level
# bookkeeping with no conversational content, plus the title/branch records
# the event loop consumes earlier (listed again here so a degenerate one with
# an empty field stays quiet too). Anything not handled and not listed is
# surfaced as a raw card, so a Claude Code format change shows up in the
# transcript instead of vanishing.
_IGNORED_RECORD_TYPES = frozenset({
    "atis-latch",           # opaque session token
    "bridge-session",       # cloud-session bridging pointer
    "cost-state",           # running cost/usage totals
    "file-history-delta",   # file-backup bookkeeping for /rewind
    "mode",                 # mode-change notices (plan/normal/…)
    "queue-operation",      # queued-prompt bookkeeping; content re-enters as
                            # attachments/user turns when dequeued
    "summary",              # legacy compact-summary title pointers
    # consumed earlier in the loop:
    "agent-name", "ai-title", "custom-title", "last-prompt",
    "permission-mode", "pr-link", "file-history-snapshot",
})


def configure(projects_dir: Path) -> None:
    """Point the module at a Claude Code projects directory."""
    global PROJECTS_DIR
    PROJECTS_DIR = Path(projects_dir).expanduser()
    SUMMARY_CACHE.clear()
    _AGENT_CALLS_CACHE.clear()


def decode_project_name(dirname: str) -> str:
    """Claude Code encodes the project cwd by replacing '/' with '-'.

    The original path isn't perfectly recoverable (dashes in real names are
    ambiguous), but we can produce a readable best-effort path.
    """
    if dirname.startswith("-"):
        return "/" + dirname[1:].replace("-", "/")
    return dirname.replace("-", "/")


# ---------- what counts as a "real" user message ----------
# Claude Code records several system-injected messages as `user` records (or as
# queued-command attachments) even though the user never typed them: background-
# task notifications, slash-command machinery, hook output, and standalone system
# reminders. They all open with a recognizable wrapper tag. We detect them in one
# place so the title, the user count, and the rendered outline all agree on which
# turns are genuine prompts. To support a new wrapper, add its opening tag here.
_SYNTHETIC_USER_LABELS = {
    "task-notification": "Background task",
    "command-name": "Slash command",
    "command-message": "Slash command",
    "command-args": "Slash command",
    "local-command-stdout": "Command output",
    "local-command-stderr": "Command output",
    "local-command-caveat": "Command caveat",
    "system-reminder": "System reminder",
    "user-prompt-submit-hook": "Hook",
}


_content_text = common.content_text


def _user_record_text(rec: dict) -> str:
    """Concatenated text of a `user` record's content."""
    return _content_text(rec.get("message", {}).get("content"))


def _content_blocks(content) -> tuple[list[dict], bool]:
    """(render blocks, has_content) from a user-message content — a plain
    string or a list of text/image blocks. Every emitted text block carries a
    plain string even when the source block's text is missing or null."""
    blocks: list[dict] = []
    has_content = False
    if isinstance(content, str):
        blocks.append({"type": "text", "text": content})
        has_content = bool(content.strip())
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                blocks.append({"type": "text", "text": b.get("text") or ""})
                has_content = True
            elif b.get("type") == "image":
                src = b.get("source", {})
                data_uri = ""
                if src.get("type") == "base64":
                    data_uri = f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"
                blocks.append({"type": "image", "data_uri": data_uri})
                has_content = True
    return blocks, has_content


def _synthetic_user_notice(text) -> dict | None:
    """If user-authored ``text`` is actually a system-injected wrapper rather than
    a real prompt, return a ``{label, text}`` notice; otherwise ``None``.

    The full message is surfaced verbatim (the frontend renders it as escaped
    preformatted text, so the angle-bracket markup is preserved, not dropped).
    """
    if not isinstance(text, str):
        return None
    m = re.match(r"<([a-zA-Z0-9_-]+)", text.lstrip())
    if not m:
        return None
    label = _SYNTHETIC_USER_LABELS.get(m.group(1))
    if not label:
        return None
    return {"label": label, "text": text.strip()}


def _first_user_text(records: list[dict]) -> str:
    """First real user prompt text (skip tool results / command noise)."""
    for rec in records:
        if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
            continue
        text = _user_record_text(rec)
        if not text or _synthetic_user_notice(text):
            continue
        text = re.sub(r"<[^>]+>", " ", text)
        text = " ".join(text.split())
        if text:
            return common.short_title(text)
    return ""


def _subagent_first_user_text(records: list[dict]) -> str:
    """First user prompt in a sub-agent transcript (every message is a sidechain).

    Returns the full text (used both for the title and for matching the prompt
    back to its spawning Task/Agent call).
    """
    for rec in records:
        if rec.get("type") != "user":
            continue
        text = " ".join(_user_record_text(rec).split())
        if text:
            return text
    return ""


def _subagent_parent(path: Path):
    """If `path` is a sub-agent transcript, return (parent_id, parent_file).

    Newer Claude Code writes each sub-agent to
    ``<project>/<session-id>/subagents/agent-<id>.jsonl``; the parent session
    file sits at ``<project>/<session-id>.jsonl``.
    """
    if path.parent.name != "subagents":
        return None
    parent_id = path.parent.parent.name
    parent_file = path.parent.parent.parent / f"{parent_id}.jsonl"
    return parent_id, parent_file


# Cache: parent path -> (mtime, [ {prompt, description, subagent_type} ])
_AGENT_CALLS_CACHE: dict[str, tuple[float, list]] = {}


def _agent_calls(parent_path: Path) -> list[dict]:
    """Task/Agent tool calls in a parent session, used to label its sub-agents."""
    try:
        mtime = parent_path.stat().st_mtime
    except OSError:
        return []
    key = str(parent_path)
    cached = _AGENT_CALLS_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    calls = []
    for rec in common.iter_jsonl(parent_path):
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in ("Task", "Agent"):
                inp = b.get("input") or {}
                calls.append(
                    {
                        "prompt": (inp.get("prompt") or "").strip(),
                        "description": inp.get("description") or "",
                        "subagent_type": inp.get("subagent_type") or inp.get("agentType") or "",
                    }
                )
    _AGENT_CALLS_CACHE[key] = (mtime, calls)
    return calls


def _subagent_meta(path: Path, records: list[dict]) -> dict | None:
    """Build the sub-agent fields (title, parent linkage) for one transcript.

    Returns None when `path` is not a sub-agent file. The opening prompt is
    matched (by prefix) against the parent's Task/Agent calls to recover a nice
    ``[type] description`` label; sub-agents with no spawning call (e.g. from
    compaction) fall back to their first prompt.
    """
    sub = _subagent_parent(path)
    if not sub:
        return None
    parent_id, parent_file = sub
    first_text = _subagent_first_user_text(records)
    description = subagent_type = ""
    for call in _agent_calls(parent_file):
        # Normalise both sides the same way: first_text has its whitespace
        # collapsed, so the raw prompt must be collapsed too before comparing.
        p = " ".join(call["prompt"].split())
        if p and first_text and p[:300] == first_text[:300]:
            description = call["description"]
            subagent_type = call["subagent_type"]
            break
    base = description or first_text or "(sub-agent)"
    title = common.short_title((f"[{subagent_type}] " if subagent_type else "") + base)
    return {
        "title": title,
        "is_subagent": True,
        "parent_id": parent_id,
        "parent_file": str(parent_file) if parent_file.exists() else "",
        "subagent_type": subagent_type,
    }


# ---------------------------------------------------------------------------
# Sidebar summaries (cached by file identity; cold scans use a process pool)
# ---------------------------------------------------------------------------
def _summary_worker(path_str: str) -> tuple[str, int, int, dict | None]:
    """Parse one Claude transcript in an isolated cold-scan worker."""
    path = Path(path_str)
    identity = common.file_identity(path)
    if identity is None:
        return path_str, 0, 0, None
    try:
        summary = _session_summary_uncached(path)
    except (OSError, ValueError):
        return path_str, identity[0], identity[1], None
    return path_str, identity[0], identity[1], summary


def _prefill_summaries(files: list[Path]) -> None:
    """Populate stale summaries, using processes only for a large cold batch."""
    stale = []
    for path in files:
        identity = common.file_identity(path)
        if identity is not None and SUMMARY_CACHE.get(str(path), identity) is None:
            stale.append(str(path))
    if len(stale) < _PARALLEL_SCAN_THRESHOLD or (os.cpu_count() or 1) < 2:
        return
    try:
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(os.cpu_count() or 1, 8), mp_context=context) as pool:
            for key, mtime, size, summary in pool.map(_summary_worker, stale, chunksize=8):
                if summary is not None:
                    SUMMARY_CACHE.put(key, (mtime, size), summary)
    except Exception:  # noqa: BLE001 - serial parsing in the caller remains the fallback
        return


def session_summary(path: Path) -> dict:
    """Lightweight metadata for a Claude Code session, cached by file identity."""
    return common.cached_summary(
        SUMMARY_CACHE, str(path), common.file_identity(path),
        lambda: _session_summary_uncached(path),
    )


def _session_summary_uncached(path: Path) -> dict:
    records = list(common.iter_jsonl(path))
    is_subagent = path.parent.name == "subagents"
    ai_title = ""
    claude_title = ""
    agent_name = ""
    cwd = ""
    git_branch = ""
    version = ""
    first_ts = None
    last_ts = None
    n_user = n_assistant = n_tool = 0
    models: set[str] = set()

    for rec in records:
        t = rec.get("type")
        if t == "ai-title" and rec.get("aiTitle"):
            ai_title = rec["aiTitle"]
        elif t == "custom-title" and rec.get("customTitle"):
            claude_title = rec["customTitle"]
        elif t == "agent-name" and rec.get("agentName"):
            agent_name = rec["agentName"]
        if rec.get("cwd"):
            cwd = rec["cwd"]
        if rec.get("gitBranch"):
            git_branch = rec["gitBranch"]
        if rec.get("version"):
            version = rec["version"]
        ts = rec.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if (
            t == "user"
            and not rec.get("isMeta")
            and (is_subagent or not rec.get("isSidechain"))
        ):
            # Skip system-injected wrappers so the count matches the user outline.
            text = _user_record_text(rec)
            if text and not _synthetic_user_notice(text):
                n_user += 1
        if t == "attachment" and not rec.get("isSidechain"):
            att = rec.get("attachment", {})
            if (
                att.get("type") == "queued_command"
                and att.get("prompt")
                and not _synthetic_user_notice(att["prompt"])
            ):
                n_user += 1
        if t == "assistant":
            msg = rec.get("message", {})
            if msg.get("model"):
                models.add(msg["model"])
            for b in msg.get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    n_tool += 1
            n_assistant += 1

    sub_meta = _subagent_meta(path, records) if is_subagent else None
    if sub_meta:
        cwd = cwd or decode_project_name(path.parent.parent.parent.name)
    else:
        cwd = cwd or decode_project_name(path.parent.name)

    st = common.safe_stat(path)
    summary = common.make_summary(
        agent="claude",
        id=path.stem,
        file=str(path),
        title=claude_title
        or (sub_meta["title"] if sub_meta else "")
        or _first_user_text(records)
        or ai_title
        or "(untitled session)",
        cwd=cwd,
        git_branch=git_branch,
        version=version,
        first_ts=first_ts,
        last_ts=last_ts,
        n_user=n_user,
        n_assistant=n_assistant,
        n_tool=n_tool,
        n_records=len(records),
        model=sorted(models)[0] if models else "",
        mtime=st.st_mtime if st else 0,
        # Latest Claude Code AI-generated session title (the one its /resume
        # picker shows); "" if none yet.
        ai_title=ai_title,
        claude_title=claude_title,
        agent_name=agent_name,
    )
    if sub_meta:
        summary.update({key: value for key, value in sub_meta.items() if key != "title"})
    return summary


def list_sessions() -> list[dict]:
    """Flat list of Claude Code session summaries (sessions + sub-agents)."""
    if not PROJECTS_DIR.exists():
        return []
    files: list[Path] = []
    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        files.extend(proj_dir.glob("*.jsonl"))
        # Sub-agents live one level down: <session-id>/subagents/agent-*.jsonl
        files.extend(proj_dir.glob("*/subagents/*.jsonl"))
    _prefill_summaries(files)
    out: list[dict] = []
    for path in files:
        try:
            out.append(session_summary(path))
        except (OSError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Full session parse
# ---------------------------------------------------------------------------
def _normalize_tool_result_content(content) -> dict:
    """Return {'text': str, 'images': [data-uri...]} from a tool_result body."""
    out = {"text": "", "images": []}
    if isinstance(content, str):
        out["text"] = content
    elif isinstance(content, list):
        texts = []
        for b in content:
            if not isinstance(b, dict):
                texts.append(str(b))
                continue
            bt = b.get("type")
            if bt == "text":
                texts.append(b.get("text", ""))
            elif bt == "image":
                src = b.get("source", {})
                if src.get("type") == "base64" and src.get("data"):
                    out["images"].append(
                        f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"
                    )
                elif src.get("type") == "url" and src.get("url"):
                    out["images"].append(src["url"])
                else:
                    texts.append("[image]")
            elif bt == "tool_reference":
                texts.append(f"[tool reference: {b.get('name', '')}]")
            else:
                texts.append(json.dumps(b)[:500])
        out["text"] = "\n".join(t for t in texts if t)
    return out


def _notice_event(notice: dict, ts, is_sidechain: bool) -> dict:
    """Build a `notice` event (a system-injected, non-prompt user record) from a
    ``{label, text}`` returned by :func:`_synthetic_user_notice`."""
    return {
        "kind": "notice",
        "ts": ts,
        "label": notice["label"],
        "text": notice["text"],
        "is_sidechain": is_sidechain,
    }


def _strip_event(ev: dict) -> dict:
    """Drop the internal branch-folding bookkeeping fields from an event."""
    for k in ("_uuid", "_idx"):
        ev.pop(k, None)
    return ev


def _fold_branches(records: list[dict], events: list[dict], active_leaf) -> list[dict]:
    """Keep only the active conversation path; fold rewound/edited branches into
    inline ``branch`` events the frontend renders as collapsible markers.

    Claude Code stores the conversation as a tree over ``uuid``/``parentUuid``.
    Editing or rewinding a message forks the tree (a parent gains a second
    child); every branch is appended to the same file, so a flat read shows
    abandoned turns inline. The active tip is the most recent ``last-prompt``
    leaf. We walk root→leaf and, at each fork on that path, bundle the abandoned
    sibling subtree(s) into one marker placed just before the active child.

    Falls back to the unchanged flat list when there are no forks, the leaf is
    unusable, or the reconstructed path wouldn't cover every event (so we never
    silently drop content).
    """
    children: dict = {}
    byu: dict = {}
    idx_by_uuid: dict = {}
    for i, r in enumerate(records):
        u = r.get("uuid")
        if not u:
            continue
        byu[u] = r
        idx_by_uuid.setdefault(u, i)
        children.setdefault(r.get("parentUuid"), []).append(u)

    if not any(len(c) > 1 for c in children.values()):
        return [_strip_event(e) for e in events]

    # An event whose record has no uuid can't be placed on the tree at all, and
    # the coverage check below can't see it either — folding would silently drop
    # it. Real Claude Code records always carry uuids, so hitting this means
    # format drift; keep the flat list rather than lose content.
    if any(not e.get("_uuid") for e in events):
        return [_strip_event(e) for e in events]

    if not active_leaf or active_leaf not in byu:
        uuids = [r.get("uuid") for r in records if r.get("uuid")]
        active_leaf = uuids[-1] if uuids else None
    if not active_leaf:
        return [_strip_event(e) for e in events]

    # `last-prompt` points at the prompt's leaf, but the assistant reply (and any
    # tool results) are appended after it as descendants. Walk down to the real
    # tip so those trailing turns stay on the active path instead of being folded
    # away as an abandoned branch. At a fork the active continuation is always the
    # latest-appended child (a rewind abandons the old branch and appends a new
    # subtree after it).
    node, guard = active_leaf, set()
    while node not in guard:
        guard.add(node)
        kids = children.get(node)
        if not kids:
            break
        node = max(kids, key=lambda c: idx_by_uuid.get(c, -1))
    active_leaf = node

    chain, seen, node = [], set(), active_leaf
    while node in byu and node not in seen:
        seen.add(node)
        chain.append(node)
        node = byu[node].get("parentUuid")
    chain.reverse()  # root → leaf

    ev_by_uuid = {e["_uuid"]: e for e in events if e.get("_uuid")}

    def subtree(root):
        out, stack = [], [root]
        while stack:
            x = stack.pop()
            out.append(x)
            stack.extend(children.get(x, []))
        return out

    new_events: list = []
    covered: set = set()
    for i, u in enumerate(chain):
        ev = ev_by_uuid.get(u)
        if ev is not None:
            new_events.append(ev)
        covered.add(u)
        nxt = chain[i + 1] if i + 1 < len(chain) else None
        groups = []
        for ab in (c for c in children.get(u, []) if c != nxt):
            sub = subtree(ab)
            covered.update(sub)
            sub_evs = sorted(
                (ev_by_uuid[x] for x in sub if x in ev_by_uuid),
                key=lambda e: e["_idx"],
            )
            if sub_evs:
                groups.append([_strip_event(e) for e in sub_evs])
        if groups:
            new_events.append(
                {
                    "kind": "branch",
                    "ts": ev.get("ts") if ev else None,
                    "groups": groups,
                    "count": sum(len(g) for g in groups),
                }
            )

    # Safety net: if anything with an event wasn't placed, don't risk dropping
    # it — return the flat list unchanged.
    all_ev_uuids = {e["_uuid"] for e in events if e.get("_uuid")}
    if not all_ev_uuids.issubset(covered):
        return [_strip_event(e) for e in events]
    return [_strip_event(e) for e in new_events]


def parse_session(path: Path) -> dict:
    """Full structured parse of one Claude Code session, ready for rendering."""
    records = list(common.iter_jsonl(path))

    results_by_id: dict[str, dict] = {}
    tool_uses_by_id: dict[str, dict] = {}
    skill_instructions_by_id: dict[str, str] = {}
    for rec in records:
        content = rec.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("id"):
                tool_uses_by_id[b["id"]] = b
            elif rec.get("type") == "user" and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid:
                    norm = _normalize_tool_result_content(b.get("content"))
                    results_by_id[tid] = {
                        "is_error": bool(b.get("is_error")),
                        "text": norm["text"],
                        "images": norm["images"],
                        "structured": rec.get("toolUseResult"),
                    }
    for rec in records:
        source_id = rec.get("sourceToolUseID")
        source_tool = tool_uses_by_id.get(source_id, {})
        if rec.get("type") == "user" and rec.get("isMeta") and source_tool.get("name") == "Skill":
            skill_instructions_by_id[source_id] = _user_record_text(rec)

    events = []
    ai_title = ""
    claude_title = ""
    agent_name = ""
    meta = {}
    active_leaf = None

    # Tag each emitted event with its record's uuid (and file order) so branch
    # folding can reorganize them afterward. `rec` is read from the loop scope
    # at call time.
    def emit(ev):
        ev["_uuid"] = rec.get("uuid")
        ev["_idx"] = len(events)
        events.append(ev)

    for rec in records:
        t = rec.get("type")
        if t == "ai-title" and rec.get("aiTitle"):
            ai_title = rec["aiTitle"]
            continue
        if t == "custom-title" and rec.get("customTitle"):
            claude_title = rec["customTitle"]
            continue
        if t == "agent-name" and rec.get("agentName"):
            agent_name = rec["agentName"]
            continue
        if t == "pr-link":
            pr_url = rec.get("prUrl") or ""
            if pr_url.startswith(("https://", "http://")):
                meta["pr"] = {
                    "number": rec.get("prNumber"),
                    "url": pr_url,
                    "repository": rec.get("prRepository") or "",
                }
            continue
        if t == "last-prompt":
            # Points at the current branch tip; the latest one wins.
            if rec.get("leafUuid"):
                active_leaf = rec["leafUuid"]
            continue
        if t in ("permission-mode", "file-history-snapshot"):
            continue

        ts = rec.get("timestamp")
        is_sidechain = rec.get("isSidechain", False)
        if rec.get("cwd"):
            meta.setdefault("cwd", rec["cwd"])
        if rec.get("gitBranch"):
            meta["git_branch"] = rec["gitBranch"]
        if rec.get("version"):
            meta["version"] = rec["version"]

        if t not in ("system", "attachment", "user", "assistant"):
            if t not in _IGNORED_RECORD_TYPES:
                # A record type this parser has never seen: surface it instead
                # of dropping it silently, so a Claude Code format change is
                # visible in the transcript rather than a quiet gap (the
                # Codex 0.147 lesson).
                emit({"kind": "raw", "ts": ts, "record_type": t,
                      "payload": rec, "is_sidechain": is_sidechain})
            continue

        if t == "system":
            ev = {
                "kind": "system",
                "ts": ts,
                "subtype": rec.get("subtype"),
                "text": rec.get("content") or rec.get("subtype") or "",
                "is_sidechain": is_sidechain,
            }
            compact = rec.get("compactMetadata")
            if rec.get("subtype") == "compact_boundary" and isinstance(compact, dict):
                preserved = compact.get("preservedMessages") or {}
                ev["compaction"] = {
                    "trigger": compact.get("trigger"),
                    "pre_tokens": compact.get("preTokens"),
                    "post_tokens": compact.get("postTokens"),
                    "duration_ms": compact.get("durationMs"),
                    "preserved_messages": len(preserved.get("uuids") or [])
                    if isinstance(preserved, dict) else None,
                    "discovered_tools": len(compact.get("preCompactDiscoveredTools") or []),
                }
            emit(ev)
            continue

        if t == "attachment":
            att = rec.get("attachment", {})
            att_type = att.get("type")
            # A message queued while Claude was still working, recorded as an
            # attachment rather than a `user` record. Usually a genuine user
            # prompt — surface it as a user turn so it doesn't vanish from the
            # transcript and outline. But system-injected events (e.g. background
            # task notifications) also get queued; route those to a notice so they
            # stay out of the user outline.
            if att_type == "queued_command" and att.get("prompt"):
                # The prompt is a plain string, or a block list when the queued
                # message carried an image.
                prompt = att["prompt"]
                notice = _synthetic_user_notice(_content_text(prompt))
                if notice:
                    emit(_notice_event(notice, ts, is_sidechain))
                else:
                    blocks, has_content = _content_blocks(prompt)
                    if has_content:
                        emit(
                            {
                                "kind": "user",
                                "ts": ts,
                                "blocks": blocks,
                                "is_sidechain": is_sidechain,
                                "queued": True,
                            }
                        )
                continue
            raw_content = att.get("content")
            content = raw_content if isinstance(raw_content, str) else ""
            num_lines = None
            # `file` attachments nest the text under content.file (re-attached
            # after a /compact); pull it back out so the viewer can show it.
            if not content and isinstance(raw_content, dict):
                nested = raw_content.get("file")
                if isinstance(nested, dict):
                    content = nested.get("content") or ""
                    num_lines = nested.get("numLines") or nested.get("totalLines")
            filename = att.get("filename")
            display_path = att.get("displayPath")
            if not display_path and filename:
                display_path = filename.rsplit("/", 1)[-1]
            ev = {
                "kind": "attachment",
                "ts": ts,
                "att_type": att_type,
                "hook_name": att.get("hookName"),
                "command": att.get("command"),
                "stdout": att.get("stdout"),
                "stderr": att.get("stderr"),
                "exit_code": att.get("exitCode"),
                "content": content,
                "filename": filename,
                "display_path": display_path,
                "num_lines": num_lines,
                "is_sidechain": is_sidechain,
            }
            if att_type == "deferred_tools_delta":
                ev["added_count"] = len(att.get("addedNames") or [])
                ev["removed_count"] = len(att.get("removedNames") or [])
                ev["readded_count"] = len(att.get("readdedNames") or [])
            emit(ev)
            continue

        if t == "user":
            blocks, has_content = _content_blocks(rec.get("message", {}).get("content"))
            if not has_content:
                continue
            # Claude Code injects a selected skill's instructions as an isMeta
            # user record. It is model context, not something the user typed.
            # Custom skills usually include their full SKILL.md here; built-in
            # skills may inject only a short instruction.
            meta_text = _user_record_text(rec)
            source_tool = tool_uses_by_id.get(rec.get("sourceToolUseID"), {})
            is_skill = source_tool.get("name") == "Skill" or meta_text.lstrip().startswith(
                "Base directory for this skill:"
            )
            if rec.get("isMeta") and is_skill:
                # A linked Skill call already exists in the transcript. Its
                # renderer includes these instructions inside that tool block,
                # so do not add a second top-level event.
                if source_tool.get("name") == "Skill":
                    continue
                skill_name = (source_tool.get("input") or {}).get("skill") or ""
                emit(
                    {
                        "kind": "instructions",
                        "ts": ts,
                        "label": "Skill instructions" + (f" · {skill_name}" if skill_name else ""),
                        "text": meta_text,
                        "is_sidechain": is_sidechain,
                    }
                )
                continue
            # System-injected wrappers (task notifications, slash-command echoes,
            # hook output, …) are recorded as `user` records but aren't real
            # prompts. Surface them as notices so they stay out of the user
            # outline. Messages carrying an image are always genuine user input.
            has_image = any(b.get("type") == "image" for b in blocks)
            notice = None if has_image else _synthetic_user_notice(_user_record_text(rec))
            if notice:
                emit(_notice_event(notice, ts, is_sidechain))
                continue
            emit(
                {
                    "kind": "user",
                    "ts": ts,
                    "blocks": blocks,
                    "is_sidechain": is_sidechain,
                }
            )
            continue

        if t == "assistant":
            msg = rec.get("message", {})
            blocks = []
            for b in msg.get("content", []) or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "thinking":
                    blocks.append({"type": "thinking", "text": b.get("thinking", "")})
                elif bt == "text":
                    blocks.append({"type": "text", "text": b.get("text", "")})
                elif bt == "tool_use":
                    tid = b.get("id")
                    result = results_by_id.get(tid)
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tid,
                            "name": b.get("name"),
                            "input": b.get("input", {}),
                            "caller": b.get("caller"),
                            "result": result,
                            "instructions": skill_instructions_by_id.get(tid, ""),
                        }
                    )
            if not blocks:
                continue
            usage = msg.get("usage", {}) or {}
            emit(
                {
                    "kind": "assistant",
                    "ts": ts,
                    "model": msg.get("model"),
                    "blocks": blocks,
                    "is_sidechain": is_sidechain,
                    "usage": {
                        "input": usage.get("input_tokens"),
                        "output": usage.get("output_tokens"),
                        "cache_read": usage.get("cache_read_input_tokens"),
                        "cache_creation": usage.get("cache_creation_input_tokens"),
                    },
                }
            )
            continue

    events = _fold_branches(records, events, active_leaf)

    # Cross-session fork: branching into a new "section" spawns a fresh session
    # file whose copied-over records each carry a `forkedFrom` pointer into the
    # parent session. The last such pointer is the divergence point; everything
    # after it is this session's new work. Surface it so the header can link back
    # to the parent (where the other branch lives).
    forked_from = None
    for rec in records:
        ff = rec.get("forkedFrom")
        if isinstance(ff, dict) and ff.get("sessionId"):
            forked_from = ff
    fork_info = None
    if forked_from:
        sid = forked_from.get("sessionId")
        cand = path.parent / f"{sid}.jsonl"
        fork_info = {
            "session_id": sid,
            "message_uuid": forked_from.get("messageUuid"),
            "file": str(cand) if cand.exists() else "",
        }

    sub_meta = _subagent_meta(path, records)
    if sub_meta:
        meta.setdefault("cwd", decode_project_name(path.parent.parent.parent.name))
    out = {
        "agent": "claude",
        "id": path.stem,
        "title": claude_title
        or (sub_meta["title"] if sub_meta else "")
        or _first_user_text(records)
        or ai_title
        or "(untitled session)",
        # Latest Claude Code AI-generated title (shown under the header); "" if none.
        "ai_title": ai_title,
        "claude_title": claude_title,
        "agent_name": agent_name,
        "forked_from": fork_info,
        "meta": meta,
        "events": events,
    }
    if sub_meta:
        out["is_subagent"] = True
        out["parent_id"] = sub_meta["parent_id"]
        out["parent_file"] = sub_meta["parent_file"]
        out["subagent_type"] = sub_meta["subagent_type"]
    return out
