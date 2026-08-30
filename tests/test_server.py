"""Focused tests for cross-parser session-list behavior in server.py."""

from __future__ import annotations

import unittest
from unittest import mock

import server


class _FixtureParser:
    def __init__(self, sessions):
        self.sessions = sessions

    def list_sessions(self):
        return [dict(session) for session in self.sessions]


class SessionOrderingTests(unittest.TestCase):
    def test_last_activity_beats_file_mtime_and_subagents_stay_with_parent(self):
        sessions = [
            {
                "file": "old",
                "title": "old session with recently touched bookkeeping",
                "last_ts": "2026-01-01T00:00:00Z",
                "mtime": 9_999_999_999,
            },
            {
                "file": "new",
                "title": "new session",
                "last_ts": "2026-02-01T00:00:00Z",
                "mtime": 1,
            },
            {
                "file": "child",
                "title": "new session child",
                "last_ts": "2025-01-01T00:00:00Z",
                "mtime": 0,
                "is_subagent": True,
                "parent_file": "new",
            },
        ]
        with (
            mock.patch.object(server, "PARSERS", {"fixture": _FixtureParser(sessions)}),
            mock.patch.object(server, "load_summary_caches"),
            mock.patch.object(server, "save_summary_caches"),
            mock.patch.object(server, "_apply_custom_name"),
        ):
            ordered = server.list_sessions()

        self.assertEqual([session["file"] for session in ordered], ["new", "child", "old"])

    def test_invalid_or_missing_activity_falls_back_to_mtime(self):
        self.assertEqual(server._recency({"last_ts": "not-a-date", "mtime": 42}), 42)
        self.assertEqual(server._recency({"mtime": 17}), 17)


if __name__ == "__main__":
    unittest.main()
