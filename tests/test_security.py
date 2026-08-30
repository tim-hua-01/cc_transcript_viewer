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
import urllib.error
import urllib.request
from unittest import mock
from urllib.parse import quote
from http.client import HTTPConnection
from pathlib import Path

import server
import claude_parser as claude
import codex_parser as codex
import cursor_parser as cursor
import opencode_parser as opencode


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


from tests.fixture_builders import (
    _write_cli_session,
    _write_cli_store,
    _write_fixture_session,
    _write_guardian_sessions,
    _write_opencode_db,
    http_get,
    patch_server_files,
    restore_server_files,
    start_http_server,
    stop_http_server,
)


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
        cls._old_parser_config = (
            claude.PROJECTS_DIR,
            codex.CODEX_HOME,
            (cursor.DB_PATH, cursor.PROJECTS_DIR, cursor.CHATS_DIR),
            opencode.DB_PATH,
        )
        claude.configure(cls.projects_dir)
        # Hermetic viewer-owned files — never touch the user's real names.json
        # or ~/.cache summary file from the test suite.
        cls._old_server_files = patch_server_files(server, tmp)
        cls.codex_parent, cls.codex_guardian, cls.codex_image = _write_guardian_sessions(tmp / "codex")
        codex.configure(tmp / "codex")
        cls.cursor_projects = tmp / "cursor-projects"
        cls.cli_fixture = _write_cli_session(cls.cursor_projects)
        cls.cursor_chats = tmp / "cursor-chats"
        cls.cli_store_fixture = _write_cli_store(cls.cursor_chats)
        cursor.configure(
            tmp / "cursor",
            projects_dir=cls.cursor_projects,
            chats_dir=cls.cursor_chats,
        )
        cls.opencode_db = tmp / "opencode" / "opencode.db"
        cls.opencode_parent, cls.opencode_child = _write_opencode_db(cls.opencode_db)
        opencode.configure(cls.opencode_db)

        # Install the outbound-connection guard for the whole class.
        socket.socket.connect = _guard(_real_connect)
        socket.socket.connect_ex = _guard(_real_connect_ex)

        # Serve on an ephemeral loopback port in a background thread.
        cls.httpd, cls.port, cls.thread = start_http_server(server.Handler)

    @classmethod
    def tearDownClass(cls):
        stop_http_server(cls.httpd, cls.thread)
        socket.socket.connect = _real_connect
        socket.socket.connect_ex = _real_connect_ex
        restore_server_files(server, cls._old_server_files)
        claude.configure(cls._old_parser_config[0])
        codex.configure(cls._old_parser_config[1])
        cursor.configure(
            cls._old_parser_config[2][0],
            projects_dir=cls._old_parser_config[2][1],
            chats_dir=cls._old_parser_config[2][2],
        )
        opencode.configure(cls._old_parser_config[3])
        cls._tmp.cleanup()

    def get(self, path: str):
        return http_get(self.port, path, timeout=5)

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
            try:
                return e.code, e.headers, e.read()
            finally:
                e.close()

    def post_json(self, path: str, payload: dict):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.headers, e.read()
            finally:
                e.close()

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
            mock.patch.object(server, "load_session", return_value=session),
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
        summary = claude.session_summary(self.metadata_fixture)
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
        self.assertEqual(match["score"], server.NATIVE_TITLE_WEIGHT)

    def test_claude_and_cursor_native_titles_have_half_custom_weight(self):
        claude = {
            "agent": "claude",
            "custom_title": "",
            "original_title": "Original Claude title",
            "claude_title": "Original Claude title",
            "ai_title": "Short Claude title",
        }
        cursor = {
            "agent": "cursor",
            "custom_title": "",
            "original_title": "Short Cursor title",
        }
        custom, native = server._search_title_segments(claude)
        self.assertEqual(custom, "")
        self.assertEqual(native.count("Original Claude title"), 1)
        self.assertIn("Short Claude title", native)
        self.assertEqual(
            server._search_title_segments(cursor),
            ("", "Short Cursor title"),
        )

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
        self.assertEqual(
            request["request"]["command"][-1],
            "python3 -m unittest tests.test_security",
        )
        self.assertEqual(request["metadata"]["model"], "guardian-test")
        self.assertEqual(request["metadata"]["duration_ms"], 2000)
        self.assertEqual(request["metadata"]["usage"]["input_tokens"], 1200)
        self.assertEqual(decision["outcome"], "allow")
        self.assertFalse(
            {"status", "context", "tokens", "raw"}
            & {event["kind"] for event in data["events"]}
        )

        sessions = server.list_sessions()
        parent_index = next(
            i for i, s in enumerate(sessions) if s["file"] == str(self.codex_parent.resolve())
        )
        self.assertEqual(sessions[parent_index + 1]["file"], str(self.codex_guardian.resolve()))

    def test_codex_exec_orchestration_is_structured(self):
        command_source = (
            'const r = await tools.exec_command({"cmd":"git status --short",'
            '"workdir":"/tmp"}); text(r.output);'
        )
        command = codex._normalize_tool_input("exec", command_source)
        self.assertEqual(command["calls"][0]["name"], "exec_command")
        self.assertEqual(command["calls"][0]["input"]["cmd"], "git status --short")
        self.assertIn("git status --short", server._event_text({"input": command}))

        patch_source = (
            'const patch = "*** Begin Patch\\n*** Update File: a\\n@@\\n-x\\n+y\\n*** End Patch";'
            " text(await tools.apply_patch(patch));"
        )
        patch = codex._normalize_tool_input("exec", patch_source)
        self.assertEqual(patch["calls"][0]["name"], "apply_patch")
        self.assertIn("*** Update File: a", patch["calls"][0]["input"])

        wrapped = (
            'Script completed\nWall time 0.1 seconds\nOutput:\n\n'
            '{"chunk_id":"abc","wall_time_seconds":0.25,"exit_code":0,'
            '"output":"actual stdout\\n"}'
        )
        result = codex._normalize_tool_output(wrapped, name="exec", args=command)
        self.assertEqual(result["text"], "actual stdout\n")
        self.assertEqual(result["metadata"]["exit_code"], 0)
        self.assertEqual(result["metadata"]["chunk_id"], "abc")

        javascript_object = r'''const r = await tools.exec_command({
          cmd: "find output -type f \\\\( -name '*.json' -o -name \"*.txt\" \\\\) -delete",
          workdir: "/tmp/project",
          yield_time_ms: 10000,
          max_output_tokens: 2000,
        }); text(r.output);'''
        parsed = codex._normalize_tool_input("exec", javascript_object)
        call = parsed["calls"][0]
        self.assertEqual(call["name"], "exec_command")
        self.assertEqual(call["input"]["workdir"], "/tmp/project")
        self.assertEqual(call["input"]["yield_time_ms"], 10000)
        self.assertIn("-name '*.json'", call["input"]["cmd"])

    def test_codex_user_images_prefer_local_and_fallback_inline(self):
        data = codex.parse_session(self.codex_parent)
        local = next(ev for ev in data["events"] if ev.get("text") == "look at this")
        fallback = next(ev for ev in data["events"] if ev.get("text") == "missing image")
        self.assertEqual(local["images"][0]["kind"], "local")
        self.assertIn(quote(str(self.codex_image.resolve()), safe=""), local["images"][0]["src"])
        self.assertEqual(fallback["images"][0]["kind"], "inline")
        self.assertTrue(fallback["images"][0]["src"].startswith("data:image/png;base64,"))

    def test_codex_turn_metadata_attaches_to_final_answer(self):
        data = codex.parse_session(self.codex_parent)
        answer = next(ev for ev in data["events"] if ev.get("text") == "metadata answer")
        self.assertEqual(answer["turn_metadata"]["model"], "codex-test")
        self.assertEqual(answer["turn_metadata"]["duration_ms"], 3000)
        self.assertEqual(answer["turn_metadata"]["usage"]["input_tokens"], 100)
        self.assertFalse(
            {"status", "context", "tokens"} & {event["kind"] for event in data["events"]}
        )

    def test_codex_compaction_is_a_visible_boundary(self):
        data = codex.parse_session(self.codex_parent)
        compact = next(
            ev for ev in data["events"]
            if ev.get("kind") == "system" and ev.get("subtype") == "compact_boundary"
        )
        self.assertEqual(compact["compaction"]["source"], "codex")
        self.assertEqual(compact["compaction"]["window_number"], 1)
        self.assertEqual(compact["compaction"]["replacement_items"], 2)
        self.assertTrue(compact["compaction"]["summary_encrypted"])
        self.assertEqual(compact["text"], "")
        self.assertEqual(
            compact["metadata"]["world_state"]["state"]["environments"]["local"]["shell"],
            "zsh",
        )
        self.assertFalse(
            any(ev.get("record_type") == "world_state" for ev in data["events"])
        )

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
            for pattern in ("*.py", "codex_export/*.py")
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

    def test_cursor_cli_session_lists_and_parses(self):
        """CLI agent-transcripts under ~/.cursor/projects are visible and readable."""
        _, _, body = self.get("/api/sessions")
        sessions = json.loads(body)["sessions"]
        match = next(s for s in sessions if s["file"] == str(self.cli_fixture.resolve()))
        self.assertEqual(match["agent"], "cursor")
        self.assertEqual(match["cursor_source"], "cli-jsonl")
        self.assertEqual(match["title"], "hello from cursor cli")
        self.assertEqual(match["cwd"], "/Users/test/demo")
        self.assertGreaterEqual(match["n_tool"], 2)

        status, _, body = self.get(
            "/api/session?file=" + quote(str(self.cli_fixture.resolve())))
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["agent"], "cursor")
        self.assertEqual(data["cursor_source"], "cli-jsonl")
        kinds = [ev["kind"] for ev in data["events"]]
        self.assertIn("user", kinds)
        self.assertIn("assistant", kinds)
        tools = [
            b for ev in data["events"] for b in ev.get("blocks") or []
            if b.get("type") == "tool_use"
        ]
        names = {t["name"] for t in tools}
        self.assertIn("Shell", names)
        self.assertIn("Edit", names)  # StrReplace normalized
        self.assertTrue(all(t.get("result", {}).get("missing") for t in tools))

    def test_cursor_cli_subagent_link_survives_preferred_db_record(self):
        """A rich duplicate keeps hierarchy learned from its JSONL path."""
        parent_id = self.cli_fixture.stem
        sub_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
        sub_dir = self.cli_fixture.parent / "subagents"
        sub_dir.mkdir()
        sub_path = sub_dir / f"{sub_id}.jsonl"
        sub_path.write_text(json.dumps({
            "role": "user",
            "message": {"content": "inspect the child task"},
        }) + "\n")

        original_list_db = cursor._list_db_sessions
        try:
            cursor._list_db_sessions = lambda: [
                {"id": parent_id, "file": "cursordb:" + parent_id,
                 "title": "Rich parent", "mtime": 10},
                {"id": sub_id, "file": "cursordb:" + sub_id,
                 "title": "Rich child", "mtime": 9},
            ]
            sessions = cursor.list_sessions()
        finally:
            cursor._list_db_sessions = original_list_db
            sub_path.unlink()
            sub_dir.rmdir()
        child = next(s for s in sessions if s["id"] == sub_id)
        self.assertEqual(child["file"], "cursordb:" + sub_id)
        self.assertTrue(child["is_subagent"])
        self.assertEqual(child["parent_id"], parent_id)
        self.assertEqual(child["parent_file"], "cursordb:" + parent_id)

    def test_cursor_cli_store_subagent_info_is_grouped(self):
        """Newer top-level chat stores use subagentInfo instead of a subdirectory."""
        parent_id = self.cli_fixture.stem
        sub_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        _write_cli_store(
            self.cursor_chats,
            session_id=sub_id,
            title="New Agent",
            user_text=(
                "<system_reminder>You are running as a subagent.</system_reminder>\n"
                "You are a candidate runner.\n\n## Task\nInspect the dependency setup."
            ),
            meta_extra={"subagentInfo": {
                "parentAgentId": parent_id,
                "rootParentAgentId": parent_id,
                "typeName": "best-of-n-runner",
            }},
        )

        sessions = cursor.list_sessions()
        child = next(s for s in sessions if s["id"] == sub_id)
        self.assertTrue(child["is_subagent"])
        self.assertEqual(child["subagent_type"], "best-of-n-runner")
        self.assertEqual(child["parent_id"], parent_id)
        self.assertEqual(child["parent_file"], str(self.cli_fixture.resolve()))
        self.assertEqual(child["title"], "[best-of-n-runner] Inspect the dependency setup.")

    def test_cursor_cli_store_db_includes_tool_results(self):
        """CLI store.db sessions are preferred and include tool outputs."""
        store_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        file_id = cursor.CLI_SESSION_SCHEME + store_id
        _, _, body = self.get("/api/sessions")
        sessions = json.loads(body)["sessions"]
        match = next(s for s in sessions if s["file"] == file_id)
        self.assertEqual(match["agent"], "cursor")
        self.assertEqual(match["cursor_source"], "cli")
        self.assertEqual(match["title"], "Store db session")
        self.assertEqual(match["cwd"], "/Users/test/demo")
        self.assertEqual(match["model"], "grok-test")

        status, _, body = self.get("/api/session?file=" + quote(file_id))
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["cursor_source"], "cli")
        tools = [
            b for ev in data["events"] for b in ev.get("blocks") or []
            if b.get("type") == "tool_use"
        ]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "Shell")
        self.assertIn("hi", tools[0]["result"]["text"])
        self.assertFalse(tools[0]["result"].get("missing"))

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
