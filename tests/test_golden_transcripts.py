"""Regression tests over checked-in, privacy-safe transcript files."""

from __future__ import annotations

import unittest
from pathlib import Path

import claude_parser as claude
import codex_parser as codex
import cursor_parser as cursor
import event_schema


FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
CLAUDE = next((FIXTURES / "claude").glob("*/*.jsonl"))
CODEX = next((FIXTURES / "codex").glob("*.jsonl"))
CURSOR = next((FIXTURES / "cursor").glob("*/agent-transcripts/*/*.jsonl"))


class GoldenTranscriptTests(unittest.TestCase):
    def assert_session_conforms(self, data: dict) -> None:
        self.assertEqual(event_schema.validate_session(data), [])

    def test_claude_file_matches_current_record_shapes(self):
        data = claude.parse_session(CLAUDE)
        self.assert_session_conforms(data)
        self.assertEqual(data["title"], "Please inspect the example configuration.")
        self.assertEqual(data["meta"]["cwd"], "/workspace/example-project")
        user = next(event for event in data["events"] if event["kind"] == "user")
        self.assertEqual([block["type"] for block in user["blocks"]], ["text", "image"])
        tool = next(
            block
            for event in data["events"]
            for block in event.get("blocks", [])
            if block.get("type") == "tool_use"
        )
        self.assertEqual(tool["name"], "Read")
        self.assertEqual(tool["result"]["text"], "mode = fixture\n")

    def test_codex_file_matches_current_record_shapes(self):
        data = codex.parse_session(CODEX)
        self.assert_session_conforms(data)
        self.assertEqual(data["title"], "Summarize the example file.")
        self.assertEqual(data["meta"]["cwd"], "/workspace/example-project")
        self.assertEqual([event["kind"] for event in data["events"]].count("reasoning"), 1)
        user = next(event for event in data["events"] if event["kind"] == "user")
        self.assertEqual(len(user["images"]), 1)
        tool = next(event for event in data["events"] if event["kind"] == "tool")
        self.assertEqual(tool["name"], "read_file")
        self.assertEqual(tool["result"]["output"], "TOOL_OUTPUT_SHOULD_NOT_COPY\n")

    def test_cursor_cli_file_matches_current_record_shapes(self):
        data = cursor.parse_cli_session(CURSOR)
        self.assertIsNotNone(data)
        self.assert_session_conforms(data)
        self.assertEqual(data["title"], "Check the fictional example.")
        tool = next(
            block
            for event in data["events"]
            for block in event.get("blocks", [])
            if block.get("type") == "tool_use"
        )
        self.assertEqual(tool["name"], "Shell")
        self.assertEqual(tool["input"]["workdir"], "/workspace/example-project")


if __name__ == "__main__":
    unittest.main()
