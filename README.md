# Claude Code and Codex Transcript Viewer

A tiny, **zero-dependency** local web app for browsing your local coding-agent transcripts —
**Claude Code and Codex together** in one time-sorted view.

It reads:

- Claude Code transcripts from `~/.claude/projects/`
- Codex transcripts from `~/.codex/sessions/` and `~/.codex/archived_sessions/`

Nothing is uploaded anywhere; it's a read-only local web app.

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
python3 server.py --host 0.0.0.0         # listen on all interfaces (LAN access)
python3 server.py --projects-dir PATH    # different Claude Code projects directory
python3 server.py --codex-home PATH      # different Codex home (default ~/.codex)
```

The server re-scans both transcript locations on every request, so new sessions show up as soon as
you hit **↻ Refresh** or reload the page.

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
  Code sub-agent (`Task`) turns are rendered inline and flagged **sub-agent**, and Codex sub-agent
  sessions are picked up and listed in the same time-sorted view.
- **Jump between prompts.** A right-side **outline** lists the top-level user messages as truncated,
  clickable headings and highlights the one you're reading as you scroll. Floating **↑ / ↓** buttons
  jump to the previous/next user prompt, and **⤓ Jump to end** (in the transcript controls) skips to
  the bottom.
- **Deep-linkable:** the open session is stored in the URL hash, so you can bookmark/share a link.

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
| `server.py` | The unified stdlib HTTP server. Scans both transcript roots into one time-sorted list, parses each Claude Code session into a clean event stream (pairing tool results to calls), dispatches `/api/session` to the right parser by transcript root, and serves the JSON API (`/api/sessions`, `/api/session?file=...`, `/api/search?q=...`, `/api/local-image`) plus the static frontend. Only files under the allowed roots are served. |
| `codex_server.py` | Codex parsing library (imported by `server.py`): parses rollout JSONL, reads `state_5.sqlite` metadata, pairs tool calls with outputs, and renders `apply_patch` diffs. Call `configure(codex_home)` to point it elsewhere. |
| `static/index.html`, `static/style.css`, `static/app.js` | The single frontend (vanilla JS, no build step). `app.js` dispatches on event kind and renders both Claude Code (block-based) and Codex (flat) shapes. |

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

## License

MIT
