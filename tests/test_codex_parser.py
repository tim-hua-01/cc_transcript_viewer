#!/usr/bin/env python3
"""Characterization tests for codex_parser internals: reasoning-summary
grouping and the JS-literal orchestration/exec unwrapping."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import codex_parser as codex
from tests.fixture_builders import _write_jsonl


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


class ItemCompletedFormatTests(unittest.TestCase):
    """Codex >=0.147 stopped writing user_message/agent_message event mirrors;
    user and agent messages only exist inside event_msg/item_completed
    envelopes. The parser must surface each exactly once, with image recovery,
    and only the messages — reasoning and tool items in the same envelope
    still arrive as response_items."""

    TS = "2026-08-29T10:00:00.000Z"

    def _rec(self, rec_type, payload):
        return {"timestamp": self.TS, "type": rec_type, "payload": payload}

    def _envelope(self, item_type, text, **item_extra):
        item = {"type": item_type, "id": "i1",
                "content": [{"type": "text", "text": text, "text_elements": []}]}
        item.update(item_extra)
        return self._rec("event_msg", {"type": "item_completed", "item": item})

    def _parse_and_summary(self, records):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            path = tmp / "rollout-2026-08-29T10-00-00-0197a2e5-b222-7ab0-8888-000000000147.jsonl"
            _write_jsonl(path, records)
            codex.configure(tmp)
            try:
                return codex.parse_session(path), codex._session_summary_uncached(path)
            finally:
                codex.configure(codex.DEFAULT_CODEX_HOME)

    def test_messages_come_from_the_item_completed_envelope(self):
        data, summary = self._parse_and_summary([
            self._rec("session_meta", {"id": "x", "cwd": "/tmp/proj",
                                       "cli_version": "0.147.0"}),
            self._envelope("UserMessage", "hello new format"),
            self._rec("response_item",
                      {"type": "message", "role": "user", "id": "m1",
                       "content": [{"type": "input_text", "text": "hello new format"}]}),
            # The same reasoning arrives both in the envelope and as a
            # response_item; only the response_item copy may render.
            self._rec("event_msg", {"type": "item_completed", "item": {
                "type": "Reasoning", "id": "r1", "summary_text": ["thinking..."]}}),
            self._rec("response_item", {"type": "reasoning", "id": "r1",
                                        "summary": [{"type": "summary_text",
                                                     "text": "thinking..."}]}),
            self._envelope("AgentMessage", "hi from 0.147", phase="commentary"),
        ])
        kinds = [e["kind"] for e in data["events"]]
        users = [e for e in data["events"] if e["kind"] == "user"]
        assistants = [e for e in data["events"] if e["kind"] == "assistant"]
        self.assertEqual([e["text"] for e in users], ["hello new format"])
        self.assertEqual([e["text"] for e in assistants], ["hi from 0.147"])
        # The response_item copy of the prompt is a repeat, not an extra turn
        # or an instructions block; the Reasoning envelope must not render on
        # top of the response_item reasoning.
        self.assertEqual(kinds.count("user"), 1)
        self.assertEqual(kinds.count("reasoning"), 1)
        self.assertNotIn("instructions", kinds)
        self.assertEqual(data["title"], "hello new format")
        # The sidebar counters must see the envelope messages too.
        self.assertEqual(summary["n_user"], 1)
        self.assertEqual(summary["n_assistant"], 1)

    def test_envelope_prompt_recovers_images_from_its_response_item_copy(self):
        data, _ = self._parse_and_summary([
            self._envelope("UserMessage", "look at this"),
            self._rec("response_item",
                      {"type": "message", "role": "user", "id": "m1",
                       "content": [
                           {"type": "input_text", "text": "look at this"},
                           {"type": "input_image",
                            "image_url": "data:image/png;base64,iVBORw0KGgo="},
                       ]}),
        ])
        users = [e for e in data["events"] if e["kind"] == "user"]
        self.assertEqual(len(users), 1)
        self.assertEqual(len(users[0]["images"]), 1)

    def test_hybrid_mirror_plus_envelope_file_renders_each_message_once(self):
        data, summary = self._parse_and_summary([
            self._rec("event_msg", {"type": "user_message", "message": "hello"}),
            self._envelope("UserMessage", "hello"),
            self._rec("event_msg", {"type": "agent_message", "message": "hi"}),
            self._envelope("AgentMessage", "hi"),
            # An envelope-only message from after a mid-session CLI upgrade
            # must still come through.
            self._envelope("UserMessage", "post-upgrade prompt"),
        ])
        users = [e["text"] for e in data["events"] if e["kind"] == "user"]
        assistants = [e["text"] for e in data["events"] if e["kind"] == "assistant"]
        self.assertEqual(users, ["hello", "post-upgrade prompt"])
        self.assertEqual(assistants, ["hi"])
        self.assertEqual(summary["n_user"], 2)
        self.assertEqual(summary["n_assistant"], 1)

    def test_unknown_event_msg_subtype_surfaces_as_a_raw_card(self):
        data, _ = self._parse_and_summary([
            self._rec("event_msg", {"type": "brand_new_thing", "detail": 1}),
            self._rec("event_msg", {"type": "thread_settings_applied"}),
        ])
        raws = [e for e in data["events"] if e["kind"] == "raw"]
        self.assertEqual([e["record_type"] for e in raws],
                         ["event_msg/brand_new_thing"])


if __name__ == "__main__":
    unittest.main()
