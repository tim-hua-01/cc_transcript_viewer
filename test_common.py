#!/usr/bin/env python3
"""Unit tests for the helpers in common.py."""

from __future__ import annotations

import unittest

import common


class ShortTitleTests(unittest.TestCase):
    def test_collapses_whitespace_and_truncates(self):
        text = "  a   very\n\nspaced   " + "x" * 200
        out = common.short_title(text)
        self.assertTrue(out.startswith("a very spaced"))
        self.assertTrue(out.endswith("…"))
        self.assertEqual(len(out), 101)

    def test_short_text_untouched(self):
        self.assertEqual(common.short_title("hello"), "hello")

if __name__ == "__main__":
    unittest.main()
