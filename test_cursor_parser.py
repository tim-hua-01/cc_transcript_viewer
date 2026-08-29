#!/usr/bin/env python3
"""Characterization tests for cursor_parser internals: blob/JSON extraction,
protobuf tool-result decoding, CLI text cleanup, per-turn models, synthetic
notices, and orphaned-bubble recovery."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import cursor_parser as cursor
from test_fixtures import _write_cli_store


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


class CursorSyntheticNoticeTests(unittest.TestCase):
    """Cursor injects finished background-task/subagent results as a user
    bubble; the viewer must show them as notices, not real prompts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.vscdb"
        self.cid = "test-composer-notices"
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
            {"bubbleId": "n1", "type": 1},   # timestamp-prefixed shell notification
            {"bubbleId": "n2", "type": 1},   # bare system_notification, subagent
            {"bubbleId": "u2", "type": 1},
        ]
        composer = {
            "name": "notices",
            "modelConfig": {"modelName": "gpt-5.6-sol"},
            "fullConversationHeadersOnly": headers,
        }
        bubbles = {
            "u1": {"type": 1, "text": "run the tests in the background"},
            "n1": {"type": 1, "text": (
                "<timestamp>Thursday, Aug 27, 2026, 11:02 PM (UTC-7)</timestamp>\n"
                "<system_notification>\nThe following task has finished.\n"
                "kind: shell\nstatus: success\n</system_notification>"
            )},
            "n2": {"type": 1, "text": (
                "<system_notification>\nThe following task has finished.\n"
                "kind: subagent\nstatus: success\n</system_notification>"
            )},
            "u2": {"type": 1, "text": "great, now clean it up"},
        }
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("create table cursorDiskKV (key text primary key, value text)")
            conn.execute("insert into cursorDiskKV values (?, ?)",
                         (f"composerData:{self.cid}", json.dumps(composer)))
            for bid, bubble in bubbles.items():
                conn.execute("insert into cursorDiskKV values (?, ?)",
                             (f"bubbleId:{self.cid}:{bid}", json.dumps(bubble)))
            conn.commit()
        finally:
            conn.close()

    def test_notification_bubbles_become_notices(self):
        events = cursor.parse_session_by_id(self.cid)["events"]
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["user", "notice", "notice", "user"])
        notices = [e for e in events if e["kind"] == "notice"]
        self.assertTrue(all(n["label"] == "Background task" for n in notices))
        self.assertIn("kind: shell", notices[0]["text"])
        self.assertIn("kind: subagent", notices[1]["text"])

    def test_real_prompts_are_unaffected(self):
        events = cursor.parse_session_by_id(self.cid)["events"]
        prompts = [e["blocks"][0]["text"] for e in events if e["kind"] == "user"]
        self.assertEqual(prompts, ["run the tests in the background", "great, now clean it up"])

    def test_title_falls_back_past_notices_to_the_real_first_prompt(self):
        # A composer whose only header is a background-task notification should
        # not surface it as the session title.
        import sqlite3

        headers = [{"bubbleId": "n1", "type": 1}, {"bubbleId": "u1", "type": 1}]
        composer = {"fullConversationHeadersOnly": headers}
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("insert into cursorDiskKV values (?, ?)",
                         (f"composerData:notice-only", json.dumps(composer)))
            conn.execute("insert into cursorDiskKV values (?, ?)",
                         ("bubbleId:notice-only:n1", json.dumps({
                             "type": 1,
                             "text": "<system_notification>\ndone\n</system_notification>",
                         })))
            conn.execute("insert into cursorDiskKV values (?, ?)",
                         ("bubbleId:notice-only:u1", json.dumps({"type": 1, "text": "the real prompt"})))
            conn.commit()
        finally:
            conn.close()
        data = cursor.parse_session_by_id("notice-only")
        self.assertEqual(data["title"], "the real prompt")

    def test_summary_n_user_excludes_notices(self):
        summaries = {s["id"]: s for s in cursor._list_db_sessions()}
        self.assertEqual(summaries[self.cid]["n_user"], 2)


class CursorOrphanRecoveryTests(unittest.TestCase):
    """Messages Cursor's checkpoint rebuilds dropped come back, badged.

    Models the real failure (composer 45f12c5f, Aug 2026): a rebuild re-created
    every bubble with a uniform fake createdAt and silently dropped an
    assistant text that never registered server-side. The original bubble
    survives only as a row no header references; the parser must restore it —
    placed after the tool call that finished before it streamed and before the
    next user message — while suppressing orphan rows that merely duplicate or
    fragment kept content.
    """

    FAKE_TS = "2026-08-28T17:10:03.368Z"  # rebuild stamp shared by all headers

    @staticmethod
    def _ms(iso: str) -> int:
        from datetime import datetime
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)

    @staticmethod
    def _tool_binary(start_ms: int, end_ms: int) -> str:
        import base64

        def varint(n):
            out = bytearray()
            while True:
                b = n & 0x7F
                n >>= 7
                out.append(b | (0x80 if n else 0))
                if not n:
                    return bytes(out)

        return base64.b64encode(
            varint(59 << 3) + varint(start_ms) + varint(60 << 3) + varint(end_ms)
        ).decode()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.vscdb"
        self.cid = "test-composer-orphans"
        self._old = (cursor.DB_PATH, cursor.PROJECTS_DIR, cursor.CHATS_DIR)
        cursor.configure(self.db_path, projects_dir=Path(self.tmp.name) / "projects",
                         chats_dir=Path(self.tmp.name) / "chats")

    def tearDown(self):
        cursor.configure(self._old[0], projects_dir=self._old[1], chats_dir=self._old[2])
        self.tmp.cleanup()

    def _write(self, headers, bubbles):
        import sqlite3

        composer = {
            "name": "orphan recovery",
            "modelConfig": {"modelName": "gpt-5.6-sol"},
            "fullConversationHeadersOnly": headers,
        }
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("create table cursorDiskKV (key text primary key, value text)")
            conn.execute("insert into cursorDiskKV values (?, ?)",
                         (f"composerData:{self.cid}", json.dumps(composer)))
            for bid, bubble in bubbles.items():
                conn.execute("insert into cursorDiskKV values (?, ?)",
                             (f"bubbleId:{self.cid}:{bid}", json.dumps(bubble)))
            conn.commit()
        finally:
            conn.close()

    def _rebuilt_session(self):
        """u1 → tool (real times in binary) → LOST TEXT → u2 → a2+a3 texts."""
        tool_end = self._ms("2026-08-28T16:00:36.000Z")
        headers = [
            {"bubbleId": "u1", "type": 1, "createdAt": self.FAKE_TS},
            {"bubbleId": "t1", "type": 2, "createdAt": self.FAKE_TS},
            {"bubbleId": "u2", "type": 1, "createdAt": self.FAKE_TS},
            {"bubbleId": "a2", "type": 2, "createdAt": self.FAKE_TS},
            {"bubbleId": "a3", "type": 2, "createdAt": self.FAKE_TS},
        ]
        bubbles = {
            "u1": {"type": 1, "text": "explain things to me", "createdAt": self.FAKE_TS},
            "t1": {"type": 2, "createdAt": self.FAKE_TS,
                   "toolFormerData": {"name": "ripgrep_raw_search", "status": "completed",
                                      "params": "{}", "result": "{}",
                                      "toolCallBinary": self._tool_binary(tool_end - 400, tool_end)}},
            "u2": {"type": 1, "text": "yeah ok for 1", "createdAt": self.FAKE_TS},
            "a2": {"type": 2, "text": "Here is the clean interpretation.", "createdAt": self.FAKE_TS},
            "a3": {"type": 2, "text": "And the follow-up detail.", "createdAt": self.FAKE_TS},
        }
        orphans = {
            # the genuinely lost message: streamed 19s after the tool finished
            "lost": {"type": 2, "text": "Here is the clean version, all sixteen points.",
                     "createdAt": "2026-08-28T16:00:55.873Z"},
            # a later rebuild generation's copy of that same lost message
            "lost-copy": {"type": 2, "text": "Here is the clean version, all sixteen points.",
                          "createdAt": "2026-08-28T16:48:05.000Z"},
            # stream debris: a partial snapshot of a kept message
            "frag": {"type": 2, "text": "Here is the clean",
                     "createdAt": "2026-08-28T16:27:30.000Z"},
            # a superseded generation that stored two kept bubbles as one
            "split": {"type": 2, "text": "Here is the clean interpretation.And the follow-up detail.",
                      "createdAt": "2026-08-28T16:27:31.000Z"},
            # an exact copy of a kept message from an older generation
            "dup": {"type": 2, "text": "And the follow-up detail.",
                    "createdAt": "2026-08-28T16:27:32.000Z"},
        }
        return headers, {**bubbles, **orphans}

    def test_lost_message_is_restored_before_the_next_user_turn(self):
        self._write(*self._rebuilt_session())
        events = cursor.parse_session_by_id(self.cid)["events"]
        recovered = [e for e in events if e.get("recovered")]
        self.assertEqual(len(recovered), 1)
        (ev,) = recovered
        self.assertEqual(ev["blocks"], [{"type": "text", "text": "Here is the clean version, all sixteen points."}])
        # the earliest copy is the original stream bubble: its timestamp is genuine
        self.assertEqual(ev["ts"], "2026-08-28T16:00:55.873Z")
        self.assertEqual(ev["model"], "gpt-5.6-sol")
        # placed after the anchoring tool call, before the user's reply to it
        idx = events.index(ev)
        self.assertEqual(events[idx - 1]["blocks"][0]["type"], "tool_use")
        self.assertEqual(events[idx + 1]["blocks"], [{"type": "text", "text": "yeah ok for 1"}])

    def test_duplicates_fragments_and_resplits_stay_suppressed(self):
        self._write(*self._rebuilt_session())
        events = cursor.parse_session_by_id(self.cid)["events"]
        texts = [b["text"] for e in events for b in e["blocks"] if b["type"] == "text"]
        # each kept message appears exactly once; no orphan debris leaks in
        self.assertEqual(texts.count("Here is the clean interpretation."), 1)
        self.assertEqual(texts.count("And the follow-up detail."), 1)
        self.assertNotIn("Here is the clean", texts)
        self.assertNotIn("Here is the clean interpretation.And the follow-up detail.", texts)

    def test_without_anchors_recovered_text_lands_at_the_end(self):
        headers = [
            {"bubbleId": "u1", "type": 1, "createdAt": self.FAKE_TS},
            {"bubbleId": "a1", "type": 2, "createdAt": self.FAKE_TS},
        ]
        bubbles = {
            "u1": {"type": 1, "text": "hello", "createdAt": self.FAKE_TS},
            "a1": {"type": 2, "text": "kept reply", "createdAt": self.FAKE_TS},
            "lost": {"type": 2, "text": "dropped reply", "createdAt": "2026-08-28T16:00:00.000Z"},
        }
        self._write(headers, bubbles)
        events = cursor.parse_session_by_id(self.cid)["events"]
        self.assertTrue(events[-1].get("recovered"))
        self.assertEqual(events[-1]["blocks"][0]["text"], "dropped reply")

    def test_untouched_sessions_recover_nothing(self):
        headers = [
            {"bubbleId": "u1", "type": 1, "createdAt": "2026-08-28T10:00:00.000Z"},
            {"bubbleId": "a1", "type": 2, "createdAt": "2026-08-28T10:00:05.000Z"},
        ]
        bubbles = {
            "u1": {"type": 1, "text": "hello", "createdAt": "2026-08-28T10:00:00.000Z"},
            "a1": {"type": 2, "text": "the reply", "createdAt": "2026-08-28T10:00:05.000Z"},
        }
        self._write(headers, bubbles)
        events = cursor.parse_session_by_id(self.cid)["events"]
        self.assertFalse(any(e.get("recovered") for e in events))
        self.assertEqual(len(events), 2)

class StoreSummaryCacheTests(unittest.TestCase):
    """The two-level store.db fingerprint: an mtime-only touch (another
    process, a backup tool, a cache-format migration) must revalidate through
    the cheap content fingerprint, never the ~1.5s-per-store blob scan."""

    def test_touched_store_reuses_summary_without_a_blob_scan(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = _write_cli_store(tmp / "chats")
            cursor.configure(tmp / "state.vscdb", projects_dir=tmp / "projects",
                             chats_dir=tmp / "chats")
            try:
                first = cursor.cli_store_summary(store)
                self.assertIsNotNone(first)

                later = time.time() + 60
                os.utime(store, (later, later))

                def refuse_scan(_conn):
                    raise AssertionError("blob scan ran for an unchanged store")

                original = cursor._iter_store_role_messages
                cursor._iter_store_role_messages = refuse_scan
                try:
                    again = cursor.cli_store_summary(store)
                finally:
                    cursor._iter_store_role_messages = original
                self.assertEqual(again, first)
            finally:
                cursor.configure(None)


if __name__ == "__main__":
    unittest.main()
