#!/usr/bin/env python3
"""Unified Claude Code + Codex + Cursor transcript browser.

A zero-dependency local web app for browsing Claude Code session transcripts
(under ~/.claude/projects), Codex session transcripts (under ~/.codex/sessions),
and Cursor agent conversations (from Cursor's state.vscdb) in a single,
time-sorted sidebar. Run it and open the printed URL.

Usage:
    python server.py [--port 3132] [--projects-dir PATH] [--codex-home PATH]
                     [--cursor-db PATH]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import multiprocessing
import os
import re
import threading
from concurrent.futures import ProcessPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import codex_server as codex
import cursor_server as cursor

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
DEFAULT_CUSTOM_NAMES_FILE = (
    Path.home() / ".config" / "cc_transcript_viewer" / "names.json"
)
# Loopback only by default: the app reads private transcripts, so it must not be
# reachable from the network unless the user deliberately overrides --host.
DEFAULT_HOST = "127.0.0.1"

# Hostnames a browser legitimately uses to reach a loopback-bound server.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# When bound to loopback we enforce a Host-header allowlist (set in main()).
# This defeats DNS-rebinding: a malicious page that rebinds its domain to
# 127.0.0.1 still sends `Host: evil.com`, which we reject. Disabled when the
# user deliberately binds a non-loopback --host (they've opted into exposure).
HOST_CHECK = True

# Set by main() so handlers can reach it.
PROJECTS_DIR = DEFAULT_PROJECTS_DIR
CUSTOM_NAMES_FILE = DEFAULT_CUSTOM_NAMES_FILE
CACHE_FILE = Path.home() / ".cache" / "transcript_viewer" / "summaries.json"


# ---------------------------------------------------------------------------
# Viewer-owned custom transcript names
# ---------------------------------------------------------------------------
_CUSTOM_NAMES_LOCK = threading.Lock()
_CUSTOM_NAMES_CACHE: tuple[int | None, dict[str, str]] | None = None


def _load_custom_names() -> dict[str, str]:
    """Load the custom-name map, refreshing when it changes on disk."""
    global _CUSTOM_NAMES_CACHE
    with _CUSTOM_NAMES_LOCK:
        try:
            mtime = CUSTOM_NAMES_FILE.stat().st_mtime_ns
        except OSError:
            mtime = None
        if _CUSTOM_NAMES_CACHE and _CUSTOM_NAMES_CACHE[0] == mtime:
            return _CUSTOM_NAMES_CACHE[1].copy()
        try:
            raw = json.loads(CUSTOM_NAMES_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        names = {
            str(key): value
            for key, value in raw.items()
            if isinstance(value, str) and value.strip()
        } if isinstance(raw, dict) else {}
        _CUSTOM_NAMES_CACHE = (mtime, names)
        return names.copy()


def _custom_name_key(session: dict) -> str:
    """Stable across transcript moves, including Codex archival."""
    parts = [session.get("agent", ""), session.get("id", "")]
    if session.get("is_subagent") and session.get("parent_id"):
        parts.insert(1, session["parent_id"])
    return ":".join(str(part) for part in parts)


def _apply_custom_name(session: dict) -> dict:
    """Add original/custom title fields and select the effective title."""
    original = session.get("original_title") or session.get("title") or "(untitled session)"
    custom = _load_custom_names().get(_custom_name_key(session), "")
    session["original_title"] = original
    session["custom_title"] = custom
    session["title"] = custom or original
    return session


def _set_custom_name(session: dict, name: str) -> None:
    """Persist one override using an atomic same-directory replacement."""
    global _CUSTOM_NAMES_CACHE
    key = _custom_name_key(session)
    with _CUSTOM_NAMES_LOCK:
        try:
            raw = json.loads(CUSTOM_NAMES_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        names = raw if isinstance(raw, dict) else {}
        if name:
            names[key] = name
        else:
            names.pop(key, None)
        CUSTOM_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = CUSTOM_NAMES_FILE.with_name(CUSTOM_NAMES_FILE.name + ".tmp")
        temp.write_text(
            json.dumps(names, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.replace(CUSTOM_NAMES_FILE)
        _CUSTOM_NAMES_CACHE = (CUSTOM_NAMES_FILE.stat().st_mtime_ns, names.copy())


# ---------------------------------------------------------------------------
# Claude Code parsing
# ---------------------------------------------------------------------------
def decode_project_name(dirname: str) -> str:
    """Claude Code encodes the project cwd by replacing '/' with '-'.

    The original path isn't perfectly recoverable (dashes in real names are
    ambiguous), but we can produce a readable best-effort path.
    """
    if dirname.startswith("-"):
        return "/" + dirname[1:].replace("-", "/")
    return dirname.replace("-", "/")


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


def _user_record_text(rec: dict) -> str:
    """Concatenated text of a `user` record's content. Ignores images and
    tool-result blocks; returns '' when the record carries no text."""
    content = rec.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


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
        if rec.get("type") != "user" or rec.get("isSidechain"):
            continue
        text = _user_record_text(rec)
        if not text or _synthetic_user_notice(text):
            continue
        text = re.sub(r"<[^>]+>", " ", text)
        text = " ".join(text.split())
        if text:
            return _short_title(text)
    return ""


def _short_title(text: str, n: int = 100) -> str:
    """First ~n characters of a message, single-spaced, with an ellipsis if cut."""
    text = " ".join((text or "").split())
    return text[:n] + ("…" if len(text) > n else "")


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


def _cc_agent_calls(parent_path: Path) -> list[dict]:
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
    for rec in _iter_records(parent_path):
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
    for call in _cc_agent_calls(parent_file):
        # Normalise both sides the same way: first_text has its whitespace
        # collapsed, so the raw prompt must be collapsed too before comparing.
        p = " ".join(call["prompt"].split())
        if p and first_text and p[:300] == first_text[:300]:
            description = call["description"]
            subagent_type = call["subagent_type"]
            break
    base = description or first_text or "(sub-agent)"
    title = _short_title((f"[{subagent_type}] " if subagent_type else "") + base)
    return {
        "title": title,
        "is_subagent": True,
        "parent_id": parent_id,
        "parent_file": str(parent_file) if parent_file.exists() else "",
        "subagent_type": subagent_type,
    }


# Cache: file path -> (mtime_ns, size, summary dict). Keeps the /api/sessions poll from
# re-reading and re-parsing every transcript on every request; only files whose
# identity moved are rebuilt. The cache is persisted so restarts stay cheap.
_SUMMARY_CACHE: dict[str, tuple[int, int, dict]] = {}
_SUMMARY_CACHE_LOCK = threading.RLock()
_CACHE_LOADED = False
_SUMMARY_CACHE_DIRTY = False
_SUMMARY_CACHE_GENERATION = 0
_CACHE_VERSION = 1
_PARALLEL_SCAN_THRESHOLD = 32


def _file_identity(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _summary_worker(path_str: str) -> tuple[str, int, int, dict | None]:
    """Parse one Claude transcript in an isolated cold-scan worker."""
    path = Path(path_str)
    identity = _file_identity(path)
    if identity is None:
        return path_str, 0, 0, None
    try:
        summary = _cc_session_summary_uncached(path)
    except (OSError, ValueError):
        return path_str, identity[0], identity[1], None
    return path_str, identity[0], identity[1], summary


def load_summary_caches() -> None:
    """Load viewer-owned sidebar summaries from disk once, best effort."""
    global _CACHE_LOADED, _SUMMARY_CACHE_DIRTY, _SUMMARY_CACHE_GENERATION
    with _SUMMARY_CACHE_LOCK:
        if _CACHE_LOADED:
            return
        _CACHE_LOADED = True
        try:
            payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("version") != _CACHE_VERSION:
            return
        for key, value in (payload.get("claude") or {}).items():
            try:
                _SUMMARY_CACHE[key] = (int(value[0]), int(value[1]), value[2])
            except (TypeError, ValueError, IndexError):
                continue
        codex.load_summary_cache(payload.get("codex") or {})
        _SUMMARY_CACHE_DIRTY = False
        _SUMMARY_CACHE_GENERATION = 0


def save_summary_caches() -> None:
    """Atomically persist a snapshot of both summary caches, best effort."""
    global _SUMMARY_CACHE_DIRTY
    with _SUMMARY_CACHE_LOCK:
        if not _SUMMARY_CACHE_DIRTY and not codex.summary_cache_dirty():
            return
        claude_generation = _SUMMARY_CACHE_GENERATION
        codex_generation = codex.summary_cache_generation()
        claude = {key: [mtime, size, summary] for key, (mtime, size, summary) in _SUMMARY_CACHE.items()}
        payload = {
            "version": _CACHE_VERSION,
            "claude": claude,
            "codex": codex.dump_summary_cache(),
        }
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
        temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temp.replace(CACHE_FILE)
        with _SUMMARY_CACHE_LOCK:
            if _SUMMARY_CACHE_GENERATION == claude_generation:
                _SUMMARY_CACHE_DIRTY = False
            codex.mark_summary_cache_saved(codex_generation)
    except OSError:
        pass


def _prefill_cc_summaries(files: list[Path]) -> None:
    """Populate stale summaries, using processes only for a large cold batch."""
    global _SUMMARY_CACHE_DIRTY, _SUMMARY_CACHE_GENERATION
    stale = []
    with _SUMMARY_CACHE_LOCK:
        for path in files:
            identity = _file_identity(path)
            cached = _SUMMARY_CACHE.get(str(path))
            if identity is not None and (not cached or cached[:2] != identity):
                stale.append(str(path))
    if len(stale) < _PARALLEL_SCAN_THRESHOLD or (os.cpu_count() or 1) < 2:
        return
    try:
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=min(os.cpu_count() or 1, 8), mp_context=context) as pool:
            results = pool.map(_summary_worker, stale, chunksize=8)
            with _SUMMARY_CACHE_LOCK:
                for key, mtime, size, summary in results:
                    if summary is not None:
                        _SUMMARY_CACHE[key] = (mtime, size, summary)
                        _SUMMARY_CACHE_DIRTY = True
                        _SUMMARY_CACHE_GENERATION += 1
    except Exception:  # noqa: BLE001 - serial calls below remain the fallback
        return


def cc_session_summary(path: Path) -> dict:
    """Lightweight metadata for a Claude Code session, cached by file mtime."""
    global _SUMMARY_CACHE_DIRTY, _SUMMARY_CACHE_GENERATION
    key = str(path)
    identity = _file_identity(path)
    if identity is not None:
        with _SUMMARY_CACHE_LOCK:
            cached = _SUMMARY_CACHE.get(key)
        if cached and cached[:2] == identity:
            return cached[2]

    summary = _cc_session_summary_uncached(path)
    if identity is not None:
        with _SUMMARY_CACHE_LOCK:
            _SUMMARY_CACHE[key] = (identity[0], identity[1], summary)
            _SUMMARY_CACHE_DIRTY = True
            _SUMMARY_CACHE_GENERATION += 1
    return summary


def _cc_session_summary_uncached(path: Path) -> dict:
    records = list(_iter_records(path))
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
        if t == "user" and (is_subagent or not rec.get("isSidechain")):
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

    summary = {
        "agent": "claude",
        "id": path.stem,
        "file": str(path),
        "title": claude_title
        or (sub_meta["title"] if sub_meta else "")
        or _first_user_text(records)
        or ai_title
        or "(untitled session)",
        # Latest Claude Code AI-generated session title (the one its /resume
        # picker shows); "" if none yet.
        "ai_title": ai_title,
        "claude_title": claude_title,
        "agent_name": agent_name,
        "cwd": cwd,
        "git_branch": git_branch,
        "version": version,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "n_user": n_user,
        "n_assistant": n_assistant,
        "n_tool": n_tool,
        "n_web": 0,
        "n_records": len(records),
        "model": sorted(models)[0] if models else "",
        "models": sorted(models),
        "mtime": path.stat().st_mtime,
    }
    if sub_meta:
        summary.update({key: value for key, value in sub_meta.items() if key != "title"})
    return summary


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
    for k in ("_uuid", "_parent", "_idx"):
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


def parse_cc_session(path: Path) -> dict:
    """Full structured parse of one Claude Code session, ready for rendering."""
    records = list(_iter_records(path))

    results_by_id: dict[str, dict] = {}
    for rec in records:
        if rec.get("type") != "user":
            continue
        content = rec.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid:
                    norm = _normalize_tool_result_content(b.get("content"))
                    results_by_id[tid] = {
                        "is_error": bool(b.get("is_error")),
                        "text": norm["text"],
                        "images": norm["images"],
                        "structured": rec.get("toolUseResult"),
                    }

    events = []
    ai_title = ""
    claude_title = ""
    agent_name = ""
    meta = {}
    active_leaf = None

    # Tag each emitted event with its record's uuid/parentUuid (and file order)
    # so branch folding can reorganize them afterward. `rec` is read from the
    # loop scope at call time.
    def emit(ev):
        ev["_uuid"] = rec.get("uuid")
        ev["_parent"] = rec.get("parentUuid")
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
                notice = _synthetic_user_notice(att["prompt"])
                if notice:
                    emit(_notice_event(notice, ts, is_sidechain))
                else:
                    emit(
                        {
                            "kind": "user",
                            "ts": ts,
                            "blocks": [{"type": "text", "text": att["prompt"]}],
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
            content = rec.get("message", {}).get("content")
            blocks = []
            has_text = False
            if isinstance(content, str):
                blocks.append({"type": "text", "text": content})
                has_text = bool(content.strip())
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        blocks.append({"type": "text", "text": b.get("text", "")})
                        has_text = True
                    elif b.get("type") == "image":
                        src = b.get("source", {})
                        data_uri = ""
                        if src.get("type") == "base64":
                            data_uri = f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"
                        blocks.append({"type": "image", "data_uri": data_uri})
                        has_text = True
            if not has_text:
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


# ---------------------------------------------------------------------------
# Unified session list / dispatch
# ---------------------------------------------------------------------------
def list_sessions() -> list[dict]:
    """Flat list of every Claude Code and Codex session, newest first."""
    load_summary_caches()
    out: list[dict] = []

    if PROJECTS_DIR.exists():
        cc_files: list[Path] = []
        for proj_dir in sorted(PROJECTS_DIR.iterdir()):
            if not proj_dir.is_dir():
                continue
            cc_files.extend(proj_dir.glob("*.jsonl"))
            # Sub-agents live one level down: <session-id>/subagents/agent-*.jsonl
            cc_files.extend(proj_dir.glob("*/subagents/*.jsonl"))
        _prefill_cc_summaries(cc_files)
        for path in cc_files:
            try:
                out.append(cc_session_summary(path))
            except (OSError, ValueError):
                continue

    try:
        for group in codex.list_sessions():
            for s in group.get("sessions", []):
                s["agent"] = "codex"
                out.append(s)
    except Exception:  # noqa: BLE001 — never let Codex errors hide CC sessions
        pass

    try:
        for group in cursor.list_sessions():
            for s in group.get("sessions", []):
                s["agent"] = "cursor"
                out.append(s)
    except Exception:  # noqa: BLE001 — never let Cursor errors hide other sessions
        pass

    save_summary_caches()

    for session in out:
        _apply_custom_name(session)
    out.sort(key=lambda s: s.get("mtime") or 0, reverse=True)

    # Place each sub-agent directly under its parent session rather than at its
    # own mtime slot. An actively-updated parent floats to the top of the
    # time-sorted list while its sub-agents (last touched earlier) sink down and
    # appear to belong to whatever unrelated session precedes them. Grouping
    # keeps a sub-agent visually attached to the session that spawned it.
    parent_files = {s["file"] for s in out if not s.get("is_subagent")}
    subs_by_parent: dict[str, list] = {}
    for s in out:
        if s.get("is_subagent") and s.get("parent_file") in parent_files:
            subs_by_parent.setdefault(s["parent_file"], []).append(s)

    grouped: list[dict] = []
    for s in out:
        if s.get("is_subagent"):
            # Orphans (parent file not in the list) keep their own mtime slot;
            # everything else is emitted under its parent below.
            if s.get("parent_file") not in parent_files:
                grouped.append(s)
            continue
        grouped.append(s)
        grouped.extend(subs_by_parent.get(s["file"], []))
    return grouped


def _under(target: Path, root: Path) -> bool:
    try:
        root = root.resolve()
    except OSError:
        return False
    return target == root or root in target.parents


def parse_session(target: Path) -> dict | None:
    """Dispatch to the right parser based on which transcript root owns the file.

    Returns None if the file is outside every allowed root.
    """
    if _under(target, PROJECTS_DIR):
        return parse_cc_session(target)
    if _under(target, codex.SESSIONS_DIR) or (
        codex.ARCHIVED_SESSIONS_DIR.exists() and _under(target, codex.ARCHIVED_SESSIONS_DIR)
    ):
        data = codex.parse_session(target)
        data["agent"] = "codex"
        return data
    return None


def load_session(file_id: str) -> dict | None:
    """Resolve a session id to parsed data, for both `/api/session` and search.

    Cursor sessions are addressed by the synthetic `cursordb:<composerId>` scheme
    (they live in a SQLite DB, not a file); everything else is a real transcript
    path that must resolve under an allowed root.
    """
    if file_id.startswith(cursor.SESSION_SCHEME):
        data = cursor.parse_session_by_id(file_id[len(cursor.SESSION_SCHEME):])
        if data is not None:
            data["agent"] = "cursor"
            _apply_custom_name(data)
        return data
    target = Path(file_id).expanduser().resolve()
    if not target.exists():
        return None
    data = parse_session(target)
    return _apply_custom_name(data) if data is not None else None


# ---------------------------------------------------------------------------
# Full-text search across transcript content
# ---------------------------------------------------------------------------
# Cache: path -> (mtime, all_user_message_text, rest_of_text)
_TEXT_CACHE: dict[str, tuple[float, str, str]] = {}

# User prompts should outrank generated/tool output without overwhelming a
# custom title selected specifically to identify the transcript.
USER_MSG_WEIGHT = 50
CUSTOM_TITLE_WEIGHT = 10_000
NATIVE_TITLE_WEIGHT = CUSTOM_TITLE_WEIGHT // 2


def _event_text(ev: dict) -> list[str]:
    """All searchable text from a single event (both agent shapes)."""
    parts: list[str] = []

    def add(x):
        if x and isinstance(x, str):
            parts.append(x)

    if ev.get("blocks"):  # Claude Code shape
        for b in ev["blocks"]:
            add(b.get("text"))
            if b.get("type") == "tool_use":
                inp = b.get("input") or {}
                if isinstance(inp, dict):
                    for k in ("command", "file_path", "pattern", "query", "prompt", "description", "content", "url"):
                        add(inp.get(k))
                res = b.get("result") or {}
                if isinstance(res, dict):
                    add(res.get("text"))
    else:  # Codex shape
        add(ev.get("text"))
        add(ev.get("summary"))
        add(ev.get("query"))
        inp = ev.get("input")
        if isinstance(inp, str):
            add(inp)
        elif isinstance(inp, dict):
            for k in ("cmd", "command", "file_path", "query", "prompt"):
                add(inp.get(k))
            for call in inp.get("calls") or []:
                nested = call.get("input") if isinstance(call, dict) else None
                if isinstance(nested, str):
                    add(nested)
                elif isinstance(nested, dict):
                    for k in ("cmd", "command", "file_path", "query", "prompt"):
                        add(nested.get(k))
        res = ev.get("result")
        if isinstance(res, dict):
            add(res.get("output"))
        act = ev.get("action")
        if isinstance(act, dict):
            for q in act.get("queries") or []:
                add(q)
        request = ev.get("request")
        if isinstance(request, dict):
            add(request.get("tool"))
            add(request.get("cwd"))
            add(request.get("justification"))
            command = request.get("command")
            if isinstance(command, str):
                add(command)
            elif isinstance(command, list):
                for part in command:
                    add(part)
    return parts


def _session_segments(data: dict) -> tuple[str, str]:
    """Split a parsed session into (all user messages, everything else).

    User messages are scored above generated/tool content. Image blobs are
    skipped throughout.
    """
    user: list[str] = []
    rest: list[str] = []
    cwd = (data.get("meta") or {}).get("cwd")
    if cwd:
        rest.append(cwd)

    def add_event(ev: dict) -> None:
        if ev.get("kind") == "branch":
            for group in ev.get("groups", []) or []:
                for ge in group:
                    add_event(ge)
        elif ev.get("kind") == "user":
            user.extend(_event_text(ev))
        else:
            rest.extend(_event_text(ev))

    for ev in data.get("events", []) or []:
        add_event(ev)
    return "\n".join(user), "\n".join(rest)


def session_segments(s: dict) -> tuple[str, str]:
    """(user messages, rest) for one session, cached by its mtime.

    Works for both real transcript files and synthetic-id sessions (Cursor's
    `cursordb:` scheme); the session summary already carries a stable mtime.
    """
    key = s.get("file", "")
    mtime = s.get("mtime") or 0
    cached = _TEXT_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]
    try:
        data = load_session(key)
    except Exception:  # noqa: BLE001
        data = None
    user, rest = _session_segments(data) if data else ("", "")
    _TEXT_CACHE[key] = (mtime, user, rest)
    return user, rest


def _search_title_segments(session: dict) -> tuple[str, str]:
    """Return viewer-custom and native-title text as distinct score tiers."""
    custom = session.get("custom_title") or ""
    native_titles = []
    if session.get("agent") in {"claude", "cursor"}:
        for title in (
            session.get("original_title"),
            session.get("claude_title"),
            session.get("ai_title"),
        ):
            if title and title != custom and title not in native_titles:
                native_titles.append(title)
    return custom, "\n".join(native_titles)


def search_sessions(query: str) -> list[dict]:
    """Return [{file, snippet, score}] for sessions whose content matches `query`.

    Viewer custom-title hits rank first. Claude/Cursor native titles receive
    half that weight, followed by user messages and ordinary transcript text.
    Among equal scores, an earlier first occurrence wins.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    out = []
    for s in list_sessions():
        user, rest = session_segments(s)
        custom, native = _search_title_segments(s)
        custom_low = custom.lower()
        native_low, user_low, rest_low = native.lower(), user.lower(), rest.lower()
        c_custom = custom_low.count(q)
        c_native = native_low.count(q)
        c_user = user_low.count(q)
        c_rest = rest_low.count(q)
        if not c_custom and not c_native and not c_user and not c_rest:
            continue
        score = (
            c_custom * CUSTOM_TITLE_WEIGHT
            + c_native * NATIVE_TITLE_WEIGHT
            + c_user * USER_MSG_WEIGHT
            + c_rest
        )
        if c_custom:
            combined, idx = custom, custom_low.find(q)
            pos = idx
        elif c_native:
            combined, idx = native, native_low.find(q)
            pos = len(custom) + idx
        elif c_user:
            combined, idx = user, user_low.find(q)
            pos = len(custom) + len(native) + idx
        else:
            combined, idx = rest, rest_low.find(q)
            pos = len(custom) + len(native) + len(user) + idx
        start = max(0, idx - 50)
        snippet = combined[start:idx + len(q) + 70].replace("\n", " ").strip()
        if start > 0:
            snippet = "…" + snippet
        out.append({"file": s["file"], "snippet": snippet, "score": score, "pos": pos})
    out.sort(key=lambda m: (-m["score"], m["pos"]))
    return out


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
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

    def _host_allowed(self) -> bool:
        """True if the request's Host header names this loopback server.

        The Host reflects the hostname in the URL the client used; an attacker
        who rebinds DNS to 127.0.0.1 cannot change it away from their domain.
        """
        host = self.headers.get("Host", "")
        if not host:
            return False
        if host.startswith("["):            # bracketed IPv6, e.g. [::1]:3132
            hostname = host[1:host.find("]")] if "]" in host else host
        elif ":" in host:
            hostname = host.rsplit(":", 1)[0]
        else:
            hostname = host
        return hostname in LOOPBACK_HOSTS

    def do_GET(self):
        if HOST_CHECK and not self._host_allowed():
            self.send_error(403, "Host not allowed")
            return

        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/" or route == "/index.html":
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if route == "/app.js":
            self._send_file(STATIC_DIR / "app.js", "application/javascript; charset=utf-8")
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
            t = Path(path_arg).expanduser().resolve()
            # Serve only image-typed files. Transcripts reference images by their
            # original local path, which may live anywhere (project dirs, /tmp,
            # external volumes), so we don't constrain the location — the
            # Host-header check above is what keeps this off-limits to the web.
            content_type = mimetypes.guess_type(str(t))[0] or ""
            if not content_type.startswith("image/"):
                self._send_json({"error": "not an image"}, status=400)
                return
            self._send_file(t, content_type)
            return

        if route == "/api/sessions":
            try:
                self._send_json({"sessions": list_sessions()})
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/search":
            qs = parse_qs(parsed.query)
            q = qs.get("q", [""])[0]
            try:
                self._send_json({"matches": search_sessions(q)})
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/session":
            qs = parse_qs(parsed.query)
            file_arg = qs.get("file", [""])[0]
            if not file_arg:
                self._send_json({"error": "missing file param"}, status=400)
                return
            # Cursor sessions use the `cursordb:<id>` scheme (no file on disk);
            # everything else is a real path confined to an allowed root.
            if not file_arg.startswith(cursor.SESSION_SCHEME):
                target = Path(file_arg).expanduser().resolve()
                if not target.exists():
                    self._send_json({"error": "not found"}, status=404)
                    return
            try:
                data = load_session(file_arg)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
                return
            if data is None:
                self._send_json({"error": "forbidden"}, status=403)
                return
            self._send_json(data)
            return

        self.send_error(404)

    def do_PUT(self):
        if HOST_CHECK and not self._host_allowed():
            self.send_error(403, "Host not allowed")
            return

        if urlparse(self.path).path != "/api/session-name":
            self.send_error(404)
            return
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._send_json({"error": "expected application/json"}, status=415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            self._send_json({"error": "invalid request size"}, status=400)
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON"}, status=400)
            return
        file_id = body.get("file") if isinstance(body, dict) else None
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(file_id, str) or not isinstance(name, str):
            self._send_json({"error": "file and name must be strings"}, status=400)
            return
        name = " ".join(name.split())
        if len(name) > 200:
            self._send_json({"error": "name must be at most 200 characters"}, status=400)
            return
        try:
            data = load_session(file_id)
            if data is None:
                self._send_json({"error": "session not found or forbidden"}, status=404)
                return
            _set_custom_name(data, name)
            _apply_custom_name(data)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, status=500)
            return
        self._send_json({
            "title": data["title"],
            "original_title": data["original_title"],
            "custom_title": data["custom_title"],
        })


def main():
    global PROJECTS_DIR, CUSTOM_NAMES_FILE, HOST_CHECK
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=3132)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--projects-dir", type=Path, default=DEFAULT_PROJECTS_DIR)
    ap.add_argument("--codex-home", type=Path, default=codex.DEFAULT_CODEX_HOME)
    ap.add_argument(
        "--custom-names-file",
        type=Path,
        default=DEFAULT_CUSTOM_NAMES_FILE,
        help="viewer-owned custom transcript names JSON file",
    )
    ap.add_argument(
        "--cursor-db",
        type=Path,
        default=cursor.DEFAULT_DB_PATH,
        help="Cursor state.vscdb (or its Cursor app-support dir)",
    )
    args = ap.parse_args()

    PROJECTS_DIR = args.projects_dir.expanduser()
    CUSTOM_NAMES_FILE = args.custom_names_file.expanduser()
    codex.configure(args.codex_home)
    cursor.configure(args.cursor_db)
    # Enforce the Host allowlist only on the safe loopback default; if the user
    # deliberately binds elsewhere for LAN access, step aside so it still works.
    HOST_CHECK = args.host in LOOPBACK_HOSTS

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print("Claude Code + Codex + Cursor transcript browser")
    print(f"  claude projects: {PROJECTS_DIR}")
    print(f"  codex sessions:  {codex.SESSIONS_DIR}")
    print(f"  cursor db:       {cursor.DB_PATH}")
    print(f"  serving at:      {url}")
    print("  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
