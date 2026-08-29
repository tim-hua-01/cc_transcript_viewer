#!/usr/bin/env python3
"""Tests for the opencode → Codex rollout exporter.

The load-bearing claim is that an exported session is a faithful Codex
rollout: the provider's encrypted reasoning survives, opencode's fused
tool-call-plus-result splits into Codex's two records, and the result reads
back through codex_parser as a valid session.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import codex_parser as codex
import event_schema
from codex_export import opencode_to_codex as o2c
from test_fixtures import _write_opencode_db


class OpencodeToCodexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.db = cls.root / "opencode" / "opencode.db"
        cls.parent_id, cls.child_id = _write_opencode_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self.conn = o2c.open_db(self.db)
        self.addCleanup(self.conn.close)

    def _lines(self, session_id=None, **kwargs):
        session_id = session_id or self.parent_id
        row = next(r for r in o2c.iter_sessions(self.conn) if r["id"] == session_id)
        return o2c.export_session(self.conn, row, **kwargs)

    def _payloads(self, lines, kind):
        return [
            rec["payload"] for rec in lines
            if rec["type"] == "response_item" and rec["payload"].get("type") == kind
        ]

    # -- reasoning -----------------------------------------------------------
    def test_reasoning_keeps_summary_and_encrypted_content(self):
        lines, report = self._lines()
        reasoning = self._payloads(lines, "reasoning")
        self.assertEqual(len(reasoning), 1)
        item = reasoning[0]
        self.assertEqual(item["id"], "rs_fixture")
        self.assertEqual(item["summary"],
                         [{"type": "summary_text", "text": "Need to look around first."}])
        self.assertEqual(item["encrypted_content"], "b3BhcXVl")
        self.assertEqual(report["encrypted"], 1)

    def test_token_by_token_summary_noise_is_not_exported(self):
        lines, _ = self._lines()
        self.assertNotIn("reasoning_details", json.dumps(lines))

    def test_reasoning_format_is_recorded_not_assumed_to_be_openai(self):
        lines, report = self._lines()
        self.assertEqual(report["formats"], ["xai-responses-v1"])
        meta = lines[0]["payload"]
        self.assertEqual(meta["opencode"]["reasoning_formats"], ["xai-responses-v1"])

    # -- tools ---------------------------------------------------------------
    def test_fused_tool_part_splits_into_call_and_output(self):
        lines, _ = self._lines()
        call = next(p for p in self._payloads(lines, "function_call") if p["name"] == "read")
        self.assertEqual(call["call_id"], "call-1")
        # Arguments stay opencode's own; they are what the model was sent.
        self.assertEqual(json.loads(call["arguments"])["filePath"], "/tmp/proj/main.py")
        output = next(
            p for p in self._payloads(lines, "function_call_output") if p["call_id"] == "call-1"
        )
        self.assertEqual(output["output"],
                         [{"type": "input_text", "text": "<path>/tmp/proj/main.py</path>"}])

    def test_failed_tool_exports_its_error_as_the_output(self):
        lines, _ = self._lines()
        output = next(
            p for p in self._payloads(lines, "function_call_output") if p["call_id"] == "call-2"
        )
        self.assertEqual(output["output"], [{"type": "input_text", "text": "oldString not found"}])

    def test_unfinished_call_has_no_output_record(self):
        lines, report = self._lines()
        calls = {p["call_id"] for p in self._payloads(lines, "function_call")}
        outputs = {p["call_id"] for p in self._payloads(lines, "function_call_output")}
        self.assertIn("call-4", calls)          # the pending grep
        self.assertNotIn("call-4", outputs)
        self.assertEqual(report["unfinished_calls"], 1)

    def test_tool_output_string_mode(self):
        lines, _ = self._lines(tool_output="string")
        output = next(
            p for p in self._payloads(lines, "function_call_output") if p["call_id"] == "call-1"
        )
        self.assertEqual(output["output"], "<path>/tmp/proj/main.py</path>")

    # -- session shape -------------------------------------------------------
    def test_session_meta_identifies_the_export_as_opencode(self):
        lines, _ = self._lines()
        self.assertEqual(lines[0]["type"], "session_meta")
        meta = lines[0]["payload"]
        self.assertEqual(meta["originator"], "opencode")
        self.assertEqual(meta["source"], "opencode")
        self.assertEqual(meta["cwd"], "/tmp/proj")
        # The session row spells the model `id`, unlike messages' `modelID`.
        self.assertEqual(meta["model"], "x-ai/grok-test")
        self.assertEqual(meta["model_provider"], "openrouter")
        self.assertEqual(meta["opencode"]["slug"], "brisk-otter")

    def test_event_msg_mirrors_only_what_the_human_typed(self):
        lines, _ = self._lines()
        mirrors = [
            rec["payload"]["message"] for rec in lines
            if rec["type"] == "event_msg" and rec["payload"].get("type") == "user_message"
        ]
        self.assertEqual(mirrors, ["look at the repo"])
        # …but the injected text still reaches the model, so it stays in the
        # response_item content.
        user = next(p for p in self._payloads(lines, "message") if p["role"] == "user")
        texts = [c["text"] for c in user["content"] if c["type"] == "input_text"]
        self.assertIn("<task>background result</task>", texts)

    def test_image_part_becomes_an_input_image(self):
        lines, _ = self._lines()
        user = next(p for p in self._payloads(lines, "message") if p["role"] == "user")
        image = next(c for c in user["content"] if c["type"] == "input_image")
        self.assertTrue(image["image_url"].startswith("data:image/png;base64,"))

    def test_no_event_msg_mode_drops_mirrors_but_keeps_the_error(self):
        lines, _ = self._lines(event_msgs=False)
        kinds = {
            rec["payload"].get("type") for rec in lines if rec["type"] == "event_msg"
        }
        self.assertNotIn("user_message", kinds)
        self.assertNotIn("agent_message", kinds)
        # A failed turn has no response_item to mirror, so dropping it would
        # lose the only record that the turn errored.
        self.assertEqual(kinds, {"error"})

    def test_timestamps_come_from_the_parts_own_clocks(self):
        lines, _ = self._lines()
        reasoning = next(
            rec for rec in lines
            if rec["type"] == "response_item" and rec["payload"].get("type") == "reasoning"
        )
        # The part started later than the message it belongs to.
        self.assertEqual(reasoning["timestamp"], "2023-11-14T22:13:20.250Z")
        output = next(
            rec for rec in lines
            if rec["type"] == "response_item"
            and rec["payload"].get("type") == "function_call_output"
            and rec["payload"]["call_id"] == "call-1"
        )
        self.assertEqual(output["timestamp"], "2023-11-14T22:13:20.350Z")

    # -- round trip ----------------------------------------------------------
    def test_output_is_a_readable_codex_session(self):
        lines, _ = self._lines()
        path = o2c.output_path(self.root / "out", self.parent_id, lines[0]["timestamp"], "codex")
        o2c.write_rollout(path, lines)
        self.assertTrue(path.name.startswith("rollout-"))
        parsed = codex.parse_session(path)
        self.assertEqual(event_schema.validate_session(parsed, path.name), [])
        # An exported rollout is labelled by where it came from, not as Codex.
        self.assertEqual(parsed["agent"], "opencode")
        kinds = [event["kind"] for event in parsed["events"]]
        self.assertEqual(kinds.count("reasoning"), 1)
        self.assertEqual(kinds.count("tool"), 4)
        self.assertEqual(kinds.count("user"), 1)

    def test_reparsed_reasoning_reports_the_encrypted_blob(self):
        lines, _ = self._lines()
        path = o2c.output_path(self.root / "rt", self.parent_id, lines[0]["timestamp"], "flat")
        o2c.write_rollout(path, lines)
        parsed = codex.parse_session(path)
        reasoning = next(e for e in parsed["events"] if e["kind"] == "reasoning")
        self.assertTrue(reasoning["has_encrypted"])
        self.assertEqual(reasoning["text"], "Need to look around first.")

    # -- selection -----------------------------------------------------------
    def test_with_encrypted_filter_keeps_only_sessions_that_have_blobs(self):
        every = {r["id"] for r in o2c.select_sessions(self.conn)}
        self.assertEqual(every, {self.parent_id, self.child_id})
        # The sub-agent session's reply carries no reasoning at all.
        filtered = {r["id"] for r in o2c.select_sessions(self.conn, with_encrypted=True)}
        self.assertEqual(filtered, {self.parent_id})

    def test_session_filter_selects_one(self):
        rows = o2c.select_sessions(self.conn, session_ids={self.child_id})
        self.assertEqual([r["id"] for r in rows], [self.child_id])

    def test_cli_writes_rollouts(self):
        out = self.root / "cli"
        with contextlib.redirect_stdout(io.StringIO()):
            code = o2c.main(["--db", str(self.db), "--out", str(out)])
        self.assertEqual(code, 0)
        written = sorted(out.glob("**/*.jsonl"))
        self.assertEqual(len(written), 2)

    def test_cli_reports_a_missing_database(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(o2c.main(["--db", str(self.root / "nope.db"), "--list"]), 2)


if __name__ == "__main__":
    unittest.main()
