#!/usr/bin/env python3
"""Security tests for the transcript viewer.

The viewer reads your private Claude Code / Codex transcripts, so the things
that matter are: (1) it never sends them anywhere, and (2) it never serves
files outside the directories it's meant to expose. These tests assert both,
so anyone who downloads the project can verify the guarantees rather than
trust them.

Run with:  python -m unittest test_security    (zero dependencies, stdlib only)
"""

from __future__ import annotations

import ast
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import server
import codex_server as codex
import cursor_server as cursor


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


def _write_fixture_session(
    projects_dir: Path,
    session_id: str = "11111111-1111-1111-1111-111111111111",
    prompt: str = "hello world",
) -> Path:
    """A minimal but valid Claude Code transcript so endpoints have real data."""
    proj = projects_dir / "-tmp-proj"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{session_id}.jsonl"
    records = [
        {"type": "user", "timestamp": "2024-01-01T00:00:00Z", "cwd": "/tmp/proj",
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "timestamp": "2024-01-01T00:00:01Z",
         "message": {"role": "assistant", "model": "claude-test",
                     "content": [{"type": "text", "text": "hi there"}]}},
    ]
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return f


class SecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Route the server at hermetic temp dirs (fast + deterministic).
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.projects_dir = tmp / "projects"
        cls.projects_dir.mkdir()
        cls.fixture = _write_fixture_session(cls.projects_dir)
        cls.priority_fixture = _write_fixture_session(
            cls.projects_dir,
            "22222222-2222-2222-2222-222222222222",
            "priorityword in the first user message",
        )
        server.PROJECTS_DIR = cls.projects_dir
        server.CUSTOM_NAMES_FILE = tmp / "viewer" / "names.json"
        server._CUSTOM_NAMES_CACHE = None
        codex.configure(tmp / "codex")  # empty -> no codex sessions
        cursor.configure(tmp / "cursor")  # empty -> no cursor sessions

        # Install the outbound-connection guard for the whole class.
        socket.socket.connect = _guard(_real_connect)
        socket.socket.connect_ex = _guard(_real_connect_ex)

        # Serve on an ephemeral loopback port in a background thread.
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        socket.socket.connect = _real_connect
        socket.socket.connect_ex = _real_connect_ex
        cls._tmp.cleanup()

    def get(self, path: str):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def put_json(self, path: str, payload: dict):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    # ----- viewer-owned custom names -------------------------------------- #

    def test_custom_name_can_be_set_and_cleared(self):
        payload = {"file": str(self.fixture), "name": "My custom transcript"}
        status, _, body = self.put_json("/api/session-name", payload)
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertEqual(saved["title"], "My custom transcript")
        self.assertEqual(saved["custom_title"], "My custom transcript")
        self.assertEqual(saved["original_title"], "hello world")

        _, _, body = self.get("/api/sessions")
        summary = next(s for s in json.loads(body)["sessions"] if s["file"] == str(self.fixture))
        self.assertEqual(summary["title"], "My custom transcript")

        status, _, body = self.put_json(
            "/api/session-name", {"file": str(self.fixture), "name": ""}
        )
        self.assertEqual(status, 200)
        restored = json.loads(body)
        self.assertEqual(restored["title"], "hello world")
        self.assertEqual(restored["custom_title"], "")

    def test_custom_title_search_outranks_first_message(self):
        data = server.load_session(str(self.fixture))
        server._set_custom_name(data, "priorityword custom name")
        try:
            matches = server.search_sessions("priorityword")
            self.assertEqual(matches[0]["file"], str(self.fixture))
            first_message_match = next(
                match for match in matches if match["file"] == str(self.priority_fixture)
            )
            self.assertGreater(matches[0]["score"], first_message_match["score"])
            self.assertGreaterEqual(matches[0]["score"], server.CUSTOM_TITLE_WEIGHT)
        finally:
            server._set_custom_name(data, "")

    # ----- exfiltration guarantees ----------------------------------------- #

    def test_runtime_makes_no_outbound_connections(self):
        """Exercising every endpoint must not dial any non-loopback host."""
        OUTBOUND.clear()
        for path in ["/", "/app.js", "/style.css", "/api/sessions",
                     "/api/search?q=hello",
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
        for mod_path in (Path("server.py"), Path("codex_server.py"), Path("cursor_server.py")):
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


if __name__ == "__main__":
    unittest.main()
