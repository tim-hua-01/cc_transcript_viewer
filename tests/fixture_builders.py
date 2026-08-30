#!/usr/bin/env python3
"""Fixture builders and harness helpers shared by the test suite.

Builders that write minimal-but-valid on-disk transcripts for each source
(Claude Code JSONL, Codex rollout JSONL, Cursor CLI JSONL and store.db), plus
the loopback HTTP harness the server-backed test classes share.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


def patch_server_files(server_module, tmp: Path):
    """Point the viewer-owned files (custom names, summary cache) into tmp so
    tests never touch the user's real ones. Return all replaced state so the
    caller can restore it even when test modules run in a different order."""
    old_state = (
        server_module.CUSTOM_NAMES_FILE,
        server_module._CUSTOM_NAMES_CACHE,
        server_module.CACHE_FILE,
    )
    server_module.CUSTOM_NAMES_FILE = tmp / "viewer" / "names.json"
    server_module._CUSTOM_NAMES_CACHE = None
    server_module.CACHE_FILE = tmp / "cache" / "summaries.json"
    return old_state


def restore_server_files(server_module, old_state) -> None:
    """Undo ``patch_server_files`` without retaining a deleted temp path."""
    (
        server_module.CUSTOM_NAMES_FILE,
        server_module._CUSTOM_NAMES_CACHE,
        server_module.CACHE_FILE,
    ) = old_state


def start_http_server(handler):
    """Serve `handler` on an ephemeral loopback port in a daemon thread."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1], thread


def stop_http_server(httpd, thread):
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def http_get(port: int, path: str, timeout: float = 10):
    """(status, headers, body) for a GET against the test server; HTTP errors
    come back as a normal tuple instead of raising."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.headers, e.read()
        finally:
            e.close()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _user(uuid, parent, text, ts):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def _assistant(uuid, parent, text, ts):
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": text}],
        },
    }


def _write_fixture_session(
    projects_dir: Path,
    session_id: str = "11111111-1111-1111-1111-111111111111",
    prompt: str = "hello world",
    additional_prompts: tuple[str, ...] = (),
    extra_records: tuple[dict, ...] = (),
) -> Path:
    """A minimal but valid Claude Code transcript so endpoints have real data."""
    proj = projects_dir / "-tmp-proj"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / f"{session_id}.jsonl"
    records = [
        {"type": "user", "timestamp": "2024-01-01T00:00:00Z", "cwd": "/tmp/proj",
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "timestamp": "2024-01-01T00:00:01Z",
         "message": {"role": "assistant", "model": "claude-test",
                     "content": [{"type": "text", "text": "hi there"}]}},
    ]
    for i, extra_prompt in enumerate(additional_prompts, start=2):
        records.extend([
            {"type": "user", "timestamp": f"2024-01-01T00:00:{i:02d}Z",
             "message": {"role": "user", "content": extra_prompt}},
            {"type": "assistant", "timestamp": f"2024-01-01T00:00:{i + 1:02d}Z",
             "message": {"role": "assistant", "model": "claude-test",
                         "content": [{"type": "text", "text": "continued reply"}]}},
        ])
    records.extend(extra_records)
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return f


def _write_cli_store(
    chats_dir: Path,
    session_id: str = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    cwd: str = "/Users/test/demo",
    *,
    title: str = "Store db session",
    user_text: str = "<user_query>\nhello from store db\n</user_query>",
    meta_extra: dict | None = None,
) -> Path:
    """Minimal Cursor CLI store.db with a tool call + result."""
    sess = chats_dir / "deadbeefcafebabe" / session_id
    sess.mkdir(parents=True)
    (sess / "meta.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "createdAtMs": 1_700_000_000_000,
            "updatedAtMs": 1_700_000_100_000,
            "hasConversation": True,
            "title": title,
            "cwd": cwd,
        }),
        encoding="utf-8",
    )
    db = sess / "store.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    meta = {
        "agentId": session_id,
        "latestRootBlobId": "0" * 64,
        "name": title,
        "mode": "default",
        "createdAt": 1_700_000_000_000,
        "lastUsedModel": "grok-test",
    }
    meta.update(meta_extra or {})
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('0', ?)",
        (json.dumps(meta).encode("utf-8").hex(),),
    )
    blobs = [
        ("a" * 64, json.dumps({
            "role": "user",
            "content": [{
                "type": "text",
                "text": user_text,
            }],
        }).encode()),
        ("b" * 64, json.dumps({
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Running a command."},
                {
                    "type": "tool-call",
                    "toolCallId": "call-1",
                    "toolName": "Shell",
                    "args": {"command": "echo hi", "description": "say hi"},
                },
            ],
        }).encode()),
        ("c" * 64, json.dumps({
            "role": "tool",
            "content": [{
                "type": "tool-result",
                "toolCallId": "call-1",
                "toolName": "Shell",
                "result": "Exit code: 0\n\nhi\n",
            }],
        }).encode()),
        ("d" * 64, json.dumps({
            "role": "assistant",
            "content": [{"type": "text", "text": "Done."}],
        }).encode()),
    ]
    for bid, data in blobs:
        conn.execute("INSERT INTO blobs(id, data) VALUES (?, ?)", (bid, data))
    conn.commit()
    conn.close()
    return db


def _write_cli_session(
    projects_dir: Path,
    session_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    project_slug: str = "Users-test-demo",
    user_text: str = "hello from cursor cli",
) -> Path:
    """Minimal Cursor CLI agent-transcripts JSONL under a fake projects dir."""
    session_dir = projects_dir / project_slug / "agent-transcripts" / session_id
    session_dir.mkdir(parents=True)
    path = session_dir / f"{session_id}.jsonl"
    records = [
        {
            "role": "user",
            "message": {
                "content": [{
                    "type": "text",
                    "text": (
                        f"<timestamp>Monday, Jan 1, 2024</timestamp>\n"
                        f"<user_query>\n{user_text}\n</user_query>"
                    ),
                }],
            },
        },
        {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Looking into it."},
                    {
                        "type": "tool_use",
                        "name": "Shell",
                        "input": {
                            "command": "echo hi",
                            "description": "say hi",
                            "working_directory": "/Users/test/demo",
                        },
                    },
                ],
            },
        },
        {
            "role": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "StrReplace",
                        "input": {
                            "path": "/Users/test/demo/a.py",
                            "old_string": "x = 1",
                            "new_string": "x = 2",
                        },
                    },
                ],
            },
        },
    ]
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


def _write_guardian_sessions(codex_home: Path) -> tuple[Path, Path, Path]:
    sessions = codex_home / "sessions" / "2026" / "01" / "01"
    sessions.mkdir(parents=True)
    parent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    guardian_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    parent = sessions / f"rollout-2026-01-01T00-00-00-{parent_id}.jsonl"
    guardian = sessions / f"rollout-2026-01-01T00-00-01-{guardian_id}.jsonl"
    image = sessions / "fixture.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    inline_image = "data:image/png;base64,iVBORw0KGgo="
    parent_records = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
         "payload": {"id": parent_id, "cwd": "/tmp/proj"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "parent task"}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text": f'<image name=[Image #1] path="{image}">'},
             {"type": "input_image", "image_url": inline_image},
             {"type": "input_text", "text": "</image>"},
             {"type": "input_text", "text": "look at this"},
         ]}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "look at this",
                     "images": [], "local_images": [str(image)]}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [
             {"type": "input_text", "text": '<image name=[Image #1] path="/missing.png">'},
             {"type": "input_image", "image_url": inline_image},
             {"type": "input_text", "text": "</image>"},
             {"type": "input_text", "text": "missing image"},
         ]}},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "missing image",
                     "images": [], "local_images": ["/missing.png"]}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "parent-turn",
                     "model_context_window": 300000, "collaboration_mode_kind": "default"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "turn_context",
         "payload": {"turn_id": "parent-turn", "model": "codex-test", "effort": "medium",
                     "approval_policy": "on-request", "sandbox_policy": {"type": "workspace-write"}}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "metadata turn"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "metadata answer"}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "model_context_window": 300000,
             "total_token_usage": {"input_tokens": 100, "output_tokens": 10},
         }}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "parent-turn",
                     "duration_ms": 3000, "time_to_first_token_ms": 700}},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "compacted",
         "payload": {
             "window_number": 1,
             "previous_window_id": "window-old",
             "window_id": "window-new",
             "message": "",
             "replacement_history": [
                 {"type": "message", "role": "user", "content": [
                     {"type": "input_text", "text": "retained prompt"},
                 ]},
                 {"type": "compaction", "id": "cmp-test", "encrypted_content": "ciphertext"},
             ],
         }},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "world_state",
         "payload": {"full": True, "state": {"environments": {"local": {"shell": "zsh"}}}}},
        {"timestamp": "2026-01-01T00:00:06Z", "type": "event_msg",
         "payload": {"type": "context_compacted"}},
    ]
    planned = {
        "command": ["/bin/zsh", "-lc", "python3 -m unittest tests.test_security"],
        "cwd": "/tmp/proj",
        "justification": "Run local tests?",
        "sandbox_permissions": "require_escalated",
        "tool": "exec_command",
    }
    guardian_records = [
        {"timestamp": "2026-01-01T00:00:02Z", "type": "session_meta",
         "payload": {
             "id": guardian_id, "parent_thread_id": parent_id,
             "thread_source": "subagent", "source": {"subagent": {"other": "guardian"}},
             "cwd": "/tmp/proj", "base_instructions": {"text": "Review actions."},
         }},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1",
                     "model_context_window": 200000,
                     "collaboration_mode_kind": "codex-auto-review"}},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "turn_context",
         "payload": {"turn_id": "turn-1", "model": "guardian-test", "effort": "low",
                     "approval_policy": "never", "sandbox_policy": {"type": "read-only"}}},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg",
         "payload": {"type": "user_message",
                     "message": "Review this action.\nPlanned action JSON:\n" + json.dumps(planned)}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": '{"outcome":"allow"}'}},
        {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "model_context_window": 200000,
             "total_token_usage": {"input_tokens": 1200, "output_tokens": 20},
         }}},
        {"timestamp": "2026-01-01T00:00:05Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-1",
                     "duration_ms": 2000, "time_to_first_token_ms": 500}},
    ]
    parent.write_text("\n".join(json.dumps(r) for r in parent_records) + "\n")
    guardian.write_text("\n".join(json.dumps(r) for r in guardian_records) + "\n")
    return parent, guardian, image


def _write_opencode_db(db_path: Path) -> tuple[str, str]:
    """A minimal opencode.db holding a parent session and its `task` sub-agent.

    Covers every part type the parser has a branch for — text, reasoning, tool
    (completed / error / pending), file, synthetic text, subtask, compaction,
    retry, patch and step-finish — so schema conformance is exercised end to
    end. Returns the (parent, sub-agent) session ids.
    """
    parent_id = "ses_parent000000000000000000"
    child_id = "ses_child0000000000000000000"
    model = json.dumps({"providerID": "openrouter", "modelID": "x-ai/grok-test"})
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT,
            slug TEXT NOT NULL, directory TEXT NOT NULL, title TEXT NOT NULL,
            version TEXT NOT NULL, cost REAL DEFAULT 0 NOT NULL, agent TEXT,
            model TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            time_created INTEGER NOT NULL, data TEXT NOT NULL
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
            data TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO session (id, project_id, parent_id, slug, directory, title, "
        "version, cost, agent, model, time_created, time_updated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (parent_id, "proj1", None, "brisk-otter", "/tmp/proj", "Fixture session",
             "1.18.18", 0.25, "build", model, 1_700_000_000_000, 1_700_000_100_000),
            # opencode leaves this placeholder title until the model names the
            # session; the parser must fall back to the first user prompt.
            (child_id, "proj1", parent_id, "tidy-cactus", "/tmp/proj",
             "New session - 2026-01-01T00:00:00.000Z", "1.18.18", 0.05, "explore",
             model, 1_700_000_010_000, 1_700_000_050_000),
        ],
    )

    def message(mid, sid, role, created, **extra):
        data = {"role": role, "time": {"created": created}, **extra}
        if role == "assistant":
            data.setdefault("modelID", "x-ai/grok-test")
            data.setdefault("providerID", "openrouter")
            data.setdefault("tokens", {"input": 100, "output": 20, "reasoning": 5,
                                       "cache": {"read": 10, "write": 0}})
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) VALUES (?,?,?,?)",
            (mid, sid, created, json.dumps(data)),
        )

    def part(pid, mid, sid, data):
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, data) VALUES (?,?,?,?)",
            (pid, mid, sid, json.dumps(data)),
        )

    # --- parent session -----------------------------------------------------
    message("msg_p1", parent_id, "user", 1_700_000_000_100, agent="build")
    part("prt_p1a", "msg_p1", parent_id, {"type": "text", "text": "look at the repo"})
    part("prt_p1b", "msg_p1", parent_id, {
        "type": "file", "mime": "image/png", "filename": "shot.png",
        "url": "data:image/png;base64,iVBORw0KGgo=",
    })
    part("prt_p1c", "msg_p1", parent_id, {
        "type": "text", "synthetic": True, "text": "<task>background result</task>"})

    message("msg_p2", parent_id, "assistant", 1_700_000_000_200,
            cost=0.2, finish="tool-calls", agent="build", variant="high")
    part("prt_p2a", "msg_p2", parent_id, {"type": "step-start", "snapshot": "abc123"})
    # Providers hang a token-by-token `reasoning_details` transcript off parts;
    # it is many times the size of the text and must never reach the viewer.
    # …and, for models that return one, an opaque `reasoning.encrypted` blob
    # that makes the visible text a summary rather than the real reasoning.
    token_noise = {"openrouter": {"reasoning_details": [
        {"type": "reasoning.summary", "summary": word, "index": 0}
        for word in ("Need", " to", " look", " around", " first", ".")
    ] + [
        {"type": "reasoning.encrypted", "id": "rs_fixture", "index": 1,
         "format": "xai-responses-v1", "data": "b3BhcXVl"},
    ]}}
    part("prt_p2b", "msg_p2", parent_id, {
        "type": "reasoning", "text": "Need to look around first.",
        "time": {"start": 1_700_000_000_250, "end": 1_700_000_000_290},
        "metadata": token_noise})
    part("prt_p2c", "msg_p2", parent_id, {
        "type": "text", "text": "Looking around.",
        "time": {"start": 1_700_000_000_295, "end": 1_700_000_000_299}})
    part("prt_p2d", "msg_p2", parent_id, {
        "type": "tool", "tool": "read", "callID": "call-1",
        "state": {"status": "completed", "title": "main.py",
                  "input": {"filePath": "/tmp/proj/main.py", "offset": 1, "limit": 20},
                  "output": "<path>/tmp/proj/main.py</path>",
                  "metadata": {"preview": "1: x = 1", "truncated": False},
                  "time": {"start": 1_700_000_000_300, "end": 1_700_000_000_350}},
        "metadata": token_noise})
    part("prt_p2e", "msg_p2", parent_id, {
        "type": "tool", "tool": "edit", "callID": "call-2",
        "state": {"status": "error", "error": "oldString not found",
                  "input": {"filePath": "/tmp/proj/main.py", "oldString": "a",
                            "newString": "b", "replaceAll": True},
                  "time": {"start": 1_700_000_000_360, "end": 1_700_000_000_370}}})
    part("prt_p2f", "msg_p2", parent_id, {
        "type": "tool", "tool": "task", "callID": "call-3",
        "state": {"status": "completed", "title": "Explore repo",
                  "input": {"description": "Explore repo", "subagent_type": "explore",
                            "prompt": "look at everything"},
                  "output": f'<task id="{child_id}" state="completed">done</task>',
                  "metadata": {"parentSessionId": parent_id, "sessionId": child_id},
                  "time": {"start": 1_700_000_000_380, "end": 1_700_000_000_390}}})
    part("prt_p2g", "msg_p2", parent_id, {
        "type": "tool", "tool": "grep", "callID": "call-4",
        "state": {"status": "pending", "input": {}, "raw": ""}})
    part("prt_p2h", "msg_p2", parent_id, {
        "type": "patch", "hash": "deadbeef", "files": ["/tmp/proj/main.py"]})
    part("prt_p2i", "msg_p2", parent_id, {
        "type": "retry", "attempt": 1, "time": {"created": 1_700_000_000_150},
        "error": {"name": "APIError", "data": {"message": "429 slow down"}}})
    part("prt_p2j", "msg_p2", parent_id, {
        "type": "step-finish", "reason": "tool-calls", "cost": 0.2,
        "tokens": {"input": 100, "output": 20, "reasoning": 5,
                   "cache": {"read": 10, "write": 0}}})

    message("msg_p3", parent_id, "user", 1_700_000_000_300, agent="build")
    part("prt_p3a", "msg_p3", parent_id, {
        "type": "subtask", "prompt": "review the diff", "description": "Review",
        "agent": "plan"})
    part("prt_p3b", "msg_p3", parent_id, {"type": "compaction", "auto": True})

    # An assistant turn that only failed: no blocks, just the error notice.
    message("msg_p4", parent_id, "assistant", 1_700_000_000_400,
            error={"name": "ProviderAuthError",
                   "data": {"message": "Missing Authentication header"}})

    # --- sub-agent session --------------------------------------------------
    message("msg_c1", child_id, "user", 1_700_000_010_100, agent="explore")
    part("prt_c1a", "msg_c1", child_id, {"type": "text", "text": "look at everything"})
    message("msg_c2", child_id, "assistant", 1_700_000_010_200,
            cost=0.05, finish="stop", agent="explore")
    part("prt_c2a", "msg_c2", child_id, {"type": "text", "text": "Here is what I found."})

    conn.commit()
    conn.close()
    return parent_id, child_id
