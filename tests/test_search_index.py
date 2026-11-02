"""SearchIndex: persistence, invalidation, background progress, and workers."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import search_index
import search_text


def load_json_session(file_id: str) -> dict:
    """A session whose file is JSON {"user": ..., "rest": ...}. Module-level,
    so worker processes can import it."""
    texts = json.loads(Path(file_id).read_text(encoding="utf-8"))
    return {"events": [{"kind": "user", "text": texts.get("user", "")},
                       {"kind": "assistant", "text": texts.get("rest", "")}]}


class SearchIndexTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "cache" / "search-index.sqlite3"
        self.indexes = []

    def tearDown(self):
        for index in self.indexes:
            index.close()
        self._tmp.cleanup()

    def index(self, load=load_json_session, **kwargs) -> search_index.SearchIndex:
        index = search_index.SearchIndex(self.path, load, **{"workers": 0, **kwargs})
        self.indexes.append(index)
        return index

    def session(self, name: str, user: str = "", rest: str = "", mtime: float = 1.0, **extra) -> dict:
        path = self.tmp / f"{name}.json"
        path.write_text(json.dumps({"user": user, "rest": rest}), encoding="utf-8")
        return {"file": str(path), "agent": "claude", "mtime": mtime, **extra}

    def wait_for(self, index, predicate, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = index.status()
            if predicate(status):
                return status
            time.sleep(0.02)
        self.fail(f"index never reached the expected state: {index.status()}")

    def files(self, matches) -> list[str]:
        return [Path(m["file"]).stem for m in matches]


class PersistenceTests(SearchIndexTestCase):
    def test_a_reopened_index_answers_without_extracting_again(self):
        sessions = [self.session("a", user="alpha prompt"), self.session("b", rest="beta output")]
        self.index().sync(sessions)
        self.indexes.pop().close()

        def must_not_load(file_id):
            raise AssertionError(f"re-extracted {file_id}")

        reopened = self.index(load=must_not_load)
        self.assertEqual(self.files(reopened.search("alpha", sessions, wait=True)), ["a"])
        self.assertEqual(self.files(reopened.search("beta", sessions, wait=True)), ["b"])

    def test_changed_sessions_are_reindexed_and_missing_ones_dropped(self):
        a, b = self.session("a", rest="old words"), self.session("b", rest="kept words")
        index = self.index()
        index.sync([a, b])
        a = self.session("a", rest="new words", mtime=2.0)
        self.assertEqual(self.files(index.search("new", [a, b], wait=True)), ["a"])
        self.assertEqual(index.search("old", [a, b], wait=True), [])
        self.assertEqual(index.search("kept", [a], wait=True), [])
        self.assertEqual(index.status()["total"], 1)

    def test_documents_from_another_extractor_version_are_cleared(self):
        sessions = [self.session("a", rest="versioned text")]
        self.index().sync(sessions)
        self.indexes.pop().close()
        loads = []
        with mock.patch.object(search_text, "EXTRACTOR_VERSION", search_text.EXTRACTOR_VERSION + 1):
            index = self.index(load=lambda f: loads.append(f) or load_json_session(f))
            self.assertEqual(index.status()["indexed"], 0)
            self.assertEqual(self.files(index.search("versioned", sessions, wait=True)), ["a"])
        self.assertEqual(len(loads), 1)

    def test_the_file_is_private_and_a_corrupt_one_is_rebuilt(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"not a database at all" * 100)
        index = self.index()
        sessions = [self.session("a", rest="recovered text")]
        self.assertEqual(self.files(index.search("recovered", sessions, wait=True)), ["a"])
        for suffix in ("", "-wal"):
            mode = stat.S_IMODE(os.stat(str(self.path) + suffix).st_mode)
            self.assertEqual(mode, 0o600, suffix)

    def test_reset_removes_the_index_and_its_log(self):
        self.index().sync([self.session("a", rest="text")])
        self.indexes.pop().close()
        removed = search_index.remove_index_files(self.path)
        self.assertIn(self.path, removed)
        self.assertFalse(any(Path(str(self.path) + s).exists() for s in ("", "-wal", "-shm")))


class SearchBehaviorTests(SearchIndexTestCase):
    def test_snippets_are_cut_from_source_across_chunks(self):
        rest = ("filler line\n" * 20000) + "the “needle” is here\n" + ("tail\n" * 10)
        hit, = self.index().search('"needle"', [self.session("a", rest=rest)], wait=True)
        self.assertIn("the “needle” is here", hit["snippet"])
        self.assertTrue(hit["snippet"].startswith("…"))
        self.assertEqual(hit["pos"], rest.index("“needle”"))

    def test_rest_positions_count_the_user_text_before_them(self):
        hit, = self.index().search("marker", [self.session("a", user="abc", rest="xx marker")], wait=True)
        self.assertEqual(hit["pos"], len("abc") + len("xx "))

    def test_a_replaced_search_stops(self):
        index = self.index()
        sessions = [self.session(name, rest="shared word") for name in "abc"]
        index.sync(sessions)
        titles = {s["file"]: ("", "", "", "") for s in sessions}
        with index._lock:
            index._search_seq += 1
        self.assertIsNone(index._content_matches("shared", titles, token=index._search_seq - 1))


class BackgroundIndexingTests(SearchIndexTestCase):
    def test_progress_is_reported_and_partial_results_returned_while_building(self):
        release = threading.Event()
        started = threading.Event()

        def slow_load(file_id):
            if Path(file_id).stem == "slow":
                started.set()
                release.wait(10)
            return load_json_session(file_id)

        fast = self.session("fast", rest="common word", mtime=2.0)
        slow = self.session("slow", rest="common word", mtime=1.0, custom_title="common title")
        index = self.index(load=slow_load)
        index.notify([fast, slow])
        self.assertTrue(started.wait(10))
        status = self.wait_for(index, lambda s: s["indexed"] == 1)
        self.assertEqual((status["state"], status["total"]), ("building", 2))
        # The indexed session matches by content, the other by its title only.
        hits = index.search("common", [fast, slow])
        self.assertEqual(sorted(self.files(hits)), ["fast", "slow"])
        release.set()
        status = self.wait_for(index, lambda s: s["state"] == "ready")
        self.assertEqual(status["indexed"], 2)

    def test_a_session_still_being_written_waits_for_the_live_delay(self):
        index = self.index(live_reindex_seconds=3600)
        live = self.session("live", rest="first")
        index.notify([live])
        self.wait_for(index, lambda s: s["state"] == "ready")
        generation = index.status()["generation"]
        live = self.session("live", rest="second", mtime=2.0)
        index.notify([live])
        time.sleep(0.3)
        status = index.status()
        self.assertEqual((status["updating"], status["generation"]), (1, generation))
        self.assertEqual(self.files(index.search("second", [live], wait=True)), ["live"])

    def test_worker_processes_extract_sessions(self):
        index = self.index(workers=2)
        sessions = [self.session(f"s{i}", rest=f"word{i} shared") for i in range(4)]
        index.notify(sessions)
        self.wait_for(index, lambda s: s["state"] == "ready" and s["indexed"] == 4, timeout=60)
        self.assertFalse(index._pool_broken)
        self.assertEqual(len(index.search("shared", sessions)), 4)

    def test_a_loader_workers_cannot_import_falls_back_to_this_process(self):
        index = self.index(load=lambda f: load_json_session(f), workers=1)
        sessions = [self.session("a", rest="fallback text")]
        index.notify(sessions)
        self.wait_for(index, lambda s: s["state"] == "ready" and s["indexed"] == 1, timeout=60)
        self.assertTrue(index._pool_broken)
        self.assertEqual(self.files(index.search("fallback", sessions)), ["a"])


if __name__ == "__main__":
    unittest.main()
