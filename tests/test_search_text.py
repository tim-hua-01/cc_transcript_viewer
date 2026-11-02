"""search_text: chunked normalization and snippets match whole-text behavior."""
from __future__ import annotations

import random
import unittest

import common
import search_text

# Every kind of input normalization changes the length of, splits on, or folds.
ALPHABET = list("abc xyz") * 6 + [
    "\n", "\n\n", "  \n ", "\t", "’", "“", "—", "­", "​", "ﬁ", "ﬃ", "Ａ",
    "é", "é", "😀", " ", "Σ",
]


def reference_snippet(text: str, normalized: str, index: int, length: int) -> tuple[str, int]:
    """The whole-text snippet mapping search used before chunking."""
    if len(text) == len(normalized) and text.lower() == normalized:
        idx, end = index, index + length
    else:
        idx, end = search_text.source_span(text, index, length)
    return search_text.format_snippet(text, idx, end), idx


class ChunkedSegmentTests(unittest.TestCase):
    def test_chunks_normalize_to_exactly_the_whole_text(self):
        rng = random.Random(0)
        for _ in range(2000):
            text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, 300)))
            size = rng.choice([1, 16, 64, 400])
            with self.subTest(text=text, size=size):
                segment = search_text.build_segment(text, size)
                self.assertEqual(segment.normalized,
                                 common.normalize_search_text(text).encode("utf-8"))
                self.assertEqual("".join(c.text for c in segment.chunks), text)

    def test_chunks_end_at_newlines_and_long_lines_stay_whole(self):
        text = "a" * 10 + "\n" + "b" * 50 + "\n" + "c"
        chunks = search_text.build_segment(text, 8).chunks
        self.assertEqual([c.text for c in chunks], ["a" * 10 + "\n", "b" * 50 + "\n", "c"])
        self.assertEqual([c.src_start for c in chunks], [0, 11, 62])

    def test_segment_snippets_match_the_whole_text_snippet(self):
        rng = random.Random(1)
        for _ in range(3000):
            text = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 400)))
            normalized = common.normalize_search_text(text)
            if len(normalized) < 2:
                continue
            start = rng.randrange(len(normalized))
            query = normalized[start:start + rng.randint(1, 8)].strip()
            if not query:
                continue
            index = normalized.find(query)
            segment = search_text.build_segment(text, rng.choice([64, 80, 128, 400]))
            byte_index = len(normalized[:index].encode("utf-8"))
            with self.subTest(text=text, query=query):
                self.assertEqual(
                    search_text.segment_snippet(list(segment.chunks), segment.normalized,
                                                byte_index, len(query)),
                    reference_snippet(text, normalized, index, len(query)),
                )


if __name__ == "__main__":
    unittest.main()
