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
    additional_prompts: tuple[str, ...] = (),
    extra_records: tuple[dict, ...] = (),
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
    for i, extra_prompt in enumerate(additional_prompts, start=2):
        records.extend([
            {"type": "user", "timestamp": f"2024-01-01T00:00:{i:02d}Z",
             "message": {"role": "user", "content": extra_prompt}},
            {"type": "assistant", "timestamp": f"2024-01-01T00:00:{i + 1:02d}Z",
             "message": {"role": "assistant", "model": "claude-test",
                         "content": [{"type": "text", "text": "continued reply"}]}},
        ])
    records.extend(extra_records)
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return f


def _write_guardian_sessions(codex_home: Path) -> tuple[Path, Path]:
    sessions = codex_home / "sessions" / "2026" / "01" / "01"
    sessions.mkdir(parents=True)
    parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    guardian_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    parent = sessions / f"rollout-2026-01-01T00-00-00-{parent_id}.jsonl"
    guardian = sessions / f"rollout-2026-01-01T00-00-01-{guardian_id}.jsonl"
    parent_records = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": parent_id, "cwd": "/tmp/proj"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "parent task"}},
    ]
    planned = {
        "command": ["/bin/zsh", "-lc", "python3 -m unittest test_security"],
        "cwd": "/tmp/proj",
        "justification": "Run local tests?",
        "sandbox_permissions": "require_escalated",
        "tool": "exec_command",
    }
    guardian_records = [
        {"timestamp": "2026-01-01T00:00:02Z", "type": "session_meta",
         "payload": {
             "id": guardian_id, "parent_thread_id": parent_id,
             "thread_source": "subagent", "source": {"subagent": {"other": "guardian"}},
             "cwd": "/tmp/proj", "base_instructions": {"text": "Review actions."},
         }},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "user_message",
                     "message": "Review this action.\nPlanned action JSON:\n" + json.dumps(planned)}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": '{"outcome":"allow"}'}},
    ]
    parent.write_text("\n".join(json.dumps(r) for r in parent_records) + "\n")
    guardian.write_text("\n".join(json.dumps(r) for r in guardian_records) + "\n")
    return parent, guardian


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
        cls.later_prompt_fixture = _write_fixture_session(
            cls.projects_dir,
            "33333333-3333-3333-3333-333333333333",
            "unrelated opening prompt",
            ("laterpromptword in a subsequent user message",),
        )
        cls.metadata_fixture = _write_fixture_session(
            cls.projects_dir,
            "44444444-4444-4444-4444-444444444444",
            "ordinary opening prompt",
            extra_records=(
                {"type": "ai-title", "aiTitle": "Generated title"},
                {"type": "custom-title", "customTitle": "nativepriority title"},
                {"type": "agent-name", "agentName": "reviewer"},
                {"type": "pr-link", "prNumber": 42,
                 "prUrl": "https://example.test/org/repo/pull/42",
                 "prRepository": "org/repo"},
                {"type": "system", "subtype": "compact_boundary",
                 "timestamp": "2024-01-01T00:01:00Z", "content": "Conversation compacted",
                 "compactMetadata": {
                     "trigger": "manual", "preTokens": 12000, "postTokens": 3500,
                     "durationMs": 1250, "preservedMessages": {"uuids": ["a", "b"]},
                     "preCompactDiscoveredTools": ["Read", "Edit"],
                 }},
            ),
        )
        server.PROJECTS_DIR = cls.projects_dir
        server.CUSTOM_NAMES_FILE = tmp / "viewer" / "names.json"
        server._CUSTOM_NAMES_CACHE = None
        cls.codex_parent, cls.codex_guardian = _write_guardian_sessions(tmp / "codex")
        codex.configure(tmp / "codex")
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

    def test_custom_title_search_outranks_user_message(self):
        data = server.load_session(str(self.fixture))
        server._set_custom_name(data, "priorityword custom name")
        try:
            matches = server.search_sessions("priorityword")
            self.assertEqual(matches[0]["file"], str(self.fixture))
            user_message_match = next(
                match for match in matches if match["file"] == str(self.priority_fixture)
            )
            self.assertGreater(matches[0]["score"], user_message_match["score"])
            self.assertGreaterEqual(matches[0]["score"], server.CUSTOM_TITLE_WEIGHT)
        finally:
            server._set_custom_name(data, "")

    def test_later_user_message_receives_user_weight(self):
        matches = server.search_sessions("laterpromptword")
        match = next(
            item for item in matches if item["file"] == str(self.later_prompt_fixture)
        )
        self.assertEqual(match["score"], server.USER_MSG_WEIGHT)

    def test_claude_native_metadata_is_exposed(self):
        summary = server.cc_session_summary(self.metadata_fixture)
        self.assertEqual(summary["title"], "nativepriority title")
        self.assertEqual(summary["claude_title"], "nativepriority title")
        self.assertEqual(summary["agent_name"], "reviewer")

        data = server.load_session(str(self.metadata_fixture))
        self.assertEqual(data["title"], "nativepriority title")
        self.assertEqual(data["meta"]["pr"]["number"], 42)
        compact = next(ev for ev in data["events"] if ev.get("subtype") == "compact_boundary")
        self.assertEqual(compact["compaction"]["pre_tokens"], 12000)
        self.assertEqual(compact["compaction"]["post_tokens"], 3500)
        self.assertEqual(compact["compaction"]["preserved_messages"], 2)
        self.assertEqual(compact["compaction"]["discovered_tools"], 2)

        match = next(
            item for item in server.search_sessions("nativepriority")
            if item["file"] == str(self.metadata_fixture)
        )
        self.assertEqual(match["score"], server.CUSTOM_TITLE_WEIGHT)

    def test_guardian_is_grouped_and_structured(self):
        summary = codex.session_summary(self.codex_guardian)
        self.assertTrue(summary["is_subagent"])
        self.assertEqual(summary["subagent_type"], "guardian")
        self.assertEqual(summary["parent_file"], str(self.codex_parent.resolve()))
        self.assertEqual(summary["title"], "Approval reviews")

        data = codex.parse_session(self.codex_guardian)
        request = next(ev for ev in data["events"] if ev["kind"] == "guardian_request")
        decision = next(ev for ev in data["events"] if ev["kind"] == "guardian_decision")
        self.assertEqual(request["request"]["tool"], "exec_command")
        self.assertEqual(request["request"]["command"][-1], "python3 -m unittest test_security")
        self.assertEqual(decision["outcome"], "allow")

        sessions = server.list_sessions()
        parent_index = next(
            i for i, s in enumerate(sessions) if s["file"] == str(self.codex_parent.resolve())
        )
        self.assertEqual(sessions[parent_index + 1]["file"], str(self.codex_guardian.resolve()))

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
