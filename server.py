#!/usr/bin/env python3
"""Unified Claude Code + Codex + Cursor + opencode transcript browser.

A zero-dependency local web app for browsing Claude Code session transcripts
(under ~/.claude/projects), Codex session transcripts (under ~/.codex/sessions),
Cursor IDE conversations (from Cursor's state.vscdb), Cursor CLI agent
transcripts (under ~/.cursor/projects/.../agent-transcripts), and opencode
sessions (from ~/.local/share/opencode/opencode.db) in a single, time-sorted
sidebar. Run it and open the printed URL.

Parsing lives in claude_parser.py / codex_parser.py / cursor_parser.py /
opencode_parser.py (one module per transcript source, all emitting the same
event shapes); this module is the HTTP layer plus what spans sources: the
unified session list, full-text search, viewer-owned custom names, and
summary-cache persistence. Bundling a session into a shareable single-file
HTML export lives in export_html.py.

Usage:
    python server.py [--port 3132] [--projects-dir PATH] [--codex-home PATH]
                     [--cursor-db PATH] [--cursor-projects-dir PATH]
                     [--cursor-chats-dir PATH] [--opencode-db PATH]
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import claude_parser as claude
import codex_parser as codex
import cursor_parser as cursor
import export_html
import opencode_parser as opencode

STATIC_DIR = Path(__file__).parent / "static"
# Session ids that name a row in a database rather than a file on disk. These
# never resolve to a transcript path, so every path-based check skips them.
SYNTHETIC_SCHEMES = (
    cursor.SESSION_SCHEME,
    cursor.CLI_SESSION_SCHEME,
    opencode.SESSION_SCHEME,
)
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
CUSTOM_NAMES_FILE = DEFAULT_CUSTOM_NAMES_FILE
CACHE_FILE = Path.home() / ".cache" / "transcript_viewer" / "summaries.json"

# File types that macOS may execute or install when opened. Local-file links
# are for source/documents, never for launching transcript-supplied programs.
UNSAFE_OPEN_SUFFIXES = {
    ".app", ".command", ".inetloc", ".pkg", ".scpt", ".terminal",
    ".url", ".webloc", ".workflow",
}


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
# Summary-cache persistence (the parsers own the caches; we own the file)
# ---------------------------------------------------------------------------
_CACHE_VERSION = 2
_CACHE_LOCK = threading.Lock()
_CACHE_LOADED = False
_PARSER_CACHES = {
    "claude": claude.SUMMARY_CACHE,
    "codex": codex.SUMMARY_CACHE,
    "cursor": cursor.SUMMARY_CACHE,
    "opencode": opencode.SUMMARY_CACHE,
}


def load_summary_caches() -> None:
    """Load viewer-owned sidebar summaries from disk once, best effort."""
    global _CACHE_LOADED
    with _CACHE_LOCK:
        if _CACHE_LOADED:
            return
        _CACHE_LOADED = True
        try:
            payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("version") != _CACHE_VERSION:
            return
        for name, cache in _PARSER_CACHES.items():
            cache.load(payload.get(name) or {})


def save_summary_caches() -> None:
    """Atomically persist a snapshot of every parser's summary cache, best effort."""
    if not any(cache.dirty for cache in _PARSER_CACHES.values()):
        return
    snapshots = {name: cache.snapshot() for name, cache in _PARSER_CACHES.items()}
    payload: dict = {"version": _CACHE_VERSION}
    payload.update({name: data for name, (_gen, data) in snapshots.items()})
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
        temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temp.replace(CACHE_FILE)
    except OSError:
        return
    # Clear each dirty flag only if nothing changed since its snapshot, so a
    # concurrent update is never marked as saved.
    for name, (generation, _data) in snapshots.items():
        _PARSER_CACHES[name].mark_saved(generation)


# ---------------------------------------------------------------------------
# Unified session list / dispatch
# ---------------------------------------------------------------------------
def list_sessions() -> list[dict]:
    """Flat list of every Claude Code, Codex, Cursor, and opencode session, newest first."""
    load_summary_caches()
    out: list[dict] = claude.list_sessions()

    try:
        out.extend(codex.list_sessions())
    except Exception:  # noqa: BLE001 — never let Codex errors hide CC sessions
        pass

    try:
        out.extend(cursor.list_sessions())
    except Exception:  # noqa: BLE001 — never let Cursor errors hide other sessions
        pass

    try:
        out.extend(opencode.list_sessions())
    except Exception:  # noqa: BLE001 — never let opencode errors hide other sessions
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
    if _under(target, claude.PROJECTS_DIR):
        return claude.parse_session(target)
    if _under(target, codex.SESSIONS_DIR) or (
        codex.ARCHIVED_SESSIONS_DIR.exists() and _under(target, codex.ARCHIVED_SESSIONS_DIR)
    ):
        return codex.parse_session(target)
    if cursor.is_cli_transcript(target):
        return cursor.parse_cli_session(target)
    return None


def load_session(file_id: str) -> dict | None:
    """Resolve a session id to parsed data, for both `/api/session` and search.

    Cursor IDE sessions use ``cursordb:<composerId>``; Cursor CLI store.db
    sessions use ``cursorcli:<sessionId>``; opencode sessions use
    ``opencode:<sessionID>``. Everything else is a real transcript path that
    must resolve under an allowed root.
    """
    if file_id.startswith(cursor.SESSION_SCHEME):
        data = cursor.parse_session_by_id(file_id[len(cursor.SESSION_SCHEME):])
        return _apply_custom_name(data) if data is not None else None
    if file_id.startswith(cursor.CLI_SESSION_SCHEME):
        data = cursor.parse_cli_store_by_id(file_id[len(cursor.CLI_SESSION_SCHEME):])
        return _apply_custom_name(data) if data is not None else None
    if file_id.startswith(opencode.SESSION_SCHEME):
        data = opencode.parse_session_by_id(file_id[len(opencode.SESSION_SCHEME):])
        return _apply_custom_name(data) if data is not None else None
    target = Path(file_id).expanduser().resolve()
    if not target.exists():
        return None
    data = parse_session(target)
    return _apply_custom_name(data) if data is not None else None


def resolve_transcript_file(file_id: str) -> Path:
    """Resolve a session id to its on-disk transcript under an allowed root.

    Raises FileNotFoundError if the id is synthetic (Cursor and opencode
    database sessions have no transcript file) or missing, and PermissionError
    if the path lies outside every transcript root.
    """
    if file_id.startswith(SYNTHETIC_SCHEMES):
        raise FileNotFoundError(file_id)
    target = Path(file_id).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(file_id)
    allowed = (
        _under(target, claude.PROJECTS_DIR)
        or _under(target, codex.SESSIONS_DIR)
        or (
            codex.ARCHIVED_SESSIONS_DIR.exists()
            and _under(target, codex.ARCHIVED_SESSIONS_DIR)
        )
        or cursor.is_cli_transcript(target)
    )
    if not allowed:
        raise PermissionError(file_id)
    return target


def session_file_mtime(file_id: str) -> float | None:
    """Return a real transcript's mtime without parsing any session content.

    Synthetic database ids return None; their change detection continues to use
    the regular session-list refresh.
    """
    if file_id.startswith(SYNTHETIC_SCHEMES):
        return None
    return resolve_transcript_file(file_id).stat().st_mtime


def reveal_transcript_file(file_id: str) -> Path:
    """Reveal a transcript file in Finder.

    Only transcripts under a known root can be revealed, and `open -R` selects
    the file in Finder rather than launching it, so this cannot be turned into
    a way to run something.
    """
    target = resolve_transcript_file(file_id)
    if sys.platform != "darwin":
        raise NotImplementedError("revealing files is currently supported on macOS")
    subprocess.run(
        ["/usr/bin/open", "-R", str(target)],
        check=True,
        timeout=5,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return target


def open_local_file(file_id: str, path_value: str) -> Path:
    """Open a transcript-linked workspace file with the OS default app.

    The target must resolve inside the session's cwd and must not itself be an
    executable/application. This endpoint is intentionally narrower than the
    image viewer: clicking model-authored text must never become a launcher for
    arbitrary programs elsewhere on the machine.
    """
    data = load_session(file_id)
    if data is None:
        raise FileNotFoundError("session not found")
    cwd = (data.get("meta") or {}).get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise PermissionError("session has no workspace directory")
    workspace = Path(cwd).expanduser().resolve()
    raw_target = Path(path_value).expanduser()
    target = (raw_target if raw_target.is_absolute() else workspace / raw_target).resolve()
    if not target.exists():
        raise FileNotFoundError("linked file not found")
    if not _under(target, workspace):
        raise PermissionError("linked file is outside the session workspace")
    if not (target.is_file() or target.is_dir()):
        raise PermissionError("linked path is not a regular file or directory")
    if target.is_file() and (
        target.suffix.lower() in UNSAFE_OPEN_SUFFIXES or os.access(target, os.X_OK)
    ):
        raise PermissionError("executable files cannot be opened from transcripts")
    if sys.platform != "darwin":
        raise NotImplementedError("opening local files is currently supported on macOS")

    subprocess.run(
        ["/usr/bin/open", str(target)],
        check=True,
        timeout=5,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return target


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
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers routinely abandon an in-flight polling response during a
            # reload/navigation. There is no client left to receive an error.
            self.close_connection = True

    def _send_file(self, path: Path, content_type: str):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            # The viewer's own assets change under a long-lived server; heuristic
            # browser caching otherwise serves a stale UI after an edit.
            if path.parent == STATIC_DIR:
                self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

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

        if route == "/api/session-state":
            qs = parse_qs(parsed.query)
            file_arg = qs.get("file", [""])[0]
            if not file_arg:
                self._send_json({"error": "missing file param"}, status=400)
                return
            try:
                mtime = session_file_mtime(file_arg)
            except FileNotFoundError:
                self._send_json({"error": "not found"}, status=404)
                return
            except PermissionError:
                self._send_json({"error": "forbidden"}, status=403)
                return
            if mtime is None:
                self._send_json({"supported": False})
            else:
                self._send_json({"supported": True, "mtime": mtime})
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
            # Cursor IDE/CLI and opencode sessions use synthetic schemes (no
            # path on disk); everything else is a real path confined to an
            # allowed root.
            if not file_arg.startswith(SYNTHETIC_SCHEMES):
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

        if route == "/api/export":
            qs = parse_qs(parsed.query)
            file_arg = qs.get("file", [""])[0]
            if not file_arg:
                self._send_json({"error": "missing file param"}, status=400)
                return
            if not file_arg.startswith(SYNTHETIC_SCHEMES):
                target = Path(file_arg).expanduser().resolve()
                if not target.exists():
                    self._send_json({"error": "not found"}, status=404)
                    return
            try:
                data = load_session(file_arg)
                if data is None:
                    self._send_json({"error": "forbidden"}, status=403)
                    return
                body = export_html.build_standalone_html(data).encode("utf-8")
                filename = export_html.export_filename(data)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                # export_filename() emits only [A-Za-z0-9-] plus ".html", so the
                # quoted form needs no further escaping.
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{filename}"'
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
            return

        self.send_error(404)

    def do_POST(self):
        if HOST_CHECK and not self._host_allowed():
            self.send_error(403, "Host not allowed")
            return

        route = urlparse(self.path).path
        if route not in ("/api/open-local", "/api/reveal-transcript"):
            self.send_error(404)
            return
        # application/json cannot be submitted by a cross-origin HTML form;
        # browser fetches from another origin require a CORS preflight, which
        # this server does not authorize.
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._send_json({"error": "expected application/json"}, status=415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 16384:
            self._send_json({"error": "invalid request size"}, status=400)
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON"}, status=400)
            return
        file_id = body.get("file") if isinstance(body, dict) else None
        if not isinstance(file_id, str) or not file_id:
            self._send_json({"error": "file must be a string"}, status=400)
            return
        path_value = body.get("path") if isinstance(body, dict) else None
        if route == "/api/open-local":
            if not isinstance(path_value, str):
                self._send_json({"error": "path must be a string"}, status=400)
                return
            if not path_value or len(path_value) > 8192 or "\x00" in path_value:
                self._send_json({"error": "invalid path"}, status=400)
                return
        try:
            opened = (
                reveal_transcript_file(file_id)
                if route == "/api/reveal-transcript"
                else open_local_file(file_id, path_value)
            )
        except FileNotFoundError as e:
            self._send_json({"error": str(e)}, status=404)
            return
        except PermissionError as e:
            self._send_json({"error": str(e)}, status=403)
            return
        except NotImplementedError as e:
            self._send_json({"error": str(e)}, status=501)
            return
        except (OSError, subprocess.SubprocessError) as e:
            self._send_json({"error": f"could not open file: {e}"}, status=500)
            return
        self._send_json({"opened": str(opened)})

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
    global CUSTOM_NAMES_FILE, HOST_CHECK
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=3132)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--projects-dir", type=Path, default=claude.DEFAULT_PROJECTS_DIR)
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
    ap.add_argument(
        "--cursor-projects-dir",
        type=Path,
        default=cursor.DEFAULT_PROJECTS_DIR,
        help="Cursor projects dir containing agent-transcripts (default ~/.cursor/projects)",
    )
    ap.add_argument(
        "--cursor-chats-dir",
        type=Path,
        default=cursor.DEFAULT_CHATS_DIR,
        help="Cursor chats dir containing per-session store.db (default ~/.cursor/chats)",
    )
    ap.add_argument(
        "--opencode-db",
        type=Path,
        default=opencode.DEFAULT_DB_PATH,
        help="opencode.db (or the opencode data dir holding it)",
    )
    args = ap.parse_args()

    CUSTOM_NAMES_FILE = args.custom_names_file.expanduser()
    claude.configure(args.projects_dir)
    codex.configure(args.codex_home)
    cursor.configure(
        args.cursor_db,
        projects_dir=args.cursor_projects_dir,
        chats_dir=args.cursor_chats_dir,
    )
    opencode.configure(args.opencode_db)
    # Enforce the Host allowlist only on the safe loopback default; if the user
    # deliberately binds elsewhere for LAN access, step aside so it still works.
    HOST_CHECK = args.host in LOOPBACK_HOSTS

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print("Claude Code + Codex + Cursor + opencode transcript browser")
    print(f"  claude projects: {claude.PROJECTS_DIR}")
    print(f"  codex sessions:  {codex.SESSIONS_DIR}")
    print(f"  cursor db:       {cursor.DB_PATH}")
    print(f"  cursor projects: {cursor.PROJECTS_DIR}")
    print(f"  cursor chats:    {cursor.CHATS_DIR}")
    print(f"  opencode db:     {opencode.DB_PATH}")
    print(f"  serving at:      {url}")
    print("  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
