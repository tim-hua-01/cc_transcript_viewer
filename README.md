# Claude Code and Codex Transcript Viewer

A tiny, **zero-dependency** local web app for browsing local coding-agent transcripts.

It has two separate viewers for now:

- Claude Code transcripts from `~/.claude/projects/`
- Codex transcripts from `~/.codex/sessions/` and `~/.codex/archived_sessions/`

Nothing is uploaded anywhere; both viewers are read-only local web apps.

## Claude Code Viewer

The Claude Code viewer reads the JSONL session files Claude Code already writes to
`~/.claude/projects/`.

### Features

- **Sidebar**: sessions grouped by project, with AI-generated titles, recency, message/tool counts,
  model, and the full session **id** (click to copy). Type to filter (or press `/`); hit **↻ Refresh**
  to rescan for new sessions.
- **Transcript view**: a chronological, color-coded conversation —
  - User prompts and Claude replies (rendered as Markdown)
  - Thinking blocks (collapsed by default)
  - Tool calls, each with **tool-specific formatting** and the result paired inline:
    - `Bash` → command + description + output
    - `Edit` / `Write` / `MultiEdit` → colorized diffs
    - `Read` / `Grep` / `Glob` → file path + options
    - `Task` / `Agent` → sub-agent prompt (sub-agent turns are flagged)
    - `TodoWrite` → checklist; everything else → pretty-printed JSON
  - **Inline images** — both pasted images and images returned by tools (e.g. screenshots) render
    directly.
- Deep-linkable: the open session is stored in the URL hash, so you can bookmark/share a link.

### Serve Claude Code Transcripts

```bash
python3 server.py
```

Then open the printed URL (default **http://127.0.0.1:3132/**).

Options:

```bash
python3 server.py --port 8080            # use a different port
python3 server.py --host 0.0.0.0         # listen on all interfaces (LAN access)
python3 server.py --projects-dir PATH    # point at a different projects directory
```

The server re-scans `~/.claude/projects/` on every request, so new sessions show up as soon as you
hit **Refresh** or reload the page.

## Codex Viewer

The Codex viewer reads the JSONL rollout files Codex writes under:

```text
~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl
~/.codex/archived_sessions/...
```

It also reads `~/.codex/state_5.sqlite` for thread titles, cwd, model metadata, and archive state
when available.

### Serve Codex Transcripts

```bash
python3 codex_server.py
```

Then open the printed URL (default **http://127.0.0.1:3133/**).

Options:

```bash
python3 codex_server.py --port 8081          # use a different port
python3 codex_server.py --host 0.0.0.0       # listen on all interfaces (LAN access)
python3 codex_server.py --codex-home PATH    # point at a different Codex home
```

### Codex Features

- Sidebar grouped by cwd, with title, recency, counts, model/source badges, and copyable thread id.
- User and assistant messages rendered as Markdown.
- Tool calls paired with outputs for `function_call`, `custom_tool_call`, `apply_patch`,
  `exec_command`, `write_stdin`, `view_image`, and fallback JSON rendering for unknown tools.
- Readable reasoning summaries when Codex records them.
- Duplicate reasoning summaries are deduped when Codex writes the same text both as
  `event_msg/agent_reasoning` and `response_item/reasoning.summary`.
- Token usage events are hidden by default and can be shown with **Toggle tokens**.
- `view_image` prefers serving the local file path via `/api/local-image`; if the file is gone, it
  falls back to embedded image payloads stored in the JSONL.

### Codex Reasoning Summaries

Codex does not save raw chain-of-thought in readable form for OpenAI models. It may save readable
reasoning summaries when configured and supported by the model. To request summaries for future
sessions, add this to `~/.codex/config.toml`:

```toml
model_reasoning_summary = "detailed"
```

This does not backfill old transcripts. Existing records with only encrypted reasoning content will
still show as not readable.

### Codex Image Notes

Codex `view_image` tool results can include large base64 data URLs inside the JSONL. The viewer
avoids pushing those huge strings through the session API when it can serve the original local image
file instead. This keeps transcript JSON smaller and lets the browser load the image normally. If the
local path no longer exists, the viewer renders the embedded payload.

## Requirements

- **Python 3.9+** (standard library only — no `pip install` needed).
- Markdown rendering uses `marked.js` + `DOMPurify` from a CDN; if you're offline it falls back to
  plain text.

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
| `server.py` | stdlib HTTP server. Parses the JSONL into a clean event stream (pairing each tool result back to its call) and serves a JSON API (`/api/sessions`, `/api/session?file=...`) plus the static frontend. Includes a path-traversal guard so only files under the projects dir are served. |
| `static/index.html`, `static/style.css`, `static/app.js` | The frontend (vanilla JS, no build step). |
| `codex_server.py` | stdlib HTTP server for Codex transcripts. Parses rollout JSONL files, reads `state_5.sqlite` metadata, pairs tool calls with outputs, serves local images, and exposes the same `/api/sessions` and `/api/session?file=...` style API. |
| `static/codex_index.html`, `static/codex_style.css`, `static/codex_app.js` | Separate Codex frontend files. |

### Transcript format notes

Claude Code stores each session as a JSONL file at
`~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`, one record per line:

- `user` / `assistant` messages, whose `message.content` is a list of typed blocks
  (`text`, `thinking`, `tool_use` on the assistant side; `text`, `tool_result`, `image` on the user
  side — tool results are paired to calls by `tool_use_id`).
- Metadata lines: `ai-title` (the session title), `system`, `attachment` (hook output), and a few
  others the viewer ignores.

## License

MIT
