#!/usr/bin/env python3
"""Bundle one parsed session into a single self-contained HTML file.

The export is the viewer's own UI frozen around one transcript: `static/`'s
stylesheet and script are inlined verbatim and the parsed session is embedded as
a JSON literal, so the saved page renders through exactly the same code path as
the live app. That is the point of building it this way — a second, export-only
renderer would drift from the real one the moment either side changed.

What the file does *not* carry is the server: `app.js` sees the embedded payload,
switches to standalone mode, and drops every feature that needs an HTTP
endpoint (session list, live polling, rename, reveal-in-Finder, open-local-file).
Locally-referenced images are read off disk and re-embedded as `data:` URIs so
they survive the trip too.

Markdown/KaTeX still come from the CDN (reusing index.html's pinned,
SRI-hashed tags): inlining them would multiply the file size, and the viewer
already degrades to plain text when they fail to load.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

STATIC_DIR = Path(__file__).parent / "static"

# Markers replaced in index.html to produce the standalone document. Each must
# match exactly once; a silent miss would ship an export that fetches from a
# server that isn't there, so a missing marker is an error.
STYLE_LINK = '<link rel="stylesheet" href="/style.css" />'
APP_SCRIPT = '<script src="/app.js"></script>'
TITLE_TAG = "<title>Transcript Viewer</title>"

# Per-image and whole-document ceilings for re-embedding local images. Base64
# inflates by 4/3, and a shareable file that needs a download manager is not
# shareable; oversized images degrade to the viewer's "image omitted" note.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 24 * 1024 * 1024

_LOCAL_IMAGE_PREFIX = "/api/local-image?path="


def _local_image_path(src: str) -> Path | None:
    """The on-disk path a `/api/local-image` src points at, if it is one."""
    if not isinstance(src, str) or not src.startswith(_LOCAL_IMAGE_PREFIX):
        return None
    query = parse_qs(urlparse(src).query)
    raw = (query.get("path") or [""])[0]
    if not raw:
        return None
    return Path(unquote(raw))


def _data_uri(path: Path, declared_type: str = "") -> str | None:
    content_type = declared_type or mimetypes.guess_type(str(path))[0] or ""
    if not content_type.startswith("image/"):
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return "data:" + content_type + ";base64," + base64.b64encode(raw).decode("ascii")


def inline_local_images(node, budget: list[int] | None = None):
    """Rewrite `/api/local-image` srcs into `data:` URIs, in place.

    `budget` is a one-element list holding the remaining byte allowance, shared
    across the whole walk. An image that is missing, unreadable, too big, or
    past the budget loses its `src` and gains a `reason`, which the viewer
    renders as an "image omitted" note rather than a broken image.
    """
    if budget is None:
        budget = [MAX_IMAGE_TOTAL_BYTES]

    if isinstance(node, list):
        for item in node:
            inline_local_images(item, budget)
        return node
    if not isinstance(node, dict):
        return node

    path = _local_image_path(node.get("src", ""))
    if path is not None:
        size = node.get("bytes")
        if not isinstance(size, int) or size <= 0:
            try:
                size = path.stat().st_size
            except OSError:
                size = None
        if size is None:
            node["src"] = ""
            node["reason"] = "local image unreadable"
        elif size > MAX_IMAGE_BYTES or size > budget[0]:
            node["src"] = ""
            node["reason"] = "local image too large to embed"
        else:
            uri = _data_uri(path, node.get("content_type", ""))
            if uri is None:
                node["src"] = ""
                node["reason"] = "local image unreadable"
            else:
                node["src"] = uri
                budget[0] -= size

    for value in node.values():
        inline_local_images(value, budget)
    return node


def _json_literal(payload: dict) -> str:
    """JSON safe to drop inside a <script> element.

    `ensure_ascii` escapes U+2028/U+2029 (newlines to a JS parser, not to a JSON
    one); escaping `</` keeps a string like "</script>" in the transcript from
    closing the element early. Both stay valid JSON.
    """
    return json.dumps(payload, ensure_ascii=True).replace("</", "<\\/")


def export_filename(data: dict) -> str:
    """A descriptive, filesystem-safe name for the downloaded file."""
    title = (data.get("title") or "transcript").strip()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:60].strip("-")
    agent = re.sub(r"[^a-z0-9]+", "", str(data.get("agent") or "").lower())
    short_id = re.sub(r"[^A-Za-z0-9]+", "", str(data.get("id") or ""))[:8]
    parts = [p for p in (agent, slug or "transcript", short_id) if p]
    return "-".join(parts) + ".html"


def _replace_once(html: str, marker: str, replacement: str, label: str) -> str:
    count = html.count(marker)
    if count != 1:
        raise RuntimeError(
            f"index.html must contain exactly one {label} ({marker!r}); found {count}"
        )
    return html.replace(marker, replacement)


def build_standalone_html(
    data: dict,
    *,
    static_dir: Path | None = None,
    exported_at: str | None = None,
) -> str:
    """Render one parsed session as a single self-contained HTML document."""
    static = static_dir or STATIC_DIR
    html = (static / "index.html").read_text(encoding="utf-8")
    css = (static / "style.css").read_text(encoding="utf-8")
    app_js = (static / "app.js").read_text(encoding="utf-8")

    payload = inline_local_images(json.loads(json.dumps(data)))
    payload["exported_at"] = exported_at or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    title = (data.get("title") or "Transcript").strip() or "Transcript"
    html = _replace_once(
        html, TITLE_TAG,
        "<title>"
        + title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        + " · Transcript</title>",
        "title tag",
    )
    html = _replace_once(
        html, STYLE_LINK, "<style>\n" + css + "\n</style>", "stylesheet link"
    )
    html = _replace_once(
        html, APP_SCRIPT,
        "<script>window.__TRANSCRIPT_EXPORT__ = " + _json_literal(payload) + ";</script>\n"
        "  <script>\n" + app_js + "\n</script>",
        "app.js script tag",
    )
    return html
