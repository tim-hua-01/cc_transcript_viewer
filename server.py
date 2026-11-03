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
event shapes), and catalog.py merges them into one session list (with
custom names, summary-cache persistence, and Mica decoration). This module is
the HTTP layer on top: the JSON API, the local-file actions, the prompt
export, and wiring for the full-text search index (search_index.py, built
from search_text.py). Bundling a session into a shareable single-file HTML
export lives in export_html.py.

Optional Mica: when the Mica daemon is capturing transcripts (see mica/),
the viewer reads its store (never writes it) to flag transcripts
that were truncated, rewritten, or deleted after capture, and to list captured
copies of transcripts that no longer exist on disk.

Usage:
    python server.py [--port 3132] [--host 127.0.0.1]
                     [--projects-dir PATH] [--codex-home PATH]
                     [--cursor-db PATH] [--cursor-projects-dir PATH]
                     [--cursor-chats-dir PATH] [--opencode-db PATH]
                     [--custom-names-file PATH]
                     [--mica PATH | --no-mica]
                     [--search-index PATH] [--reset-cache]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import claude_parser as claude
import codex_parser as codex
import catalog
import common
import cursor_parser as cursor
import export_html
import opencode_parser as opencode
import search_index
import settings_check
from mica import store as micastore

STATIC_DIR = export_html.STATIC_DIR

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

DEFAULT_SEARCH_INDEX_FILE = catalog.CACHE_FILE.with_name("search-index.sqlite3")
# The full-text search index, set by configure_search(); None leaves search
# off (tests that don't configure one never touch the real cache).
SEARCH: search_index.SearchIndex | None = None

# File types that macOS may execute or install when opened. Local-file links
# are for source/documents, never for launching transcript-supplied programs.
UNSAFE_OPEN_SUFFIXES = {
    ".app", ".command", ".inetloc", ".pkg", ".scpt", ".terminal",
    ".url", ".webloc", ".workflow",
}


def list_sessions() -> list[dict]:
    """catalog.list_sessions(), also telling the search index about the list."""
    sessions = catalog.list_sessions()
    if SEARCH is not None:
        SEARCH.notify(sessions)
    return sessions


def reveal_transcript_file(file_id: str) -> Path:
    """Reveal a transcript file in Finder.

    Only transcripts under a known root can be revealed, and `open -R` selects
    the file in Finder rather than launching it, so this cannot be turned into
    a way to run something.
    """
    target = catalog.resolve_transcript_file(file_id)
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
    data = catalog.load_session(file_id)
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
    if not catalog.is_within(target, workspace):
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
# Full-text search (the index itself lives in search_index.py)
# ---------------------------------------------------------------------------
def configure_search(path, workers: int = search_index.DEFAULT_WORKERS) -> None:
    """Use the search index at ``path`` (closing any previous one)."""
    global SEARCH
    if SEARCH is not None:
        SEARCH.close()
    SEARCH = search_index.SearchIndex(
        path, catalog.load_session, workers=workers,
        worker_setup=lambda: (catalog.configure_sources, (catalog.source_settings(),)),
    )


def search_status() -> dict | None:
    return SEARCH.status() if SEARCH is not None else None


def search_sessions(query: str, *, wait: bool = False) -> list[dict] | None:
    """[{file, snippet, score, pos}] for sessions matching ``query``, best
    first, or None when a newer search replaced this one. Answers from what is
    indexed unless ``wait`` brings the index up to date first."""
    if SEARCH is None or not common.normalize_search_text(query):
        return []
    return SEARCH.search(query, list_sessions(), wait=wait)


# ---------------------------------------------------------------------------
# Prompt export: every user prompt from a set of sessions, as one Markdown
# document (for pasting into another model and asking "when did I say X?").
# ---------------------------------------------------------------------------
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROMPT_EXPORT_MAX_SESSIONS = 2000
_AGENT_LABELS = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor", "opencode": "opencode"}


def _prompt_text(ev: dict) -> str:
    """The human-typed text of a `user` event (either message shape)."""
    blocks = ev.get("blocks")
    if not blocks:
        return (ev.get("text") or "").strip()
    parts = [b.get("text") for b in blocks if b.get("type") == "text" and b.get("text")]
    if any(b.get("type") == "image" for b in blocks):
        parts.append("[image attached]")
    return "\n".join(parts).strip()


def _local_day_and_time(ts) -> tuple[str, str]:
    """("YYYY-MM-DD", "HH:MM") in local time for an ISO timestamp, or ("", "")."""
    if not isinstance(ts, str) or not ts:
        return "", ""
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return "", ""
    local = parsed.astimezone() if parsed.tzinfo else parsed
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def _session_prompts(data: dict, from_day: str, to_day: str) -> list[tuple[str, str, str, bool]]:
    """[(day, time, text, in_abandoned_branch)] for each real user prompt
    inside the local-day window; an undated prompt is kept only when no
    window is set."""
    out: list[tuple[str, str, str, bool]] = []

    def walk(events, abandoned: bool) -> None:
        for ev in events or []:
            if ev.get("kind") == "branch":
                for group in ev.get("groups", []) or []:
                    walk(group, True)
            elif ev.get("kind") == "user":
                text = _prompt_text(ev)
                if not text:
                    continue
                day, time = _local_day_and_time(ev.get("ts"))
                if from_day or to_day:
                    if not day:
                        continue
                    if from_day and day < from_day:
                        continue
                    if to_day and day > to_day:
                        continue
                out.append((day, time, text, abandoned))

    walk(data.get("events"), False)
    return out


def prompts_markdown(file_ids: list[str], from_day: str = "", to_day: str = "") -> dict:
    """Build the Markdown prompt digest for `file_ids` (in the given order).

    Only sessions the sidebar lists are included, and sub-agents are skipped:
    their "user" turns are the parent agent's instructions, not the person.
    Returns {"markdown", "sessions", "prompts"}.
    """
    listed = {s["file"]: s for s in list_sessions() if not s.get("is_subagent")}
    seen: set[str] = set()
    sections: list[str] = []
    n_sessions = n_prompts = 0
    for file_id in file_ids:
        summary = listed.get(file_id)
        if summary is None or file_id in seen:
            continue
        seen.add(file_id)
        try:
            data = catalog.load_session(file_id)
        except Exception:  # noqa: BLE001 — one unreadable session must not sink the export
            data = None
        if not data:
            continue
        prompts = _session_prompts(data, from_day, to_day)
        if not prompts:
            continue
        n_sessions += 1
        n_prompts += len(prompts)
        label = _AGENT_LABELS.get(summary.get("agent"), summary.get("agent") or "")
        title = (summary.get("title") or data.get("title") or "(untitled session)").strip()
        lines = [f"## [{label}] {title}"]
        cwd = (data.get("meta") or {}).get("cwd") or summary.get("cwd")
        if cwd:
            lines.append(f"- Project: {cwd}")
        if summary.get("git_branch"):
            lines.append(f"- Branch: {summary['git_branch']}")
        last_day, last_time = _local_day_and_time(summary.get("last_ts"))
        if last_day:
            lines.append(f"- Last active: {last_day} {last_time}")
        lines.append(f"- Session: {summary.get('id') or data.get('id') or file_id}")
        lines.append(f"- Prompts: {len(prompts)}")
        lines.append("")
        for day, time, text, abandoned in prompts:
            when = f"{day} {time}".strip() or "undated"
            suffix = " (abandoned branch)" if abandoned else ""
            lines.append(f"### {when}{suffix}")
            lines.append("")
            lines.append(text)
            lines.append("")
        sections.append("\n".join(lines))

    if from_day and to_day:
        span = f"{from_day} to {to_day}"
    elif from_day:
        span = f"since {from_day}"
    elif to_day:
        span = f"through {to_day}"
    else:
        span = "all time"
    head = [
        f"# My prompts, {span}",
        "",
        f"{n_prompts} prompts across {n_sessions} sessions, in the viewer's current order "
        "(newest activity first unless a search is active). Each `##` is one session; "
        "each `###` is one prompt with its local date and time.",
        "",
    ]
    return {
        "markdown": "\n".join(head + sections).rstrip() + "\n",
        "sessions": n_sessions,
        "prompts": n_prompts,
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
# The frontend's three files, served with explicit types (see _send_file for
# the no-store rationale).
_STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quieter logs
        pass

    def _send_bytes(self, body: bytes, content_type: str, status=200, extra_headers=()):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            for name, value in extra_headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers routinely abandon an in-flight polling response during a
            # reload/navigation. There is no client left to receive an error.
            self.close_connection = True

    def _send_json(self, obj, status=200):
        self._send_bytes(json.dumps(obj).encode("utf-8"), "application/json", status)

    def _send_file(self, path: Path, content_type: str):
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(404)
            return
        # The viewer's own assets change under a long-lived server; heuristic
        # browser caching otherwise serves a stale UI after an edit.
        headers = (("Cache-Control", "no-store"),) if path.parent == STATIC_DIR else ()
        self._send_bytes(body, content_type, extra_headers=headers)

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

    def _read_json_body(self, max_len: int) -> dict | None:
        """The parsed JSON object body of a POST/PUT, or None after answering
        with the right 4xx.

        application/json cannot be submitted by a cross-origin HTML form, and
        browser fetches from another origin require a CORS preflight this
        server never authorizes — so requiring it keeps the write endpoints
        unreachable from the web.
        """
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._send_json({"error": "expected application/json"}, status=415)
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > max_len:
            self._send_json({"error": "invalid request size"}, status=400)
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON"}, status=400)
            return None
        if not isinstance(body, dict):
            self._send_json({"error": "expected a JSON object"}, status=400)
            return None
        return body

    def _session_file_arg(self, parsed) -> str | None:
        """The ?file= session id, or None after answering with the 400/404 the
        shared /api/session-shaped preamble owes the client.

        Cursor IDE/CLI and opencode sessions use synthetic schemes (no path on
        disk); everything else is a real path confined to an allowed root.
        """
        file_arg = parse_qs(parsed.query).get("file", [""])[0]
        if not file_arg:
            self._send_json({"error": "missing file param"}, status=400)
            return None
        if not file_arg.startswith(catalog.SYNTHETIC_SCHEMES):
            target = Path(file_arg).expanduser().resolve()
            if not target.exists():
                self._send_json({"error": "not found"}, status=404)
                return None
        return file_arg

    def do_GET(self):
        if HOST_CHECK and not self._host_allowed():
            self.send_error(403, "Host not allowed")
            return

        parsed = urlparse(self.path)
        route = parsed.path

        static = _STATIC_ROUTES.get(route)
        if static:
            self._send_file(STATIC_DIR / static[0], static[1])
            return

        if route == common.LOCAL_IMAGE_ROUTE:
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
            content_type = common.image_mime(t)
            if not content_type:
                self._send_json({"error": "not an image"}, status=400)
                return
            self._send_file(t, content_type)
            return

        if route == "/api/sessions":
            try:
                self._send_json({"sessions": list_sessions(), "mica": catalog.mica_status(),
                                 "search": search_status(),
                                 "setup": settings_check.check_settings(claude.PROJECTS_DIR.parent, codex.CODEX_HOME)})
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
                mtime = catalog.session_file_mtime(file_arg)
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

        if route == "/api/mica-compare":
            file_arg = self._session_file_arg(parsed)
            if file_arg is None:
                return
            try:
                self._send_json(catalog.mica_compare(file_arg))
            except FileNotFoundError:
                self._send_json({"error": "not found"}, status=404)
            except PermissionError:
                self._send_json({"error": "forbidden"}, status=403)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/search":
            qs = parse_qs(parsed.query)
            q = qs.get("q", [""])[0]
            try:
                matches = search_sessions(q)
                body = {"matches": matches or [], "search": search_status()}
                if matches is None:
                    body["superseded"] = True  # a newer search is running
                self._send_json(body)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/session":
            file_arg = self._session_file_arg(parsed)
            if file_arg is None:
                return
            try:
                data = catalog.load_session(file_arg)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
                return
            if data is None:
                self._send_json({"error": "forbidden"}, status=403)
                return
            self._send_json(data)
            return

        if route == "/api/export":
            file_arg = self._session_file_arg(parsed)
            if file_arg is None:
                return
            try:
                data = catalog.load_session(file_arg)
                if data is None:
                    self._send_json({"error": "forbidden"}, status=403)
                    return
                body = export_html.build_standalone_html(data).encode("utf-8")
                filename = export_html.export_filename(data)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
                return
            # export_filename() emits only [A-Za-z0-9-] plus ".html", so the
            # quoted form needs no further escaping.
            self._send_bytes(
                body,
                "text/html; charset=utf-8",
                extra_headers=(("Content-Disposition", f'attachment; filename="{filename}"'),),
            )
            return

        self.send_error(404)

    def do_POST(self):
        if HOST_CHECK and not self._host_allowed():
            self.send_error(403, "Host not allowed")
            return

        route = urlparse(self.path).path
        if route == "/api/prompts":
            self._post_prompts()
            return
        if route not in ("/api/open-local", "/api/reveal-transcript"):
            self.send_error(404)
            return
        body = self._read_json_body(max_len=16384)
        if body is None:
            return
        file_id = body.get("file")
        if not isinstance(file_id, str) or not file_id:
            self._send_json({"error": "file must be a string"}, status=400)
            return
        path_value = body.get("path")
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

    def _post_prompts(self):
        """POST /api/prompts {files: [...], from?: "YYYY-MM-DD", to?: "YYYY-MM-DD"}
        -> {markdown, sessions, prompts}. Session ids are a long list, hence
        the larger body cap; each one still has to be a listed session."""
        body = self._read_json_body(max_len=512 * 1024)
        if body is None:
            return
        files = body.get("files")
        if (
            not isinstance(files, list)
            or len(files) > PROMPT_EXPORT_MAX_SESSIONS
            or not all(isinstance(f, str) and f for f in files)
        ):
            self._send_json({"error": "files must be a list of session ids"}, status=400)
            return
        days = []
        for key in ("from", "to"):
            value = body.get(key) or ""
            if not isinstance(value, str) or (value and not _DAY_RE.match(value)):
                self._send_json({"error": f"{key} must be YYYY-MM-DD"}, status=400)
                return
            days.append(value)
        try:
            result = prompts_markdown(files, *days)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, status=500)
            return
        self._send_json(result)

    def do_PUT(self):
        if HOST_CHECK and not self._host_allowed():
            self.send_error(403, "Host not allowed")
            return

        if urlparse(self.path).path != "/api/session-name":
            self.send_error(404)
            return
        body = self._read_json_body(max_len=4096)
        if body is None:
            return
        file_id = body.get("file")
        name = body.get("name")
        if not isinstance(file_id, str) or not isinstance(name, str):
            self._send_json({"error": "file and name must be strings"}, status=400)
            return
        name = " ".join(name.split())
        if len(name) > 200:
            self._send_json({"error": "name must be at most 200 characters"}, status=400)
            return
        try:
            data = catalog.load_session(file_id)
            if data is None:
                self._send_json({"error": "session not found or forbidden"}, status=404)
                return
            catalog.set_custom_name(data, name)
            catalog.apply_custom_name(data)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, status=500)
            return
        self._send_json({
            "title": data["title"],
            "original_title": data["original_title"],
            "custom_title": data["custom_title"],
        })


def main():
    global HOST_CHECK
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=3132)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--projects-dir", type=Path, default=claude.DEFAULT_PROJECTS_DIR)
    ap.add_argument("--codex-home", type=Path, default=codex.DEFAULT_CODEX_HOME)
    ap.add_argument(
        "--custom-names-file",
        type=Path,
        default=catalog.DEFAULT_CUSTOM_NAMES_FILE,
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
    ap.add_argument(
        "--mica",
        type=Path,
        default=micastore.DEFAULT_STORE_DIR,
        help="Mica to check transcripts against (used when it exists)",
    )
    ap.add_argument("--no-mica", action="store_true", help="ignore any Mica")
    ap.add_argument(
        "--search-index",
        type=Path,
        default=DEFAULT_SEARCH_INDEX_FILE,
        help="full-text search index (an SQLite file; rebuilt if deleted)",
    )
    ap.add_argument(
        "--reset-cache",
        action="store_true",
        help="delete the search index and sidebar summary cache, then exit",
    )
    args = ap.parse_args()

    if args.reset_cache:
        removed = search_index.remove_index_files(args.search_index.expanduser())
        if catalog.CACHE_FILE.exists():
            catalog.CACHE_FILE.unlink()
            removed.append(catalog.CACHE_FILE)
        for path in removed:
            print(f"removed {path}")
        print("caches reset; they rebuild the next time the viewer runs" if removed
              else "no caches to reset")
        return

    catalog.configure_sources({
        "projects_dir": args.projects_dir,
        "codex_home": args.codex_home,
        "cursor_db": args.cursor_db,
        "cursor_projects": args.cursor_projects_dir,
        "cursor_chats": args.cursor_chats_dir,
        "opencode_db": args.opencode_db,
        "mica": None if args.no_mica else args.mica,
        "custom_names": args.custom_names_file,
    })
    configure_search(args.search_index)
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
    if catalog.MICA is not None:
        print(f"  Mica:     {catalog.MICA.root}")
    print(f"  search index:    {SEARCH.path}")
    print(f"  serving at:      {url}")
    print("  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        SEARCH.close()


if __name__ == "__main__":
    main()
