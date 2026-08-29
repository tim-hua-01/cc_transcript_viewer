#!/usr/bin/env python3
"""Characterization tests for codex_parser internals: reasoning-summary
grouping and the JS-literal orchestration/exec unwrapping."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import codex_parser as codex
from test_fixtures import _write_jsonl


class CodexReasoningSummaryTests(unittest.TestCase):
    def _parse(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            _write_jsonl(path, records)
            return codex.parse_session(path)

    def test_multi_part_response_groups_agent_reasoning_mirrors(self):
        parts = ["**First**\n\nAlpha", "**Second**\n\nBeta", "**Third**\n\nGamma"]
        records = [
            {
                "timestamp": "2026-01-01T00:00:00.000Z",
                "type": "event_msg",
                "payload": {"type": "agent_reasoning", "text": text},
            }
            for text in parts
        ]
        records.append(
            {
                "timestamp": "2026-01-01T00:00:00.002Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "id": "rs_grouped",
                    "summary": [{"type": "summary_text", "text": text} for text in parts],
                    "encrypted_content": "opaque",
                },
            }
        )

        reasoning = [event for event in self._parse(records)["events"] if event["kind"] == "reasoning"]

        self.assertEqual(len(reasoning), 1)
        self.assertEqual(reasoning[0]["text"], "\n".join(parts))
        self.assertTrue(reasoning[0]["has_encrypted"])

    def test_non_matching_reasoning_events_remain_separate(self):
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "agent_reasoning", "text": "Independent episode"},
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "Grouped first"},
                        {"type": "summary_text", "text": "Grouped second"},
                    ],
                },
            },
        ]

        reasoning = [event for event in self._parse(records)["events"] if event["kind"] == "reasoning"]

        self.assertEqual(
            [event["text"] for event in reasoning],
            ["Independent episode", "Grouped first\nGrouped second"],
        )

    def test_encrypted_only_reasoning_is_not_rendered(self):
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "id": "rs_opaque",
                    "summary": [],
                    "encrypted_content": "opaque continuation state",
                },
            }
        ]

        reasoning = [event for event in self._parse(records)["events"] if event["kind"] == "reasoning"]

        self.assertEqual(reasoning, [])


class CodexOrchestrationTests(unittest.TestCase):
    """The JS-literal parser that unpacks generated `tools.name(...)` calls."""

    def test_literal_call_and_constant_reference(self):
        source = (
            'const CMD = {"cmd": "ls -la", "timeout": 5};\n'
            "// a comment with tools.fake(1) inside\n"
            "tools.exec_command(CMD);\n"
            'tools.apply_patch({input: `*** Update File: x.py`});\n'
            "tools.shell(buildCommand());\n"
        )
        out = codex._exec_orchestration(source)
        self.assertEqual(out["code"], source)
        calls = out["calls"]
        self.assertEqual([c["name"] for c in calls], ["exec_command", "apply_patch", "shell"])
        self.assertEqual(calls[0]["input"], {"cmd": "ls -la", "timeout": 5})
        self.assertEqual(calls[1]["input"], {"input": "*** Update File: x.py"})
        # Non-literal argument: recorded with no recoverable input.
        self.assertIsNone(calls[2]["input"])

    def test_calls_inside_strings_are_ignored(self):
        source = 'const s = "tools.exec_command({cmd: 1})";\ntools.real({"a": true});'
        calls = codex._exec_orchestration(source)["calls"]
        self.assertEqual([c["name"] for c in calls], ["real"])
        self.assertEqual(calls[0]["input"], {"a": True})

    def test_string_escapes_and_literals(self):
        value, _ = codex._parse_js_literal(
            '{"a": "line\\nbreak", b: \'x\', c: null, d: undefined, e: -1.5e2, f: [1, 2,]}', 0
        )
        self.assertEqual(
            value,
            {"a": "line\nbreak", "b": "x", "c": None, "d": None, "e": -150.0, "f": [1, 2]},
        )

    def test_template_expression_is_rejected(self):
        with self.assertRaises(ValueError):
            codex._parse_js_literal("`prefix ${expr}`", 0)

    def test_mask_preserves_offsets(self):
        source = 'x = "abc"; // hi\ny = 1;'
        masked = codex._mask_js_literals(source)
        self.assertEqual(len(masked), len(source))
        self.assertNotIn("abc", masked)
        self.assertIn("y = 1;", masked)


class CodexExecUnwrapTests(unittest.TestCase):
    def test_wrapped_output_is_unwrapped(self):
        inner = {"output": "hello world", "exit_code": 1, "wall_time_seconds": 0.4}
        text = "Preamble noise\nOutput: " + json.dumps(inner)
        out = codex._unwrap_exec_text(text)
        self.assertEqual(out["text"], "hello world")
        self.assertTrue(out["is_error"])
        self.assertEqual(out["metadata"]["exit_code"], 1)

    def test_non_wrapper_text_is_left_alone(self):
        self.assertIsNone(codex._unwrap_exec_text("plain output"))
        self.assertIsNone(codex._unwrap_exec_text('{"no_output_key": 1}'))

    def test_patch_files_extracted(self):
        patch = (
            "*** Begin Patch\n*** Update File: a.py\n+x\n*** Add File: b.py\n+y\n*** End Patch"
        )
        self.assertEqual(codex._patch_files(patch), ["a.py", "b.py"])

if __name__ == "__main__":
    unittest.main()
