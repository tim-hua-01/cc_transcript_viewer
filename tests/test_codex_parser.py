#!/usr/bin/env python3
"""Characterization tests for codex_parser internals: reasoning-summary
grouping and the JS-literal orchestration/exec unwrapping."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import codex_parser as codex
import search_text
import server
from tests.fixture_builders import ViewerServerTestCase, _write_jsonl


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


class CodexTokenUsageRecordTests(unittest.TestCase):
    def test_detailed_usage_is_folded_into_turn_metadata(self):
        records = [
            {
                "timestamp": "2026-09-04T19:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "turn-1"},
            },
            {
                "timestamp": "2026-09-04T19:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hello"},
            },
            {
                "timestamp": "2026-09-04T19:00:02Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "working"},
            },
            {
                "timestamp": "2026-09-04T19:00:03Z",
                "type": "token_usage_record",
                "payload": {
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "session_id": "thread-1",
                    "root_turn_id": "turn-1",
                    "response_id": "resp-1",
                    "usage": {"input_tokens": 90, "output_tokens": 10},
                    "turn_token_usage": {"input_tokens": 90, "output_tokens": 10},
                    "thread_token_usage": {"input_tokens": 900, "output_tokens": 100},
                },
            },
            {
                "timestamp": "2026-09-04T19:00:04Z",
                "type": "token_usage_record",
                "payload": {
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "session_id": "thread-1",
                    "root_turn_id": "turn-1",
                    "response_id": "resp-2",
                    "usage": {"input_tokens": 95, "output_tokens": 5},
                    "turn_token_usage": {"input_tokens": 185, "output_tokens": 15},
                    "thread_token_usage": {"input_tokens": 995, "output_tokens": 105},
                },
            },
            # The legacy aggregate is still emitted after the detailed record.
            # It must not replace the more useful turn-scoped primary usage.
            {
                "timestamp": "2026-09-04T19:00:05Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": {"input_tokens": 995, "output_tokens": 105}},
                },
            },
            {
                "timestamp": "2026-09-04T19:00:06Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1"},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            _write_jsonl(path, records)
            data = codex.parse_session(path)

        answer = next(event for event in data["events"] if event.get("text") == "working")
        metadata = answer["turn_metadata"]
        self.assertEqual(metadata["usage"], {"input_tokens": 185, "output_tokens": 15})
        self.assertEqual(metadata["last_response_usage"],
                         {"input_tokens": 95, "output_tokens": 5})
        self.assertEqual(metadata["thread_usage"],
                         {"input_tokens": 995, "output_tokens": 105})
        self.assertEqual(metadata["last_response_id"], "resp-2")
        self.assertEqual(metadata["turn_id"], "turn-1")
        self.assertFalse(
            {"raw", "tokens", "status"} & {event["kind"] for event in data["events"]}
        )


class CodexGeneratedTitleTests(unittest.TestCase):
    def test_thread_name_is_exposed_like_claude_ai_title(self):
        records = [{
            "timestamp": "2026-09-05T12:00:00Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "a long initial request"},
        }]
        row = {
            "id": "thread-1",
            "title": "a long initial request",
            "name": "Summarize parser metadata",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            _write_jsonl(path, records)
            summary = codex.session_summary(path, row)
            with mock.patch.object(codex, "_read_thread_rows", return_value={str(path): row}):
                data = codex.parse_session(path)

        self.assertEqual(summary["title"], "a long initial request")
        self.assertEqual(summary["ai_title"], "Summarize parser metadata")
        self.assertEqual(data["title"], "a long initial request")
        self.assertEqual(data["ai_title"], "Summarize parser metadata")

    def test_forked_subagent_keeps_own_meta_and_matching_titles(self):
        # A spawned subagent replays its parent's session_meta after its own.
        # The list and the open view must agree on the title, or the live
        # poller re-renders the open transcript every tick.
        records = [
            {"timestamp": "2026-09-25T12:00:00Z", "type": "session_meta", "payload": {
                "id": "child-1", "thread_source": "subagent",
                "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent-1"}}},
                "parent_thread_id": "parent-1",
            }},
            {"timestamp": "2026-09-25T12:00:00Z", "type": "session_meta", "payload": {
                "id": "parent-1", "source": "cli",
            }},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            _write_jsonl(path, records)
            summary = codex.session_summary(path, None)
            with mock.patch.object(codex, "_read_thread_rows", return_value={}):
                data = codex.parse_session(path)

        self.assertEqual(summary["id"], "child-1")
        self.assertTrue(summary.get("is_subagent"))
        self.assertEqual(data["id"], "child-1")
        self.assertEqual(summary["title"], data["title"])


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

    def test_codex_exec_orchestration_is_structured(self):
        command_source = (
            'const r = await tools.exec_command({"cmd":"git status --short",'
            '"workdir":"/tmp"}); text(r.output);'
        )
        command = codex._normalize_tool_input("exec", command_source)
        self.assertEqual(command["calls"][0]["name"], "exec_command")
        self.assertEqual(command["calls"][0]["input"]["cmd"], "git status --short")
        self.assertIn("git status --short", search_text.event_text({"input": command}))

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


class CodexFixtureSessionTests(ViewerServerTestCase):
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


if __name__ == "__main__":
    unittest.main()
