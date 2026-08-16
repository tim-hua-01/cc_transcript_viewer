# Claude Code, Codex, Cursor, and opencode Transcript Viewer

A tiny, **zero-dependency** local web app for browsing your local coding-agent transcripts —
**Claude Code, Codex, Cursor, and opencode together** in one time-sorted view.

It reads:

- Claude Code transcripts from `~/.claude/projects/`
- Codex transcripts from `~/.codex/sessions/` and `~/.codex/archived_sessions/`
- Cursor IDE conversations from Cursor's store at
  `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`
- Cursor CLI / agent transcripts from
  `~/.cursor/chats/<workspace-hash>/<session>/store.db` (full fidelity), with a
  lossy JSONL fallback under `~/.cursor/projects/<project>/agent-transcripts/`
- opencode sessions from its SQLite database at `~/.local/share/opencode/opencode.db`

Nothing is uploaded anywhere. Transcript sources are opened read-only; the only write is the
viewer-owned custom-names file. The app listens on loopback only, contains no outbound-network code,
and has a [test suite](#privacy--security) that verifies those guarantees.

> [!WARNING]
> **This is vibe-coded.** It was built quickly and iteratively with an AI coding agent, so expect
> rough edges and the occasional rendering bug. It also depends on the *current* on-disk transcript
> formats of Claude Code, Codex, Cursor, and opencode — if any tool changes how it saves sessions, parts of the
> viewer may silently break or drop records until the parser is updated.

## Run it

```bash
python3 server.py
```

Then open the printed URL (default **http://127.0.0.1:3132/**).

Options:

```bash
python3 server.py --port 8080            # use a different port
python3 server.py --host 0.0.0.0         # listen on all interfaces (LAN access — opt-in)
python3 server.py --projects-dir PATH    # different Claude Code projects directory
python3 server.py --codex-home PATH      # different Codex home (default ~/.codex)
python3 server.py --cursor-db PATH       # different Cursor state.vscdb (or its Cursor app-support dir)
python3 server.py --cursor-chats-dir PATH     # different ~/.cursor/chats (CLI store.db sessions)
python3 server.py --cursor-projects-dir PATH  # different ~/.cursor/projects (CLI JSONL fallback)
python3 server.py --opencode-db PATH     # different opencode.db (or the opencode data dir holding it)
python3 server.py --custom-names-file PATH # different custom transcript names file
```

By default the server binds **127.0.0.1** (loopback only), so it isn't reachable from other machines
unless you deliberately pass `--host 0.0.0.0`.

The viewer **auto-refreshes**: it checks the open on-disk transcript about three times a second and
scans for sidebar changes once a second, preserving your scroll position and which
thinking/tool blocks you've expanded. So an active session you're watching tails live. Reload the
page if you ever need a full reset. Session metadata is cached by file mtime, so each poll only
re-reads the files that actually changed — the cost doesn't grow with how many transcripts you've
accumulated.

## Features

- **One sidebar, sorted by time.** Every Claude Code, Codex, Cursor, and opencode session in a single
  flat list, newest (most recently modified) first — no per-project grouping. Each entry shows an
  **agent tag** (Claude / Codex / Cursor / opencode), a **CLI** badge for Cursor command-line agent
  transcripts, the project
  path, recency, message/tool/web counts, model, and the full session **id** (click to copy).
- **Readable and custom titles.** Each session is titled with the first ~100 characters of its
  first user message unless the source has an explicit title. Claude Code `/rename`/`--name` titles
  are respected, and distinct branch/team agent names appear as badges. Use the edit button beside
  an open transcript's title to give it a viewer-specific name; clearing it restores the source
  title. Overrides are stored separately in `~/.config/cc_transcript_viewer/names.json`, leaving the
  agent-owned transcripts and Cursor database untouched.
- **Search across everything.** The search box (press `/` to focus) matches session **content** —
  prompts, replies, reasoning, tool commands/paths/queries/outputs — in addition to titles and
  directories, and shows a snippet of the match. Viewer custom-title matches receive `10,000×`
  weight, native Claude/Cursor titles receive `5,000×`, every user-message match receives `50×`,
  and ordinary transcript-content matches receive `1×`. Powered by `/api/search` with an
  mtime-keyed cache.
- **Filters that compose.**
  - **All / Claude / Codex / Cursor / opencode** chips.
  - A **Model** dropdown grouped by family (Claude / GPT / Other): tick a family to select all its
    models, or pick individual ones (e.g. only Sonnet). The family box shows an indeterminate state
    on partial selection.
  - A **Directory** dropdown to narrow to specific project paths.
- **Transcript view** — a chronological, color-coded conversation that renders all three agents'
  formats:
  - User prompts and assistant replies as Markdown, **with LaTeX math** (KaTeX) and inline images.
  - **Thinking / reasoning** blocks (collapsed by default).
  - **Tool calls** with tool-specific formatting and results paired inline:
    - Claude Code: `Bash`, `Read`, `Edit`/`Write`/`MultiEdit` (colorized diffs), `Grep`/`Glob`,
      `Task`/`Agent` (sub-agent turns flagged), `TodoWrite`, and pretty-printed JSON for the rest.
    - Codex: `function_call`, `custom_tool_call`, `apply_patch` (rendered as a colored diff),
      `exec_command`/`shell`, `write_stdin`, `view_image`, web search, plus fallback JSON. Newer
      orchestration-style `exec` calls are unpacked into their underlying commands or patches, with
      the generated JavaScript kept in a secondary disclosure. Wrapper results are reduced to actual
      stdout plus exit/session/timing metadata.
    - Cursor IDE: tool calls **with their outputs** — `run_terminal_command` (stdout + exit code),
      `read_file` (contents), `edit_file` (reconstructed before/after diff), `ripgrep`, `glob`,
      `read_lints`, `todo_write`, `delete_file`, semantic/web search. Names/inputs are normalized onto
      the same renderers as Claude Code, so a Cursor `edit_file` shows the same colorized diff.
    - Cursor CLI: same tool renderers; results come from per-chat `store.db` when
      available. JSONL-only fallbacks omit tool outputs.
    - opencode: `bash`, `read`, `edit`, `write`, `grep`, `glob`, `webfetch`, `task`, `todowrite`,
      `skill`, `lsp`, `apply_patch`. opencode's camelCase arguments (`filePath`, `oldString`) are
      renamed server-side onto the same renderers, and a `task` call links straight through to the
      sub-agent session it spawned.
  - **Copy all text** copies user/assistant messages, reasoning, and tool calls (including diffs),
    but skips tool results and system/notice noise.
  - Codex task/context/token bookkeeping consolidated into one **Turn metadata** disclosure at the
    end of the final assistant response.
- **Survives `/compact`.** After a conversation is compacted, the harness injects bookkeeping records
  that most viewers drop. This one renders them: 📎 *referenced-file* pointers (including the note the
  model is actually handed when its context is rebuilt), 📄 re-attached file reads with their full
  content, and tool-set changes (`+N tools available via ToolSearch`). Compaction boundaries show
  their trigger, before/after token counts, duration, and preserved message/tool counts. Codex
  boundaries show the context-window number, replacement-item count, and whether the replacement
  summary is encrypted and therefore unreadable from the transcript.
- **Pull-request links.** Claude Code sessions associated with a PR show a safe, clickable PR link in
  the transcript header.
- **Local source links.** Absolute Markdown links inside a session's workspace open through macOS
  Launch Services in the file's default application. Line suffixes such as `:42` are recognized and
  removed before opening (the default application decides where to position the document). Targets
  outside the workspace and executable/application files are rejected.
- **Images.** Inline images in prompts/replies and Codex `view_image` are shown. For
  Codex user prompts, the viewer prefers the original `local_images` file and falls back to the
  embedded `input_image` data URL if the file is gone.
- **Sub-agents & guardians.** Claude Code sub-agents (which newer versions write to their own
  `…/<session-id>/subagents/agent-*.jsonl`
  files) and Codex sub-agent sessions are each picked up as their own entry in the same time-sorted
  list, indented and flagged **sub-agent**. A sub-agent is titled `[type] description` when it can be
  matched back to a Claude `Task`/`Agent` call that spawned it (others — e.g. compaction agents —
  fall back to their opening prompt), and its transcript view links **↑ parent session**. Older Claude
  transcripts that inline sub-agent turns as `isSidechain` records still render those turns inline,
  flagged **sub-agent**. Codex approval guardians are linked under their parent session and labeled
  **guardian**. Each received transcript snapshot or delta renders as a user-style review input,
  followed by reasoning and the allow/deny decision; generic task/context/token bookkeeping is
  consolidated into one collapsed metadata block per turn. Cursor CLI sub-agents under
  `agent-transcripts/<id>/subagents/` are listed the same way.
- **Jump between prompts.** A right-side **outline** lists the top-level user messages as truncated,
  clickable headings and highlights the one you're reading as you scroll. Floating **↑ / ↓** buttons
  jump to the previous/next user prompt, and **Jump to end** (in the transcript controls) skips to
  the bottom. Sidebar and outline panels can be collapsed.
- **Twelve themes.** Warm, Paper, Botanical, and Lavender cover the quiet solid palettes; Night is
  the standard dark option. The playful set changes the UI as well as its colors: Sorbet uses soft
  pills, while Terminal uses crisp monospace controls.
  Highlighter and Nineties are the two intentionally odd options, with chunky offset borders and
  classic desktop bevels respectively. System 7 draws from historic Macintosh interfaces, while
  Bauhaus and Art Deco add broader design-history options through geometry and double rules. All
  are static—no theme animations. The choice is stored in the browser on that machine and restored
  before the page paints.
- **Live updates.** The sidebar refreshes about once a second, while an open on-disk transcript is
  checked about three times a second, so an in-progress session tails quickly without disturbing
  your scroll position or your expanded/collapsed blocks.
- **Deep-linkable:** the open session is stored in the URL hash, so you can bookmark/share a link.

## Privacy & security

This app reads your private transcripts, so it's built to keep them on your machine:

- **No outbound network code.** The server imports only Python's standard-library `http.server`
  (inbound), with no HTTP client, sockets, mail, or telemetry anywhere. Nothing is ever uploaded.
- **Pinned, integrity-checked frontend assets.** The browser page loads three libraries from a CDN
  (marked, DOMPurify, KaTeX) for markdown/math rendering. Each is pinned to an exact version with an
  SRI `integrity` hash, so a compromised or changed CDN file is refused by the browser rather than
  executed; a refused file degrades gracefully (plain-text rendering), exactly like being offline.
- **Loopback by default.** It binds `127.0.0.1`; LAN exposure requires an explicit `--host 0.0.0.0`.
- **DNS-rebinding guard.** While bound to loopback, it enforces a `Host`-header allowlist
  (`127.0.0.1` / `localhost`), so a malicious web page that rebinds its domain to `127.0.0.1` can't
  read your transcripts through the browser — its requests still carry `Host: evil.com` and get a
  `403`. (Skipped when you deliberately bind a non-loopback `--host`.)
- **Transcript reads are confined.** `/api/session` only parses files under the transcript roots
  (`~/.claude/projects`, `~/.codex`, `~/.cursor/projects/.../agent-transcripts`), or a Cursor
  conversation addressed by `cursordb:<id>` / `cursorcli:<id>` (read from the local IDE DB or CLI
  `store.db` by id, never an arbitrary path); anything else returns `403`. `/api/local-image` serves only
  image-typed files (it renders images referenced by a transcript, which may live anywhere on disk),
  never arbitrary file contents.
- **Name writes are confined.** `/api/session-name` accepts only JSON, verifies that the referenced
  transcript exists under an allowed source, and writes only the configured custom-names file using
  an atomic replacement.
- **Local opens are confined.** `/api/open-local` accepts only JSON POSTs, resolves the requested
  path inside the selected session's workspace, rejects executables and application-like file types,
  and invokes `/usr/bin/open` directly without a shell. It is unavailable on non-macOS hosts.

These guarantees are enforced by a zero-dependency test suite — run it yourself:

```bash
python3 -m unittest            # everything: security, parsers, schema, caching
python3 -m unittest test_security   # just the security guarantees
```

It spins the server up on loopback, exercises every endpoint with a socket-level guard installed, and
fails if any request dials a non-loopback host; it also forges a foreign `Host` header to confirm the
rebinding guard rejects it, and statically asserts neither module imports a network client and that
the default bind is loopback.

## Notes on Codex transcripts

- **Reasoning summaries.** Codex doesn't save raw chain-of-thought in readable form for OpenAI
  models, but it can save readable summaries when configured. To request them for future sessions,
  add to `~/.codex/config.toml`:

  ```toml
  model_reasoning_summary = "detailed"
  ```

  This doesn't backfill old transcripts. Encrypted-only reasoning records contain no displayable
  text and are omitted; readable duplicates (written as both `event_msg/agent_reasoning` and
  `response_item/reasoning.summary`) are grouped and deduped.
- **Web search results aren't stored.** Codex records only the search *queries* it issued (the viewer
  lists all of them); the fetched pages are sent to the model but never written to the rollout. The
  findings survive only as the assistant's prose, with citation links.
- **Images.** Codex may record a prompt image twice: as an embedded `input_image` data URL in a
  `response_item/message`, and as a path in the corresponding `event_msg/user_message`'s
  `local_images` list. The viewer correlates those records, prefers the original file through
  `/api/local-image`, and falls back to the embedded data URL if the file is gone. `view_image` uses
  the same local-file-first behavior. The parser also reads `~/.codex/state_5.sqlite` for thread
  metadata (cwd, model) when available.

## Notes on Cursor conversations

Cursor has two product surfaces with different canonical stores; the viewer reads both:

- **IDE (`state.vscdb`) — preferred when present.** Full conversations from the Cursor app live in
  `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` (table `cursorDiskKV`).
  This has tool outputs, model, thinking text, timestamps, and AI-generated titles. Sessions are
  addressed as `cursordb:<composerId>`. Empty draft composers (no messages) are hidden. The DB is
  opened read-only and safe while Cursor is running; the session list is cached and re-read only when
  the DB file changes. Override with `--cursor-db` (a `state.vscdb` or a Cursor app-support directory)
  for Linux/Windows or a non-standard install.
- **CLI (`store.db`) — canonical for command-line agent chats.** Each CLI session is a SQLite file at
  `~/.cursor/chats/<md5(cwd)>/<session-uuid>/store.db`, with a sibling `meta.json` (cwd, title).
  Blobs hold full turns including `tool-call` / `tool-result`, reasoning, and model. Sessions are
  addressed as `cursorcli:<sessionId>` and show a **CLI** badge. Override with `--cursor-chats-dir`.
- **CLI JSONL fallback.** The command-line agent also writes a lossy export at
  `~/.cursor/projects/<encoded-cwd>/agent-transcripts/<uuid>/<uuid>.jsonl` (text + tool *calls*
  only). Used only when no `store.db` exists for that UUID. Override with `--cursor-projects-dir`.
- **Dedup.** Same UUID preference order: IDE `state.vscdb` → CLI `store.db` → JSONL.
- **Tool outputs (IDE + CLI store.db).** Terminal stdout, file reads, search hits, edits, etc.
  IDE `edit_file` reconstructs diffs from `composer.content.*` snapshots; CLI `StrReplace` carries
  old/new strings directly in the tool call.
- **Thinking** where recorded (IDE bubble thinking text; CLI `reasoning` blocks — often signature-only).

## Exporting Cursor GPT sessions as Codex rollouts

`cursor_to_codex.py` converts Cursor's GPT conversations into the Codex rollout JSONL format
(`~/.codex/sessions/**/rollout-*.jsonl`), so Cursor turns can be read — or replayed — by anything
that already speaks Codex:

```bash
python3 cursor_to_codex.py --list                     # matching sessions, one per line
python3 cursor_to_codex.py --out ~/cursor-rollouts    # YYYY/MM/DD/rollout-<time>-<id>.jsonl
python3 cursor_to_codex.py --session <composer-id> --out -   # one session to stdout
```

The rendered bubbles are *not* the source here. Cursor also keeps the exact provider-format message
array it sends to OpenAI, content-addressed: `composerData:<id>.conversationState` is a protobuf of
32-byte sha256s, each addressing an `agentKv:blob:<sha256>` row holding one message. In those blobs
reasoning is intact — the `rs_…` id and the `gAAAAA…` `encrypted_content` live in a JSON-encoded
`signature` (the bubbles' `thinking.signature` is always empty) — so `response_item` reasoning
records come out with a real `summary` *and* `encrypted_content`.

Mapping: Cursor's system prompt → `session_meta.base_instructions`; each prompt → `turn_context` +
`message` + a `user_message` mirror; `tool-call` → `function_call` (Cursor's `call_…\nfc_…` id is
split into `call_id`/`id`); `tool-result` → `function_call_output`. Tool *names* stay Cursor's
(`ReadFile`, `ApplyPatch`, …) rather than being renamed to Codex's.

Caveats: `conversationState` is the live context window, so a compacted thread has lost its oldest
turns there (the export prints how many; the bubbles still have that text, minus the encrypted
reasoning). Only bubbles carry timestamps, so times are exact for prompts and for tool calls in
newer composers and carried forward otherwise. Non-GPT models store an opaque signature instead of
an OpenAI reasoning item; `--all-models` exports them anyway, putting that string in
`encrypted_content`.

## Exporting opencode sessions as Codex rollouts

`opencode_to_codex.py` does the same for opencode. opencode's own database already holds a complete
transcript, so unlike the Cursor exporter this is a format conversion rather than a recovery
operation — what it buys you is that Codex-aware tooling can read the session, and that the
provider's **encrypted reasoning travels with it**:

```bash
python3 opencode_to_codex.py --list                       # sessions + their reasoning format
python3 opencode_to_codex.py --out ~/opencode-rollouts    # YYYY/MM/DD/rollout-<time>-<id>.jsonl
python3 opencode_to_codex.py --session <session-id> --out -    # one session to stdout
python3 opencode_to_codex.py --with-encrypted --out DIR   # only sessions that carry blobs
```

The reasoning sits inline on the part, in `metadata.<providerID>.reasoning_details`: alongside the
token-by-token summary fragments is a `reasoning.encrypted` entry with an `rs_…` id and opaque
`data`. That maps 1:1 onto a Codex `reasoning` response_item — `summary[0].summary_text` plus
`encrypted_content` — with no blob chain to chase, unlike Cursor.

**The blobs are not necessarily OpenAI's.** Codex rollouts carry OpenAI Responses-API reasoning
items, but opencode records whatever its provider returned and stamps the shape in `format`. Grok
through OpenRouter writes `xai-responses-v1`: a faithful record, but *not* replayable against the
OpenAI Responses API. The formats found are printed on export and stored in
`session_meta.opencode.reasoning_formats` so an export can't be mistaken for an OpenAI-replayable
one.

Mapping: the session → `session_meta`; each user turn → `turn_context` + `message` + a
`user_message` mirror; assistant text → `message` + an `agent_message` mirror; each tool part →
`function_call` **and** `function_call_output`, since opencode fuses the call and its result into
one record where Codex keeps two. Tool names and arguments stay opencode's own (`read`/`filePath`)
rather than being renamed. Every part carries its own clock, so timestamps are exact rather than
inferred. A call with no result (interrupted, or still running) emits the call alone and is
reported.

## Requirements

- **Python 3.9+** (standard library only — no `pip install` needed).
- Markdown / math rendering uses `marked.js`, `DOMPurify`, and `KaTeX` from a CDN; offline, it falls
  back to plain text.

## Install

```bash
git clone https://github.com/tim-hua-01/cc_transcript_viewer.git
cd cc_transcript_viewer
```

## Important: keep your transcripts around

By default **Claude Code deletes chat transcripts after 30 days**, so this viewer can only show what
hasn't been cleaned up yet. To retain them, edit your global settings at `~/.claude/settings.json`
and add:

```jsonc
{
  // Days to keep transcripts before automatic cleanup (default: 30).
  // A large value effectively keeps them forever (~274 years here).
  "cleanupPeriodDays": 99999,

  // Optional but recommended: persist human-readable thinking summaries.
  // Without this, Claude Code stores only an encrypted signature for thinking
  // blocks, so they show up empty in the viewer. With it on, NEW sessions save
  // a readable summary the viewer can display. (It can't backfill old sessions.)
  "showThinkingSummaries": true
}
```

Restart Claude Code (or run `/config` once) for the change to take effect. Note this only stops
*future* deletion — anything already cleaned up is gone.

## How it works

| File | Role |
|------|------|
| `server.py` | The unified stdlib HTTP layer plus everything that spans sources: merges the parsers' session lists into one time-sorted sidebar list, dispatches `/api/session` to the right parser by transcript root / `cursordb:`/`cursorcli:`/`opencode:` scheme, runs full-text search, owns custom names and summary-cache persistence, and serves the JSON API (`/api/sessions`, `/api/session?file=...`, `/api/session-name`, `/api/search?q=...`, `/api/local-image`) plus the static frontend. `/api/session` only serves files under the allowed roots; `/api/session-name` only updates the viewer-owned names file; `/api/local-image` serves only image-typed files; and a `Host`-header allowlist guards the loopback server against DNS rebinding. |
| `claude_parser.py` | Claude Code parsing library (imported by `server.py`): parses session JSONL into a clean event stream (pairing tool results to calls), detects system-injected "user" records, folds rewound/edited branches into collapsible markers, labels sub-agents from their spawning `Task`/`Agent` calls, and builds sidebar summaries (cold scans use a process pool). Call `configure(projects_dir)` to point it elsewhere. |
| `codex_parser.py` | Codex parsing library: parses rollout JSONL, reads `state_5.sqlite` metadata, pairs tool calls with outputs, unpacks orchestration-style `exec` calls, correlates local and embedded prompt images, consolidates per-turn bookkeeping, recognizes guardian/sub-agent relationships, and renders `apply_patch` diffs. Call `configure(codex_home)` to point it elsewhere. |
| `cursor_parser.py` | Cursor parsing library: reads IDE `state.vscdb`; reads CLI `~/.cursor/chats/.../store.db` (with JSONL fallback under `agent-transcripts`); normalizes tool names/inputs; reconstructs IDE `edit_file` diffs; decodes grep/glob results out of Cursor's binary protobuf tool records (`toolCallBinary`) since newer Cursor versions no longer store them as JSON; emits Claude-shaped events. IDE uses `cursordb:<id>`; CLI store uses `cursorcli:<id>`. Call `configure(db_path, projects_dir=…, chats_dir=…)` to retarget. |
| `cursor_binary.py` | Decoder for Cursor's `toolCallBinary` protobuf records (wire-format only, no schema/dependency): exact grep and glob result reconstruction, plus a generic best-effort string recovery for any other completed call whose result was never written to JSON (e.g. `await`). |
| `opencode_parser.py` | opencode parsing library: reads the single SQLite database at `~/.local/share/opencode/opencode.db` (`session` / `message` / `part` tables), folds each message's parts into block-shaped events, attaches each tool part's own result, renames opencode's camelCase tool arguments onto the canonical names, links `task` calls to the sub-agent session they spawned, flags thinking whose real reasoning came back encrypted, and drops the per-token provider `reasoning_details` noise. Sessions are addressed as `opencode:<id>`; the list cache is keyed on the database *and* its write-ahead log, so a live session is not served stale. Call `configure(db_path)` to retarget. |
| `opencode_to_codex.py` | Exports opencode sessions as Codex rollout JSONL, carrying the provider's encrypted reasoning (`reasoning.encrypted` → `encrypted_content`) and splitting opencode's fused tool record into Codex's separate `function_call` / `function_call_output`. Records the provider's reasoning `format` so a non-OpenAI blob isn't mistaken for a replayable one. |
| `codex_rollout.py` | The parts of writing a Codex rollout that aren't specific to any source — record shape, `rollout-<time>-<id>.jsonl` naming, dated output layout — shared by both exporters so they can't drift. |
| `common.py` | Helpers shared by the parsers: JSONL iteration, title truncation, timestamp conversion, and the thread-safe fingerprint-keyed `SummaryCache` all of them use. |
| `event_schema.py` | The written-down (and machine-checked) event contract between the parsers and the frontend: every event kind, both message shapes, and validators the test suite runs over every parser's output. |
| `static/index.html`, `static/style.css`, `static/app.js` | The single frontend (vanilla JS, no build step). `app.js` dispatches on event kind and renders the Claude Code / Cursor / opencode (block-based) and Codex (flat) shapes, and runs the always-on live-refresh poll loop. CDN assets (marked, DOMPurify, KaTeX) are version-pinned with SRI integrity hashes. |
| `test_security.py` | Zero-dependency security tests (`python3 -m unittest test_security`): asserts no outbound connections at runtime, no network-client imports, loopback default bind, the `Host`-header rebinding guard, that `/api/session` is confined to the transcript roots while `/api/local-image` serves images only, and that every CDN asset is version-pinned and SRI-hashed. |
| `test_opencode_to_codex.py` | Tests for the opencode exporter: encrypted reasoning survives, the fused tool record splits into two, timestamps come from the parts' own clocks, and the output reparses through `codex_parser` as a valid `opencode`-labelled session. |
| `test_parsers.py` | Characterization tests for the parser internals: branch folding, the Codex JS-literal orchestration parser, Cursor blob/JSON extraction and diff reconstruction, queued prompts with images, and opencode's
part→event mapping (argument renaming, tool states, sub-agent linkage, provider-noise stripping). |
| `test_event_schema.py` | Conformance tests: every parser's summaries and full parses must satisfy `event_schema.py`, and `app.js` must dispatch on every declared kind. |
| `test_summary_cache.py` | Summary-cache unit tests (round-trip persistence, fingerprint invalidation, dirty-flag races). |
| `test_fixtures.py` | Shared fixture builders that write minimal-but-valid transcripts for each source. |

### Transcript format notes

Claude Code stores each session as a JSONL file at
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`, one record per line:

- `user` / `assistant` messages, whose `message.content` is a list of typed blocks
  (`text`, `thinking`, `tool_use` on the assistant side; `text`, `tool_result`, `image` on the user
  side — tool results are paired to calls by `tool_use_id`).
- Metadata lines include `ai-title`, `custom-title`, `agent-name`, `pr-link`, `attachment` (hook
  output), and `system` records such as compaction boundaries and `compactMetadata`. Some other
  operational records are ignored.

Codex stores each session as a rollout JSONL file at
`~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl` (and under
`~/.codex/archived_sessions/`), primarily using these record types:

- `session_meta` — session identity, working directory, base instructions, and sub-agent provenance.
- `turn_context` — per-turn model, effort, sandbox, approval, and working-directory metadata.
- `response_item` — structured messages, reasoning, tool calls/results, and embedded `input_image`
  content.
- `event_msg` — the user/assistant event stream plus status, token, compaction, and `local_images`
  bookkeeping. The viewer merges duplicate representations into one chronological transcript.

Cursor IDE stores conversations in a SQLite DB at
`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` (table `cursorDiskKV`):

- `composerData:<id>` — conversation metadata: `name` (AI title), `modelConfig.modelName`,
  `workspaceIdentifier`/`trackedGitRepos` (cwd), and `fullConversationHeadersOnly` (the ordered list
  of bubbles, with per-bubble type and timestamp).
- `bubbleId:<id>:<bubbleId>` — one message: a user prompt (`type: 1`), or an assistant `type: 2`
  bubble carrying `text`, a `thinking.text` block, or a `toolFormerData` tool call with its `result`.
- `composer.content.<hash>` — raw file snapshots; `edit_file` results reference these by id, so
  before/after pairs reconstruct into diffs.

Cursor CLI's canonical store is SQLite at
`~/.cursor/chats/<md5(cwd)>/<uuid>/store.db` (plus `meta.json`):

- `meta` row `key=0` — hex-encoded JSON: `agentId`, `name`, `lastUsedModel`, `createdAt`, …
- `blobs` — content-addressed payloads; JSON role messages (`user` / `assistant` / `tool`) are
  embedded (sometimes inside a binary wrapper). Assistant content uses `text`, `reasoning`, and
  `tool-call`; tool messages use `tool-result` keyed by `toolCallId`.

A lossy JSONL export also exists at
`~/.cursor/projects/<encoded-cwd>/agent-transcripts/<uuid>/<uuid>.jsonl`
(`text` + `tool_use` only). The viewer uses it only when no `store.db` is present for that UUID.
Project folder names encode the cwd by replacing `/` and `_` with `-`; cwd for `store.db` sessions
usually comes from `meta.json` instead.

opencode keeps everything in one SQLite database at `~/.local/share/opencode/opencode.db` — there
are no per-session files, so sessions are addressed by the synthetic id `opencode:<sessionID>`:

- `session` — one row per conversation: `title`, `directory`, `version`, `agent`, `model`, cost and
  token totals, and `parent_id`, which is set on the sub-agent sessions the `task` tool spawns.
- `message` — one row per turn; the `data` column is the JSON message record, either
  `role: "user"` or `role: "assistant"` (which also carries `modelID`/`providerID`, `cost`,
  `tokens`, `finish` and any turn-level `error`).
- `part` — the actual content, one row per part, `data` holding a union discriminated on `type`:
  `text` (with `synthetic`/`ignored` flags), `reasoning`, `tool`, `file`, `agent`, `subtask`,
  `step-start`, `step-finish`, `snapshot`, `patch`, `retry`, `compaction`.

Tool calls live on the assistant turn — a `tool` part carries its own result in
`state` (`pending` / `running` / `completed` / `error`) — so opencode maps onto the same block
shape as Claude Code and Cursor rather than Codex's flat shape.

Parts also carry a per-provider `metadata` blob holding a `reasoning_details` list. Most of it is
a token-by-token copy of the summary that dwarfs the text itself, so the parser reads only what it
renders — but the list also holds any `reasoning.encrypted` entry, an `rs_…` id plus opaque `data`
only the provider can read. When one is present the visible thinking is a *summary* of a chain of
thought the transcript doesn't contain, and the viewer labels it as such. Unlike Cursor, opencode
keeps this inline on the part rather than in an out-of-line blob chain.

The database runs in WAL mode, so change detection has to watch `opencode.db-wal` as well as the
main file.

## License

MIT
