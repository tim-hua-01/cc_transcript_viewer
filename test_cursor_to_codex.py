#!/usr/bin/env python3
"""Tests for the Cursor → Codex rollout exporter.

These pin the parts that are easy to get subtly wrong: the agentKv blob chain
hidden in ``conversationState``, and the OpenAI reasoning item Cursor buries in
a JSON-encoded ``signature`` (the whole point of the export is that the
``rs_…`` id and ``encrypted_content`` survive intact).
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import codex_parser as codex
import cursor_binary
import cursor_to_codex as c2c

ENCRYPTED = "gAAAAABtEST_encrypted_reasoning_blob"
REASONING_ITEM = {
    "id": "rs_abc123",
    "type": "reasoning",
    "content": [],
    "encrypted_content": ENCRYPTED,
}
TOOL_CALL_ID = "call_tool1\nfc_item1"


def _conversation_state(digests: list[str]) -> str:
    """Cursor's ``~<base64>`` protobuf: repeated field 1, one sha256 each."""
    raw = b"".join(b"\x0a\x20" + bytes.fromhex(d) for d in digests)
    return "~" + base64.b64encode(raw).decode("ascii")


def _write_store(path: Path, messages: list[dict], *, composer: dict) -> str:
    conn = sqlite3.connect(path)
    conn.execute("create table cursorDiskKV (key text primary key, value blob)")
    digests = []
    for message in messages:
        blob = json.dumps(message)
        digest = hashlib.sha256(blob.encode()).hexdigest()
        digests.append(digest)
        conn.execute(
            "insert or replace into cursorDiskKV values (?, ?)", (f"agentKv:blob:{digest}", blob)
        )
    composer = dict(composer)
    composer["conversationState"] = _conversation_state(digests)
    composer_id = composer["composerId"]
    conn.execute(
        "insert into cursorDiskKV values (?, ?)",
        (f"composerData:{composer_id}", json.dumps(composer)),
    )
    conn.commit()
    conn.close()
    return composer_id


def _demo_messages() -> list[dict]:
    return [
        {"role": "system", "content": "You are GPT-5.5."},
        {"role": "user", "content": [{"type": "text", "text": "<user_query>\nfix the bug\n</user_query>"}]},
        {
            "role": "assistant",
            "id": "msg_1",
            "providerOptions": {"cursor": {"modelName": "gpt-5.5-high"}},
            "content": [
                {
                    "type": "reasoning",
                    "text": "**Thinking**\n\nread the file first",
                    "signature": json.dumps(REASONING_ITEM),
                },
                {
                    "type": "tool-call",
                    "toolCallId": TOOL_CALL_ID,
                    "toolName": "ReadFile",
                    "args": {"path": "/tmp/a.py"},
                },
            ],
        },
        {
            "role": "tool",
            "content": [
                {
                    "type": "tool-result",
                    "toolCallId": TOOL_CALL_ID,
                    "toolName": "ReadFile",
                    "result": "print(1)",
                    "experimental_content": [{"type": "text", "text": "print(1)"}],
                }
            ],
        },
        {
            "role": "assistant",
            "id": "msg_2",
            "content": [{"type": "text", "text": "Fixed it."}],
        },
    ]


DEMO_COMPOSER = {
    "composerId": "11111111-2222-3333-4444-555555555555",
    "name": "Demo session",
    "createdAt": 1_700_000_000_000,
    "modelConfig": {"modelName": "gpt-5.5"},
    "workspaceIdentifier": {"uri": {"fsPath": "/tmp/proj"}},
    "fullConversationHeadersOnly": [
        {"bubbleId": "b1", "type": 1, "createdAt": "2026-01-01T00:00:01.000Z"},
        {
            "bubbleId": "b2",
            "type": 2,
            "createdAt": "2026-01-01T00:00:09.000Z",
            "grouping": {"toolCallId": TOOL_CALL_ID},
        },
    ],
}


class ConversationStateTest(unittest.TestCase):
    def test_hashes_round_trip(self):
        digests = [hashlib.sha256(str(i).encode()).hexdigest() for i in range(3)]
        self.assertEqual(
            cursor_binary.conversation_state_hashes(_conversation_state(digests)), digests
        )

    def test_junk_is_not_fatal(self):
        for value in ("", "~", "not base64!!", None):
            self.assertEqual(cursor_binary.conversation_state_hashes(value), [])


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "state.vscdb"
        self.composer_id = _write_store(self.db, _demo_messages(), composer=DEMO_COMPOSER)
        self.conn = c2c.open_db(self.db)
        self.composer = dict(c2c.iter_composers(self.conn))[self.composer_id]

    def _lines(self, **kwargs):
        lines, report = c2c.export_session(
            self.conn, self.composer_id, self.composer, **kwargs
        )
        return lines, report

    def _payloads(self, lines, kind, payload_type=None):
        return [
            line["payload"]
            for line in lines
            if line["type"] == kind
            and (payload_type is None or line["payload"].get("type") == payload_type)
        ]

    def test_blob_chain_resolves(self):
        messages, missing = c2c.session_messages(self.conn, self.composer)
        self.assertEqual(missing, 0)
        self.assertEqual([m["role"] for m in messages],
                         ["system", "user", "assistant", "tool", "assistant"])

    def test_reasoning_keeps_summary_and_encrypted_content(self):
        lines, report = self._lines()
        reasoning = self._payloads(lines, "response_item", "reasoning")
        self.assertEqual(len(reasoning), 1)
        self.assertEqual(reasoning[0]["id"], "rs_abc123")
        self.assertEqual(reasoning[0]["encrypted_content"], ENCRYPTED)
        self.assertEqual(
            reasoning[0]["summary"],
            [{"type": "summary_text", "text": "**Thinking**\n\nread the file first"}],
        )
        self.assertEqual(report["encrypted"], 1)
        self.assertTrue(report["openai_reasoning"])

    def test_tool_call_ids_are_split(self):
        lines, _ = self._lines()
        call = self._payloads(lines, "response_item", "function_call")[0]
        self.assertEqual(call["call_id"], "call_tool1")
        self.assertEqual(call["id"], "fc_item1")
        self.assertEqual(call["name"], "ReadFile")
        self.assertEqual(json.loads(call["arguments"]), {"path": "/tmp/a.py"})

        output = self._payloads(lines, "response_item", "function_call_output")[0]
        self.assertEqual(output["call_id"], "call_tool1")
        self.assertEqual(output["output"], [{"type": "input_text", "text": "print(1)"}])

    def test_tool_output_string_mode(self):
        lines, _ = self._lines(tool_output="string")
        output = self._payloads(lines, "response_item", "function_call_output")[0]
        self.assertEqual(output["output"], "print(1)")

    def test_session_meta_carries_system_prompt_and_model(self):
        lines, _ = self._lines()
        meta = lines[0]
        self.assertEqual(meta["type"], "session_meta")
        self.assertEqual(meta["payload"]["base_instructions"], {"text": "You are GPT-5.5."})
        self.assertEqual(meta["payload"]["model"], "gpt-5.5")
        self.assertEqual(meta["payload"]["cwd"], "/tmp/proj")
        self.assertEqual(meta["payload"]["session_id"], self.composer_id)
        # The system prompt is metadata, not a replayable item.
        self.assertFalse(
            [p for p in self._payloads(lines, "response_item", "message") if p["role"] == "system"]
        )

    def test_turn_context_uses_the_replying_model(self):
        lines, _ = self._lines()
        contexts = self._payloads(lines, "turn_context")
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["model"], "gpt-5.5-high")

    def test_event_msg_mirrors_only_the_typed_prompt(self):
        lines, _ = self._lines()
        users = self._payloads(lines, "event_msg", "user_message")
        self.assertEqual([u["message"] for u in users], ["fix the bug"])
        agents = self._payloads(lines, "event_msg", "agent_message")
        self.assertEqual([a["message"] for a in agents], ["Fixed it."])
        # …and the full envelope is still there as the API-shaped item.
        user_items = [
            p for p in self._payloads(lines, "response_item", "message") if p["role"] == "user"
        ]
        self.assertIn("<user_query>", user_items[0]["content"][0]["text"])

    def test_no_event_msg_mode(self):
        lines, _ = self._lines(event_msgs=False)
        self.assertFalse(self._payloads(lines, "event_msg"))

    def test_timestamps_come_from_matching_bubbles(self):
        lines, _ = self._lines()
        call = next(
            line for line in lines
            if line["type"] == "response_item" and line["payload"].get("type") == "function_call"
        )
        self.assertEqual(call["timestamp"], "2026-01-01T00:00:09.000Z")
        prompt = next(
            line for line in lines
            if line["type"] == "event_msg" and line["payload"].get("type") == "user_message"
        )
        self.assertEqual(prompt["timestamp"], "2026-01-01T00:00:01.000Z")

    def test_output_is_a_readable_codex_session(self):
        lines, _ = self._lines()
        path = c2c.output_path(self.root / "out", self.composer_id, lines[0]["timestamp"], "codex")
        c2c.write_rollout(path, lines)
        self.assertTrue(path.name.startswith("rollout-"))
        parsed = codex.parse_session(path)
        kinds = [event["kind"] for event in parsed["events"]]
        self.assertEqual(parsed["title"], "fix the bug")
        self.assertEqual(kinds.count("reasoning"), 1)
        self.assertEqual(kinds.count("tool"), 1)
        self.assertEqual(kinds.count("user"), 1)
        self.assertEqual(kinds.count("assistant"), 1)

    def test_model_filter_selects_gpt_only(self):
        other = self.root / "other.vscdb"
        composer = dict(DEMO_COMPOSER)
        composer["composerId"] = "99999999-9999-9999-9999-999999999999"
        composer["modelConfig"] = {"modelName": "claude-opus-5"}
        _write_store(other, _demo_messages(), composer=composer)
        conn = c2c.open_db(other)
        self.assertEqual(
            c2c.select_sessions(conn, model_regex=c2c.DEFAULT_MODEL_REGEX, session_ids=None), []
        )
        self.assertEqual(len(c2c.select_sessions(conn, model_regex=None, session_ids=None)), 1)

    def test_compaction_is_reported(self):
        composer = dict(self.composer)
        composer["fullConversationHeadersOnly"] = [
            {"bubbleId": "b0", "type": 1, "createdAt": "2025-12-31T00:00:00.000Z"},
            *DEMO_COMPOSER["fullConversationHeadersOnly"],
        ]
        _, report = c2c.export_session(self.conn, self.composer_id, composer)
        self.assertEqual(report["dropped_turns"], 1)


if __name__ == "__main__":
    unittest.main()
