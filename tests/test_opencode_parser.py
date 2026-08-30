#!/usr/bin/env python3
"""Characterization tests for opencode_parser's part-to-event mapping:
argument renaming, tool states, sub-agent linkage, and provider-noise
stripping."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import opencode_parser as opencode
from tests.fixture_builders import _write_opencode_db


class OpencodeParserTests(unittest.TestCase):
    """opencode's SQLite parts → viewer events."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old_db = opencode.DB_PATH
        db = Path(cls._tmp.name) / "opencode" / "opencode.db"
        cls.parent_id, cls.child_id = _write_opencode_db(db)
        opencode.configure(db)

    @classmethod
    def tearDownClass(cls):
        opencode.configure(cls._old_db)
        cls._tmp.cleanup()

    def setUp(self):
        self.data = opencode.parse_session_by_id(self.parent_id)
        self.blocks = [
            b for e in self.data["events"] if e["kind"] == "assistant"
            for b in e["blocks"]
        ]

    def tool(self, name):
        return next(b for b in self.blocks if b.get("name") == name)

    def test_tool_arguments_renamed_to_canonical_names(self):
        """camelCase opencode arguments become the names the frontend formats."""
        self.assertEqual(
            self.tool("read")["input"],
            {"file_path": "/tmp/proj/main.py", "offset": 1, "limit": 20},
        )
        self.assertEqual(
            self.tool("edit")["input"],
            {"file_path": "/tmp/proj/main.py", "old_string": "a",
             "new_string": "b", "replace_all": True},
        )

    def test_tool_states_map_to_results(self):
        self.assertFalse(self.tool("read")["result"]["is_error"])
        error = self.tool("edit")["result"]
        self.assertTrue(error["is_error"])
        self.assertEqual(error["text"], "oldString not found")
        # A call opencode recorded but never finished has no result at all.
        pending = self.tool("grep")
        self.assertIsNone(pending["result"])
        self.assertEqual(pending["status"], "pending")

    def test_task_tool_links_to_the_subagent_session(self):
        task = self.tool("task")
        self.assertEqual(task["child_session_id"], self.child_id)
        self.assertEqual(task["child_file"], opencode.SESSION_SCHEME + self.child_id)

    def test_reasoning_becomes_a_thinking_block(self):
        thinking = [b for b in self.blocks if b["type"] == "thinking"]
        self.assertEqual([b["text"] for b in thinking], ["Need to look around first."])
        # The provider kept an opaque blob beside the summary, so the visible
        # text is not the whole chain of thought — flag it rather than imply it.
        self.assertTrue(thinking[0]["has_encrypted"])

    def test_provider_token_noise_is_not_carried_through(self):
        """Nothing may smuggle in the per-token provider `reasoning_details`."""
        self.assertNotIn("reasoning_details", json.dumps(self.data["events"]))

    def test_synthetic_user_text_is_a_notice_not_a_prompt(self):
        prompts = [
            b["text"] for e in self.data["events"] if e["kind"] == "user"
            for b in e["blocks"] if b["type"] == "text"
        ]
        self.assertEqual(prompts, ["look at the repo"])
        notice = next(e for e in self.data["events"]
                      if e["kind"] == "notice" and e["label"] == "Injected")
        self.assertEqual(notice["text"], "<task>background result</task>")

    def test_failed_turn_emits_an_error_notice(self):
        notice = next(e for e in self.data["events"]
                      if e["kind"] == "notice" and e["label"] == "Error")
        self.assertEqual(notice["text"],
                         "ProviderAuthError: Missing Authentication header")

    def test_retry_and_patch_bookkeeping_is_surfaced(self):
        retry = next(e for e in self.data["events"]
                     if e["kind"] == "notice" and e["label"].startswith("Retry"))
        self.assertEqual(retry["text"], "APIError: 429 slow down")
        turn = next(e for e in self.data["events"] if e["kind"] == "assistant")
        self.assertEqual(turn["turn_metadata"]["patched_files"], ["/tmp/proj/main.py"])

    def test_image_part_becomes_an_image_block(self):
        user = next(e for e in self.data["events"] if e["kind"] == "user")
        image = next(b for b in user["blocks"] if b["type"] == "image")
        self.assertTrue(image["data_uri"].startswith("data:image/png;base64,"))

    def test_placeholder_title_falls_back_to_the_first_prompt(self):
        child = opencode.parse_session_by_id(self.child_id)
        self.assertEqual(child["title"], "look at everything")
        self.assertTrue(child["is_subagent"])
        self.assertEqual(child["subagent_type"], "explore")
        self.assertEqual(child["parent_file"],
                         opencode.SESSION_SCHEME + self.parent_id)

    def test_summary_counts_and_subagent_linkage(self):
        summaries = {s["id"]: s for s in opencode.list_sessions()}
        parent = summaries[self.parent_id]
        self.assertEqual(parent["n_user"], 2)
        self.assertEqual(parent["n_assistant"], 2)
        self.assertEqual(parent["n_tool"], 4)
        self.assertEqual(parent["model"], "openrouter/x-ai/grok-test")
        self.assertNotIn("is_subagent", parent)
        self.assertEqual(summaries[self.child_id]["parent_id"], self.parent_id)

    def test_missing_database_lists_nothing(self):
        opencode.configure(Path(self._tmp.name) / "nope" / "opencode.db")
        try:
            self.assertEqual(opencode.list_sessions(), [])
            self.assertIsNone(opencode.parse_session_by_id(self.parent_id))
        finally:
            opencode.configure(Path(self._tmp.name) / "opencode" / "opencode.db")

class RawFallbackTests(unittest.TestCase):
    """Unknown part types must surface as raw cards, never vanish; known
    bookkeeping parts stay silent."""

    def _parse_with_extra_parts(self, parts):
        import sqlite3 as sq
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "opencode.db"
            parent_id, _child = _write_opencode_db(db)
            conn = sq.connect(db)
            (msg_id,) = conn.execute(
                "SELECT id FROM message WHERE session_id=? ORDER BY time_created LIMIT 1",
                (parent_id,),
            ).fetchone()
            with conn:
                for i, part in enumerate(parts):
                    conn.execute(
                        "INSERT INTO part(id, message_id, session_id, data) VALUES (?,?,?,?)",
                        (f"prt_extra{i:04d}", msg_id, parent_id, json.dumps(part)),
                    )
            conn.close()
            old = opencode.DB_PATH
            opencode.configure(db)
            try:
                return opencode.parse_session_by_id(parent_id)
            finally:
                opencode.configure(old)

    def test_unknown_part_type_surfaces_as_a_raw_card(self):
        data = self._parse_with_extra_parts([
            {"type": "hologram", "detail": 42},
        ])
        raws = [e for e in data["events"] if e["kind"] == "raw"]
        self.assertEqual([e["record_type"] for e in raws], ["part/hologram"])
        self.assertEqual(raws[0]["payload"]["detail"], 42)

    def test_known_bookkeeping_parts_stay_silent(self):
        data = self._parse_with_extra_parts([
            {"type": "step-start"},
            {"type": "snapshot", "id": "snap"},
            {"type": "agent", "name": "explore"},
        ])
        self.assertNotIn("raw", [e["kind"] for e in data["events"]])


if __name__ == "__main__":
    unittest.main()
