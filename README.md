# Claude Code and Codex Transcript Viewer

A tiny, **zero-dependency** local web app for browsing your local coding-agent transcripts —
**Claude Code and Codex together** in one time-sorted view.

It reads:

- Claude Code transcripts from `~/.claude/projects/`
- Codex transcripts from `~/.codex/sessions/` and `~/.codex/archived_sessions/`

Nothing is uploaded anywhere; it's a read-only local web app that listens on loopback only. The
server contains no outbound-network code at all, and a [test suite](#privacy--security) verifies it —
so you can check that claim rather than take it on faith.

> [!WARNING]
> **This is vibe-coded.** It was built quickly and iteratively with an AI coding agent, so expect
> rough edges and the occasional rendering bug. It also depends on the *current* on-disk transcript
> formats of Claude Code and Codex — if either tool changes how it saves sessions, parts of the viewer
> may silently break or drop records until the parser is updated.

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
```

By default the server binds **127.0.0.1** (loopback only), so it isn't reachable from other machines
unless you deliberately pass `--host 0.0.0.0`.

The viewer **auto-refreshes**: while the **● Live** toggle (top of the sidebar) is on — the default —
it polls about once a second and updates the sidebar **and the open transcript in place** as sessions
change on disk, preserving your scroll position and which thinking/tool blocks you've expanded. So an
active session you're watching tails live. Click **○ Live** to pause it, **↻ Refresh** to force a
rescan, or just reload. Session metadata is cached by file mtime, so each poll only re-reads the
files that actually changed — the cost doesn't grow with how many transcripts you've accumulated.

The summary cache is also **persisted to disk** (`~/.cache/transcript_viewer/summaries.json`) and the
first cold scan is **parallelized across CPU cores** (via the standard library's `multiprocessing` —
no new dependency and no network), so the very first run on a large history takes seconds rather than
minutes, and every run after that loads near-instantly. That cache file holds only the same metadata
shown in the sidebar — session title, project path, model, counts, mtime — derived from your
transcripts and kept **local** (nothing is uploaded). Delete it any time; it's rebuilt on demand.

## Features

- **One sidebar, sorted by time.** Every Claude Code and Codex session in a single flat list, newest
  (most recently modified) first — no per-project grouping. Each entry shows an **agent tag**
  (Claude / Codex), the project path, recency, message/tool/web counts, model, and the full session
  **id** (click to copy).
- **Titles from the first message.** Each session is titled with the first ~100 characters of its
  first user message, so the list reads like your prompts.
- **Search across everything.** The search box (press `/` to focus) matches session **content** —
  prompts, replies, reasoning, tool commands/paths/queries/outputs — in addition to titles and
  directories, and shows a snippet of the match. Powered by `/api/search` with an mtime-keyed cache.
- **Filters that compose.**
  - **All / Claude / Codex** chips.
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
      `exec_command`/`shell`, `write_stdin`, `view_image`, web search, plus fallback JSON.
  - Codex status/context and token-usage events (tokens hidden by default — **Toggle tokens**).
- **Survives `/compact`.** After a conversation is compacted, the harness injects bookkeeping records
  that most viewers drop. This one renders them: 📎 *referenced-file* pointers (including the note the
  model is actually handed when its context is rebuilt), 📄 re-attached file reads with their full
  content, and tool-set changes (`+N tools available via ToolSearch`).
- **Images & sub-agents.** Inline images in prompts/replies and Codex `view_image` are shown. Claude
  Code sub-agents (which newer versions write to their own `…/<session-id>/subagents/agent-*.jsonl`
  files) and Codex sub-agent sessions are each picked up as their own entry in the same time-sorted
  list, indented and flagged **sub-agent**. A sub-agent is titled `[type] description` when it can be
  matched back to the `Task`/`Agent` call that spawned it (others — e.g. compaction agents — fall back
  to their opening prompt), and its transcript view links **↑ parent session**. Older transcripts that
  inline sub-agent turns as `isSidechain` records still render those turns inline, flagged **sub-agent**.
- **Sub-agents inline, in place.** Within a parent transcript, each spawned sub-agent also appears
  **right where it was spawned** — under the `Task`/`Agent` call that launched it (linked exactly via
  the `…​.meta.json` `toolUseId`, with a first-prompt fallback), or, for fleet/teammate agents that
  aren't tied to a single call, slotted into the timeline by their start timestamp. Click one to expand
  its **full transcript inline** (fetched lazily — these files are large), recursively for agents that
  spawn their own. `<teammate-message>` wrappers are unwrapped for clean titles and matching.
- **Collapsible panels & copy.** The sessions sidebar and the right-hand outline each have a **«**
  collapse toggle (a small edge button brings them back), and **⧉ Copy all** in the transcript controls
  copies the whole conversation as plain text.
- **Jump between prompts.** A right-side **outline** lists the top-level user messages as truncated,
  clickable headings and highlights the one you're reading as you scroll. Floating **↑ / ↓** buttons
  jump to the previous/next user prompt, and **⤓ Jump to end** (in the transcript controls) skips to
  the bottom. Large transcripts render in time-sliced chunks, so even a session with tens of thousands
  of turns stays responsive while it streams in.
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
  (`~/.claude/projects`, `~/.codex`); anything else returns `403`. `/api/local-image` serves only
  image-typed files (it renders images referenced by a transcript, which may live anywhere on disk),
  never arbitrary file contents.

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
| `server.py` | The unified stdlib HTTP server. Scans both transcript roots into one time-sorted list (caching per-file summaries by mtime, **persisted to disk** and warmed by a **parallel** first scan), parses each Claude Code session into a clean event stream (pairing tool results to calls, and linking each `Task`/`Agent` call to the sub-agent transcript it spawned), dispatches `/api/session` to the right parser by transcript root, and serves the JSON API (`/api/sessions`, `/api/session?file=...`, `/api/search?q=...`, `/api/local-image`) plus the static frontend. `/api/session` only serves files under the allowed roots; `/api/local-image` serves only image-typed files; static assets are sent `no-cache` and versioned; and a `Host`-header allowlist guards the loopback server against DNS rebinding. |
| `codex_server.py` | Codex parsing library (imported by `server.py`): parses rollout JSONL, reads `state_5.sqlite` metadata, pairs tool calls with outputs, and renders `apply_patch` diffs. Call `configure(codex_home)` to point it elsewhere. |
| `static/index.html`, `static/style.css`, `static/app.js` | The single frontend (vanilla JS, no build step). `app.js` dispatches on event kind and renders both Claude Code (block-based) and Codex (flat) shapes, interleaves spawned sub-agents into the timeline (expanded lazily), renders large transcripts in time-sliced chunks, and runs the live-refresh poll loop. |
| `test_security.py` | Zero-dependency security tests (`python3 -m unittest test_security`): asserts no outbound connections at runtime, no network-client imports, loopback default bind, the `Host`-header rebinding guard, and that `/api/session` is confined to the transcript roots while `/api/local-image` serves images only. |

### Transcript format notes

Claude Code stores each session as a JSONL file at
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`, one record per line:

- `user` / `assistant` messages, whose `message.content` is a list of typed blocks
  (`text`, `thinking`, `tool_use` on the assistant side; `text`, `tool_result`, `image` on the user
  side — tool results are paired to calls by `tool_use_id`).
- Metadata lines: `ai-title`, `system`, `attachment` (hook output), and a few others the viewer
  ignores.
- Sub-agents are written to `…/<session-uuid>/subagents/agent-<id>.jsonl`, each with a sibling
  `agent-<id>.meta.json` sidecar holding `agentType`, `description`, and (when known) the spawning
  `toolUseId` — which the viewer uses to link a sub-agent back to its exact `Task`/`Agent` call.

Codex stores each session as a rollout JSONL file at
`~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl` (and under
`~/.codex/archived_sessions/`), with `session_meta`, `turn_context`, `response_item`, and `event_msg`
records.

## License

MIT
