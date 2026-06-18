#!/usr/bin/env python3
"""Unified Claude Code + Codex transcript browser.

A zero-dependency local web app for browsing both Claude Code session
transcripts (under ~/.claude/projects) and Codex session transcripts (under
~/.codex/sessions) in a single, time-sorted sidebar. Run it and open the
printed URL.

Usage:
    python server.py [--port 3132] [--projects-dir PATH] [--codex-home PATH]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
from concurrent.futures import ProcessPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import codex_server as codex

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"
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


def _first_user_text(records: list[dict]) -> str:
    """First real user prompt text (skip tool results / command noise)."""
    for rec in records:
        if rec.get("type") != "user" or rec.get("isSidechain"):
            continue
        content = rec.get("message", {}).get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(p for p in parts if p)
        if not text:
            continue
        if text.lstrip().startswith("<") and ("command-name" in text or "local-command" in text):
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
        content = rec.get("message", {}).get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(p for p in parts if p)
        text = " ".join((text or "").split())
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


def _subagents_dir_for(path: Path) -> Path:
    """The directory holding a session's sub-agent transcripts.

    For a sub-agent file the dir is its own parent; for a top-level session file
    ``<project>/<id>.jsonl`` the sub-agents live in ``<project>/<id>/subagents``.
    """
    if path.parent.name == "subagents":
        return path.parent
    return path.parent / path.stem / "subagents"


# Cache: subagents-dir -> (mtime, {"by_id": {toolUseId: file}, "by_prompt": [(prefix, file)]})
_SUBAGENT_INDEX_CACHE: dict[str, tuple[float, dict]] = {}


def _subagent_index(subagents_dir: Path) -> dict:
    """Map a session's Task/Agent tool calls to the sub-agent files they spawned.

    All of a session's sub-agents (and their sub-agents, recursively) are stored
    flat in one ``subagents/`` dir. Each ``agent-<id>.jsonl`` has a sibling
    ``agent-<id>.meta.json`` that carries the spawning ``toolUseId`` when known —
    an exact link. When it's absent (e.g. fleet/teammate agents), we fall back to
    matching the sub-agent's first prompt against the Task call's prompt prefix.

    Keyed on the dir's mtime so a single index serves every Task block at every
    nesting depth and is rebuilt only when a sub-agent is added or rewritten.
    """
    empty = {"by_id": {}, "by_prompt": [], "agents": []}
    if not subagents_dir.is_dir():
        return empty
    key = str(subagents_dir)
    try:
        mtime = subagents_dir.stat().st_mtime
    except OSError:
        return empty
    cached = _SUBAGENT_INDEX_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]

    by_id: dict[str, str] = {}
    by_prompt: list[tuple[str, str]] = []
    agents: list[dict] = []
    for f in sorted(subagents_dir.glob("agent-*.jsonl")):
        fstr = str(f)
        info = _subagent_file_info(f)
        if info["tool_use_id"]:
            by_id[info["tool_use_id"]] = fstr
        # A fleet/teammate agent wraps its real prompt in <teammate-message …>; we
        # index both the wrapped and unwrapped forms so prompt-matching works.
        if info["inner"]:
            by_prompt.append((info["inner"][:300], fstr))
        if info["prompt"]:
            by_prompt.append((info["prompt"][:300], fstr))
        title = info["description"] or info["summary"] or (info["inner"] or info["prompt"])[:120] or "(sub-agent)"
        agents.append({
            "file": fstr,
            "id": f.stem.replace("agent-", ""),
            "agent_type": info["agent_type"],
            "title": title,
            "first_ts": info.get("first_ts", ""),
        })

    index = {"by_id": by_id, "by_prompt": by_prompt, "agents": agents}
    _SUBAGENT_INDEX_CACHE[key] = (mtime, index)
    return index


# Cache: agent file path -> (meta_mtime, info dict). A sub-agent's first prompt and
# meta are immutable once written, so we re-read a file only when its .meta.json
# changes — keeping live-session index rebuilds O(stat), not O(read), per file.
_SUBAGENT_FILE_CACHE: dict[str, tuple[float, dict]] = {}


def _subagent_file_info(f: Path) -> dict:
    meta_path = f.with_suffix(".meta.json")
    try:
        meta_mtime = meta_path.stat().st_mtime
    except OSError:
        meta_mtime = 0.0
    cached = _SUBAGENT_FILE_CACHE.get(str(f))
    if cached and cached[0] == meta_mtime:
        return cached[1]

    tool_use_id = agent_type = description = ""
    if meta_mtime:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            tool_use_id = meta.get("toolUseId") or ""
            agent_type = meta.get("agentType") or ""
            description = meta.get("description") or ""
        except (OSError, ValueError):
            pass
    # Stream records and stop at the first user prompt — never read the whole
    # (often multi-MB) sub-agent file just to title/place it.
    prompt = ""
    first_ts = ""
    try:
        for rec in _iter_records(f):
            if not first_ts and rec.get("timestamp"):
                first_ts = rec["timestamp"]
            if rec.get("type") == "user":
                prompt = _subagent_first_user_text([rec])
                if prompt:
                    break
    except OSError:
        pass
    summary, inner = _unwrap_teammate(prompt)
    info = {
        "tool_use_id": tool_use_id,
        "agent_type": agent_type,
        "description": description,
        "prompt": prompt,
        "summary": summary,
        "inner": inner,
        "first_ts": first_ts,
    }
    _SUBAGENT_FILE_CACHE[str(f)] = (meta_mtime, info)
    return info


def _unwrap_teammate(prompt: str):
    """Split a possible <teammate-message summary="…">body</…> wrapper.

    Returns (summary, body). Both empty when there is no wrapper. Lets fleet/
    teammate sub-agents (whose first message is wrapped) match their spawning
    Task prompt and get a readable title.
    """
    if not prompt or "<teammate-message" not in prompt:
        return "", ""
    m = re.search(r'<teammate-message[^>]*\bsummary="([^"]*)"', prompt)
    summary = m.group(1) if m else ""
    body = re.sub(r"^.*?<teammate-message[^>]*>", "", prompt, count=1, flags=re.S)
    body = re.sub(r"</teammate-message>\s*$", "", body, flags=re.S).strip()
    return summary, body


def _resolve_subagent_file(index: dict, tool_use_id: str, prompt: str) -> str:
    """Find the sub-agent file a Task/Agent call spawned, by id then prompt prefix."""
    if tool_use_id and tool_use_id in index["by_id"]:
        return index["by_id"][tool_use_id]
    needle = " ".join((prompt or "").split())[:300]
    if needle:
        for prefix, fstr in index["by_prompt"]:
            if prefix and prefix[:200] == needle[:200]:
                return fstr
    return ""


# Cache: file path -> (mtime, summary dict). Keeps the /api/sessions poll from
# re-reading and re-parsing every transcript on every request; only files whose
# mtime moved are rebuilt. This is also persisted to disk (see CACHE_FILE) so a
# server restart doesn't trigger a multi-minute cold rescan of every transcript.
_SUMMARY_CACHE: dict[str, tuple[float, dict]] = {}

# Where the persisted summary cache lives. Parsing every transcript from scratch
# can take minutes on a large history (gigabytes of JSONL); persisting the
# per-file summaries keyed by mtime makes every run after the first one instant,
# and incremental thereafter (only changed files are re-parsed).
CACHE_FILE = Path.home() / ".cache" / "transcript_viewer" / "summaries.json"
_CACHE_LOADED = False
_LAST_SAVED_SIG: tuple | None = None


def _cache_signature() -> tuple:
    """Cheap fingerprint of both caches; changes whenever an entry is added or its mtime moves."""
    cc = len(_SUMMARY_CACHE)
    cc_m = 0.0
    for m, _ in _SUMMARY_CACHE.values():
        cc_m += m or 0
    cx = codex._SUMMARY_CACHE
    cx_m = 0.0
    for v in cx.values():
        cx_m += v[0] or 0
    return (cc, round(cc_m, 3), len(cx), round(cx_m, 3))


def load_caches() -> None:
    """Load persisted summaries from disk into both in-memory caches (best effort)."""
    global _CACHE_LOADED, _LAST_SAVED_SIG
    _CACHE_LOADED = True
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for key, val in (data.get("cc") or {}).items():
        try:
            _SUMMARY_CACHE[key] = (float(val[0]), val[1])
        except (TypeError, ValueError, IndexError):
            continue
    codex.load_cache(data.get("codex") or {})
    _LAST_SAVED_SIG = _cache_signature()


def save_caches() -> None:
    """Persist both summary caches to disk, but only when something changed."""
    global _LAST_SAVED_SIG
    try:
        sig = _cache_signature()
        if sig == _LAST_SAVED_SIG:
            return
        # Snapshot the live caches with list() before building the payload; under
        # ThreadingHTTPServer another request may be mutating them concurrently.
        payload = {
            "cc": {k: [m, s] for k, (m, s) in list(_SUMMARY_CACHE.items())},
            "codex": codex.dump_cache(),
        }
    except RuntimeError:
        return  # a concurrent request mutated a cache mid-iteration; next save retries
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(CACHE_FILE)
        _LAST_SAVED_SIG = sig
    except OSError:
        pass


def _summary_worker(path_str: str):
    """Process-pool worker: parse one Claude Code transcript into its summary."""
    p = Path(path_str)
    try:
        mtime = p.stat().st_mtime
        return (path_str, mtime, _cc_session_summary_uncached(p))
    except Exception:  # noqa: BLE001 — a bad file shouldn't abort the whole scan
        return (path_str, 0.0, None)


# Below this many cold (uncached) files we just parse serially — the process-pool
# spin-up isn't worth it for a handful of changed transcripts.
_PARALLEL_THRESHOLD = 32


def _prefill_cc_cache(files: list[Path]) -> None:
    """Warm the Claude Code summary cache for any stale files, in parallel when cold."""
    stale: list[str] = []
    for f in files:
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        cached = _SUMMARY_CACHE.get(str(f))
        if not (cached and cached[0] == mtime):
            stale.append(str(f))

    if not stale:
        return

    workers = min(os.cpu_count() or 1, 8)
    if len(stale) >= _PARALLEL_THRESHOLD and workers > 1:
        try:
            with ProcessPoolExecutor(max_workers=workers) as ex:
                for path_str, mtime, summary in ex.map(_summary_worker, stale, chunksize=8):
                    if summary is not None:
                        _SUMMARY_CACHE[path_str] = (mtime, summary)
            return
        except Exception:  # noqa: BLE001 — fall back to serial if the pool can't start
            pass

    for path_str in stale:
        try:
            cc_session_summary(Path(path_str))  # computes and caches in-process
        except (OSError, ValueError):
            continue


def cc_session_summary(path: Path) -> dict:
    """Lightweight metadata for a Claude Code session, cached by file mtime."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is not None:
        cached = _SUMMARY_CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]

    summary = _cc_session_summary_uncached(path)
    if mtime is not None:
        _SUMMARY_CACHE[key] = (mtime, summary)
    return summary


def _cc_session_summary_uncached(path: Path) -> dict:
    records = list(_iter_records(path))
    is_subagent = path.parent.name == "subagents"
    title = ""
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
            title = rec["aiTitle"]
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
            content = rec.get("message", {}).get("content")
            if isinstance(content, str) or (
                isinstance(content, list)
                and any(isinstance(b, dict) and b.get("type") == "text" for b in content)
            ):
                n_user += 1
        if t == "attachment" and not rec.get("isSidechain"):
            att = rec.get("attachment", {})
            if att.get("type") == "queued_command" and att.get("prompt"):
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
        "title": _first_user_text(records) or title or "(untitled session)",
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
        summary.update(sub_meta)
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


def parse_cc_session(path: Path) -> dict:
    """Full structured parse of one Claude Code session, ready for rendering."""
    records = list(_iter_records(path))

    # Index of this session's sub-agents so each Task/Agent call can point at the
    # transcript it spawned (rendered inline, lazily, by the frontend).
    sub_index = _subagent_index(_subagents_dir_for(path))

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
    title = ""
    meta = {}
    for rec in records:
        t = rec.get("type")
        if t == "ai-title" and rec.get("aiTitle"):
            title = rec["aiTitle"]
            continue
        if t in ("permission-mode", "last-prompt", "file-history-snapshot"):
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
            events.append(
                {
                    "kind": "system",
                    "ts": ts,
                    "subtype": rec.get("subtype"),
                    "text": rec.get("content") or rec.get("subtype") or "",
                    "is_sidechain": is_sidechain,
                }
            )
            continue

        if t == "attachment":
            att = rec.get("attachment", {})
            att_type = att.get("type")
            # A message the user queued while Claude was still working. It's a
            # genuine user prompt, but Claude Code records it as an attachment
            # (not a `user` record), so surface it as a user turn — otherwise it
            # silently vanishes from the transcript and the outline.
            if att_type == "queued_command" and att.get("prompt"):
                events.append(
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
            events.append(ev)
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
            events.append(
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
                    name = b.get("name")
                    inp = b.get("input", {})
                    block = {
                        "type": "tool_use",
                        "id": tid,
                        "name": name,
                        "input": inp,
                        "caller": b.get("caller"),
                        "result": result,
                    }
                    if name in ("Task", "Agent"):
                        prompt = inp.get("prompt", "") if isinstance(inp, dict) else ""
                        sub_file = _resolve_subagent_file(sub_index, tid, prompt)
                        if sub_file:
                            block["subagent_file"] = sub_file
                    blocks.append(block)
            if not blocks:
                continue
            usage = msg.get("usage", {}) or {}
            events.append(
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

    sub_meta = _subagent_meta(path, records)
    if sub_meta:
        meta.setdefault("cwd", decode_project_name(path.parent.parent.parent.name))
    out = {
        "agent": "claude",
        "id": path.stem,
        "title": (sub_meta["title"] if sub_meta else "")
        or _first_user_text(records)
        or title
        or "(untitled session)",
        "meta": meta,
        "events": events,
        # Every sub-agent this session spawned (flat, including nested ones),
        # surfaced as a section so they're reachable even when they can't be tied
        # to a specific Task call (e.g. fleet/teammate agents).
        "subagents": sub_index.get("agents", []),
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
    if not _CACHE_LOADED:
        load_caches()

    out: list[dict] = []

    if PROJECTS_DIR.exists():
        cc_files: list[Path] = []
        for proj_dir in sorted(PROJECTS_DIR.iterdir()):
            if not proj_dir.is_dir():
                continue
            cc_files.extend(proj_dir.glob("*.jsonl"))
            # Sub-agents live one level down: <session-id>/subagents/agent-*.jsonl
            cc_files.extend(proj_dir.glob("*/subagents/*.jsonl"))

        # Warm the cache (in parallel on a cold start) before building summaries,
        # so the expensive first scan uses every core instead of one.
        _prefill_cc_cache(cc_files)

        for f in cc_files:
            try:
                out.append(cc_session_summary(f))
            except (OSError, ValueError):
                continue

    try:
        for group in codex.list_sessions():
            for s in group.get("sessions", []):
                s["agent"] = "codex"
                out.append(s)
    except Exception:  # noqa: BLE001 — never let Codex errors hide CC sessions
        pass

    # Persist whatever we just (re)parsed so the next start is instant.
    save_caches()

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


# ---------------------------------------------------------------------------
# Full-text search across transcript content
# ---------------------------------------------------------------------------
# Cache: path -> (mtime, first_user_message_text, rest_of_text)
_TEXT_CACHE: dict[str, tuple[float, str, str]] = {}

# A hit inside the first user message is worth this many ordinary hits.
FIRST_MSG_WEIGHT = 1000


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
        res = ev.get("result")
        if isinstance(res, dict):
            add(res.get("output"))
        act = ev.get("action")
        if isinstance(act, dict):
            for q in act.get("queries") or []:
                add(q)
    return parts


def _session_segments(data: dict) -> tuple[str, str]:
    """Split a parsed session into (first user message, everything else).

    The first user message is scored heavily; the rest is searchable too, but
    weighted as ordinary content. Image blobs are skipped throughout.
    """
    first = ""
    rest: list[str] = []
    cwd = (data.get("meta") or {}).get("cwd")
    if cwd:
        rest.append(cwd)
    seen_first = False
    for ev in data.get("events", []) or []:
        if not seen_first and ev.get("kind") == "user":
            seen_first = True
            first = " ".join(_event_text(ev))
            continue
        rest.extend(_event_text(ev))
    return first, "\n".join(rest)


def session_segments(path: Path) -> tuple[str, str]:
    """(first message, rest) for one transcript, cached by file mtime."""
    try:
        st = path.stat()
    except OSError:
        return "", ""
    key = str(path)
    cached = _TEXT_CACHE.get(key)
    if cached and cached[0] == st.st_mtime:
        return cached[1], cached[2]
    try:
        data = parse_session(path)
    except Exception:  # noqa: BLE001
        data = None
    first, rest = _session_segments(data) if data else ("", "")
    _TEXT_CACHE[key] = (st.st_mtime, first, rest)
    return first, rest


def search_sessions(query: str) -> list[dict]:
    """Return [{file, snippet, score}] for sessions whose content matches `query`.

    Score = (#hits in the first user message) * FIRST_MSG_WEIGHT + (#hits elsewhere),
    so a single first-message hit outranks many scattered ones; among equal scores,
    an earlier first occurrence wins. Sorted best-first.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    out = []
    for s in list_sessions():
        first, rest = session_segments(Path(s["file"]))
        first_low, rest_low = first.lower(), rest.lower()
        c_first = first_low.count(q)
        c_rest = rest_low.count(q)
        if not c_first and not c_rest:
            continue
        score = c_first * FIRST_MSG_WEIGHT + c_rest
        # snippet from the earliest occurrence (first message preferred)
        if c_first:
            combined, idx = first, first_low.find(q)
        else:
            combined, idx = rest, rest_low.find(q)
        start = max(0, idx - 50)
        snippet = combined[start:idx + len(q) + 70].replace("\n", " ").strip()
        if start > 0:
            snippet = "…" + snippet
        out.append({"file": s["file"], "snippet": snippet, "score": score, "pos": idx if c_first else len(first) + idx})
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

    def _send_index(self):
        """Serve index.html with app.js/style.css versioned by their mtime.

        A changing query string forces the browser to fetch fresh assets after an
        update, so a stale cached app.js can never linger across runs.
        """
        try:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            self.send_error(404)
            return
        ver = 0
        for name in ("app.js", "style.css"):
            try:
                ver = max(ver, int((STATIC_DIR / name).stat().st_mtime))
            except OSError:
                pass
        html = html.replace("/app.js", f"/app.js?v={ver}").replace("/style.css", f"/style.css?v={ver}")
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
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
        # The frontend is served from disk and changes between runs; never let the
        # browser serve a stale app.js/style.css from its cache.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
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
            self._send_index()
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
            target = Path(file_arg).expanduser().resolve()
            if not target.exists():
                self._send_json({"error": "not found"}, status=404)
                return
            try:
                data = parse_session(target)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
                return
            if data is None:
                self._send_json({"error": "forbidden"}, status=403)
                return
            self._send_json(data)
            return

        self.send_error(404)


def main():
    global PROJECTS_DIR, HOST_CHECK
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=3132)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--projects-dir", type=Path, default=DEFAULT_PROJECTS_DIR)
    ap.add_argument("--codex-home", type=Path, default=codex.DEFAULT_CODEX_HOME)
    args = ap.parse_args()

    PROJECTS_DIR = args.projects_dir.expanduser()
    codex.configure(args.codex_home)
    # Enforce the Host allowlist only on the safe loopback default; if the user
    # deliberately binds elsewhere for LAN access, step aside so it still works.
    HOST_CHECK = args.host in LOOPBACK_HOSTS

    load_caches()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print("Claude Code + Codex transcript browser")
    print(f"  claude projects: {PROJECTS_DIR}")
    print(f"  codex sessions:  {codex.SESSIONS_DIR}")
    print(f"  serving at:      {url}")
    print(f"  summary cache:   {CACHE_FILE}")
    print("  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
