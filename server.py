#!/usr/bin/env python3
"""Claude Code transcript browser.

A zero-dependency local web app for browsing Claude Code session transcripts
stored under ~/.claude/projects. Run it and open the printed URL.

Usage:
    python server.py [--port 8765] [--projects-dir PATH]
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Set by main() so handlers can reach it.
PROJECTS_DIR = DEFAULT_PROJECTS_DIR


def decode_project_name(dirname: str) -> str:
    """Claude Code encodes the project cwd by replacing '/' with '-'.

    The original path isn't perfectly recoverable (dashes in real names are
    ambiguous), but we can produce a readable best-effort path.
    """
    # Leading dash means absolute path.
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
        # Skip slash-command / local-command meta blocks.
        if text.lstrip().startswith("<") and ("command-name" in text or "local-command" in text):
            continue
        text = re.sub(r"<[^>]+>", " ", text)  # strip stray tags
        text = " ".join(text.split())
        if text:
            return text[:200]
    return ""


def session_summary(path: Path) -> dict:
    """Lightweight metadata for the session list (cheap scan)."""
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
            # only count "real" user turns (those with text), not tool results
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
        "id": path.stem,
        "file": str(path),
        "title": title or _first_user_text(records) or "(untitled session)",
        "cwd": cwd or decode_project_name(path.parent.name),
        "git_branch": git_branch,
        "version": version,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "n_user": n_user,
        "n_assistant": n_assistant,
        "n_tool": n_tool,
        "n_records": len(records),
        "models": sorted(models),
        "mtime": path.stat().st_mtime,
    }


def list_sessions() -> list[dict]:
    projects = []
    if not PROJECTS_DIR.exists():
        return projects
    for proj_dir in sorted(PROJECTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        sessions = []
        for f in proj_dir.glob("*.jsonl"):
            try:
                sessions.append(session_summary(f))
            except (OSError, ValueError):
                continue
        if not sessions:
            continue
        sessions.sort(key=lambda s: s["mtime"], reverse=True)
        projects.append(
            {
                "dir": proj_dir.name,
                "path": decode_project_name(proj_dir.name),
                "sessions": sessions,
                "last_mtime": max(s["mtime"] for s in sessions),
            }
        )
    projects.sort(key=lambda p: p["last_mtime"], reverse=True)
    return projects


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


def parse_session(path: Path) -> dict:
    """Full structured parse of one session, ready for rendering.

    Produces a flat list of 'events' in chronological order. Tool results are
    attached to their originating tool_use via tool_use_id so the frontend can
    render them inline together.
    """
    records = list(_iter_records(path))

    # Index tool results (and structured toolUseResult) by tool_use_id.
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
            events.append(
                {
                    "kind": "attachment",
                    "ts": ts,
                    "att_type": att.get("type"),
                    "hook_name": att.get("hookName"),
                    "command": att.get("command"),
                    "stdout": att.get("stdout"),
                    "stderr": att.get("stderr"),
                    "exit_code": att.get("exitCode"),
                    "content": att.get("content") if isinstance(att.get("content"), str) else "",
                    "is_sidechain": is_sidechain,
                }
            )
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
                    # tool_result blocks handled separately (attached to tool_use)
            # Skip user records that are purely tool results (no human text).
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
        "id": path.stem,
        "title": title,
        "meta": meta,
        "events": events,
    }


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

        if route == "/api/sessions":
            try:
                self._send_json({"projects": list_sessions()})
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
            return

        if route == "/api/session":
            qs = parse_qs(parsed.query)
            file_arg = qs.get("file", [""])[0]
            if not file_arg:
                self._send_json({"error": "missing file param"}, status=400)
                return
            target = Path(file_arg).resolve()
            # Security: only allow files under PROJECTS_DIR.
            if PROJECTS_DIR.resolve() not in target.parents:
                self._send_json({"error": "forbidden"}, status=403)
                return
            if not target.exists():
                self._send_json({"error": "not found"}, status=404)
                return
            try:
                self._send_json(parse_session(target))
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": str(e)}, status=500)
            return

        self.send_error(404)


def main():
    global PROJECTS_DIR
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=3132)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--projects-dir", type=Path, default=DEFAULT_PROJECTS_DIR)
    args = ap.parse_args()

    PROJECTS_DIR = args.projects_dir
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Claude Code transcript browser")
    print(f"  projects dir: {PROJECTS_DIR}")
    print(f"  serving at:   {url}")
    print("  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
