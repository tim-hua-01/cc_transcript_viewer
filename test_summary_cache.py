"""Focused tests for persistent transcript summary caching."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import codex_server as codex
import server


class SummaryCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.old_cache_file = server.CACHE_FILE
        server.CACHE_FILE = self.root / "cache" / "summaries.json"
        server._SUMMARY_CACHE.clear()
        server._CACHE_LOADED = False
        server._SUMMARY_CACHE_DIRTY = False
        server._SUMMARY_CACHE_GENERATION = 0
        codex._SUMMARY_CACHE.clear()
        codex._SUMMARY_CACHE_DIRTY = False
        codex._SUMMARY_CACHE_GENERATION = 0

    def tearDown(self):
        server.CACHE_FILE = self.old_cache_file
        server._SUMMARY_CACHE.clear()
        server._CACHE_LOADED = False
        server._SUMMARY_CACHE_DIRTY = False
        server._SUMMARY_CACHE_GENERATION = 0
        codex._SUMMARY_CACHE.clear()
        codex._SUMMARY_CACHE_DIRTY = False
        codex._SUMMARY_CACHE_GENERATION = 0
        self.tempdir.cleanup()

    def _claude_session(self) -> Path:
        path = self.root / "session.jsonl"
        path.write_text(json.dumps({
            "type": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {"role": "user", "content": "cached title"},
        }) + "\n", encoding="utf-8")
        return path

    def test_cache_round_trip(self):
        path = self._claude_session()
        expected = server.cc_session_summary(path)
        server.save_summary_caches()

        server._SUMMARY_CACHE.clear()
        server._CACHE_LOADED = False
        server.load_summary_caches()

        self.assertEqual(server.cc_session_summary(path), expected)
        payload = json.loads(server.CACHE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], server._CACHE_VERSION)

    def test_file_size_invalidates_same_mtime_cache_entry(self):
        path = self._claude_session()
        first = server.cc_session_summary(path)
        original_mtime = path.stat().st_mtime_ns
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
            }) + "\n")
        path.touch()
        os.utime(path, ns=(original_mtime, original_mtime))

        second = server.cc_session_summary(path)
        self.assertEqual(first["n_assistant"], 0)
        self.assertEqual(second["n_assistant"], 1)

    def test_codex_row_metadata_invalidates_cache(self):
        path = self.root / "rollout-test.jsonl"
        path.write_text(json.dumps({
            "type": "session_meta",
            "timestamp": "2026-01-01T00:00:00Z",
            "payload": {"id": "test-id"},
        }) + "\n", encoding="utf-8")

        first = codex.session_summary(path, {"title": "first"})
        second = codex.session_summary(path, {"title": "second"})
        self.assertEqual(first["title"], "first")
        self.assertEqual(second["title"], "second")


if __name__ == "__main__":
    unittest.main()
