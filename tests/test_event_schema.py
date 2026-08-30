#!/usr/bin/env python3
"""Event-contract conformance tests.

Every parser's output — summaries and full parses — must satisfy
event_schema.py, and the frontend's renderEvent() must dispatch on every kind
the schema declares. This is what keeps the three parsers and app.js from
drifting apart.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

import claude_parser as claude
import codex_parser as codex
import cursor_parser as cursor
import event_schema
import opencode_parser as opencode
from tests.fixture_builders import (
    _write_cli_session,
    _write_cli_store,
    _write_fixture_session,
    _write_guardian_sessions,
    _write_opencode_db,
)


class EventSchemaConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)

        # Remember prior module configuration so this class leaves no trace.
        cls._old_claude = claude.PROJECTS_DIR
        cls._old_codex = codex.CODEX_HOME
        cls._old_cursor = (cursor.DB_PATH, cursor.PROJECTS_DIR, cursor.CHATS_DIR)
        cls._old_opencode = opencode.DB_PATH

        cls.projects_dir = tmp / "projects"
        cls.projects_dir.mkdir()
        _write_fixture_session(cls.projects_dir)
        _write_fixture_session(
            cls.projects_dir,
            "55555555-5555-5555-5555-555555555555",
            "prompt with metadata",
            extra_records=(
                {"type": "system", "subtype": "compact_boundary",
                 "timestamp": "2024-01-01T00:01:00Z", "content": "Compacted",
                 "compactMetadata": {"trigger": "auto", "preTokens": 100, "postTokens": 10}},
                {"type": "attachment", "timestamp": "2024-01-01T00:02:00Z",
                 "attachment": {"type": "queued_command", "prompt": "queued question"}},
            ),
        )
        # A forked session so a `branch` event is exercised.
        fork = cls.projects_dir / "-tmp-proj" / "66666666-6666-6666-6666-666666666666.jsonl"
        fork_records = [
            {"type": "user", "uuid": "u1", "parentUuid": None,
             "timestamp": "2024-01-01T00:00:00Z",
             "message": {"role": "user", "content": "first"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "timestamp": "2024-01-01T00:00:01Z",
             "message": {"role": "assistant", "model": "claude-test",
                         "content": [{"type": "text", "text": "abandoned"}]}},
            {"type": "user", "uuid": "u2", "parentUuid": "u1",
             "timestamp": "2024-01-01T00:00:02Z",
             "message": {"role": "user", "content": "edited"}},
            {"type": "last-prompt", "leafUuid": "u2"},
        ]
        fork.write_text("\n".join(json.dumps(r) for r in fork_records) + "\n")
        claude.configure(cls.projects_dir)

        cls.codex_home = tmp / "codex"
        # (parent session, guardian session, referenced image) — the image is
        # not a transcript, so only the first two are parse targets.
        parent, guardian, _image = _write_guardian_sessions(cls.codex_home)
        cls.codex_files = (parent, guardian)
        codex.configure(cls.codex_home)

        cls.cursor_projects = tmp / "cursor-projects"
        cls.cli_jsonl = _write_cli_session(cls.cursor_projects)
        cls.cursor_chats = tmp / "cursor-chats"
        cls.cli_store = _write_cli_store(cls.cursor_chats)
        cursor.configure(
            tmp / "cursor-db",  # nonexistent IDE DB: IDE source stays empty
            projects_dir=cls.cursor_projects,
            chats_dir=cls.cursor_chats,
        )

        cls.opencode_db = tmp / "opencode" / "opencode.db"
        cls.opencode_parent, cls.opencode_child = _write_opencode_db(cls.opencode_db)
        opencode.configure(cls.opencode_db)

    @classmethod
    def tearDownClass(cls):
        claude.configure(cls._old_claude)
        codex.configure(cls._old_codex)
        cursor.configure(cls._old_cursor[0], projects_dir=cls._old_cursor[1],
                         chats_dir=cls._old_cursor[2])
        opencode.configure(cls._old_opencode)
        cls._tmp.cleanup()

    def assertConforms(self, errors):
        self.assertEqual(errors, [], "\n".join(errors))

    def test_all_summaries_conform(self):
        for module in (claude, codex, cursor, opencode):
            summaries = module.list_sessions()
            self.assertTrue(summaries, f"{module.__name__} listed no fixtures")
            for s in summaries:
                self.assertConforms(
                    event_schema.validate_summary(s, f"{module.__name__}:{s.get('id')}")
                )

    def test_claude_sessions_conform(self):
        for path in self.projects_dir.glob("*/*.jsonl"):
            data = claude.parse_session(path)
            self.assertConforms(event_schema.validate_session(data, path.name))

    def test_claude_branch_event_emitted_and_valid(self):
        data = claude.parse_session(
            self.projects_dir / "-tmp-proj" / "66666666-6666-6666-6666-666666666666.jsonl"
        )
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("branch", kinds)
        self.assertConforms(event_schema.validate_session(data, "fork"))

    def test_codex_sessions_conform(self):
        for path in self.codex_files:
            data = codex.parse_session(path)
            self.assertConforms(event_schema.validate_session(data, path.name))

    def test_cursor_sessions_conform(self):
        data = cursor.parse_cli_session(self.cli_jsonl)
        self.assertConforms(event_schema.validate_session(data, "cli-jsonl"))
        data = cursor.parse_cli_store(self.cli_store)
        self.assertConforms(event_schema.validate_session(data, "cli-store"))

    def test_opencode_sessions_conform(self):
        for session_id in (self.opencode_parent, self.opencode_child):
            data = opencode.parse_session_by_id(session_id)
            self.assertConforms(event_schema.validate_session(data, session_id))

    def test_frontend_dispatches_every_kind(self):
        """renderEvent() in app.js must have a case for every schema kind."""
        js = (Path("static") / "app.js").read_text(encoding="utf-8")
        cases = set(re.findall(r'case "([a-z_]+)"\s*:', js))
        missing = event_schema.KINDS - cases
        self.assertEqual(missing, set(),
                         f"app.js renderEvent() has no case for kinds: {sorted(missing)}")

    def test_validator_rejects_malformed_events(self):
        self.assertTrue(event_schema.validate_event({"kind": "nope"}))
        self.assertTrue(event_schema.validate_event({"kind": "user"}))  # no blocks/text
        self.assertTrue(event_schema.validate_event(
            {"kind": "user", "blocks": [{"type": "mystery"}]}))
        self.assertTrue(event_schema.validate_event(
            {"kind": "branch", "groups": [], "count": 0}))
        self.assertEqual(event_schema.validate_event(
            {"kind": "user", "blocks": [{"type": "text", "text": "hi"}]}), [])


class ThemeBootstrapSyncTest(unittest.TestCase):
    """index.html's pre-paint theme bootstrap duplicates the theme id list from
    app.js by necessity (it must run before app.js loads). A theme added to one
    but not the other fails silently — the saved theme just stops restoring —
    so keep the two lists identical."""

    def test_bootstrap_theme_list_matches_app_js(self):
        js = (Path("static") / "app.js").read_text(encoding="utf-8")
        html = (Path("static") / "index.html").read_text(encoding="utf-8")
        app_ids = re.findall(r'\{ id: "([a-z0-9]+)"', js)
        self.assertGreaterEqual(len(app_ids), 5, "THEMES not found in app.js")
        bootstrap = re.search(r"if \(\[([^\]]+)\]\.includes\(theme\)\)", html)
        self.assertIsNotNone(bootstrap, "theme bootstrap not found in index.html")
        boot_ids = re.findall(r'"([a-z0-9]+)"', bootstrap.group(1))
        self.assertEqual(boot_ids, app_ids)

    def test_theme_storage_key_matches(self):
        js = (Path("static") / "app.js").read_text(encoding="utf-8")
        html = (Path("static") / "index.html").read_text(encoding="utf-8")
        key = '"transcript-viewer:theme"'
        self.assertIn(key, js)
        self.assertIn(key, html)


if __name__ == "__main__":
    unittest.main()
