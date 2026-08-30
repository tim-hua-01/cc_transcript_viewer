# Build-it-yourself prompt

Don't trust this repo? Don't run it. Instead, paste the prompt below to your own
coding agent (Claude Code, Codex, Cursor, etc.) and have it build the same tool
from scratch, in front of you, reading only your local files. The result is a
zero-dependency local web app — you can read every line before you run it.

Everything below the line is the prompt. Copy from there down.

---

You are building a small, **local, read-only web app** that lets me browse my own
AI coding-agent transcripts — **Claude Code** and **OpenAI Codex CLI** sessions —
in a single, time-sorted, searchable view in my browser. Build it incrementally,
explain your choices, and let me read the code as you go. Do not add anything that
phones home.

## Non-negotiable constraints

1. **Zero third-party dependencies.** Backend is **Python 3.9+ standard library
   only** (`http.server`, `json`, `pathlib`, `sqlite3`, `mimetypes`, `argparse`,
   `urllib.parse`, `re`). No `pip install`. No frameworks (no Flask/FastAPI).
2. **No build step on the frontend.** Plain HTML + CSS + vanilla JavaScript served
   as static files. No npm, no bundler, no TypeScript compile.
3. **No outbound network code whatsoever.** The server must never open an outbound
   connection. The only network library allowed is stdlib `http.server` (inbound).
   Markdown/math rendering may load `marked`, `DOMPurify`, and `KaTeX` from a CDN
   *in the browser*, but must degrade to plain text if offline. Nothing server-side
   ever uploads or fetches.
4. **Local and private by default.** Bind `127.0.0.1` only. Treat the transcripts
   as sensitive.
5. Keep it a handful of files: `server.py`, a Codex-parsing module
   (`codex_server.py`), and `static/{index.html,style.css,app.js}`, plus
   `tests/test_security.py`.

## Where the data lives (read-only)

**Claude Code** writes one JSONL file per session:
- `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`
- The `<encoded-cwd>` directory name is the project's working directory with path
  separators replaced (e.g. `/Users/me/proj` → `-Users-me-proj`). Decode it back
  for display when a record doesn't carry an explicit `cwd`.
- Newer Claude Code versions write **sub-agent** transcripts to their own files at
  `~/.claude/projects/<encoded-cwd>/<session-uuid>/subagents/agent-*.jsonl`.

**Codex CLI** writes one "rollout" JSONL file per session:
- `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl`
- also `~/.codex/archived_sessions/...`
- plus a SQLite DB `~/.codex/state_5.sqlite` with thread metadata (cwd, model,
  `rollout_path`). Read it best-effort; tolerate its absence.

**Important:** these on-disk formats are undocumented and change over time. Before
writing parsers, **open a few of my real files and infer the current shape** rather
than trusting any spec (including this one). Make every parser defensive: skip
malformed lines, tolerate missing keys, never crash a whole session over one bad
record.

## Claude Code JSONL shape (verify against real files)

One JSON object per line. Common fields: `type`, `timestamp`, `cwd`, `gitBranch`,
`version`, `isSidechain`.
- `type: "user"` — `message.content` is either a string or a list of typed blocks
  (`{type:"text"}`, `{type:"tool_result", tool_use_id, content}`,
  `{type:"image", source}`).
- `type: "assistant"` — `message.model` and `message.content` is a list of blocks:
  `{type:"text"}`, `{type:"thinking", thinking, signature}`,
  `{type:"tool_use", id, name, input}`.
- `type: "ai-title"` — `aiTitle` (a generated session title; optional).
- `type: "system"` and `type: "attachment"` — bookkeeping. Attachments include hook
  output and **queued user commands** (`attachment.type == "queued_command"` with a
  `prompt`) — surface those as user messages, don't drop them.
- **Pair tool calls to results:** an assistant `tool_use` block (by `id`) is answered
  by a user `tool_result` block (by matching `tool_use_id`). Render them together.
- **Sidechains / sub-agents:** older transcripts inline sub-agent turns as records
  with `isSidechain: true`; newer ones put them in the `subagents/agent-*.jsonl`
  files above. Support both. A sub-agent can be titled from the `Task`/`Agent`
  tool_use call in the parent that spawned it (`[subagent_type] description`);
  fall back to its first prompt.
- **Survive `/compact`:** after a compaction, the harness injects records that most
  viewers drop — referenced-file pointers, re-attached file reads with full content,
  and tool-set changes (e.g. "+N tools available"). Render them rather than hiding
  them.

## Codex rollout shape (verify against real files)

One JSON object per line, each with a `type` such as `session_meta`, `turn_context`,
`response_item`, `event_msg`.
- `response_item` carries the model I/O: `message` (role + content), `reasoning`
  (summaries), `function_call` / `custom_tool_call` (+ matching `*_output`).
- Tool flavors to format nicely: `apply_patch` (render as a colored diff),
  `exec_command` / `shell`, `write_stdin`, `view_image`, and web search.
- **Reasoning:** Codex stores readable reasoning only when the user enabled
  summaries; the same summary can appear as both `event_msg/agent_reasoning` and
  `response_item/reasoning.summary` — **dedupe**. Encrypted-only reasoning shows as
  "not readable".
- **Web search:** only the *queries* are stored, never the fetched pages — list the
  queries and note the results aren't persisted.
- **Images:** prefer the original local file path (served via `/api/local-image`);
  fall back to an inline base64 payload if present.
- **Token usage** events exist; show them but hidden behind a toggle by default.

## HTTP API (all local)

Implement a `ThreadingHTTPServer` with these GET routes:
- `GET /`, `/index.html`, `/app.js`, `/style.css` — static files.
- `GET /api/sessions` → `{"sessions": [ ... ]}`. One lightweight summary per session
  across **both** agents, merged and **sorted by last recorded activity first** (falling
  back to file mtime when the transcript has no usable timestamp). Each
  summary: `agent` ("claude"|"codex"), `id`, `file` (absolute path), `title` (first
  user message, ~100 chars), `cwd`, `git_branch`, `model`, message/tool/web counts,
  `mtime`, and sub-agent linkage (`is_subagent`, `parent_file`). Place each sub-agent
  directly under its parent in the ordering, indented.
- `GET /api/session?file=<abs path>` → fully parsed session: ordered event stream
  with paired tool calls/results, normalized content, and meta (cwd, branch, model,
  id, title, parent link). **Reject any path not under the transcript roots with 403.**
- `GET /api/search?q=<query>` → `{"matches": [{file, snippet, score}]}`. Full-text
  over prompts, replies, reasoning, tool commands/paths/queries/outputs. Weight
  first-message hits highest.
- `GET /api/local-image?path=<abs path>` → raw bytes, but **only** if the file's
  guessed MIME type is `image/*` (else 400). This renders images referenced by a
  transcript, whose original paths may legitimately live anywhere on disk.

**Performance:** `/api/sessions` is polled frequently (see live refresh). Cache each
file's parsed summary keyed by its `(path, mtime)` so a poll only re-reads files that
actually changed; do the same for the search text index. The cost must not grow with
how many transcripts I've accumulated.

## Frontend behavior

A three-pane layout: left sidebar, center transcript, right outline.

**Sidebar (session list):**
- Last-activity-sorted flat list (no per-project grouping), each row: agent tag
  (Claude/Codex), title, project path, recency, badges (💬 user / 🔧 tool / 🌐 web
  counts), model, and the full session id (click to copy). Sub-agents indented and
  flagged.
- A search box (focus with `/`) that matches content (via `/api/search`) plus
  title/cwd/id client-side, showing a snippet of the match.
- Filters that compose: **All / Claude / Codex** chips; a **Model** dropdown grouped
  by family (Claude / GPT / Other) with select-all-per-family and an indeterminate
  state; a **Directory** dropdown.

**Transcript view:**
- Chronological, color-coded conversation rendering both agents' shapes.
- User prompts & assistant replies as **Markdown with LaTeX math** (KaTeX) and inline
  images; fall back to escaped plain text if the CDN libs didn't load.
- **Thinking / reasoning** blocks, collapsed by default, expandable.
- **Tool calls** with tool-specific formatting and the result paired inline; colorize
  Edit/`apply_patch` diffs. Pretty-print unknown tool JSON.
- Codex status/context and token-usage events (tokens hidden behind a toggle).
- Controls to collapse/expand all thinking/tool blocks and jump to the end.

**User-message navigation:**
- A right-side **outline**: the top-level user messages as truncated, clickable
  headings; highlight the one I'm reading as I scroll.
- Floating **↑ / ↓** buttons to jump to the previous/next user prompt, and a
  **jump-to-end** control.

**Live auto-refresh (always on):**
- Poll `/api/sessions` about once a second. When the set/mtimes change, rebuild the
  sidebar **without** losing the sidebar's scroll position or the active selection.
- If the open transcript's file mtime advanced, re-render it **in place**, preserving
  the reader's scroll position (tail-follow if they were pinned to the bottom) and
  which thinking/tool blocks they had expanded. An idle transcript must never
  re-render.

**Deep links:** store the open session in the URL hash so a link reopens it.

## Security (and prove it with tests)

- Bind `127.0.0.1` by default; only expose to the LAN via an explicit `--host`.
- **DNS-rebinding guard:** while bound to loopback, enforce a `Host`-header allowlist
  (`127.0.0.1`, `localhost`, `::1`). A malicious page that rebinds its domain to
  `127.0.0.1` still sends `Host: evil.com`; reject it with 403. Skip the check when I
  deliberately bind a non-loopback `--host` (I've opted into exposure).
- Confine `/api/session` to the transcript roots; restrict `/api/local-image` to
  image MIME types.

Write `tests/test_security.py` using **stdlib `unittest` only**. It should boot the server
on an ephemeral loopback port against a temporary fixture transcript and assert:
1. Exercising every endpoint opens **no outbound socket** — install a guard that wraps
   `socket.socket.connect`/`connect_ex` and records any non-loopback destination;
   assert the list stays empty.
2. Neither server module imports a network/mail/ftp client — parse the source with
   `ast` and check the import names.
3. The default bind is loopback.
4. A forged `Host: evil.com` request gets 403; loopback Hosts get 200.
5. `/api/session` outside the roots → 403; inside → 200.
6. `/api/local-image` serves an image from an arbitrary path but 400s a non-image.

## Suggested build order

1. Server skeleton + static file serving + `--port/--host` args.
2. Claude Code summary scan → `/api/sessions` → minimal sidebar that lists sessions.
3. Full Claude Code session parse → `/api/session` → transcript rendering (text,
   thinking, tool calls with paired results, markdown).
4. Search + filters.
5. Codex parser module → merge into the unified list and transcript view.
6. Outline + jump navigation; live auto-refresh; image serving.
7. Security hardening (Host guard, path confinement) + `tests/test_security.py`.
8. mtime caching for `/api/sessions` and search.

After each step, show me how to run it (`python3 server.py`, open the printed
`http://127.0.0.1:PORT/`) so I can watch it come together. Keep functions small and
commented; prefer clarity over cleverness. Ask me to point you at a couple of real
transcript files whenever you're unsure of a format.
