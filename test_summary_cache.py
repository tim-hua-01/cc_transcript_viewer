"""Focused tests for persistent transcript summary caching."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import claude_parser as claude
import codex_parser as codex
import common
import server


class SummaryCacheTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.old_cache_file = server.CACHE_FILE
        server.CACHE_FILE = self.root / "cache" / "summaries.json"
        server._CACHE_LOADED = False
        for cache in server._PARSER_CACHES.values():
            cache.clear()

    def tearDown(self):
        server.CACHE_FILE = self.old_cache_file
        server._CACHE_LOADED = False
        for cache in server._PARSER_CACHES.values():
            cache.clear()
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
        expected = claude.session_summary(path)
        server.save_summary_caches()

        claude.SUMMARY_CACHE.clear()
        server._CACHE_LOADED = False
        server.load_summary_caches()

        # The loaded entry must be served straight from the cache (no re-parse).
        identity = common.file_identity(path)
        self.assertEqual(claude.SUMMARY_CACHE.get(str(path), identity), expected)
        self.assertEqual(claude.session_summary(path), expected)
        payload = json.loads(server.CACHE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], server._CACHE_VERSION)

    def test_file_size_invalidates_same_mtime_cache_entry(self):
        path = self._claude_session()
        first = claude.session_summary(path)
        original_mtime = path.stat().st_mtime_ns
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
            }) + "\n")
        path.touch()
        os.utime(path, ns=(original_mtime, original_mtime))

        second = claude.session_summary(path)
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

    def test_dirty_flag_clears_only_when_no_concurrent_update(self):
        cache = common.SummaryCache()
        cache.put("a", (1, 2), {"x": 1})
        self.assertTrue(cache.dirty)
        generation, data = cache.snapshot()
        self.assertEqual(data, {"a": [[1, 2], {"x": 1}]})

        # An update racing the save keeps the cache dirty.
        cache.put("b", (3, 4), {"y": 2})
        cache.mark_saved(generation)
        self.assertTrue(cache.dirty)

        generation, _ = cache.snapshot()
        cache.mark_saved(generation)
        self.assertFalse(cache.dirty)

    def test_load_rejects_malformed_entries(self):
        cache = common.SummaryCache()
        cache.load({
            "good": [[1, 2], {"t": "ok"}],
            "bad-shape": [1, 2, {"t": "no"}],
            "bad-types": ["fp", "summary"],
        })
        self.assertEqual(cache.get("good", (1, 2)), {"t": "ok"})
        self.assertIsNone(cache.get("good", (1, 3)))  # fingerprint mismatch
        self.assertIsNone(cache.get("bad-shape", (1, 2)))
        self.assertIsNone(cache.get("bad-types", (1, 2)))


    def test_save_merges_with_entries_from_other_processes(self):
        """Several viewer processes share the cache file; one process's save
        must not discard entries another process computed."""
        claude.SUMMARY_CACHE.put("/theirs/session.jsonl", [1, 2], {"title": "theirs"})
        server.save_summary_caches()

        claude.SUMMARY_CACHE.clear()
        claude.SUMMARY_CACHE.put("/ours/session.jsonl", [3, 4], {"title": "ours"})
        server.save_summary_caches()

        payload = json.loads(server.CACHE_FILE.read_text(encoding="utf-8"))
        self.assertIn("/theirs/session.jsonl", payload["claude"])
        self.assertIn("/ours/session.jsonl", payload["claude"])


if __name__ == "__main__":
    unittest.main()
