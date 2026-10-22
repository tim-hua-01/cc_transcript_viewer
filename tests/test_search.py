"""Search regressions: copied typography, edit bodies, snippets and ranking."""
from __future__ import annotations

import json
import unittest
from unittest import mock
from urllib.parse import urlencode

import server
import common
from tests.fixture_builders import ViewerServerTestCase, _write_fixture_session


class NormalizedSearchTests(unittest.TestCase):
    def search(self, query, sessions):
        summaries = [dict(s, agent="claude") for s in sessions]

        def segments(s, *, normalized=False):
            pair = s.get("user", ""), s.get("rest", "")
            return tuple(common.normalize_search_text(t) for t in pair) if normalized else pair

        with (
            mock.patch.object(server, "list_sessions", return_value=summaries),
            mock.patch.object(server, "session_segments", side_effect=segments),
        ):
            return server.search_sessions(query)

    def test_normalizes_both_query_and_content_in_all_ranked_segments(self):
        sessions = [
            {"file": "rest", "rest": "Ada's result"},
            {"file": "user", "user": "Ada’s result"},
            {"file": "native", "original_title": "Ａｄａ’s\nresult"},
            {"file": "custom", "custom_title": "Adaʼs\u00a0result"},
        ]
        for query in ("Ada's result", "Ａｄａ’s\tresult"):
            with self.subTest(query=query):
                hits = self.search(query, sessions)
                self.assertEqual([h["file"] for h in hits], ["custom", "native", "user", "rest"])
                self.assertEqual([h["score"] for h in hits], [10000, 5000, 50, 1])

    def test_counts_equivalent_occurrences_and_preserves_source_snippet(self):
        text = "She wrote “Ada’s example”—then repeated \"Ada's example\"."
        hit, = self.search('"ada\'s example"', [{"file": "example", "rest": text}])
        self.assertEqual(hit["score"], 2)
        self.assertIn("“Ada’s example”—", hit["snippet"])

    def test_snippet_survives_length_changes_before_and_inside_match(self):
        for prefix in ("x " * 200, "ﬃ" * 200, " \t\n" * 200, "\u200b" * 200):
            with self.subTest(prefix=prefix[:10]):
                original = "Ａｄａ’s\u00a0oﬃce"
                hit, = self.search("Ada's office", [{"file": "example", "rest": prefix + original + " end"}])
                self.assertIn("Ａｄａ’s oﬃce", hit["snippet"])
                self.assertIn("end", hit["snippet"])
                self.assertLess(len(hit["snippet"]), 150)

    def test_code_punctuation_is_not_discarded(self):
        hits = self.search("C++", [{"file": "c", "rest": "C"}, {"file": "cpp", "rest": "C++"}])
        self.assertEqual([h["file"] for h in hits], ["cpp"])

    def test_match_crosses_lines_and_blank_lines(self):
        text = " \n\u200b\nＡｄａ’s\n\n\t\noﬃce\n\nend"
        hit, = self.search("Ada's office", [{"file": "example", "rest": text}])
        self.assertIn("Ａｄａ’s oﬃce end", hit["snippet"])

    def test_empty_normalized_query_does_not_scan_sessions(self):
        with mock.patch.object(server, "list_sessions") as listing:
            self.assertEqual(server.search_sessions(" \u200b\ufeff\t"), [])
            listing.assert_not_called()


class ToolInputSearchTests(unittest.TestCase):
    def test_edit_and_patch_text_is_indexed_in_block_flat_and_nested_tools(self):
        inputs = [
            {"old_string": "old wording", "new_string": "new wording"},
            {"edits": [{"old_text": "old wording", "new_text": "new wording"}]},
            {"patch": "old wording\nnew wording"},
            {"patch_text": "old wording\nnew wording"},
            {"calls": [{"input": {"old_str": "old wording", "new_str": "new wording"}}]},
        ]
        for inp in inputs:
            for shape in ("flat", "block"):
                with self.subTest(inp=inp, shape=shape):
                    event = {"input": inp} if shape == "flat" else {
                        "blocks": [{"type": "tool_use", "name": "Edit", "input": inp}]}
                    text = "\n".join(server._event_text(event))
                    self.assertIn("old wording", text)
                    self.assertIn("new wording", text)


class SearchCacheTests(unittest.TestCase):
    def test_normalized_cache_is_reused_and_invalidated_with_source(self):
        session = {"file": "fixture", "mtime": 1}
        first = {"events": [{"kind": "user", "text": "Ada’s example"}]}
        second = {"events": [{"kind": "user", "text": "changed example"}]}
        with (
            mock.patch.object(server, "_TEXT_CACHE", {}),
            mock.patch.object(server, "load_session", side_effect=[first, second]) as load,
            mock.patch.object(common, "normalize_search_text", wraps=common.normalize_search_text) as normalize,
        ):
            self.assertEqual(server.session_segments(session), ("Ada’s example", ""))
            calls = normalize.call_count
            self.assertEqual(server.session_segments(session, normalized=True), ("ada's example", ""))
            self.assertEqual(normalize.call_count, calls)
            session["mtime"] = 2
            self.assertEqual(server.session_segments(session, normalized=True), ("changed example", ""))
            self.assertEqual(load.call_count, 2)


class SearchEndpointTests(ViewerServerTestCase):
    def test_curly_query_finds_text_that_only_exists_in_an_edit(self):
        path = _write_fixture_session(
            self.projects_dir,
            "55555555-5555-4555-8555-555555555555",
            extra_records=({
                "type": "assistant", "timestamp": "2024-01-01T00:00:02Z",
                "message": {"role": "assistant", "model": "claude-test", "content": [{
                    "type": "tool_use", "id": "edit-example", "name": "Edit",
                    "input": {"file_path": "/tmp/proj/notes.md", "old_string": "before",
                              "new_string": "The author's own example\nworks exactly."},
                }]},
            },),
        )
        status, _, body = self.get("/api/search?" + urlencode({"q": "author’s own example works"}))
        self.assertEqual(status, 200)
        hit, = json.loads(body)["matches"]
        self.assertEqual(hit["file"], str(path))
        self.assertIn("author's own example works", hit["snippet"])


if __name__ == "__main__":
    unittest.main()
