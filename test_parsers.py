#!/usr/bin/env python3
"""Characterization tests for the parser internals.

These pin the behavior of the trickiest pure-parsing code — branch folding,
the Codex JS-literal orchestration parser, Cursor's blob/JSON extraction and
diff reconstruction — so format-handling refactors can't silently change what
the viewer renders.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import claude_parser as claude
import codex_parser as codex
import common
import cursor_parser as cursor


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _user(uuid, parent, text, ts):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _assistant(uuid, parent, text, ts):
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": text}],
        },
    }


class BranchFoldingTests(unittest.TestCase):
    """parse_cc_session folds rewound/edited branches into `branch` events."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _parse(self, records):
        path = self.dir / "session.jsonl"
        _write_jsonl(path, records)
        return claude.parse_session(path)

    def test_linear_history_stays_flat(self):
        data = self._parse(
            [
                _user("u1", None, "first", "2026-01-01T00:00:00Z"),
                _assistant("a1", "u1", "reply", "2026-01-01T00:00:01Z"),
            ]
        )
        self.assertEqual([e["kind"] for e in data["events"]], ["user", "assistant"])

    def test_edited_message_folds_abandoned_branch(self):
        records = [
            _user("u1", None, "first", "2026-01-01T00:00:00Z"),
            _assistant("a1", "u1", "old reply", "2026-01-01T00:00:01Z"),
            # The user rewound and edited: a second child of u1, appended later.
            _user("u2", "u1", "edited prompt", "2026-01-01T00:00:02Z"),
            _assistant("a2", "u2", "new reply", "2026-01-01T00:00:03Z"),
            {"type": "last-prompt", "leafUuid": "u2"},
        ]
        data = self._parse(records)
        kinds = [e["kind"] for e in data["events"]]
        self.assertEqual(kinds, ["user", "branch", "user", "assistant"])
        branch = data["events"][1]
        self.assertEqual(branch["count"], 1)
        self.assertEqual(branch["groups"][0][0]["blocks"][0]["text"], "old reply")
        # The active path keeps the edited prompt and its reply.
        self.assertEqual(data["events"][2]["blocks"][0]["text"], "edited prompt")
        self.assertEqual(data["events"][3]["blocks"][0]["text"], "new reply")

    def test_leaf_walks_down_to_trailing_reply(self):
        # `last-prompt` points at the prompt; the assistant reply appended after
        # it must stay on the active path, not fold away.
        records = [
            _user("u1", None, "first", "2026-01-01T00:00:00Z"),
            _assistant("a1", "u1", "old reply", "2026-01-01T00:00:01Z"),
            _user("u2", "u1", "edited", "2026-01-01T00:00:02Z"),
            _assistant("a2", "u2", "kept reply", "2026-01-01T00:00:03Z"),
            {"type": "last-prompt", "leafUuid": "u2"},
        ]
        data = self._parse(records)
        texts = [
            b["text"]
            for e in data["events"]
            if e["kind"] == "assistant"
            for b in e["blocks"]
        ]
        self.assertIn("kept reply", texts)

    def test_unplaceable_events_fall_back_to_flat_list(self):
        # A fork whose events can't all be placed on the active path must not
        # silently drop content: the parser returns the flat list instead.
        records = [
            _user("u1", None, "first", "2026-01-01T00:00:00Z"),
            _user("u2", "u1", "branch a", "2026-01-01T00:00:01Z"),
            _user("u3", "u1", "branch b", "2026-01-01T00:00:02Z"),
            # Orphan with no uuid linkage — has an event but no tree position.
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:03Z",
                "message": {"role": "user", "content": "orphan"},
            },
            {"type": "last-prompt", "leafUuid": "u3"},
        ]
        data = self._parse(records)

        def all_texts(events):
            out = []
            for e in events:
                if e.get("kind") == "branch":
                    for g in e.get("groups", []):
                        out.extend(all_texts(g))
                for b in e.get("blocks") or []:
                    if b.get("text"):
                        out.append(b["text"])
            return out

        texts = all_texts(data["events"])
        self.assertIn("orphan", texts)
        self.assertIn("branch a", texts)
        self.assertIn("branch b", texts)


class SyntheticUserNoticeTests(unittest.TestCase):
    def test_known_wrappers_become_notices(self):
        n = claude._synthetic_user_notice("<system-reminder>tick</system-reminder>")
        self.assertEqual(n["label"], "System reminder")
        n = claude._synthetic_user_notice("  <command-name>/foo</command-name>")
        self.assertEqual(n["label"], "Slash command")

    def test_real_prompts_and_unknown_tags_pass_through(self):
        self.assertIsNone(claude._synthetic_user_notice("please fix the bug"))
        self.assertIsNone(claude._synthetic_user_notice("<unknown-tag> hello"))
        self.assertIsNone(claude._synthetic_user_notice(None))

    def test_tool_result_pairing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "t1",
                                    "name": "Bash",
                                    "input": {"command": "ls"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "uuid": "u1",
                        "parentUuid": "a1",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "t1",
                                    "content": "file.txt",
                                }
                            ],
                        },
                    },
                ],
            )
            data = claude.parse_session(path)
        tools = [
            b
            for e in data["events"]
            if e.get("blocks")
            for b in e["blocks"]
            if b["type"] == "tool_use"
        ]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["result"]["text"], "file.txt")
        self.assertFalse(tools[0]["result"]["is_error"])


class SkillInstructionTests(unittest.TestCase):
    def test_skill_body_is_attached_to_tool_not_added_as_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            _write_jsonl(
                path,
                [
                    _user("u1", None, "review this", "2026-01-01T00:00:00Z"),
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "parentUuid": "u1",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "skill1",
                                    "name": "Skill",
                                    "input": {"skill": "review"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "uuid": "u2",
                        "parentUuid": "a1",
                        "timestamp": "2026-01-01T00:00:02Z",
                        "isMeta": True,
                        "sourceToolUseID": "skill1",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "# Review\nCheck the PR."}],
                        },
                    },
                ],
            )
            data = claude.parse_session(path)
            summary = claude.session_summary(path)

        skill = next(
            b
            for event in data["events"]
            for b in event.get("blocks", [])
            if b.get("type") == "tool_use" and b.get("name") == "Skill"
        )
        self.assertEqual(skill["instructions"], "# Review\nCheck the PR.")
        self.assertNotIn("instructions", [e["kind"] for e in data["events"]])
        self.assertEqual([e["kind"] for e in data["events"]].count("user"), 1)
        self.assertEqual(summary["n_user"], 1)
        self.assertEqual(summary["title"], "review this")


class QueuedPromptTests(unittest.TestCase):
    """queued_command attachments may carry a plain string or a block list."""

    def _parse(self, prompt):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            _write_jsonl(path, [{
                "type": "attachment",
                "uuid": "q1",
                "timestamp": "2026-01-01T00:00:00Z",
                "attachment": {"type": "queued_command", "prompt": prompt},
            }])
            return claude.parse_session(path)

    def test_string_prompt(self):
        data = self._parse("queued question")
        self.assertEqual(data["events"][0]["kind"], "user")
        self.assertTrue(data["events"][0]["queued"])
        self.assertEqual(data["events"][0]["blocks"], [{"type": "text", "text": "queued question"}])

    def test_block_list_prompt_with_image(self):
        # Queuing a message with a pasted image records the prompt as a block
        # list; every emitted text block must still carry a plain string.
        prompt = [
            {"type": "text", "text": "[Image #1] what is this?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}},
        ]
        data = self._parse(prompt)
        ev = data["events"][0]
        self.assertEqual(ev["kind"], "user")
        types = [b["type"] for b in ev["blocks"]]
        self.assertEqual(types, ["text", "image"])
        self.assertEqual(ev["blocks"][0]["text"], "[Image #1] what is this?")
        self.assertTrue(ev["blocks"][1]["data_uri"].startswith("data:image/png;base64,"))

    def test_block_list_synthetic_wrapper_becomes_notice(self):
        prompt = [{"type": "text", "text": "<system-reminder>tick</system-reminder>"}]
        data = self._parse(prompt)
        self.assertEqual(data["events"][0]["kind"], "notice")
        self.assertEqual(data["events"][0]["label"], "System reminder")


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


class CodexItemCompletedTests(unittest.TestCase):
    """Codex 0.147 moved messages into an `item_completed` envelope."""

    def _parse(self, records):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            _write_jsonl(path, records)
            return codex.parse_session(path)

    @staticmethod
    def _item(ts, item):
        return {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "item_completed", "item": item},
        }

    def test_messages_become_turns_not_injected_instructions(self):
        prompt = "Summarize the build failure"
        records = [
            self._item(
                "2026-01-01T00:00:00Z",
                {"type": "UserMessage", "content": [{"type": "text", "text": prompt}]},
            ),
            # The mirrored response_items the model actually received.
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            },
            {
                "timestamp": "2026-01-01T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}],
                },
            },
            self._item(
                "2026-01-01T00:00:03Z",
                {
                    "type": "AgentMessage",
                    "content": [{"type": "Text", "text": "Done — nothing changed."}],
                    "phase": "commentary",
                },
            ),
        ]

        events = self._parse(records)["events"]

        self.assertEqual([e["text"] for e in events if e["kind"] == "user"], [prompt])
        self.assertEqual(
            [e["text"] for e in events if e["kind"] == "assistant"], ["Done — nothing changed."]
        )
        # The prompt is not repeated as injected context; only the real one is.
        self.assertEqual(
            [e["label"] for e in events if e["kind"] == "instructions"], ["Environment context"]
        )

    def test_title_comes_from_the_first_user_item(self):
        records = [
            self._item(
                "2026-01-01T00:00:00Z",
                {"type": "UserMessage", "content": [{"type": "text", "text": "Long " * 200}]},
            )
        ]

        title = self._parse(records)["title"]

        self.assertTrue(title.endswith("…"))
        self.assertLessEqual(len(title), 101)

    def test_command_and_file_items_do_not_duplicate_tool_blocks(self):
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call_1",
                    "name": "exec",
                    "input": "ls",
                },
            },
            self._item("2026-01-01T00:00:01Z", {"type": "CommandExecution", "id": "exec-1", "stdout": "a\nb"}),
            self._item("2026-01-01T00:00:02Z", {"type": "FileChange", "id": "exec-2", "changes": {}}),
        ]

        events = self._parse(records)["events"]

        self.assertEqual(len([e for e in events if e["kind"] == "tool"]), 1)

    def test_web_search_item_keeps_its_results(self):
        records = [
            self._item(
                "2026-01-01T00:00:00Z",
                {
                    "type": "Extension",
                    "kind": "web.search",
                    "id": "exec-3",
                    "query": "codex rollout format",
                    "action": {"type": "search", "query": "codex rollout format"},
                    "results": [
                        {
                            "type": "text_result",
                            "title": "Rollouts",
                            "url": "https://example.com/a",
                            "domain": "example.com",
                            "snippet": "…",
                        }
                    ],
                },
            )
        ]

        searches = [e for e in self._parse(records)["events"] if e["kind"] == "web_search"]

        self.assertEqual(len(searches), 1)
        self.assertEqual(searches[0]["query"], "codex rollout format")
        self.assertEqual(searches[0]["results"][0]["url"], "https://example.com/a")


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


class CursorBlobExtractionTests(unittest.TestCase):
    """Balanced-JSON scanning over store.db blobs."""

    def test_objects_amid_binary_junk(self):
        blob = (
            b"\x00\x01junk"
            + json.dumps({"role": "user", "content": 'has {braces} and "quotes\\"'}).encode()
            + b"\xff garbage "
            + json.dumps({"a": {"nested": [1, 2]}}).encode()
        )
        objs = cursor._extract_json_objects(blob)
        self.assertEqual(len(objs), 2)
        self.assertEqual(objs[0]["role"], "user")
        self.assertEqual(objs[1], {"a": {"nested": [1, 2]}})

    def test_unbalanced_braces_do_not_crash(self):
        self.assertEqual(cursor._extract_json_objects(b'{"open": '), [])

    def test_unified_diff_shows_only_changed_hunks(self):
        before = "\n".join(f"line {i}" for i in range(50))
        after = before.replace("line 25", "line twenty-five")
        diff = cursor._unified_diff(before, after, "big.txt")
        self.assertIn("-line 25", diff)
        self.assertIn("+line twenty-five", diff)
        self.assertNotIn("line 1\n", diff.replace("line 1", "line 1\n", 1)[:0] or diff)
        self.assertLess(diff.count("\n"), 20)  # not the whole file
        self.assertEqual(cursor._unified_diff("same", "same", "x"), "(no textual change)")


def _pb_varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _pb_str(field: int, s: str) -> bytes:
    payload = s.encode("utf-8")
    return _pb_varint((field << 3) | 2) + _pb_varint(len(payload)) + payload


def _pb_int(field: int, n: int) -> bytes:
    return _pb_varint(field << 3) + _pb_varint(n)


def _pb_msg(field: int, payload: bytes) -> bytes:
    return _pb_varint((field << 3) | 2) + _pb_varint(len(payload)) + payload


class CursorToolBinaryTests(unittest.TestCase):
    """Grep/glob results that only exist in Cursor's toolCallBinary protobuf."""

    def _grep_tf(self, binary: bytes, additional=None):
        import base64
        tf = {
            "name": "ripgrep_raw_search",
            "status": "completed",
            "params": {"pattern": "needle", "path": "/ws"},
            "toolCallBinary": base64.b64encode(binary).decode(),
        }
        if additional is not None:
            tf["additionalData"] = additional
        return tf

    def test_grep_content_mode_lines_decoded(self):
        # result msg (f5.f2.f1): f4 = workspace { f1 root, f2 container of
        # f3 { f1 file { f1 relpath, f2 line {f1 no, f2 text} } } }
        line1 = _pb_msg(2, _pb_int(1, 12) + _pb_str(2, "matched line one"))
        line2 = _pb_msg(2, _pb_int(1, 40) + _pb_str(2, "matched line two"))
        file_msg = _pb_msg(1, _pb_str(1, "src/thing.py") + line1 + line2)
        container = _pb_msg(2, _pb_msg(3, file_msg))
        workspace = _pb_msg(4, _pb_str(1, "/ws") + container)
        blob = _pb_msg(5, _pb_msg(2, _pb_msg(1, workspace)))
        out = cursor._normalize_tool(None, self._grep_tf(blob))
        self.assertEqual(out["name"], "Grep")
        self.assertIn("src/thing.py", out["result"]["text"])
        self.assertIn("12: matched line one", out["result"]["text"])
        self.assertIn("40: matched line two", out["result"]["text"])

    def test_grep_files_with_matches_decoded(self):
        # files_with_matches mode: container carries file msgs directly at f2.
        file_msg = _pb_msg(2, _pb_str(1, "a/b.tsx") + _pb_int(2, 1))
        workspace = _pb_msg(4, _pb_str(1, "/ws") + _pb_msg(2, file_msg))
        blob = _pb_msg(5, _pb_msg(2, _pb_msg(1, workspace)))
        out = cursor._normalize_tool(None, self._grep_tf(blob))
        self.assertIn("a/b.tsx", out["result"]["text"])

    def test_grep_pruned_note_appended(self):
        line = _pb_msg(2, _pb_int(1, 3) + _pb_str(2, "hit"))
        file_msg = _pb_msg(1, _pb_str(1, "f.py") + line)
        workspace = _pb_msg(4, _pb_str(1, "/ws") + _pb_msg(2, _pb_msg(3, file_msg)))
        blob = _pb_msg(5, _pb_msg(2, _pb_msg(1, workspace)))
        additional = {"isPruned": True, "totalMatches": 262, "totalFiles": 56}
        out = cursor._normalize_tool(None, self._grep_tf(blob, additional))
        self.assertIn("262 matches in 56 files", out["result"]["text"])

    def test_grep_falls_back_to_stats_summary(self):
        additional = {
            "totalMatches": 9, "totalFiles": 2, "isPruned": True,
            "topFiles": [{"uri": "x.py", "matchCount": 7}, {"uri": "y.py", "matchCount": 2}],
        }
        out = cursor._normalize_tool(None, self._grep_tf(b"\xff\xff not protobuf", additional))
        self.assertIn("9 matches in 2 files", out["result"]["text"])
        self.assertIn("x.py (7 matches)", out["result"]["text"])

    def test_grep_zero_matches_says_so(self):
        out = cursor._normalize_tool(
            None, self._grep_tf(b"", {"totalMatches": 0, "totalFiles": 0}))
        self.assertEqual(out["result"]["text"], "(no matches)")

    def test_glob_files_recovered_from_binary(self):
        import base64
        # dir msg (f4.f2.f1): f2 absPath, repeated f3 relPath, f4 count
        dir_msg = (
            _pb_str(2, "/ws/lib")
            + _pb_str(3, "a/one.py")
            + _pb_str(3, "b/two.py")
            + _pb_int(4, 2)
        )
        blob = _pb_msg(4, _pb_msg(2, _pb_msg(1, dir_msg)))
        tf = {
            "name": "glob_file_search",
            "status": "completed",
            "params": {"globPattern": "**/*.py", "targetDirectory": "/ws/lib"},
            "result": json.dumps({"directories": [{"absPath": "/ws/lib"}]}),  # hollow JSON
            "toolCallBinary": base64.b64encode(blob).decode(),
        }
        out = cursor._normalize_tool(None, tf)
        self.assertEqual(out["name"], "Glob")
        self.assertIn("/ws/lib/a/one.py", out["result"]["text"])
        self.assertIn("/ws/lib/b/two.py", out["result"]["text"])

    def test_generic_binary_fallback_recovers_result_strings(self):
        import base64
        # Unknown tool, no JSON result: the envelope's result section (f2)
        # should be dumped as readable strings; the request echo (f1) skipped.
        request = _pb_msg(1, _pb_str(1, "request-echo-should-not-appear"))
        result = _pb_msg(2, _pb_msg(4, _pb_str(3, "/path/to/terminal/output") + _pb_int(2, 9)))
        blob = _pb_msg(42, request + result) + _pb_str(57, "call_abc\nfc_def")
        tf = {
            "name": "await",
            "status": "completed",
            "params": {"shell_id": 7},
            "toolCallBinary": base64.b64encode(blob).decode(),
        }
        out = cursor._normalize_tool(None, tf)
        self.assertIn("/path/to/terminal/output", out["result"]["text"])
        self.assertNotIn("request-echo", out["result"]["text"])
        self.assertNotIn("call_abc", out["result"]["text"])

    def test_generic_fallback_skipped_for_cancelled_calls(self):
        import base64
        blob = _pb_msg(42, _pb_msg(2, _pb_str(1, "leftover partial output")))
        tf = {
            "name": "await",
            "status": "cancelled",
            "params": {},
            "toolCallBinary": base64.b64encode(blob).decode(),
        }
        out = cursor._normalize_tool(None, tf)
        self.assertEqual(out["result"]["text"], "")

    def test_generic_fallback_handles_garbage_binary(self):
        import base64
        tf = {
            "name": "mystery_tool",
            "status": "completed",
            "params": {"x": 1},
            "toolCallBinary": base64.b64encode(b"\x00\xff\x07 not a protobuf").decode(),
        }
        out = cursor._normalize_tool(None, tf)  # must not raise
        self.assertEqual(out["result"]["text"], "")

    def test_glob_json_result_still_preferred(self):
        tf = {
            "name": "glob_file_search",
            "status": "completed",
            "params": {"globPattern": "*.md", "targetDirectory": "/d"},
            "result": json.dumps({"directories": [{"absPath": "/d", "files": [{"relPath": "README.md"}]}]}),
        }
        out = cursor._normalize_tool(None, tf)
        self.assertEqual(out["result"]["text"], "/d/README.md")


class CursorCliTextTests(unittest.TestCase):
    def test_user_query_wrapper_is_unwrapped(self):
        raw = "<timestamp>2026</timestamp><user_query>  do the thing  </user_query>"
        self.assertEqual(cursor._clean_cli_user_text(raw), "do the thing")

    def test_other_wrappers_are_stripped(self):
        self.assertEqual(cursor._clean_cli_user_text("<mode>plan</mode> hello"), "plan hello")

    def test_system_reminder_removed_but_task_kept(self):
        raw = "<system_reminder>be careful</system_reminder><user_query>task</user_query>"
        self.assertEqual(cursor._store_prompt_text(raw), "task")

    def test_subagent_title_prefers_task_section(self):
        raw = "You are a subagent runner.\n## Task\nFind the bug in parser.py"
        self.assertEqual(cursor._store_subagent_title(raw), "Find the bug in parser.py")

    def test_decode_project_dir_fallbacks(self):
        self.assertEqual(cursor.decode_project_dir(""), "")
        self.assertEqual(cursor.decode_project_dir("12345"), "")
        self.assertEqual(cursor.decode_project_dir("var-folders-xy"), "")
        self.assertEqual(cursor.decode_project_dir("tmp-foo"), "")
        # Non-resolvable paths degrade to a readable slash path.
        out = cursor.decode_project_dir("Users-nosuchuser-proj")
        self.assertEqual(out, "/Users/nosuchuser/proj")


class CursorIdePerTurnModelTests(unittest.TestCase):
    """IDE sessions store the selected model on each user bubble as modelInfo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.vscdb"
        self.cid = "test-composer-multi-model"
        self._old = (cursor.DB_PATH, cursor.PROJECTS_DIR, cursor.CHATS_DIR)
        self._write_fixture()
        cursor.configure(self.db_path, projects_dir=Path(self.tmp.name) / "projects",
                         chats_dir=Path(self.tmp.name) / "chats")

    def tearDown(self):
        cursor.configure(self._old[0], projects_dir=self._old[1], chats_dir=self._old[2])
        self.tmp.cleanup()

    def _write_fixture(self):
        import sqlite3

        headers = [
            {"bubbleId": "u1", "type": 1},
            {"bubbleId": "a1", "type": 2},
            {"bubbleId": "u2", "type": 1},
            {"bubbleId": "a2", "type": 2},
            {"bubbleId": "u3", "type": 1},
            {"bubbleId": "a3", "type": 2},
        ]
        composer = {
            "name": "multi-model chat",
            "modelConfig": {"modelName": "claude-opus-4-8"},
            "fullConversationHeadersOnly": headers,
            "createdAt": 1_700_000_000_000,
            "lastUpdatedAt": 1_700_000_100_000,
        }
        bubbles = {
            "u1": {"type": 1, "text": "use grok", "modelInfo": {"modelName": "grok-4.5"}},
            "a1": {"type": 2, "text": "grok reply"},
            "u2": {"type": 1, "text": "switch to gpt", "modelInfo": {"modelName": "gpt-5.6-sol"}},
            "a2": {"type": 2, "text": "gpt reply"},
            "u3": {"type": 1, "text": "now opus", "modelInfo": {"modelName": "claude-opus-4-8"}},
            "a3": {"type": 2, "text": "opus reply"},
        }
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("create table cursorDiskKV (key text primary key, value text)")
            conn.execute(
                "insert into cursorDiskKV values (?, ?)",
                (f"composerData:{self.cid}", json.dumps(composer)),
            )
            for bid, bubble in bubbles.items():
                conn.execute(
                    "insert into cursorDiskKV values (?, ?)",
                    (f"bubbleId:{self.cid}:{bid}", json.dumps(bubble)),
                )
            conn.commit()
        finally:
            conn.close()

    def test_assistant_events_use_preceding_user_modelInfo(self):
        data = cursor.parse_session_by_id(self.cid)
        self.assertIsNotNone(data)
        asst = [e for e in data["events"] if e["kind"] == "assistant"]
        self.assertEqual([e["model"] for e in asst], ["grok-4.5", "gpt-5.6-sol", "claude-opus-4-8"])
        self.assertEqual(data["meta"]["model"], "claude-opus-4-8")
        self.assertEqual(data["meta"]["models"], ["grok-4.5", "gpt-5.6-sol", "claude-opus-4-8"])

    def test_falls_back_to_session_model_without_modelInfo(self):
        import sqlite3

        # Clear modelInfo on all user bubbles — assistants should use session config.
        conn = sqlite3.connect(self.db_path)
        try:
            for bid in ("u1", "u2", "u3"):
                key = f"bubbleId:{self.cid}:{bid}"
                row = conn.execute("select value from cursorDiskKV where key=?", (key,)).fetchone()
                b = json.loads(row[0])
                b.pop("modelInfo", None)
                conn.execute("update cursorDiskKV set value=? where key=?", (json.dumps(b), key))
            conn.commit()
        finally:
            conn.close()
        data = cursor.parse_session_by_id(self.cid)
        asst = [e for e in data["events"] if e["kind"] == "assistant"]
        self.assertEqual({e["model"] for e in asst}, {"claude-opus-4-8"})


class ShortTitleTests(unittest.TestCase):
    def test_collapses_whitespace_and_truncates(self):
        text = "  a   very\n\nspaced   " + "x" * 200
        out = common.short_title(text)
        self.assertTrue(out.startswith("a very spaced"))
        self.assertTrue(out.endswith("…"))
        self.assertEqual(len(out), 101)

    def test_short_text_untouched(self):
        self.assertEqual(common.short_title("hello"), "hello")


if __name__ == "__main__":
    unittest.main()
