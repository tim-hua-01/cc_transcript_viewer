# Claude Code, Codex, and Cursor Transcript Viewer

A tiny, **zero-dependency** local web app for browsing your local coding-agent transcripts —
**Claude Code, Codex, and Cursor together** in one time-sorted view.

It reads:

- Claude Code transcripts from `~/.claude/projects/`
- Codex transcripts from `~/.codex/sessions/` and `~/.codex/archived_sessions/`
- Cursor agent conversations from Cursor's store at
  `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` (not the lossy
  `~/.cursor/projects/.../agent-transcripts/` JSONL export — see [Notes on Cursor](#notes-on-cursor-conversations))

Nothing is uploaded anywhere; it's a read-only local web app that listens on loopback only. The
server contains no outbound-network code at all, and a [test suite](#privacy--security) verifies it —
so you can check that claim rather than take it on faith.

> [!WARNING]
> **This is vibe-coded.** It was built quickly and iteratively with an AI coding agent, so expect
> rough edges and the occasional rendering bug. It also depends on the *current* on-disk transcript
> formats of Claude Code, Codex, and Cursor — if any tool changes how it saves sessions, parts of the
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
python3 server.py --custom-names-file PATH # different custom transcript names file
```

By default the server binds **127.0.0.1** (loopback only), so it isn't reachable from other machines
unless you deliberately pass `--host 0.0.0.0`.

The viewer **auto-refreshes**: while the **● Live** toggle (top of the sidebar) is on — the default —
it polls about once a second and updates the sidebar **and the open transcript in place** as sessions
change on disk, preserving your scroll position and which thinking/tool blocks you've expanded. So an
active session you're watching tails live. Click **○ Live** to pause it, **↻ Refresh** to force a
rescan, or just reload. Session metadata is cached by file mtime, so each poll only re-reads the
files that actually changed — the cost doesn't grow with how many transcripts you've accumulated.

## Features

- **One sidebar, sorted by time.** Every Claude Code, Codex, and Cursor session in a single flat list,
  newest (most recently modified) first — no per-project grouping. Each entry shows an **agent tag**
  (Claude / Codex / Cursor), the project path, recency, message/tool/web counts, model, and the full
  session **id** (click to copy).
- **Titles from the first message.** Each session is titled with the first ~100 characters of its
  first user message unless the source has an explicit title. Claude Code `/rename`/`--name` titles
  are respected, and distinct branch/team agent names appear as badges. Use the edit button beside
  an open transcript's title to give it a viewer-specific name; clearing it restores the source
  title. Overrides are stored separately in `~/.config/cc_transcript_viewer/names.json`, leaving the
  agent-owned transcripts and Cursor database untouched.
- **Search across everything.** The search box (press `/` to focus) matches session **content** —
  prompts, replies, reasoning, tool commands/paths/queries/outputs — in addition to titles and
  directories, and shows a snippet of the match. Custom-title matches receive `10,000×` weight,
  every user-message match receives `50×`, and ordinary transcript-content matches receive `1×`.
  Powered by `/api/search` with an mtime-keyed cache.
- **Filters that compose.**
  - **All / Claude / Codex / Cursor** chips.
  - A **Model** dropdown grouped by family (Claude / GPT / Other): tick a family to select all its
    models, or pick individual ones (e.g. only Sonnet). The family box shows an indeterminate state
    on partial selection.
  - A **Directory** dropdown to narrow to specific project paths.
- **Transcript view** — a chronological, color-coded conversation that renders both agents' formats:
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
    - Cursor: tool calls **with their outputs** — `run_terminal_command` (stdout + exit code),
      `read_file` (contents), `edit_file` (reconstructed before/after diff), `ripgrep`, `glob`,
      `read_lints`, `todo_write`, `delete_file`, semantic/web search. Names/inputs are normalized onto
      the same renderers as Claude Code, so a Cursor `edit_file` shows the same colorized diff.
  - Codex task/context/token bookkeeping consolidated into one **Turn metadata** disclosure at the
    end of the final assistant response.
- **Survives `/compact`.** After a conversation is compacted, the harness injects bookkeeping records
  that most viewers drop. This one renders them: 📎 *referenced-file* pointers (including the note the
  model is actually handed when its context is rebuilt), 📄 re-attached file reads with their full
  content, and tool-set changes (`+N tools available via ToolSearch`). Compaction boundaries show
  their trigger, before/after token counts, duration, and preserved message/tool counts.
- **Pull-request links.** Claude Code sessions associated with a PR show a safe, clickable PR link in
  the transcript header.
- **Images & sub-agents.** Inline images in prompts/replies and Codex `view_image` are shown. For
  Codex user prompts, the viewer prefers the original `local_images` file and falls back to the
  embedded `input_image` data URL if the file is gone. Claude
  Code sub-agents (which newer versions write to their own `…/<session-id>/subagents/agent-*.jsonl`
  files) and Codex sub-agent sessions are each picked up as their own entry in the same time-sorted
  list, indented and flagged **sub-agent**. A sub-agent is titled `[type] description` when it can be
  matched back to the `Task`/`Agent` call that spawned it (others — e.g. compaction agents — fall back
  to their opening prompt), and its transcript view links **↑ parent session**. Older transcripts that
  inline sub-agent turns as `isSidechain` records still render those turns inline, flagged **sub-agent**.
  Codex approval guardians are linked under their parent session. Each received transcript snapshot
  or delta renders as a user-style review input followed by reasoning and the allow/deny decision;
  generic task/context/token bookkeeping is consolidated into one collapsed metadata block per turn.
- **Jump between prompts.** A right-side **outline** lists the top-level user messages as truncated,
  clickable headings and highlights the one you're reading as you scroll. Floating **↑ / ↓** buttons
  jump to the previous/next user prompt, and **⤓ Jump to end** (in the transcript controls) skips to
  the bottom.
- **Live updates.** With the **● Live** toggle on (default), the sidebar and the transcript you're
  reading refresh themselves about once a second — new sessions appear, and an in-progress session
  tails as it's written — without disturbing your scroll position or your expanded/collapsed blocks.
- **Deep-linkable:** the open session is stored in the URL hash, so you can bookmark/share a link.

## Privacy & security

This app reads your private transcripts, so it's built to keep them on your machine:

- **No outbound network code.** The server imports only Python's standard-library `http.server`
  (inbound), with no HTTP client, sockets, mail, or telemetry anywhere. Nothing is ever uploaded.
- **Loopback by default.** It binds `127.0.0.1`; LAN exposure requires an explicit `--host 0.0.0.0`.
- **DNS-rebinding guard.** While bound to loopback, it enforces a `Host`-header allowlist
  (`127.0.0.1` / `localhost`), so a malicious web page that rebinds its domain to `127.0.0.1` can't
  read your transcripts through the browser — its requests still carry `Host: evil.com` and get a
  `403`. (Skipped when you deliberately bind a non-loopback `--host`.)
- **Transcript reads are confined.** `/api/session` only parses files under the transcript roots
  (`~/.claude/projects`, `~/.codex`), or a Cursor conversation addressed by the `cursordb:<id>` scheme
  (read from the local `state.vscdb` by id, never an arbitrary path); anything else returns `403`.
  `/api/local-image` serves only
  image-typed files (it renders images referenced by a transcript, which may live anywhere on disk),
  never arbitrary file contents.
- **Name writes are confined.** `/api/session-name` accepts only JSON, verifies that the referenced
  transcript exists under an allowed source, and writes only the configured custom-names file using
  an atomic replacement.

These guarantees are enforced by a zero-dependency test suite — run it yourself:

```bash
python3 -m unittest test_security
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

  This doesn't backfill old transcripts; records with only encrypted reasoning still show as not
  readable. Duplicate summaries (written as both `event_msg/agent_reasoning` and
  `response_item/reasoning.summary`) are deduped.
- **Web search results aren't stored.** Codex records only the search *queries* it issued (the viewer
  lists all of them); the fetched pages are sent to the model but never written to the rollout. The
  findings survive only as the assistant's prose, with citation links.
- **Images.** `view_image` prefers serving the original local file via `/api/local-image`; if the file
  is gone it falls back to the (often large) base64 payload embedded in the JSONL. It also reads
  `~/.codex/state_5.sqlite` for thread metadata (cwd, model) when available.

## Notes on Cursor conversations

- **Read from the SQLite store, not the JSONL export.** Cursor also writes a transcript export under
  `~/.cursor/projects/<project>/agent-transcripts/`, but that export is **lossy** — it drops tool
  outputs, the model, thinking text, timestamps, and token counts. The viewer instead reads Cursor's
  real store at `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`, which has all of
  it. (Each conversation is a "composer"; each message is a "bubble".)
- **Tool outputs are included.** `run_terminal_command` stdout + exit code, `read_file` contents,
  `ripgrep`/`glob` results, lints, etc. `edit_file` only stores before/after *content ids*, so the
  viewer resolves them against the `composer.content.*` file snapshots to reconstruct a real diff.
- **Thinking is included** where Cursor recorded it (the readable summary text; the encrypted
  reasoning signature is not stored). Model, per-turn timestamps, and AI-generated titles are shown.
- **Read-only & safe while Cursor runs.** The DB is opened read-only; the session list is cached and
  re-read only when the DB file changes.
- **Empty draft composers are hidden** (conversations with no messages).
- **macOS path by default.** Override the location with `--cursor-db` (point it at a `state.vscdb` or
  at a Cursor app-support directory) for Linux/Windows or a non-standard install.

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
| `server.py` | The unified stdlib HTTP server. Scans both transcript roots into one time-sorted list (caching per-file summaries by mtime), parses each Claude Code session into a clean event stream (pairing tool results to calls), dispatches `/api/session` to the right parser by transcript root, and serves the JSON API (`/api/sessions`, `/api/session?file=...`, `/api/session-name`, `/api/search?q=...`, `/api/local-image`) plus the static frontend. `/api/session` only serves files under the allowed roots; `/api/session-name` only updates the viewer-owned names file; `/api/local-image` serves only image-typed files; and a `Host`-header allowlist guards the loopback server against DNS rebinding. |
| `codex_server.py` | Codex parsing library (imported by `server.py`): parses rollout JSONL, reads `state_5.sqlite` metadata, pairs tool calls with outputs, and renders `apply_patch` diffs. Call `configure(codex_home)` to point it elsewhere. |
| `cursor_server.py` | Cursor parsing library (imported by `server.py`): reads Cursor's `state.vscdb` (`composerData`/`bubbleId`/`composer.content` keys) read-only, normalizes tool names/inputs onto the canonical renderers, reconstructs `edit_file` diffs from content snapshots, and emits events in the Claude Code block shape. Sessions are addressed by the `cursordb:<id>` scheme. Call `configure(db_path)` to point it elsewhere. |
| `static/index.html`, `static/style.css`, `static/app.js` | The single frontend (vanilla JS, no build step). `app.js` dispatches on event kind and renders the Claude Code / Cursor (block-based) and Codex (flat) shapes, and runs the live-refresh poll loop. |
| `test_security.py` | Zero-dependency security tests (`python3 -m unittest test_security`): asserts no outbound connections at runtime, no network-client imports, loopback default bind, the `Host`-header rebinding guard, and that `/api/session` is confined to the transcript roots while `/api/local-image` serves images only. |

### Transcript format notes

Claude Code stores each session as a JSONL file at
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`, one record per line:

- `user` / `assistant` messages, whose `message.content` is a list of typed blocks
  (`text`, `thinking`, `tool_use` on the assistant side; `text`, `tool_result`, `image` on the user
  side — tool results are paired to calls by `tool_use_id`).
- Metadata lines: `ai-title`, `system`, `attachment` (hook output), and a few others the viewer
  ignores.

Codex stores each session as a rollout JSONL file at
`~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl` (and under
`~/.codex/archived_sessions/`), with `session_meta`, `turn_context`, `response_item`, and `event_msg`
records.

Cursor stores conversations in a SQLite DB at
`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` (table `cursorDiskKV`):

- `composerData:<id>` — conversation metadata: `name` (AI title), `modelConfig.modelName`,
  `workspaceIdentifier`/`trackedGitRepos` (cwd), and `fullConversationHeadersOnly` (the ordered list
  of bubbles, with per-bubble type and timestamp).
- `bubbleId:<id>:<bubbleId>` — one message: a user prompt (`type: 1`), or an assistant `type: 2`
  bubble carrying `text`, a `thinking.text` block, or a `toolFormerData` tool call with its `result`.
- `composer.content.<hash>` — raw file snapshots; `edit_file` results reference these by id, so
  before/after pairs reconstruct into diffs.

(Cursor also writes a lossy JSONL export under `~/.cursor/projects/.../agent-transcripts/`, but it
omits tool outputs, model, thinking, and timestamps, so the viewer does not use it.)

## License

MIT
