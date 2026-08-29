#!/usr/bin/env python3
"""Characterization tests for claude_parser internals: branch folding,
synthetic-user notices, skill injections, and queued prompts with images."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import claude_parser as claude
from test_fixtures import _assistant, _user, _write_jsonl


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

if __name__ == "__main__":
    unittest.main()
