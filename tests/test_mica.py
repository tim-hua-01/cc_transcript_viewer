#!/usr/bin/env python3
"""Tests for the optional Mica: the capture engine, the read-only
reader, the viewer's use of it, and the installer's plan.

The capture engine runs with a fake clock against temp directories, one
``poll_once`` at a time, so every scenario (append, truncation, rewrite,
replacement, deletion, move, fork, offline change, lost access) is exact and
fast. Nothing here needs root; the installer is only exercised as a plan.

Run with:  python -m unittest tests.test_mica
"""

from __future__ import annotations

import json
import os
import pwd
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

import claude_parser as claude
import codex_parser as codex
import cursor_parser as cursor
import opencode_parser as opencode
import server
from tests.fixture_builders import (
    _write_fixture_session,
    http_get,
    patch_server_files,
    restore_server_files,
    start_http_server,
    stop_http_server,
)
from mica import capture, install
from mica import store as v


def _line(obj) -> str:
    return json.dumps(obj) + "\n"


def _append(path: Path, *records) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(_line(rec))


class CaptureTestCase(unittest.TestCase):
    """A temp Claude projects tree + Codex sessions tree, a store, and a
    capturer driven by a fake clock."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.projects = self.tmp / "home" / ".claude" / "projects"
        self.codex_sessions = self.tmp / "home" / ".codex" / "sessions"
        self.codex_archived = self.tmp / "home" / ".codex" / "archived_sessions"
        for d in (self.projects, self.codex_sessions, self.codex_archived):
            d.mkdir(parents=True)
        self.store_dir = self.tmp / "mica"
        # Start from real time so heartbeat ages match the fake clock.
        self.now = time.time()
        self.capturer = self._new_capturer()

    def tearDown(self):
        for dirpath, dirnames, _files in os.walk(self.tmp):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        self._tmp.cleanup()

    def _new_capturer(self, **kw):
        return capture.Capturer(
            self.store_dir,
            capture.default_sources(self.tmp / "home"),
            clock=lambda: self.now,
            **kw,
        )

    def tick(self, n: int = 1, seconds: float = 2.0):
        for _ in range(n):
            self.now += seconds
            self.capturer.poll_once(self.now)

    @property
    def reader(self) -> v.StoreReader:
        return v.StoreReader(self.store_dir)

    def key(self, path: Path) -> str:
        key = self.reader.key_for_path(str(path))
        self.assertIsNotNone(key, f"{path} not tracked")
        return key

    def entry_for_name(self, name: str) -> tuple:
        matches = [(k, e) for k, e in self.reader.index().items() if e["path"].endswith(name)]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def session(self, name="s1", records=None) -> Path:
        proj = self.projects / "-tmp-proj"
        proj.mkdir(exist_ok=True)
        path = proj / f"{name}.jsonl"
        recs = records if records is not None else [{"n": 1}, {"n": 2}]
        path.write_text("".join(_line(r) for r in recs), encoding="utf-8")
        return path

    def events_of(self, key: str) -> list:
        return [e["type"] for e in self.reader.events(key)]


class CaptureScenarios(CaptureTestCase):
    def test_restoring_original_capture_does_not_clear_rewrite_flag(self):
        path = self.session(records=[{"text": "original"}])
        self.capturer.start()
        key = self.key(path)
        original = self.reader.generation_path(key, self.reader.record(key)["generations"][0])
        path.write_text(_line({"text": "tampered"}))
        self.tick()
        self.assertEqual(self.reader.badge(key)["flags"], ["rewritten"])
        path.write_bytes(original.read_bytes())
        self.tick()
        result = self.reader.compare(key, path)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(result["flags"], ["rewritten"])
        self.assertEqual(len(result["generations"]), 3)
        self.assertEqual(self.reader.badge(key)["flags"], ["rewritten"])
        self.capturer = self._new_capturer()
        self.capturer.start()
        self.assertEqual(self.reader.compare(key, path)["flags"], ["rewritten"])

    def test_initial_capture_and_appends_verify(self):
        path = self.session()
        self.capturer.start()
        key = self.key(path)
        self.assertEqual(self.reader.compare(key, path)["state"], "verified")

        _append(path, {"n": 3})
        self.assertEqual(self.reader.compare(key, path)["state"], "ahead")
        self.tick()
        result = self.reader.compare(key, path)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(result["flags"], [])
        self.assertEqual(self.reader.index()[key]["gens"], 1)
        self.assertEqual(capture.verify_store(self.store_dir), [])

    def test_files_are_captured_with_the_source_layout(self):
        path = self.session("abc")
        sub = self.projects / "-tmp-proj" / "abc" / "subagents"
        sub.mkdir(parents=True)
        agent = sub / "agent-1.jsonl"
        agent.write_text(_line({"x": 1}))
        (self.projects / "-tmp-proj" / "abc" / "tool-results").mkdir()
        (self.projects / "-tmp-proj" / "abc" / "tool-results" / "t.txt").write_text("not a transcript")
        (self.projects / "-tmp-proj" / "notes.md").write_text("not a transcript")
        self.capturer.start()

        rec = self.reader.record(self.key(path))
        self.assertEqual(rec["generations"][0]["mirror"], "g0000/claude/-tmp-proj/abc.jsonl")
        sub_rec = self.reader.record(self.key(agent))
        self.assertEqual(sub_rec["generations"][0]["mirror"], "g0000/claude/-tmp-proj/abc/subagents/agent-1.jsonl")
        self.assertEqual(len(self.reader.index()), 2)

    def test_new_files_are_found_on_the_next_poll(self):
        self.capturer.start()
        path = self.session("later")
        self.tick()
        self.assertIsNotNone(self.reader.key_for_path(str(path)))
        # ...including in a brand-new project directory.
        newproj = self.projects / "-new-proj"
        newproj.mkdir()
        other = newproj / "x.jsonl"
        other.write_text(_line({"a": 1}))
        self.tick()
        self.assertIsNotNone(self.reader.key_for_path(str(other)))

    def test_truncation_keeps_the_original_and_flags(self):
        path = self.session(records=[{"n": i} for i in range(5)])
        self.capturer.start()
        key = self.key(path)
        path.write_text(_line({"n": 0}))
        self.tick()

        rec = self.reader.record(key)
        self.assertEqual(rec["flags"], ["truncated"])
        g0, g1 = rec["generations"]
        self.assertEqual((g0["close_reason"], g1["reason"]), ("truncated", "truncated"))
        original = (self.store_dir / "files" / key / g0["mirror"]).read_text()
        self.assertEqual(original, "".join(_line({"n": i}) for i in range(5)))
        # The live file matches the new capture, but the session stays flagged.
        result = self.reader.compare(key, path)
        self.assertEqual((result["state"], result["flags"]), ("verified", ["truncated"]))
        self.assertEqual(self.reader.badge(key)["flags"], ["truncated"])
        self.assertEqual(capture.verify_store(self.store_dir), [])

    def test_compare_explains_each_change_with_readable_lines(self):
        records = [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf ~/.claude/projects/x"}}]}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:02Z", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "done, traces removed"}]}},
        ]
        path = self.session(records=records)
        self.capturer.start()
        key = self.key(path)
        path.write_text(_line(records[0]))
        self.tick()
        (change,) = self.reader.compare(key, path)["changes"]
        self.assertEqual((change["from"], change["to"], change["reason"]), ("g0000", "g0001", "truncated"))
        self.assertEqual((change["missing_count"], change["extra_count"]), (2, 0))
        self.assertEqual([m["line"] for m in change["missing"]], [2, 3])
        self.assertEqual(change["missing"][0]["text"], "[Bash] rm -rf ~/.claude/projects/x")
        self.assertEqual(change["missing"][1]["role"], "assistant")
        self.assertEqual(change["missing"][1]["text"], "done, traces removed")

    def test_every_later_tampering_is_captured_too(self):
        path = self.session(records=[{"n": 1}, {"n": 2}])
        self.capturer.start()
        key = self.key(path)
        _append(path, {"n": 3})                                # normal growth
        self.tick()
        path.write_text(_line({"n": 1}))                       # tamper 1: cut 2 and 3
        self.tick()
        _append(path, {"n": 4}, {"n": 5})                      # the session keeps going
        self.tick()
        path.write_text(_line({"n": 1}) + _line({"forged": 4}) + _line({"n": 5}))  # tamper 2: same size, edited
        self.tick()
        _append(path, {"n": 6})
        self.tick()

        rec = self.reader.record(key)
        self.assertEqual(rec["flags"], ["truncated", "rewritten"])
        files = [self.reader.generation_path(key, g).read_text() for g in rec["generations"]]
        self.assertEqual(files, [
            "".join(_line({"n": i}) for i in (1, 2, 3)),                     # before tamper 1
            "".join(_line({"n": i}) for i in (1, 4, 5)),                     # between the two
            _line({"n": 1}) + _line({"forged": 4}) + _line({"n": 5}) + _line({"n": 6}),  # current
        ])
        changes = self.reader.compare(key, path)["changes"]
        self.assertEqual([(c["from"], c["to"], c["reason"]) for c in changes],
                         [("g0000", "g0001", "truncated"), ("g0001", "g0002", "rewritten")])
        self.assertEqual([m["line"] for m in changes[1]["missing"]], [2])   # {"n": 4} was replaced
        self.assertEqual(self.reader.compare(key, path)["state"], "verified")
        self.assertEqual(capture.verify_store(self.store_dir), [])

    def test_emptied_file_then_new_content(self):
        path = self.session()
        self.capturer.start()
        key = self.key(path)
        path.write_text("")
        self.tick()
        rec = self.reader.record(key)
        self.assertEqual(len(rec["generations"]), 1)
        self.assertEqual(rec["generations"][0]["close_reason"], "truncated")
        _append(path, {"after": 1})
        self.tick()
        rec = self.reader.record(key)
        self.assertEqual([g["reason"] for g in rec["generations"]], ["initial", "truncated"])
        self.assertEqual(self.reader.compare(key, path)["state"], "verified")

    def test_same_size_rewrite_near_the_end_is_caught_immediately(self):
        path = self.session(records=[{"text": "aaaa"}, {"text": "bbbb"}])
        self.capturer.start()
        key = self.key(path)
        path.write_text(_line({"text": "aaaa"}) + _line({"text": "XXXX"}))
        self.tick()
        self.assertEqual(self.reader.record(key)["flags"], ["rewritten"])

    def test_same_size_edit_with_restored_mtime_is_caught(self):
        path = self.session(records=[{"text": "aaaa"}, {"text": "bbbb"}])
        self.capturer.start()
        key = self.key(path)
        before = os.stat(path)
        path.write_text(_line({"text": "aaaa"}) + _line({"text": "XXXX"}))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # hide the edit from mtime
        self.assertEqual(os.stat(path).st_mtime_ns, before.st_mtime_ns)
        self.tick()
        self.assertEqual(self.reader.record(key)["flags"], ["rewritten"])

    def test_deep_same_size_edit_to_an_idle_file_is_caught_at_once(self):
        path = self.session(records=[{"text": "secret"}] + [{"pad": "y" * 50}] * 5)
        with mock.patch.object(capture, "TAIL_WINDOW", 64):
            self.capturer = self._new_capturer(verify_seconds=10_000)
            self.capturer.start()
            key = self.key(path)
            before = os.stat(path)
            path.write_text(path.read_text().replace("secret", "benign"))
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.tick()
        self.assertEqual(self.reader.record(key)["flags"], ["rewritten"])

    def test_new_file_hidden_by_restoring_directory_mtime_is_found(self):
        proj = self.projects / "-tmp-proj"
        proj.mkdir()
        self.capturer = self._new_capturer(rescan_seconds=10_000)
        self.capturer.start()
        before = os.stat(proj)
        path = proj / "sneaky.jsonl"
        path.write_text(_line({"n": 1}))
        os.utime(proj, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(os.stat(proj).st_mtime_ns, before.st_mtime_ns)
        self.tick()
        self.assertIsNotNone(self.reader.key_for_path(str(path)))

    def test_restart_skips_unchanged_files(self):
        self.session("a")
        self.session("b")
        self.capturer.start()
        self.tick()
        self.capturer = self._new_capturer()
        with mock.patch.object(capture.Capturer, "_process") as process:
            self.capturer.start()
        process.assert_not_called()

    def test_deep_rewrite_is_caught_by_the_periodic_full_check(self):
        path = self.session(records=[{"text": "secret"}] + [{"pad": "y" * 50}] * 5)
        with mock.patch.object(capture, "TAIL_WINDOW", 64):
            self.capturer = self._new_capturer(verify_seconds=30)
            self.capturer.start()
            key = self.key(path)
            content = path.read_text().replace("secret", "benign")
            path.write_text(content)
            _append(path, {"more": 1})
            self.tick()  # only the tail is checked: looks like an append
            self.assertEqual(self.reader.record(key)["flags"], [])
            self.tick(n=20)  # the full comparison runs within verify_seconds
            rec = self.reader.record(key)
        self.assertEqual(rec["flags"], ["rewritten"])
        g0 = rec["generations"][0]
        captured = (self.store_dir / "files" / key / g0["mirror"]).read_text()
        self.assertIn("secret", captured)

    def test_deletion_is_flagged_and_the_copy_kept(self):
        path = self.session()
        self.capturer.start()
        key = self.key(path)
        path.unlink()
        self.tick(n=2)
        entry = self.reader.index()[key]
        self.assertEqual((entry["status"], entry["flags"]), ("deleted", ["deleted"]))
        self.assertEqual(self.reader.compare(key, path)["state"], "deleted")
        gen = self.reader.best_generation(self.reader.record(key))
        self.assertTrue(self.reader.generation_path(key, gen).is_file())

    def test_deleting_a_whole_project_directory(self):
        path = self.session()
        self.capturer.start()
        key = self.key(path)
        for f in path.parent.iterdir():
            f.unlink()
        path.parent.rmdir()
        self.tick(n=2)
        self.assertEqual(self.reader.index()[key]["status"], "deleted")

    def test_old_deletions_stay_flagged(self):
        path = self.session()
        os.utime(path, (self.now - 40 * 86400, self.now - 40 * 86400))
        self.capturer.start()
        key = self.key(path)
        path.unlink()
        self.tick(n=2)
        self.assertEqual(self.reader.badge(key)["state"], "deleted")
        self.assertEqual(self.reader.badge(key)["flags"], ["deleted"])
        result = self.reader.compare(key, path)
        self.assertEqual(result["state"], "deleted")
        self.assertEqual(result["flags"], ["deleted"])

        # The original capture remains readable, including after a restart.
        self.capturer = self._new_capturer()
        self.capturer.start()
        self.assertEqual(self.reader.badge(key)["flags"], ["deleted"])
        gen = self.reader.best_generation(self.reader.record(key))
        self.assertTrue(self.reader.generation_path(key, gen).is_file())

    def test_codex_archive_move_is_not_tampering(self):
        day = self.codex_sessions / "2026" / "09" / "28"
        day.mkdir(parents=True)
        path = day / "rollout-2026-09-28T00-00-00-abc.jsonl"
        path.write_text(_line({"type": "session_meta"}))
        self.capturer.start()
        key = self.key(path)
        dest = self.codex_archived / path.name
        os.rename(path, dest)
        self.tick(n=2)
        entry = self.reader.index()[key]
        self.assertEqual((entry["status"], entry["flags"], entry["path"]), ("active", [], os.path.realpath(dest)))
        self.assertIn("moved", self.events_of(key))
        _append(dest, {"type": "later"})
        self.tick()
        self.assertEqual(self.reader.compare(key, dest)["state"], "verified")

    def test_forks_and_in_file_branches_are_not_flagged(self):
        parent = self.session("parent", [{"uuid": "a", "parentUuid": None}, {"uuid": "b", "parentUuid": "a"}])
        self.capturer.start()
        # A rewind appends a sibling branch to the same file...
        _append(parent, {"uuid": "c", "parentUuid": "a"})
        # ...and a fork copies the history into a new session file.
        fork = self.session("fork", [{"uuid": "a", "parentUuid": None}, {"uuid": "b", "parentUuid": "a"},
                                     {"uuid": "d", "parentUuid": "b", "forkedFrom": "parent"}])
        self.tick(n=2)
        for p in (parent, fork):
            key = self.key(p)
            self.assertEqual(self.reader.record(key)["flags"], [], p)
            self.assertEqual(self.reader.compare(key, p)["state"], "verified")

    def test_atomic_rewrite_that_only_extends_is_benign(self):
        path = self.session()
        self.capturer.start()
        key = self.key(path)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(path.read_text() + _line({"n": 3}))
        os.replace(tmp, path)
        self.tick()
        self.assertEqual(self.reader.record(key)["flags"], [])
        self.assertIn("inode_changed", self.events_of(key))
        self.assertEqual(self.reader.compare(key, path)["state"], "verified")

    def test_replacement_with_different_content_is_flagged(self):
        path = self.session(records=[{"n": 1}, {"n": 2}])
        self.capturer.start()
        key = self.key(path)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_line({"forged": True}) + _line({"n": 2}) + _line({"n": 3}))
        os.replace(tmp, path)
        self.tick()
        self.assertEqual(self.reader.record(key)["flags"], ["replaced"])

    def test_delete_then_recreate_links_the_new_file_to_the_old_capture(self):
        path = self.session(records=[{"n": 1}, {"n": 2}])
        self.capturer.start()
        old_key = self.key(path)
        path.unlink()
        self.tick(n=2)
        path.write_text(_line({"n": 99}))  # the harness starts logging again
        self.tick()
        new_key = self.key(path)
        self.assertNotEqual(old_key, new_key)
        self.assertEqual(self.reader.record(new_key)["flags"], ["recreated"])
        self.assertEqual(self.reader.record(new_key)["recreated_from"], old_key)
        result = self.reader.compare(new_key, path)
        old_copy = Path(result["recreated_from_file"])
        self.assertEqual(old_copy.read_text(), _line({"n": 1}) + _line({"n": 2}))

    def test_changes_while_the_daemon_was_down_are_flagged(self):
        path = self.session(records=[{"n": 1}, {"n": 2}, {"n": 3}])
        kept = self.session("kept")
        self.capturer.start()
        key = self.key(path)
        self.tick()
        # Daemon stops; the transcript is truncated; time passes.
        path.write_text(_line({"n": 1}))
        self.now += 3600
        self.capturer = self._new_capturer()
        self.capturer.start()
        rec = self.reader.record(key)
        self.assertEqual(rec["flags"], ["truncated"])
        event = [e for e in self.reader.events(key) if e["type"] == "truncated"][0]
        self.assertTrue(event["detail"]["while_offline"])
        global_events = [e["type"] for e in v.read_jsonl(self.store_dir / v.EVENTS_FILE)]
        self.assertIn("capture_gap", global_events)
        self.assertEqual(self.reader.record(self.key(kept))["flags"], [])

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_lost_access_is_flagged_and_survives_recovery(self):
        path = self.session()
        self.capturer.start()
        key = self.key(path)
        os.chmod(self.projects, 0)
        self.tick(n=3)
        self.assertEqual(self.reader.index()[key]["status"], "active")
        self.assertEqual(self.reader.record(key)["flags"], ["unreadable"])
        self.assertEqual(self.reader.badge(key)["flags"], ["unreadable"])
        global_events = [e["type"] for e in v.read_jsonl(self.store_dir / v.EVENTS_FILE)]
        self.assertIn("access_lost", global_events)
        os.chmod(self.projects, 0o755)
        self.tick()
        global_events = [e["type"] for e in v.read_jsonl(self.store_dir / v.EVENTS_FILE)]
        self.assertIn("access_restored", global_events)
        # The gap has a recorded start and end on the transcript itself.
        self.assertEqual([e["type"] for e in self.reader.events(key)], ["unreadable", "readable_again"])
        self.assertEqual(self.reader.compare(key, path)["state"], "verified")
        self.assertEqual(self.reader.compare(key, path)["flags"], ["unreadable"])
        self.capturer = self._new_capturer()
        self.capturer.start()
        self.assertEqual(self.reader.badge(key)["flags"], ["unreadable"])

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_individual_file_access_loss_is_flagged(self):
        path = self.session()
        self.capturer.start()
        key = self.key(path)
        os.chmod(path, 0)
        try:
            self.tick()
            self.assertTrue(self.reader.status()["sources"]["claude"]["ok"])
            self.assertEqual(self.reader.badge(key)["flags"], ["unreadable"])
            self.assertEqual(self.reader.status()["n_flagged"], 1)
        finally:
            os.chmod(path, 0o644)
        self.tick()
        result = self.reader.compare(key, path)
        self.assertEqual(result["state"], "verified")
        self.assertEqual(result["flags"], ["unreadable"])
        self.assertEqual([e["type"] for e in result["events"]], ["unreadable", "readable_again"])

    def test_symlinks_are_not_followed(self):
        outside = self.tmp / "outside.jsonl"
        outside.write_text(_line({"private": True}))
        proj = self.projects / "-tmp-proj"
        proj.mkdir()
        (proj / "link.jsonl").symlink_to(outside)
        (self.projects / "-linked-dir").symlink_to(self.tmp, target_is_directory=True)
        self.capturer.start()
        self.assertEqual(self.reader.index(), {})

    def test_crash_between_append_and_record_save_recovers(self):
        path = self.session()
        self.capturer.start()
        key = self.key(path)
        rec = self.reader.record(key)
        data = self.store_dir / "files" / key / rec["generations"][0]["mirror"]
        extra = _line({"n": 3})
        with open(data, "a") as fh:  # bytes landed, record/chain did not
            fh.write(extra)
        _append(path, {"n": 3})
        self.capturer = self._new_capturer()
        self.capturer.start()
        self.assertEqual(capture.verify_store(self.store_dir), [])
        self.assertEqual(self.reader.compare(key, path)["state"], "verified")

    def test_verify_detects_changes_to_captured_bytes(self):
        path = self.session()
        self.capturer.start()
        key = self.key(path)
        rec = self.reader.record(key)
        data = self.store_dir / "files" / key / rec["generations"][0]["mirror"]
        data.write_text(data.read_text().replace("1", "7"))
        problems = capture.verify_store(self.store_dir)
        self.assertTrue(any("hash mismatch" in p for p in problems), problems)

    def test_modified_compare_reports_line_diff(self):
        path = self.session(records=[{"n": 1}, {"n": 2}, {"n": 3}])
        self.capturer.start()
        key = self.key(path)
        # Before the daemon's next poll, the live file no longer matches.
        path.write_text(_line({"n": 1}) + _line({"n": 3}))
        result = self.reader.compare(key, path)
        self.assertEqual(result["state"], "modified")
        diff = result["diff"]
        self.assertEqual((diff["missing_count"], diff["extra_count"], diff["first_diff_line"]), (1, 0, 2))
        self.assertEqual(diff["missing"][0]["line"], 2)

    def test_heartbeat_reports_cpu_and_the_reader_warns_above_two_percent(self):
        self.session()
        clock = {"wall": 1000.0, "cpu": 50.0}
        with mock.patch.object(capture.time, "monotonic", side_effect=lambda: clock["wall"]), \
                mock.patch.object(capture.time, "process_time", side_effect=lambda: clock["cpu"]):
            self.capturer.start()
            self.assertIsNone(v.read_json(self.store_dir / v.STATUS_FILE)["cpu_percent"])  # no full window yet
            for cpu_per_second, warn in ((0.003, False), (0.05, True)):
                clock["wall"] += 120
                clock["cpu"] += 120 * cpu_per_second
                self.tick(seconds=capture.HEARTBEAT_SECONDS)
                with mock.patch.object(v.time, "time", return_value=self.now):
                    status = self.reader.status()
                self.assertAlmostEqual(status["cpu_percent"], 100 * cpu_per_second, places=2)
                self.assertEqual(status["cpu_warn"], warn)

    def test_heartbeat_and_status(self):
        self.session()
        self.capturer.start()
        with mock.patch.object(v.time, "time", return_value=self.now + 1):
            status = self.reader.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["n_files"], 1)
        with mock.patch.object(v.time, "time", return_value=self.now + 3600):
            self.assertFalse(self.reader.status()["running"])

    def test_a_long_lived_reader_rereads_the_index_after_the_recheck_interval(self):
        path = self.session()
        self.capturer.start()
        reader = self.reader  # one reader for the whole test, as the viewer holds
        clock = [1000.0]
        with mock.patch.object(v.time, "monotonic", side_effect=lambda: clock[0]):
            key = reader.key_for_path(str(path))
            self.assertEqual(reader.badge(key)["flags"], [])
            self.capturer._flag(key, "some_future_flag", {})
            self.capturer._write_index()
            # Within the interval the last look at the index still answers.
            self.assertEqual(reader.badge(key)["flags"], [])
            clock[0] += v.INDEX_RECHECK_SECONDS
            self.assertEqual(reader.badge(key)["flags"], ["some_future_flag"])

    def test_reader_reports_what_the_daemon_recorded(self):
        path = self.session()
        self.capturer.start()
        key = self.key(path)
        # A flag this reader has no label for is still reported, unchanged.
        self.capturer._flag(key, "some_future_flag", {})
        self.capturer._write_index()
        self.assertEqual(self.reader.badge(key)["flags"], ["some_future_flag"])
        self.assertEqual(self.reader.status()["n_flagged"], 1)
        # A restart after downtime records a store-wide gap, which the reader
        # passes on alongside the status.
        self.now += 3600
        self.capturer = self._new_capturer()
        self.capturer.start()
        gaps = self.reader.status()["store_events"]
        self.assertEqual([e["type"] for e in gaps], ["capture_gap"])
        self.assertEqual(set(gaps[0]["detail"]), {"from", "to"})


class ViewerIntegration(CaptureTestCase):
    """The viewer reads the store: badges, mica-copy rows, comparison API."""

    def setUp(self):
        super().setUp()
        self._old_sources = (claude.PROJECTS_DIR, codex.CODEX_HOME,
                             (cursor.DB_PATH, cursor.PROJECTS_DIR, cursor.CHATS_DIR),
                             opencode.DB_PATH)
        # Every source, not just Claude: otherwise the viewer lists (and
        # search loads) the real ~/.codex, Cursor, and opencode sessions.
        claude.configure(self.projects)
        codex.configure(self.tmp / "home" / ".codex")
        cursor.configure(self.tmp / "cursor-db", projects_dir=self.tmp / "cursor-projects",
                         chats_dir=self.tmp / "cursor-chats")
        opencode.configure(self.tmp / "opencode" / "opencode.db")
        self._old_files = patch_server_files(server, self.tmp)
        self._old_mica = server.MICA
        self.live = _write_fixture_session(self.projects, "aaaaaaaa-0000-4000-8000-000000000001", "live prompt")
        self.doomed = _write_fixture_session(self.projects, "aaaaaaaa-0000-4000-8000-000000000002", "doomed prompt")
        self.cut = _write_fixture_session(self.projects, "aaaaaaaa-0000-4000-8000-000000000003", "cut prompt",
                                          ("second prompt",))
        self.capturer.start()
        self.doomed.unlink()
        self.cut.write_text(self.cut.read_text().splitlines(True)[0])
        self.tick(n=2)
        server.configure_mica(self.store_dir)
        self.httpd, self.port, self.thread = start_http_server(server.Handler)

    def tearDown(self):
        stop_http_server(self.httpd, self.thread)
        server.MICA = self._old_mica
        restore_server_files(server, self._old_files)
        projects, codex_home, (cursor_db, cursor_projects, cursor_chats), opencode_db = self._old_sources
        claude.configure(projects)
        codex.configure(codex_home)
        cursor.configure(cursor_db, projects_dir=cursor_projects, chats_dir=cursor_chats)
        opencode.configure(opencode_db)
        super().tearDown()

    def get_json(self, path):
        status, _headers, body = http_get(self.port, path)
        return status, json.loads(body)

    def _snapshot(self):
        out = {}
        for dirpath, _dirs, files in os.walk(self.store_dir):
            for name in files:
                st = os.stat(os.path.join(dirpath, name))
                out[os.path.join(dirpath, name)] = (st.st_size, st.st_mtime_ns)
        return out

    def test_session_list_carries_mica_state_and_deleted_copies(self):
        status, body = self.get_json("/api/sessions")
        self.assertEqual(status, 200)
        self.assertTrue(body["mica"]["enabled"])
        by_prompt = {s["title"]: s for s in body["sessions"]}
        self.assertEqual(by_prompt["live prompt"]["mica"]["flags"], [])
        self.assertEqual(by_prompt["cut prompt"]["mica"]["flags"], ["truncated"])
        doomed = by_prompt["doomed prompt"]
        self.assertTrue(doomed["mica"]["copy"])
        self.assertEqual(doomed["mica"]["state"], "deleted")
        self.assertTrue(doomed["file"].startswith(os.path.realpath(self.store_dir)))

    def test_mica_copy_opens_like_a_transcript(self):
        _status, body = self.get_json("/api/sessions")
        copy = next(s for s in body["sessions"] if s["title"] == "doomed prompt")
        status, data = self.get_json("/api/session?file=" + quote(copy["file"]))
        self.assertEqual(status, 200)
        self.assertEqual(data["mica_copy"]["generation"], "g0000")
        self.assertEqual(data["mica_copy"]["original_path"], os.path.realpath(self.doomed))
        self.assertFalse(data["mica_copy"]["live"])
        self.assertTrue(any(ev.get("kind") == "user" for ev in data["events"]))

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_access_lost_session_remains_visible_when_live_listing_loses_it(self):
        os.chmod(self.live, 0)
        self.tick()
        status, body = self.get_json("/api/sessions")
        self.assertEqual(status, 200)
        copy = next(s for s in body["sessions"] if s["title"] == "live prompt")
        self.assertTrue(copy["mica"]["copy"])
        self.assertEqual(copy["mica"]["state"], "active")
        self.assertEqual(copy["mica"]["flags"], ["unreadable"])
        status, data = self.get_json("/api/session?file=" + quote(copy["file"]))
        self.assertEqual(status, 200)
        self.assertTrue(data["mica_copy"]["live"])

    def test_retention_settings_cannot_hide_deleted_sessions(self):
        settings = self.projects.parent / "settings.json"
        for days in (30, 0.01, 1, 99999):
            with self.subTest(cleanupPeriodDays=days):
                settings.write_text(json.dumps({"cleanupPeriodDays": days}), encoding="utf-8")
                status, body = self.get_json("/api/sessions")
                self.assertEqual(status, 200)
                copy = next(s for s in body["sessions"] if s["title"] == "doomed prompt")
                self.assertTrue(copy["mica"]["copy"])
                self.assertEqual(copy["mica"]["state"], "deleted")
                self.assertEqual(copy["mica"]["flags"], ["deleted"])
                status, comparison = self.get_json("/api/mica-compare?file=" + quote(copy["file"]))
                self.assertEqual(status, 200)
                self.assertEqual(comparison["state"], "deleted")
                self.assertEqual(comparison["flags"], ["deleted"])

    def test_session_list_reports_recording_settings_independently_of_mica(self):
        settings = self.projects.parent / "settings.json"
        config = self.codex_sessions.parent / "config.toml"
        with mock.patch.object(server.codex, "CODEX_HOME", self.codex_sessions.parent):
            status, body = self.get_json("/api/sessions")
            self.assertEqual(status, 200)
            self.assertEqual({i["setting"] for i in body["setup"]["issues"]},
                             {"cleanupPeriodDays", "showThinkingSummaries", "model_reasoning_summary"})
            settings.write_text(json.dumps({"cleanupPeriodDays": 10000, "showThinkingSummaries": True}))
            config.write_text('model_reasoning_summary = "detailed"\n')
            server.configure_mica(self.store_dir, enabled=False)
            status, body = self.get_json("/api/sessions")
            self.assertEqual(status, 200)
            self.assertEqual(body["setup"]["issues"], [])
            self.assertEqual(body["mica"], {"enabled": False})

    def test_compare_endpoint(self):
        _s, verified = self.get_json("/api/mica-compare?file=" + quote(str(self.live)))
        self.assertEqual(verified["state"], "verified")
        _s, cut = self.get_json("/api/mica-compare?file=" + quote(str(self.cut)))
        self.assertEqual(cut["flags"], ["truncated"])
        self.assertEqual(len(cut["generations"]), 2)
        original = Path(cut["generations"][0]["file"])
        self.assertIn("second prompt", original.read_text())
        # The original capture is itself openable and compares as the history.
        _s, via_copy = self.get_json("/api/mica-compare?file=" + quote(str(original)))
        self.assertEqual(via_copy["viewing_generation"], "g0000")

    def test_paths_outside_roots_and_mica_are_refused(self):
        outside = self.tmp / "elsewhere.jsonl"
        outside.write_text("{}\n")
        status, _body = self.get_json("/api/mica-compare?file=" + quote(str(outside)))
        self.assertEqual(status, 403)
        # A mica file that is not a captured generation is not a session.
        status, _body = self.get_json("/api/session?file=" + quote(str(self.store_dir / v.INDEX_FILE)))
        self.assertEqual(status, 403)

    def test_viewer_never_writes_to_the_mica(self):
        before = self._snapshot()
        _s, body = self.get_json("/api/sessions")
        for s in body["sessions"]:
            self.get_json("/api/session?file=" + quote(s["file"]))
            self.get_json("/api/mica-compare?file=" + quote(s["file"]))
        self.get_json("/api/search?q=prompt")
        self.assertEqual(self._snapshot(), before)

    def test_every_capture_is_listed_even_when_the_live_list_misses_it(self):
        n_captured = len(self.reader.index())
        with mock.patch.object(claude, "list_sessions", return_value=[]):
            _status, body = self.get_json("/api/sessions")
        copies = [s for s in body["sessions"] if s.get("mica", {}).get("copy")]
        self.assertEqual(len(copies), n_captured)
        live = next(s for s in copies if s["title"] == "live prompt")
        self.assertEqual((live["mica"]["state"], live["mica"]["flags"]), ("active", []))
        # A capture that can't be summarized is still listed, with the reason.
        with mock.patch.object(claude, "list_sessions", return_value=[]), \
                mock.patch.object(claude, "session_summary", side_effect=ValueError("bad capture")):
            _status, body = self.get_json("/api/sessions")
        copies = [s for s in body["sessions"] if s.get("mica", {}).get("copy")]
        self.assertEqual(len(copies), n_captured)
        self.assertTrue(all("bad capture" in s["mica"]["error"] for s in copies))

    def test_store_read_failures_are_reported(self):
        with mock.patch.object(server.MICA, "badge", side_effect=OSError("store vanished")):
            status, body = self.get_json("/api/sessions")
        self.assertEqual(status, 200)
        self.assertIn("store vanished", body["mica"]["error"])
        _status, body = self.get_json("/api/sessions")
        self.assertNotIn("error", body["mica"])

    def test_disabled_mica_changes_nothing(self):
        server.configure_mica(self.store_dir, enabled=False)
        _s, body = self.get_json("/api/sessions")
        self.assertEqual(body["mica"], {"enabled": False})
        self.assertFalse(any("mica" in s for s in body["sessions"]))
        _s, result = self.get_json("/api/mica-compare?file=" + quote(str(self.live)))
        self.assertEqual(result["state"], "disabled")

    def test_missing_store_directory_is_off(self):
        server.configure_mica(self.tmp / "no-such-mica")
        self.assertIsNone(server.MICA)


class InstallerPlan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self._tmp.name)) / "someone"
        (self.home / ".claude" / "projects").mkdir(parents=True)
        (self.home / ".codex" / "sessions").mkdir(parents=True)
        self.user = pwd.struct_passwd(("someone", "*", 501, 20, "", str(self.home), "/bin/zsh"))

    def tearDown(self):
        self._tmp.cleanup()

    def _plan(self, service_exists=False):
        return install.build_install_plan(
            self.user, Path("/Library/Mica"), "/usr/bin/python3", 1.0,
            service_exists=service_exists, free_id=321,
        )

    def test_plan_creates_service_user_and_grants_read_only_access(self):
        steps, sources, notes = self._plan()
        argvs = [s.argv for s in steps if s.argv]
        self.assertIn(["dscl", ".", "-create", "/Users/_mica", "UniqueID", "321"], argvs)
        self.assertIn(["dscl", ".", "-create", "/Users/_mica", "UserShell", "/usr/bin/false"], argvs)
        grants = [a for a in argvs if a[:2] in (["chmod", "-R"], ["chmod", "+a"])]
        for argv in grants:
            ace = argv[-2]
            self.assertNotRegex(ace, r"\b(write|append|delete|add_file|add_subdirectory|writeattr|chown)\b", argv)
        self.assertIn(["chmod", "-R", "+a", install.SOURCE_ACE, str(self.home / ".claude" / "projects")], argvs)
        self.assertIn(["chmod", "+a", install.ANCESTOR_ACE, str(self.home)], argvs)
        # ~/.codex gets pass-through only: it also holds credentials.
        self.assertIn(["chmod", "+a", install.ANCESTOR_ACE, str(self.home / ".codex")], argvs)
        self.assertNotIn(["chmod", "-R", "+a", install.SOURCE_ACE, str(self.home / ".codex")], argvs)
        self.assertEqual([s.name for s in sources], ["claude", "codex", "codex-archived"])
        self.assertEqual(notes, [])

    def test_plan_skips_the_user_when_it_exists_and_notes_missing_tools(self):
        (self.home / ".codex" / "sessions").rmdir()
        (self.home / ".codex").rmdir()
        steps, sources, notes = self._plan(service_exists=True)
        # A reinstall stops the old daemon before re-granting access, so it
        # never sees transcripts become unreadable mid-install.
        self.assertEqual(steps[0].argv, ["launchctl", "bootout", "system/" + install.LABEL])
        self.assertEqual(sum(s.argv[:2] == ["launchctl", "bootout"] for s in steps if s.argv), 1)
        self.assertFalse(any(s.argv and s.argv[0] == "dscl" for s in steps))
        self.assertEqual([s.name for s in sources], ["claude"])
        self.assertEqual(len(notes), 2)

    def test_plist_runs_isolated_as_the_service_user(self):
        plist = install.plist_dict("/usr/bin/python3", Path("/Library/Mica"))
        self.assertEqual(plist["UserName"], "_mica")
        self.assertEqual(plist["ProgramArguments"][:2], ["/usr/bin/python3", "-I"])
        self.assertTrue(plist["KeepAlive"] and plist["RunAtLoad"])

    def test_reset_deletes_the_store_after_stopping_the_daemon(self):
        steps, _sources, _notes = self._plan(service_exists=True)
        self.assertNotIn(["rm", "-rf", "/Library/Mica"], [s.argv for s in steps])
        steps, _sources, _notes = install.build_install_plan(
            self.user, Path("/Library/Mica"), "/usr/bin/python3", 1.0, True, reset=True)
        self.assertEqual([s.argv for s in steps[:2]],
                         [["launchctl", "bootout", "system/" + install.LABEL], ["rm", "-rf", "/Library/Mica"]])

    def test_uninstall_plan_keeps_the_mica_by_default(self):
        steps = install.build_uninstall_plan(self.user, Path("/Library/Mica"), delete_store=False)
        argvs = [s.argv for s in steps if s.argv]
        self.assertNotIn(["rm", "-rf", "/Library/Mica"], argvs)
        self.assertIn(["chown", "-R", "root:wheel", "/Library/Mica"], argvs)
        steps = install.build_uninstall_plan(self.user, Path("/Library/Mica"), delete_store=True)
        self.assertIn(["rm", "-rf", "/Library/Mica"], [s.argv for s in steps if s.argv])

    def test_user_owned_paths_are_rejected_for_daemon_code(self):
        self.assertTrue(install.root_owned_chain(self.home))
        if sys.platform == "darwin":
            self.assertEqual(install.root_owned_chain(Path("/usr/bin/python3")), [])


if __name__ == "__main__":
    unittest.main()
