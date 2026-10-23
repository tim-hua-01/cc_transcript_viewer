# Performance: transcript loading and search

Status: in progress, on branch `perf/transcript-loading`. Measured 2026-10-23 on
one machine: 263 sessions, about 1.1 GB of Claude Code and 0.5 GB of Codex JSONL,
including 133 MB, 121 MB and 104 MB single transcripts.

## Goals

- Opening a large transcript should feel immediate: well under a second for the
  largest ones, including the browser's parse and render.
- The first content search after startup should take seconds, not tens of
  seconds. Repeat searches stay at or below today's 0.05–0.4 s.
- The search index should take a fraction of its current memory. Target: under
  200 MB resident for this corpus (today it adds 870 MB).
- Keep the project zero-dependency (stdlib Python and plain browser JS).
- Keep search semantics exactly as they are: normalized substring match
  (`common.normalize_search_text`, mirrored in `app.js`), the same score tiers
  (custom title, native title, user text, everything else), and snippets cut
  from the source text with its original typography.

## Non-goals (for now)

- An on-disk full-text index such as SQLite FTS5 with the trigram tokenizer. The
  sqlite3 module here supports it, and it's the likely end state. But it changes
  ranking, needs a fallback for queries under 3 characters, and is a large
  change. The steps below build toward it: search text gets one storage type
  with a narrow interface, which could later be backed by disk.
- Incremental parsing of live transcripts (parsing only the appended tail).
- Virtualized rendering in the browser. It isn't needed at current sizes (see
  below).

## How it was measured

`scripts/bench.py` runs each scenario in a fresh interpreter, using a copy of
the summary cache (never the real file):

- `list`: `list_sessions()` with no summary cache (cold) and with the real one
  (warm), per source.
- `open`: `load_session()` plus `json.dumps` for the N largest transcripts.
- `search`: three queries twice each, after a warm session list, and the
  resulting size of the search text cache.

Browser timings came from a throwaway harness: headless Chrome against the real
server, with a probe injected into `index.html` that times the `/api/session`
fetch, the JSON parse, and the end of rendering. If browser timings become a
regular need, it can become a `browser` scenario in `bench.py`.

## Baseline

Before this branch (`main`), opening the 133 MB transcript in Chrome took
**12.8 s**: a 131 MB response, 11 s of body download and `JSON.parse`, and 820
raw JSON cards. Most of that file was Claude Code's new `api-request*` log
records. Commit `373ab19` skips them, which brings the same open to **0.87 s**.
The numbers below are after that commit.

| Path | Measurement |
|---|---|
| Session list, warm (what the 1 s sidebar poll pays) | 10–15 ms, 0.26 MB JSON |
| Session list, cold (summary cache lost) | 1.9 s (Codex 1.3 s serial, Claude 0.6 s in a process pool); RSS stays 325 MB higher afterwards |
| Open 133 MB Claude transcript | server 0.1 s parse + 0.07 s dumps; **60 MB** response; browser 0.87 s to rendered, 163 MB JS heap |
| Open 121 MB / 85 MB Claude transcripts | 7–8 MB responses; browser 0.4–0.6 s |
| Open 104 MB Codex rollout | server 0.42 s; 16 MB response; browser 2.0 s |
| First search (cold) | **15.2 s**; **+870 MB RSS** |
| Repeat searches | 0.05–0.4 s |
| Search text cache | 263 sessions, 220 M characters, **472 MB** of Python strings |

## Findings

1. **`result.structured` is shipped but never read.** Each Claude tool result
   carries the raw `toolUseResult` record. No frontend, export or search code
   reads it, and it has been like that since the first commit. For a `Read` of an
   image it holds a second base64 copy of the image (25 MB of the 60 MB
   response above), and for edits it holds the whole original file.
2. **Normalization is half of the cold search.** In a profile of the cold
   search, `str.translate` took 7.3 s, the whitespace regex 1.4 s and NFKC 0.6 s.
   `translate` with a dict table does a lookup for every character. Almost no
   character needs mapping, so finding the rare ones with a compiled regex
   character class first is 4× faster with identical output (prototype: 1.95 s
   down to 0.48 s on 23 M characters).
3. **The search cache stores everything twice, at up to 4 bytes per character.**
   Each entry keeps the source text and its normalized form for both the
   user-text and other-text segments. Python stores a string at the width of
   its widest character, so a single emoji in a 50 MB tool dump makes that
   whole string 4 bytes per character (34 of the cached strings are like this).
4. **Snippet mapping re-normalizes everything before the match.**
   `_search_snippet` walks the source lines from the start of the segment until
   it reaches the match, normalizing each line. That's cheap for an early match
   and costly for a rare word at the end of a 50 MB session.
5. **Search builds its text from a full parse.** The cold path calls
   `load_session()`, which builds images, branch folding and Codex's JS-literal
   masking (3.5 s of the profile), only to keep the text. A text-only
   extraction would be faster, but it risks drifting from what the parsers
   consider searchable. Deferred: revisit after 2–3 are done.
6. **The search cache never forgets.** Sessions that are deleted or moved
   stay in `_TEXT_CACHE` for the life of the server.
7. **Stale searches run to completion.** The frontend ignores superseded
   responses, but the server still finishes every scan, and concurrent scans
   compete for the GIL. This matters only while the cache is cold.

Not problems right now: the warm session list, server-side parse time, and
browser rendering (0.4–2 s for the largest transcripts, with batched rendering
already in place).

## Plan

One small, separately tested commit per step, with a `bench.py` run before
and after. Steps 1–3 are independent; 4 depends on 3.

### 1. Stop shipping `result.structured` (Finding 1) — done, `fa0bf28`

Result: the 133 MB transcript's response went from 60 MB to 33 MB, and the
121 MB one's from 7.1 MB to 4.2 MB.

Remove the field from `claude_parser.parse_session`'s tool-result dicts.
Expected: the 133 MB transcript's response goes from 60 MB to about 35 MB, with
the matching cut in browser parse time and JS heap. Risk: something reads it
that grep missed. Check: the frontend, export and search code have no reference
to it, and the full suite and browser tests pass.

### 2. Faster `normalize_search_text` (Finding 2) — done, `2a3622a`

Result: the cold first search went from 15.2 s to 8.8 s; repeat searches are
unchanged.

Replace both `translate` calls with a compiled character-class regex whose
replacement function looks up the same `_SEARCH_TRANSLATION` table, so there's
still a single source of truth. Output must be identical:

- the existing `search_normalization.json` cases (shared with `app.js`) keep
  passing;
- a new equivalence test compares against the current implementation, kept in
  the test as a reference, on random strings drawn from the mapped characters,
  combining marks, compatibility forms, emoji and whitespace.

Expected: cold search roughly 15 s to 9 s.

### 3. A compact search-text store (Findings 3, 4, 6)

A new module `search_text.py` with one immutable type and no server imports,
so it can be unit-tested on its own:

```python
class SearchText:
    """One segment of a session's searchable text, stored compactly."""
    @classmethod
    def build(cls, source: str) -> "SearchText": ...
    def count(self, needle: bytes) -> int: ...       # needle = normalized query, UTF-8
    def find(self, needle: bytes) -> int: ...
    def snippet(self, offset: int, length: int) -> tuple[str, int]: ...
```

Storage:

- **Normalized text as UTF-8 `bytes`.** Substring search on UTF-8 bytes gives
  the same matches as on `str`, because UTF-8 is self-synchronizing.
  `bytes.count` and `bytes.find` are as fast as the `str` versions. This
  removes the 4-bytes-per-character problem: ASCII-heavy text costs 1 byte per
  character.
- **Source text in zlib-compressed chunks** of about 256 KB, split at line
  boundaries. Each chunk records the normalized offset where it starts.
  Normalizing chunk by chunk and joining with a space gives exactly the
  whole-text normalization. The current snippet code already relies on this
  property (line boundaries are safe normalization boundaries). Building this
  way also lowers peak memory, since no full-size temporary copies are made.
- **Snippets** find the chunk by binary search on its normalized start offset,
  decompress only that chunk (plus the next one if the snippet crosses into
  it), and run the existing line walk inside it. The cost depends on chunk
  size, not on where the match is.

`server.session_segments` keeps its role and its mtime-keyed cache, but stores
two `SearchText` values per session. `search_sessions` keeps its scoring and
calls `count`, `find` and `snippet`. Titles stay as plain strings; they're
tiny. Cache entries for sessions no longer listed are dropped at the end of
each search.

Expected memory, from the measured 220 M characters (half source, half
normalized): about 115 MB of normalized bytes plus about 25 MB of compressed
source, roughly **140 MB instead of 472 MB**. RSS after a cold search will
still be higher, because Python keeps freed memory from the parse in its pools.

Tests: the existing `test_search.py` suite unchanged; `SearchText` unit tests
for count and find against the `str` behaviour; and a snippet-equivalence test
against the current `_search_snippet` across chunk boundaries,
length-changing normalizations, and matches in the first and last chunk.

### 4. Background warm-up and superseded-search cancellation (Finding 7)

Once step 3 makes the index cheap to keep:

- After the first `/api/sessions`, a daemon thread fills the search cache one
  session at a time, so the first search usually finds it warm.
- Each `/api/search` takes a sequence number. The scan loop checks it between
  sessions and returns early when a newer search has started (the frontend
  already discards that response). Warm-up and search share the cache. The
  existing per-session mtime check keeps them consistent, and a lock around
  filling each entry stops two threads from building the same session twice.

### Later, decided after the steps above are measured

- **Persist the search store** under `~/.cache/transcript_viewer/search/`, one
  file per session keyed by its fingerprint, so a restart doesn't re-parse
  everything. This is the stepping stone to a disk-backed index or FTS5.
- **A text-only extraction path** shared with the parsers (Finding 5), for
  example a per-parser `iter_search_text()` generator.
- **Cheaper live refresh** for large active transcripts. Today every change
  re-fetches and re-renders the whole session; the fix is an events-since
  endpoint.
- **Cold session list**: run Codex summaries through the same process pool
  Claude already uses, and stream records instead of `list(iter_jsonl(...))`
  to lower peak memory.
- **Images outside the JSON**: once step 1 is done, data-URI images are the
  bulk of the largest responses. Serving them from an endpoint would shrink
  the response further, but the export needs them inline, and Claude's inline
  base64 images have no file to serve. Only worth doing if browser timings
  call for it.
