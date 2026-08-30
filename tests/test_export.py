#!/usr/bin/env python3
"""Tests for the single-file transcript export (export_html.py + /api/export).

Two things have to hold for a saved transcript to be shareable at all: the file
must carry the whole conversation with no reference back to the server or the
machine it was read from, and it must keep rendering through the viewer's real
code rather than an export-only copy of it that could drift.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
import unittest
from pathlib import Path

import claude_parser as claude
import export_html
import server
from tests.fixture_builders import (
    _write_fixture_session,
    http_get,
    patch_server_files,
    restore_server_files,
    start_http_server,
    stop_http_server,
)


class BuildStandaloneTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def sample(self, **overrides) -> dict:
        data = {
            "id": "abcdef12-3456-7890-abcd-ef1234567890",
            "agent": "claude",
            "title": "Add a save button",
            "file": "/tmp/proj/session.jsonl",
            "meta": {"cwd": "/tmp/proj", "model": "claude-test"},
            "events": [
                {"kind": "user", "text": "hello"},
                {"kind": "assistant", "blocks": [{"type": "text", "text": "hi"}]},
            ],
        }
        data.update(overrides)
        return data

    def test_document_inlines_css_and_js_and_drops_server_urls(self):
        html = export_html.build_standalone_html(self.sample())
        css = (export_html.STATIC_DIR / "style.css").read_text(encoding="utf-8")
        app_js = (export_html.STATIC_DIR / "app.js").read_text(encoding="utf-8")

        self.assertIn(css, html)
        self.assertIn(app_js, html)
        # Nothing may point back at the server the export was made from.
        self.assertNotIn('href="/style.css"', html)
        self.assertNotIn('src="/app.js"', html)
        self.assertNotIn("/api/", html.replace(app_js, ""))

    def test_payload_round_trips_as_json(self):
        data = self.sample()
        html = export_html.build_standalone_html(data)
        literal = re.search(
            r"window\.__TRANSCRIPT_EXPORT__ = (\{.*?\});</script>", html, re.S
        ).group(1)
        payload = json.loads(literal.replace("<\\/", "</"))
        self.assertEqual(payload["events"], data["events"])
        self.assertEqual(payload["title"], data["title"])
        self.assertIn("exported_at", payload)

    def test_transcript_text_cannot_close_the_script_element(self):
        data = self.sample(events=[{"kind": "user", "text": "</script><img src=x>"}])
        html = export_html.build_standalone_html(data)
        literal = re.search(
            r"window\.__TRANSCRIPT_EXPORT__ = (\{.*?\});</script>", html, re.S
        ).group(1)
        self.assertNotIn("</script>", literal)
        payload = json.loads(literal.replace("<\\/", "</"))
        self.assertEqual(payload["events"][0]["text"], "</script><img src=x>")

    def test_title_is_html_escaped_in_the_title_tag(self):
        html = export_html.build_standalone_html(self.sample(title="a <b> & c"))
        self.assertIn("<title>a &lt;b&gt; &amp; c · Transcript</title>", html)
        self.assertNotIn("<title>Transcript Viewer</title>", html)

    def test_missing_marker_is_an_error_not_a_broken_export(self):
        static = self.tmp / "static"
        static.mkdir()
        (static / "style.css").write_text("body{}")
        (static / "app.js").write_text("// app")
        (static / "index.html").write_text("<html><body>no markers</body></html>")
        with self.assertRaises(RuntimeError):
            export_html.build_standalone_html(self.sample(), static_dir=static)

    def test_source_data_is_not_mutated(self):
        data = self.sample()
        before = json.dumps(data, sort_keys=True)
        export_html.build_standalone_html(data)
        self.assertEqual(json.dumps(data, sort_keys=True), before)


class InlineLocalImagesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.png = self.tmp / "shot.png"
        self.png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"pixels")

    def tearDown(self):
        self._tmp.cleanup()

    def payload(self, **overrides) -> dict:
        from urllib.parse import quote
        image = {
            "kind": "local",
            "src": "/api/local-image?path=" + quote(str(self.png), safe=""),
            "path": str(self.png),
            "bytes": self.png.stat().st_size,
            "content_type": "image/png",
        }
        image.update(overrides)
        return {"events": [{"kind": "tool", "images": [image]}]}

    def image_of(self, data):
        return data["events"][0]["images"][0]

    def test_local_image_becomes_a_data_uri(self):
        image = self.image_of(export_html.inline_local_images(self.payload()))
        expected = base64.b64encode(self.png.read_bytes()).decode()
        self.assertEqual(image["src"], "data:image/png;base64," + expected)

    def test_missing_file_degrades_to_an_omitted_note(self):
        data = self.payload()
        self.png.unlink()
        image = self.image_of(export_html.inline_local_images(data))
        self.assertEqual(image["src"], "")
        self.assertIn("unreadable", image["reason"])

    def test_oversized_image_is_dropped_rather_than_embedded(self):
        data = self.payload(bytes=export_html.MAX_IMAGE_BYTES + 1)
        image = self.image_of(export_html.inline_local_images(data))
        self.assertEqual(image["src"], "")
        self.assertIn("too large", image["reason"])

    def test_total_budget_stops_after_the_first_image(self):
        from urllib.parse import quote
        src = "/api/local-image?path=" + quote(str(self.png), safe="")
        size = self.png.stat().st_size
        make = lambda: {"kind": "local", "src": src, "path": str(self.png),
                        "bytes": size, "content_type": "image/png"}
        data = {"events": [{"images": [make(), make()]}]}
        export_html.inline_local_images(data, budget=[size])
        first, second = data["events"][0]["images"]
        self.assertTrue(first["src"].startswith("data:image/png;base64,"))
        self.assertEqual(second["src"], "")

    def test_non_image_path_is_refused(self):
        from urllib.parse import quote
        secret = self.tmp / "secrets.env"
        secret.write_text("TOKEN=hunter2")
        data = {"events": [{"images": [{
            "kind": "local",
            "src": "/api/local-image?path=" + quote(str(secret), safe=""),
            "path": str(secret),
            "bytes": secret.stat().st_size,
        }]}]}
        image = self.image_of(export_html.inline_local_images(data))
        self.assertEqual(image["src"], "")
        self.assertNotIn("hunter2", json.dumps(data))


class ExportFilenameTest(unittest.TestCase):
    def test_name_is_descriptive_and_filesystem_safe(self):
        name = export_html.export_filename(
            {"title": "Fix the /api/export route!", "agent": "codex", "id": "abcd1234-ef"}
        )
        self.assertEqual(name, "codex-fix-the-api-export-route-abcd1234.html")

    def test_name_survives_a_title_with_no_usable_characters(self):
        name = export_html.export_filename({"title": "———", "agent": "", "id": ""})
        self.assertEqual(name, "transcript.html")
        self.assertRegex(name, r"^[A-Za-z0-9-]+\.html$")


class ExportRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.projects_dir = tmp / "projects"
        cls.projects_dir.mkdir()
        cls.fixture = _write_fixture_session(cls.projects_dir)
        cls._old_claude = claude.PROJECTS_DIR
        claude.configure(cls.projects_dir)
        cls._old_server_files = patch_server_files(server, tmp)
        cls.httpd, cls.port, cls.thread = start_http_server(server.Handler)

    @classmethod
    def tearDownClass(cls):
        stop_http_server(cls.httpd, cls.thread)
        claude.configure(cls._old_claude)
        restore_server_files(server, cls._old_server_files)
        cls._tmp.cleanup()

    def get(self, path: str):
        return http_get(self.port, path)

    def test_export_returns_a_downloadable_html_document(self):
        from urllib.parse import quote
        status, headers, body = self.get(
            "/api/export?file=" + quote(str(self.fixture), safe="")
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertRegex(
            headers["Content-Disposition"], r'^attachment; filename="[A-Za-z0-9-]+\.html"$'
        )
        html = body.decode("utf-8")
        self.assertIn("window.__TRANSCRIPT_EXPORT__", html)
        self.assertIn("hello world", html)

    def test_export_requires_a_file_param(self):
        status, _, body = self.get("/api/export")
        self.assertEqual(status, 400)
        self.assertIn("missing file param", json.loads(body)["error"])

    def test_export_refuses_a_path_outside_the_transcript_roots(self):
        from urllib.parse import quote
        outside = Path(self._tmp.name) / "elsewhere.jsonl"
        outside.write_text('{"type":"user","message":{"role":"user","content":"x"}}\n')
        status, _, _ = self.get("/api/export?file=" + quote(str(outside), safe=""))
        self.assertEqual(status, 403)

    def test_export_reports_a_missing_transcript(self):
        status, _, _ = self.get("/api/export?file=%2Ftmp%2Fnope-does-not-exist.jsonl")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
