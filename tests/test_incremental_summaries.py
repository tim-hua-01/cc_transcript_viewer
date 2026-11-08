"""Sidebar summaries read only appended lines, and still match a full read."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import claude_parser as claude
import codex_parser as codex
import common

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
CLAUDE_FIXTURE = next((FIXTURES / "claude").glob("*/*.jsonl"))
CODEX_FIXTURE = next((FIXTURES / "codex").glob("*.jsonl"))


class Recorder:
    def __init__(self):
        self.records = []

    def feed(self, record):
        self.records.append(record)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class JsonlAccumulatorTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "t.jsonl"
        self.acc = common.JsonlAccumulators(Recorder)

    def seen(self):
        return self.acc.summarize(self.path, lambda r: [rec["n"] for rec in r.records])

    def append(self, text):
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(text)

    def test_only_appended_lines_are_fed(self):
        self.path.write_text('{"n": 1}\n{"n": 2}\n')
        self.assertEqual(self.seen(), [1, 2])
        self.append('{"n": 3}\n')
        self.assertEqual(self.seen(), [1, 2, 3])  # 1 and 2 were not fed again

    def test_a_half_written_line_waits_for_the_rest(self):
        self.path.write_text('{"n": 1}\n{"n": 2')
        self.assertEqual(self.seen(), [1])
        self.append('}\n')
        self.assertEqual(self.seen(), [1, 2])

    def test_a_truncated_or_rewritten_file_is_read_from_the_top(self):
        self.path.write_text('{"n": 1}\n{"n": 2}\n')
        self.seen()
        self.path.write_text('{"n": 9}\n')
        self.assertEqual(self.seen(), [9])
        self.path.write_text('{"n": 8}\n{"n": 5}\n')  # longer, but its start changed
        self.assertEqual(self.seen(), [8, 5])

    def test_skipped_and_corrupt_lines_are_dropped(self):
        acc = common.JsonlAccumulators(Recorder, skip_line=lambda line: '"skip"' in line)
        self.path.write_bytes(b'{"n": 1}\n{"n": "skip"}\nnot json\n\xff\xfe\n\n{"n": 2}\n')
        self.assertEqual(acc.summarize(self.path, lambda r: [x["n"] for x in r.records]), [1, 2])

    def test_a_failed_feed_does_not_feed_the_same_lines_twice(self):
        calls = []

        class Flaky(Recorder):
            def feed(self, record):
                calls.append(record["n"])
                if len(calls) == 2:
                    raise ValueError("boom")
                super().feed(record)

        acc = common.JsonlAccumulators(Flaky)
        self.path.write_text('{"n": 1}\n{"n": 2}\n')
        with self.assertRaises(ValueError):
            acc.summarize(self.path, lambda r: None)
        self.assertEqual(acc.summarize(self.path, lambda r: [x["n"] for x in r.records]), [1, 2])


class IncrementalSummaryTests(TempDirTestCase):
    """A summary updated after each append equals one from reading the whole
    file, at every step, including steps that end mid-line."""

    def grow(self, source: Path, dest: Path, incremental, full, state_class):
        data = source.read_bytes()
        line_ends = [i + 1 for i, b in enumerate(data) if b == ord("\n")]
        cuts = sorted(set(line_ends[::2] + [end - 5 for end in line_ends[1::3]] + [len(data)]))
        dest.parent.mkdir(parents=True, exist_ok=True)
        feeds = []
        original = state_class.feed
        with mock.patch.object(state_class, "feed", autospec=True,
                               side_effect=lambda self, rec: (feeds.append(1), original(self, rec))):
            for end in cuts:
                dest.write_bytes(data[:end])
                updated = incremental(dest)
                feeds_so_far = len(feeds)
                self.assertEqual(updated, full(dest), f"after {end} of {len(data)} bytes")
                del feeds[feeds_so_far:]  # the full read's feeds don't count
        # Every line was fed once by the incremental reads, never re-read.
        self.assertEqual(len(feeds), len(line_ends))

    def test_claude(self):
        dest = self.tmp / CLAUDE_FIXTURE.parent.name / CLAUDE_FIXTURE.name
        self.grow(
            CLAUDE_FIXTURE, dest,
            claude._session_summary_uncached,
            lambda path: common.JsonlAccumulators(claude._SummaryState, skip_line=claude._skip_line)
            .summarize(path, lambda state: claude._finish_summary(path, state)),
            claude._SummaryState,
        )

    def test_codex(self):
        dest = self.tmp / CODEX_FIXTURE.name
        self.grow(
            CODEX_FIXTURE, dest,
            codex._session_summary_uncached,
            lambda path: common.JsonlAccumulators(codex._SummaryState)
            .summarize(path, lambda state: codex._finish_summary(path, state, None)),
            codex._SummaryState,
        )

    def test_codex_counts_a_message_once_when_its_copies_arrive_apart(self):
        ts = "2026-08-29T10:00:00.000Z"
        envelope = {"timestamp": ts, "type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "UserMessage", "id": "i1",
            "content": [{"type": "text", "text": "hello", "text_elements": []}]}}}
        mirror = {"timestamp": ts, "type": "event_msg",
                  "payload": {"type": "user_message", "message": "hello"}}
        path = self.tmp / "rollout-2026-08-29T10-00-00-0197a2e5-b222-7ab0-8888-000000000147.jsonl"
        path.write_text(json.dumps(envelope) + "\n")
        self.assertEqual(codex._session_summary_uncached(path)["n_user"], 1)
        with path.open("a") as fh:
            fh.write(json.dumps(mirror) + "\n")
        self.assertEqual(codex._session_summary_uncached(path)["n_user"], 1)


class ColdRebuildTests(TempDirTestCase):
    def test_a_large_stale_codex_batch_is_summarized_in_worker_processes(self):
        sessions = self.tmp / "sessions" / "2026" / "01" / "02"
        sessions.mkdir(parents=True)
        copies = []
        for i in range(3):
            name = CODEX_FIXTURE.name.replace("22222222-2222", f"2222222{i}-2222")
            copies.append(sessions / name)
            shutil.copy(CODEX_FIXTURE, copies[-1])
        old_home = codex.CODEX_HOME
        codex.configure(self.tmp)
        try:
            with mock.patch.object(codex, "_PARALLEL_SCAN_THRESHOLD", 2):
                rows = codex.list_sessions()
            self.assertEqual(len(rows), 3)
            # The server process never read them: workers filled the cache.
            self.assertFalse(any(str(p.resolve()) in codex._SUMMARIES._files for p in copies))
        finally:
            codex.configure(old_home)


if __name__ == "__main__":
    unittest.main()
