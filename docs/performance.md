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
  (warm). Per-source first calls run in a separate interpreter so they do not
  warm the unified list measurement. JSON serialization is timed on the
  already-fetched session list, without another scan.
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

Each step is a separately tested commit (step 3 is one larger commit), with a
`bench.py` run before and after.

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

### 3. A persistent search index (Findings 3–7) — done

Result, on 333 sessions (about 1.6 GB of transcripts):

| | Before | After |
|---|---|---|
| First search after a server start | 15 s to over a minute, nothing on screen | answers at once from the index; a status line shows progress while it is built |
| Building the index from nothing | (rebuilt in every server process) | 6–8 s with 4 workers, once; a restart reopens it in 0.02 s |
| Repeat searches | 0.05–0.4 s | 0.07–0.3 s, snippets included |
| Server memory for search | 472 MB cache, +870 MB RSS | +23 MB during a full build, about 100 MB total after searches |
| Disk | — | 275 MB (`search-index.sqlite3`, mode `0600`) |

Two adjustments from the design after measuring. Workers store their own
documents instead of sending them back to the coordinator; sending them back
left the server 186 MB larger, because the allocator holds on to memory
freed in pieces the size of a 64 KB chunk. And filtering rows with SQLite's
`instr()` before they reach Python was slower than Python's `bytes.count`, so
search reads every row.

Still open: a cold *session list* (summary cache lost) parses Codex
transcripts inside the server process and leaves it a few hundred MB larger.
That is the "cold session list" item under Later.

This replaces the earlier plan of a compact in-memory store plus background
warm-up. The in-memory cache was never designed: it is a dict in `server.py`
that fills up during the first search in each server process. As a result:

- every restart throws it away;
- the rebuild (10 s to over a minute) runs inside the user's search request,
  with nothing on screen to say so;
- each search typed while it's cold starts its own rebuild, and they compete;
- it holds hundreds of MB, and parsing in the server process leaves Python's
  memory pools grown even after the text is freed;
- while there's a query in the box, every sidebar change re-runs the search,
  which re-parses any live transcript inside the request.

A prototype answers the memory question. With every session's normalized text
in SQLite and nothing kept in memory, a search that reads all rows takes
0.03–0.06 s on this corpus (faster than today's in-memory path), and the
process peaks under 80 MB. The OS file cache keeps the file warm, and that
memory belongs to the OS, which reclaims it when other apps need it.

#### Modules

```
server.py ──(sessions, load_session, source settings)──► search_index.py ──► search_text.py
 HTTP + wiring                                           storage, indexer,      pure: extract,
                                                         status, scoring        normalize, chunk, snippet
```

- **`search_text.py`**: pure functions, no I/O. It extracts the searchable
  text from a parsed session (moved from `server.py`). It builds a
  `SearchDoc` per segment: the normalized text as UTF-8 bytes, plus the
  source text split at line boundaries into chunks of about 256 KB, each
  recording the byte offset in the normalized text where it starts.
  Normalizing chunk by chunk and joining with a space gives exactly the
  whole-text normalization, because line boundaries are safe normalization
  boundaries (the existing snippet code already relies on this). It also
  maps a normalized match to a snippet using a single chunk, so the cost
  doesn't depend on where in a 50 MB session the match is.
- **`search_index.py`**: the `SearchIndex` class, which owns storage,
  indexing, status and scoring. It never imports `server`; the server passes
  in a `load_session` function and its source settings.
- **`server.py`**: creates the index, tells it about each fresh session list,
  and routes `/api/search` to it.

#### Storage

One SQLite file, `~/.cache/transcript_viewer/search-index.sqlite3` (set with
`--search-index`), next to the summary cache:

- `docs(file, fingerprint, user_norm, rest_norm)`: the normalized text a
  search scans;
- `chunks(file, segment, norm_start, source)`: source text, read only to
  build snippets for matching sessions;
- `meta`: schema and extractor versions.

It uses write-ahead logging, so searches read while the indexer writes. The
file is created with mode `0600` (it holds transcript text). A session's
fingerprint is the summary's `mtime`, the same key the old cache used.
Changing what gets extracted bumps `EXTRACTOR_VERSION`, which clears the
index. A database that can't be opened is deleted and rebuilt rather than
breaking search. The source text is stored uncompressed: disk is cheap here,
and it keeps the code simple. Expected size: about 250 MB for this corpus.

`python3 server.py --reset-cache` deletes the search index and the summary
cache, then exits; both rebuild on the next start.

#### Indexing

- One coordinator thread in the server is the only writer. It sleeps until
  `notify(sessions)` reports sessions whose fingerprint doesn't match the
  index, so there's no timer and no cost while nothing changes.
- Parsing and extraction run in worker processes (up to 4, using the spawn
  start method), so neither the parse's CPU time nor its memory lands in the
  server process. The pool exists only while there's work. Workers get the
  server's source settings (`--projects-dir`, `--codex-home`, Cursor,
  opencode, Mica), because a spawned process doesn't inherit them. If the pool
  can't start, the coordinator extracts in-process instead.
- Most recently active sessions go first. A transcript that is still being
  written is re-indexed at most every 30 s, unless a search is waiting.
  Sessions no longer listed are removed from the index.

#### Searching and status

- `search(query, sessions)` streams `docs` rows, scores them with the existing
  tiers (custom title, native title, user text, everything else), and builds
  snippets from `chunks`. It answers from whatever is indexed, never waiting
  for the indexer. `search(…, wait=True)` brings the index up to date
  first; tests and scripts use it.
- A search that a newer search has replaced stops early.
- `status()` returns `{state, indexed, total, pending, generation, error}`.
  `generation` changes whenever indexed content changes. The status rides on
  both `/api/search` and the existing `/api/sessions` poll, so it adds no
  requests.

#### UI

A status line under the search box (`aria-live="polite"`):

- while indexing, with or without a query: "Indexing transcripts for search:
  140 of 263", with a thin progress bar;
- "Searching…" while a request is running;
- "12 sessions match", plus "· still indexing, more may appear" while the
  index is incomplete;
- "Search index error: …".

The frontend re-runs an active search only when the index `generation` or the
session titles change, not on every sidebar change, so a query left in the box
costs nothing while transcripts are being written.

#### Tests

- `search_text`: extraction (moved tests), normalization of the chunks equals
  normalization of the whole text, and snippets match the old `_search_snippet`
  across chunk boundaries and length-changing normalization.
- `search_index`: survives a reopen; fingerprint and extractor-version
  invalidation; pruning; status counts; partial results while indexing;
  superseded searches; file mode; recovery from a corrupt file; in-process
  fallback when no worker can start.
- Existing search and server tests keep their assertions and use
  `wait=True`. Test servers point the index at a temp file, like the summary
  cache.
- A browser test for the status line.

### Later, decided after the steps above are measured

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

## Battery: idle cost

With nothing happening, an open viewer tab still polls `/api/sessions` every
second and `/api/session-state` every 300 ms, in the foreground and in the
background. Measured with one tab open and idle:

| Cost | Measurement |
|---|---|
| Server CPU | 1.6–1.9% of one core, continuously |
| One sidebar poll | 14 ms on the server: about 1,600 `stat` calls, 200 directory listings, and Mica's `index()` called about 560 times, each with its own `stat` of `index.json`; then a 312 KB JSON response that the browser parses |
| One transcript poll | about 1 ms; cheap, but it wakes the browser and server 3.3 times a second |
| Wakeups | about 4.3 a second, which keeps the CPU out of its deeper idle states |
| Browser | no endless CSS animations or timers apart from the polls, and a DOM re-render only when the list changes (headless Chrome's own overhead hides the page's share, so it wasn't measurable that way) |

The Mica daemon is separate: it runs whether or not the viewer is open, polls
every second, and has averaged about 0.2% CPU (31 CPU-minutes over 10 days).

### B1. Don't poll while the tab is hidden — done, `662f3c6`

Stop both pollers while `document.hidden` is true, and poll at once when the tab
becomes visible again, so what you see is never stale. Chrome only slows timers
in background tabs to once a second, and to once a minute after five minutes,
so today a forgotten tab keeps polling. This doesn't cost any responsiveness.

### B2. Slow down when idle — done, `662f3c6`

Result (B1 + B2): with one idle tab, server CPU went from 1.8% to 0.7%, and
requests from 4.3 a second to 0.7; a hidden tab makes none.

Both pollers keep their current intervals while the viewer is active. "Active"
means the last user input (pointer, keyboard, wheel, focus) or the last change
to the open transcript was within 60 s. Changes to other sessions don't count:
some agent is nearly always writing a transcript somewhere, so counting them
would keep the viewer fast almost all the time. After that the sidebar polls every 5 s and the open
transcript every 2 s. Any input or change switches back to the fast intervals
immediately, and input after an idle period also triggers an immediate poll.

The cost: while idle, a new session takes up to 5 s to appear in the sidebar,
and the first new line of a live transcript up to 2 s. A transcript Claude is
actively writing changes often, so once that first change arrives the poll
stays fast and tailing works as it does today.

One scheduler (a `setTimeout` chain per poller) replaces the two
`setInterval`s. Each poll function returns whether it saw a change.

### B3. Check Mica's index at most every 250 ms — done, `eceb33d`

Result: smaller than estimated. A poll went from 12.4 ms to 11.5 ms; the
remaining Mica cost is in the per-session lookups, not the `stat`.

`StoreReader.index()` re-`stat`s `index.json` on every call to see whether it
changed, and a sidebar poll calls it once per session. Skip the `stat` when the
last check was under 250 ms ago. Mica rewrites the index at most once a second,
and a quarter-second delay can't be seen in the UI. Expected: about 30% less
server time per poll.

### Considered, not planned

- **A `304 Not Modified` response for an unchanged session list.** It would
  save sending and parsing 312 KB per poll. But the response includes Mica's
  heartbeat age, which changes every second, so it would almost never match
  unless the status moved to its own endpoint. Revisit if B1–B2 aren't enough.
- **Slowing Mica's own 1 s poll.** That interval is how long a transcript
  rewrite can go uncaptured, which is a deliberate choice for a
  tamper-evidence tool. It's the user's call, not a performance fix.

## Structure follow-ups (from review)

Three changes to how the search work is organized. None changes what search
returns. Each is its own commit.

### S1. Worker crashes are retried, not permanent — done, `7208044`

Today any failure of the worker pool sets `_pool_broken` for the life of the
server, and every later session is parsed inside the server process. That
brings back the memory growth the workers exist to avoid, even when the cause
was one worker killed while parsing a huge transcript.

Two kinds of failure, handled differently:

- **The pool can't work here at all.** The loader can't be pickled or
  imported by a worker, or a pool can't be created. Retrying won't help, so
  fall back to in-process extraction for good, as today.
- **A worker died** (`BrokenProcessPool`). Stop the pool and retry with a
  fresh one after 5 s, then 10 s, then 20 s. Don't index in-process during the
  wait, and say so in `status()["error"]`. A pool that completes a session
  resets the count. After three crashes in a row, fall back to in-process
  extraction so that search still works. That is the old behaviour, now only a
  last resort.

Lifecycle tests: a worker that crashes once (search recovers on a new pool, no
fallback); workers that always crash (fallback after three, still indexes);
the idle pool shutting down; and `close()` while a session is being indexed.

### S2. One owner for search in the frontend — done, `f131dda`

Search state is spread over seven globals (`CONTENT_MATCHES`, `SEARCH_STATUS`,
`SEARCH_IN_FLIGHT`, `LAST_SEARCH_KEY`, `searchSeq`, `searchFailed`,
`searchTimer`) and five functions. Both race conditions fixed so far came from
that spread. A `createContentSearch({request, sessions, onChange})` factory
in `app.js` returns the one object that owns:

- the query and the debounce;
- request ordering (a response for anything but the latest request is
  dropped) and cancellation by another tab;
- the results (`matches`, `has(file)`, `get(file)`) and the index status;
- freshness: the key (query, index generation, titles) recorded when a
  request starts, and `refreshIfStale()`;
- `statusText(visibleCount)`, which returns what the status line says. The
  DOM stays in `renderSearchStatus`.

It stays inside `app.js`, because the HTML export inlines that one file. The
request function and the session list are passed in, so browser tests can
drive a controller in isolation with a scripted server.

### S3. A session catalog module — done

`server.py` mixes the HTTP layer with source configuration, the unified
session list, loading, custom names, the summary-cache file, and Mica
decoration. A new `catalog.py` takes those. `server.py` keeps HTTP, the
local-file actions, the prompt export, and search wiring. Every fresh list
still informs the index, through a thin `list_sessions()` wrapper in the
server.

The concrete gain: search workers import `catalog` to load sessions, not the
whole HTTP server, and the index's `worker_setup` comes from the catalog.
This is a pure move. Behaviour and tests stay the same, with test patches
pointed at `catalog.*` where the state now lives.

## Sidebar summaries: read only what's new

Each sidebar row (a session's *summary*: title, counts, timestamps, model) is
built by reading the whole transcript, and `summaries.json` caches it by the
file's size and modification time. Two cases still cost a lot:

- **A transcript being written.** Each append changes the file, so every
  sidebar poll (once a second while you're active) re-reads the whole file
  in the server process: 0.32 s for a 104 MB Codex rollout, 0.13 s for a
  159 MB Claude transcript. That's about a third of a core for one row while
  a big session runs.
- **A missing cache** (first run, `--reset-cache`, a format change). Codex
  summaries are rebuilt one by one inside the server, holding each file's
  parsed records at once. The server stayed at 531 MB after one rebuild
  (about 50 MB normally).

### R1. Feed summaries only the appended lines

Both summaries can be built one record at a time. Every field is the latest
value seen, a running count, or the first match (the title). The one
exception is Codex's de-duplication of messages written in both the old and
the new format, which today pre-scans the whole file. Done record by record,
it keeps counters of both copies' message keys (hashed) and subtracts the
overlap at the end, which gives the same count regardless of order.

`common.JsonlAccumulators` keeps one accumulator per file, plus how far into
the file it has read:

- it reads from that point on, and only complete lines (a half-written last
  line waits for the next read);
- it starts again from the top if the file shrank, or if its first 4 KB or the
  4 KB just before the stopping point changed (a rewrite);
- it reads line by line, so even a full read never holds a whole file's
  records in memory.

Each parser supplies an accumulator class with `feed(record)` and a function
that turns the result into a summary. The existing `SummaryCache` (and
`summaries.json`) stays in front of it unchanged: an unchanged file never
reaches the accumulator. Accumulators are memory-only. After a restart, a
changed file is read in full once.

### R2. Rebuild many summaries in worker processes, for every source

Claude already hands 32 or more stale summaries to worker processes. A shared
`common.prefill_summaries(cache, jobs, worker)` does that for Claude and
Codex. Codex passes each file's SQLite thread row along, because the summary
depends on it. It uses up to 4 workers (Claude used up to 8), matching the
search index. Fewer stale files are summarized in-process, which R1 makes
cheap.

### Tests

- The accumulator: only appended lines are fed, a partial last line waits,
  and a truncated or rewritten file is read again from the top.
- For both parsers, a summary built from appends after every step equals one
  built by reading the whole file, on the checked-in fixture transcripts.
- Codex's de-duplication when a message's two copies arrive in different
  appends.
- A cold Codex rebuild goes through worker processes.
