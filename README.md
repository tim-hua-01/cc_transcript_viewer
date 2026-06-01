# Claude Code Transcript Viewer

A tiny, **zero-dependency** local web app for browsing your [Claude Code](https://claude.com/claude-code)
session transcripts — every prompt, response, thinking block, and tool call, rendered to be easy to read.

It reads the JSONL session files Claude Code already writes to `~/.claude/projects/`. Nothing is
uploaded anywhere; it's a read-only local viewer.

## Features

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

## Requirements

- **Python 3.9+** (standard library only — no `pip install` needed).
- Markdown rendering uses `marked.js` + `DOMPurify` from a CDN; if you're offline it falls back to
  plain text.

## Serve it

```bash
git clone https://github.com/tim-hua-01/cc_transcript_viewer.git
cd cc_transcript_viewer
python3 server.py
```

Then open the printed URL (default **http://127.0.0.1:3132/**).

### Options

```bash
python3 server.py --port 8080            # use a different port
python3 server.py --host 0.0.0.0         # listen on all interfaces (LAN access)
python3 server.py --projects-dir PATH    # point at a different projects directory
```

The server re-scans `~/.claude/projects/` on every request, so new sessions show up as soon as you
hit **↻ Refresh** (or reload the page).

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
