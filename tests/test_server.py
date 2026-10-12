"""Focused tests for cross-parser session-list behavior in server.py."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import claude_parser as claude
import server
from tests.fixture_builders import ViewerServerTestCase


class _FixtureParser:
    def __init__(self, sessions):
        self.sessions = sessions

    def list_sessions(self):
        return [dict(session) for session in self.sessions]


class SessionOrderingTests(unittest.TestCase):
    def test_last_activity_beats_file_mtime_and_subagents_stay_with_parent(self):
        sessions = [
            {
                "file": "old",
                "title": "old session with recently touched bookkeeping",
                "last_ts": "2026-01-01T00:00:00Z",
                "mtime": 9_999_999_999,
            },
            {
                "file": "new",
                "title": "new session",
                "last_ts": "2026-02-01T00:00:00Z",
                "mtime": 1,
            },
            {
                "file": "child",
                "title": "new session child",
                "last_ts": "2025-01-01T00:00:00Z",
                "mtime": 0,
                "is_subagent": True,
                "parent_file": "new",
            },
        ]
        with (
            mock.patch.object(server, "PARSERS", {"fixture": _FixtureParser(sessions)}),
            mock.patch.object(server, "load_summary_caches"),
            mock.patch.object(server, "save_summary_caches"),
            mock.patch.object(server, "_apply_custom_name"),
        ):
            ordered = server.list_sessions()

        self.assertEqual([session["file"] for session in ordered], ["new", "child", "old"])

    def test_invalid_or_missing_activity_falls_back_to_mtime(self):
        self.assertEqual(server._recency({"last_ts": "not-a-date", "mtime": 42}), 42)
        self.assertEqual(server._recency({"mtime": 17}), 17)


class CustomNameAndSearchTests(ViewerServerTestCase):
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


class SearchTitleSegmentTests(unittest.TestCase):
    def test_agent_native_titles_have_half_custom_weight(self):
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
        codex = {
            "agent": "codex",
            "custom_title": "",
            "original_title": "A long Codex prompt",
            "ai_title": "Short Codex title",
        }
        custom, native = server._search_title_segments(claude)
        self.assertEqual(custom, "")
        self.assertEqual(native.count("Original Claude title"), 1)
        self.assertIn("Short Claude title", native)
        self.assertEqual(
            server._search_title_segments(cursor),
            ("", "Short Cursor title"),
        )
        self.assertEqual(
            server._search_title_segments(codex),
            ("", "A long Codex prompt\nShort Codex title"),
        )


if __name__ == "__main__":
    unittest.main()


class PromptExportTests(ViewerServerTestCase):
    """POST /api/prompts: the user's own prompts, grouped by session, as Markdown."""

    def test_markdown_groups_prompts_by_session_in_request_order(self):
        status, _, body = self.post_json(
            "/api/prompts",
            {"files": [str(self.later_prompt_fixture), str(self.fixture)]},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["sessions"], 2)
        self.assertEqual(payload["prompts"], 3)
        md = payload["markdown"]
        self.assertTrue(md.startswith("# My prompts, all time\n"))
        self.assertIn("## [Claude] ", md)
        self.assertIn("- Project: /tmp/proj", md)
        # Headings carry local time, so derive the expected day the same way.
        day, time = server._local_day_and_time("2024-01-01T00:00:00Z")
        self.assertIn(f"### {day} {time}\n", md)
        self.assertIn("\nhello world\n", md)
        self.assertIn("\nlaterpromptword in a subsequent user message\n", md)
        # Assistant text never leaks into the prompt digest.
        self.assertNotIn("hi there", md)
        self.assertNotIn("continued reply", md)
        # Request order is preserved: the later-prompt session was listed first.
        self.assertLess(md.index("unrelated opening prompt"), md.index("hello world"))

    def test_date_window_filters_individual_prompts(self):
        status, _, body = self.post_json(
            "/api/prompts", {"files": [str(self.fixture)], "from": "2024-01-02"}
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual((payload["sessions"], payload["prompts"]), (0, 0))
        self.assertIn("# My prompts, since 2024-01-02", payload["markdown"])

        status, _, body = self.post_json(
            "/api/prompts",
            {"files": [str(self.fixture)], "from": "2023-12-31", "to": "2024-01-01"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["prompts"], 1)
        self.assertIn("# My prompts, 2023-12-31 to 2024-01-01", payload["markdown"])

    def test_unlisted_sessions_and_duplicates_are_ignored(self):
        outside = self.projects_dir.parent / "outside.jsonl"
        outside.write_text("")
        status, _, body = self.post_json(
            "/api/prompts",
            {"files": [str(outside), "/etc/passwd", str(self.fixture), str(self.fixture)]},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual((payload["sessions"], payload["prompts"]), (1, 1))
        self.assertNotIn("outside", payload["markdown"])

    def test_rejects_malformed_bodies(self):
        for bad in (
            {"files": "not-a-list"},
            {"files": [1]},
            {"files": [str(self.fixture)], "from": "yesterday"},
            {"files": [str(self.fixture)], "to": 7},
        ):
            status, _, _ = self.post_json("/api/prompts", bad)
            self.assertEqual(status, 400, bad)
        status, _, _ = self.post_json(
            "/api/prompts", {"files": [str(self.fixture)] * (server.PROMPT_EXPORT_MAX_SESSIONS + 1)}
        )
        self.assertEqual(status, 400)


class PromptTextTests(unittest.TestCase):
    def test_prompt_text_handles_both_shapes_and_images(self):
        self.assertEqual(server._prompt_text({"kind": "user", "text": "  flat  "}), "flat")
        blocks = [
            {"type": "text", "text": "look at this"},
            {"type": "image", "data": "..."},
            {"type": "tool_use", "name": "x"},
        ]
        self.assertEqual(
            server._prompt_text({"kind": "user", "blocks": blocks}),
            "look at this\n[image attached]",
        )

    def test_abandoned_branches_are_marked_and_undated_prompts_need_no_window(self):
        data = {"events": [
            {"kind": "user", "ts": None, "blocks": [{"type": "text", "text": "first"}]},
            {"kind": "branch", "groups": [[
                {"kind": "user", "ts": "2024-05-05T12:00:00Z", "blocks": [{"type": "text", "text": "dropped"}]},
            ]]},
            {"kind": "notice", "text": "not a prompt"},
        ]}
        rows = server._session_prompts(data, "", "")
        self.assertEqual([(r[2], r[3]) for r in rows], [("first", False), ("dropped", True)])
        rows = server._session_prompts(data, "2024-05-05", "2024-05-05")
        self.assertEqual([r[2] for r in rows], ["dropped"])
