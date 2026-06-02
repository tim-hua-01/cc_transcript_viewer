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
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import codex_server as codex

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

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


def cc_session_summary(path: Path) -> dict:
    """Lightweight metadata for a Claude Code session (cheap scan)."""
    records = list(_iter_records(path))
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
        if t == "user" and not rec.get("isSidechain"):
            content = rec.get("message", {}).get("content")
            if isinstance(content, str) or (
                isinstance(content, list)
                and any(isinstance(b, dict) and b.get("type") == "text" for b in content)
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

    return {
        "agent": "claude",
        "id": path.stem,
        "file": str(path),
        "title": _first_user_text(records) or title or "(untitled session)",
        "cwd": cwd or decode_project_name(path.parent.name),
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

    return {
        "agent": "claude",
        "id": path.stem,
        "title": _first_user_text(records) or title or "(untitled session)",
        "meta": meta,
        "events": events,
    }


# ---------------------------------------------------------------------------
# Unified session list / dispatch
# ---------------------------------------------------------------------------
def list_sessions() -> list[dict]:
    """Flat list of every Claude Code and Codex session, newest first."""
    out: list[dict] = []

    if PROJECTS_DIR.exists():
        for proj_dir in sorted(PROJECTS_DIR.iterdir()):
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob("*.jsonl"):
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

    out.sort(key=lambda s: s.get("mtime") or 0, reverse=True)
    return out


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

    def do_GET(self):
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
    global PROJECTS_DIR
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=3132)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--projects-dir", type=Path, default=DEFAULT_PROJECTS_DIR)
    ap.add_argument("--codex-home", type=Path, default=codex.DEFAULT_CODEX_HOME)
    args = ap.parse_args()

    PROJECTS_DIR = args.projects_dir.expanduser()
    codex.configure(args.codex_home)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print("Claude Code + Codex transcript browser")
    print(f"  claude projects: {PROJECTS_DIR}")
    print(f"  codex sessions:  {codex.SESSIONS_DIR}")
    print(f"  serving at:      {url}")
    print("  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
