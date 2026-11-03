#!/usr/bin/env python3
"""Security tests for the transcript viewer.

The viewer reads your private Claude Code / Codex transcripts, so the things
that matter are: (1) it never sends them anywhere, and (2) it never serves
files outside the directories it's meant to expose. These tests assert both,
so anyone who downloads the project can verify the guarantees rather than
trust them.

Run with:  python -m unittest tests.test_security    (zero dependencies, stdlib only)
"""

from __future__ import annotations

import ast
import json
import re
import socket
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote
from http.client import HTTPConnection
from pathlib import Path

import catalog
import server


# --------------------------------------------------------------------------- #
# Outbound-connection guard: record any socket that dials a non-loopback host.
# --------------------------------------------------------------------------- #
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
OUTBOUND: list = []


def _is_loopback(addr) -> bool:
    try:
        host = addr[0]
    except (TypeError, IndexError, KeyError):
        return True  # AF_UNIX / odd address — local IPC, not remote exfiltration
    host = str(host)
    return host in ("127.0.0.1", "::1", "localhost") or host.startswith("127.")


def _guard(real):
    def wrapper(self, addr, *a, **kw):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(addr):
            OUTBOUND.append(tuple(addr) if isinstance(addr, tuple) else addr)
        return real(self, addr, *a, **kw)
    return wrapper

from tests.fixture_builders import ViewerServerTestCase


class SecurityTest(ViewerServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Install the outbound-connection guard for the whole class.
        socket.socket.connect = _guard(_real_connect)
        socket.socket.connect_ex = _guard(_real_connect_ex)

    @classmethod
    def tearDownClass(cls):
        socket.socket.connect = _real_connect
        socket.socket.connect_ex = _real_connect_ex
        super().tearDownClass()

    def test_json_response_ignores_disconnected_client(self):
        """A cancelled browser poll should not raise or trigger a second response."""
        class BrokenWriter:
            def write(self, _body):
                raise BrokenPipeError(32, "Broken pipe")

        class FakeHandler:
            wfile = BrokenWriter()
            close_connection = False
            _send_bytes = server.Handler._send_bytes

            def send_response(self, _status):
                pass

            def send_header(self, _name, _value):
                pass

            def end_headers(self):
                pass

        fake = FakeHandler()
        server.Handler._send_json(fake, {"sessions": []})
        self.assertTrue(fake.close_connection)

    def test_open_local_file_is_workspace_confined_and_non_executable(self):
        workspace = self.projects_dir.parent / "workspace"
        workspace.mkdir()
        linked = workspace / "notes.txt"
        linked.write_text("hello", encoding="utf-8")
        outside = self.projects_dir.parent / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        unsafe = workspace / "run.command"
        unsafe.write_text("echo no", encoding="utf-8")
        session = {"meta": {"cwd": str(workspace)}}

        with (
            mock.patch.object(catalog, "load_session", return_value=session),
            mock.patch.object(server.sys, "platform", "darwin"),
            mock.patch.object(server.subprocess, "run") as run,
        ):
            status, _, body = self.post_json(
                "/api/open-local", {"file": str(self.fixture), "path": str(linked)}
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["opened"], str(linked.resolve()))
            self.assertEqual(run.call_args.args[0], ["/usr/bin/open", str(linked.resolve())])

            status, _, _ = self.post_json(
                "/api/open-local", {"file": str(self.fixture), "path": str(outside)}
            )
            self.assertEqual(status, 403)

            status, _, _ = self.post_json(
                "/api/open-local", {"file": str(self.fixture), "path": str(unsafe)}
            )
            self.assertEqual(status, 403)
            self.assertEqual(run.call_count, 1)

    def test_reveal_transcript_is_confined_to_transcript_roots(self):
        outside = self.projects_dir.parent / "elsewhere.jsonl"
        outside.write_text("{}\n", encoding="utf-8")

        with (
            mock.patch.object(server.sys, "platform", "darwin"),
            mock.patch.object(server.subprocess, "run") as run,
        ):
            status, _, body = self.post_json(
                "/api/reveal-transcript", {"file": str(self.fixture)}
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["opened"], str(self.fixture.resolve()))
            self.assertEqual(
                run.call_args.args[0], ["/usr/bin/open", "-R", str(self.fixture.resolve())]
            )

            status, _, _ = self.post_json("/api/reveal-transcript", {"file": str(outside)})
            self.assertEqual(status, 403)

            status, _, _ = self.post_json(
                "/api/reveal-transcript", {"file": "cursordb:abc123"}
            )
            self.assertEqual(status, 404)
            self.assertEqual(run.call_count, 1)

    # ----- exfiltration guarantees ----------------------------------------- #

    def test_runtime_makes_no_outbound_connections(self):
        """Exercising every endpoint must not dial any non-loopback host."""
        OUTBOUND.clear()
        for path in ["/", "/app.js", "/style.css", "/api/sessions",
                     "/api/search?q=hello",
                     "/api/session-state?file=" + quote(str(self.fixture)),
                     "/api/session?file=" + quote(str(self.fixture))]:
            self.get(path)
        self.assertEqual(OUTBOUND, [], f"server made outbound connections: {OUTBOUND}")

    def test_default_bind_is_loopback(self):
        """The server must default to 127.0.0.1, not a network-exposed address."""
        self.assertEqual(server.DEFAULT_HOST, "127.0.0.1")

    def test_no_network_client_imports(self):
        """Neither module may import an outbound network client / mail / ftp lib."""
        forbidden_roots = {
            "requests", "httpx", "aiohttp", "urllib3", "socket",
            "smtplib", "ftplib", "telnetlib", "poplib", "imaplib",
            "websocket", "websockets", "paramiko", "boto3", "google",
        }
        forbidden_full = {"urllib.request", "urllib.error", "http.client"}
        # Every product module, discovered rather than listed, so a new module
        # can't silently skip the scan. (Tests themselves use urllib as the
        # loopback client, so they're excluded.)
        modules = sorted(
            p
            for pattern in ("*.py", "codex_export/*.py", "mica/*.py")
            for p in Path(".").glob(pattern)
            if not p.name.startswith("test_")
        )
        self.assertGreaterEqual(len(modules), 5, f"suspiciously few modules: {modules}")
        for mod_path in modules:
            tree = ast.parse(mod_path.read_text())
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(n.name for n in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            for name in imported:
                root = name.split(".")[0]
                self.assertNotIn(root, forbidden_roots,
                                 f"{mod_path} imports {name} (root {root})")
                self.assertNotIn(name, forbidden_full,
                                 f"{mod_path} imports {name}")

    # ----- arbitrary-file-read guarantees ---------------------------------- #

    def test_session_outside_roots_is_forbidden(self):
        """A real file outside the transcript roots must not be parseable."""
        status, _, _ = self.get("/api/session?file=" + quote("/etc/hosts"))
        self.assertEqual(status, 403)

    def test_session_inside_roots_ok(self):
        status, _, body = self.get(
            "/api/session?file=" + quote(str(self.fixture)))
        self.assertEqual(status, 200)
        self.assertNotIn(b"forbidden", body)

    def test_session_state_is_lightweight_and_confined(self):
        status, _, body = self.get(
            "/api/session-state?file=" + quote(str(self.fixture)))
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertTrue(state["supported"])
        self.assertEqual(state["mtime"], self.fixture.stat().st_mtime)
        self.assertNotIn("events", state)

        status, _, _ = self.get(
            "/api/session-state?file=" + quote("/etc/hosts"))
        self.assertEqual(status, 403)

    def test_local_image_serves_image_from_any_path(self):
        """Images are served from anywhere (transcripts reference original paths)."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tf:
            tf.write(b"\x89PNG\r\n\x1a\n")
            tf.flush()
            status, headers, _ = self.get(
                "/api/local-image?path=" + quote(str(Path(tf.name).resolve())))
            self.assertEqual(status, 200)
            self.assertTrue(headers.get("Content-Type", "").startswith("image/"))

    def test_local_image_rejects_non_image(self):
        """Only image-typed files are served, never arbitrary content."""
        status, _, _ = self.get("/api/local-image?path=" + quote("/etc/hosts"))
        self.assertEqual(status, 400)

    # ----- DNS-rebinding guard (Host-header allowlist) --------------------- #

    def request_with_host(self, path: str, host: str):
        """Issue a GET to the loopback server but forge the Host header."""
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host)
            conn.endheaders()
            return conn.getresponse().status
        finally:
            conn.close()

    def test_foreign_host_header_rejected(self):
        """A request claiming a non-loopback Host (DNS rebinding) is refused."""
        self.assertEqual(self.request_with_host("/api/sessions", "evil.com"), 403)

    def test_loopback_host_header_allowed(self):
        """A normal loopback Host is served as usual."""
        self.assertEqual(self.request_with_host("/api/sessions", "localhost:1234"), 200)
        self.assertEqual(self.request_with_host("/api/sessions", "127.0.0.1"), 200)


class FrontendAssetIntegrityTest(unittest.TestCase):
    """Every external script/stylesheet must be version-pinned and SRI-hashed.

    The CDN-served scripts (marked, DOMPurify, KaTeX) are the trust root for
    sanitizing transcript content in the browser; without an integrity hash a
    compromised or silently-updated CDN file would execute with full access to
    every transcript. A floating version tag (e.g. ``@12``) defeats SRI because
    the alias can move to bytes that no longer match the hash.
    """

    def test_external_resources_have_pinned_versions_and_sri(self):
        html = (Path("static") / "index.html").read_text(encoding="utf-8")
        tags = re.findall(r"<(?:script|link)\b[^>]*>", html)
        external = [t for t in tags if re.search(r"""(?:src|href)=["']https?://""", t)]
        self.assertTrue(external, "expected CDN tags in index.html")
        for tag in external:
            with self.subTest(tag=tag):
                self.assertRegex(tag, r'integrity="sha(256|384|512)-[A-Za-z0-9+/=]+"',
                                 "external resource missing SRI integrity hash")
                self.assertIn('crossorigin="anonymous"', tag)
                url = re.search(r"""(?:src|href)=["'](https?://[^"']+)""", tag).group(1)
                self.assertRegex(url, r"@\d+\.\d+\.\d+/",
                                 "CDN URL must pin an exact version (x.y.z)")

if __name__ == "__main__":
    unittest.main()
