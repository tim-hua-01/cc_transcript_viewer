#!/usr/bin/env python3
"""Focused unit tests for the helpers in common.py."""

from __future__ import annotations

import json
import random
import re
import unicodedata
import unittest
from pathlib import Path

import common


class SearchNormalizationTests(unittest.TestCase):
    def test_search_equivalences_and_literal_code_punctuation(self):
        cases = json.loads((Path(__file__).parent / "fixtures/search_normalization.json").read_text())
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(common.normalize_search_text(source), expected)
                self.assertEqual(common.normalize_search_text(expected), expected)

    def test_matches_the_straightforward_translate_implementation(self):
        # The regex-gated translation is an optimization; its output must
        # match plain str.translate exactly, on every kind of input it folds.
        def reference(text):
            text = unicodedata.normalize("NFKC", text.translate(common._SEARCH_TRANSLATION))
            return re.sub(r"\s+", " ", text.translate(common._SEARCH_TRANSLATION).lower()).strip()

        alphabet = (
            [chr(c) for c in common._SEARCH_TRANSLATION]
            + list("aZ9 _-'\"\t\n\r\u00a0\u2003\u3000")
            + ["\u0301", "\u0308", "e\u0301", "\ufb01", "\uff21", "\u2460", "\u00bd",
               "\u212b", "\u03a3", "\u0130", "\u00df", "\U0001f600", "\u2018\u0301"]
        )
        rng = random.Random(0)
        for _ in range(2000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
            with self.subTest(text=text):
                self.assertEqual(common.normalize_search_text(text), reference(text))


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
