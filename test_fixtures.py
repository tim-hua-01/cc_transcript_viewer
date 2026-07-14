#!/usr/bin/env python3
"""Shared transcript fixtures for the test suite.

Builders that write minimal-but-valid on-disk transcripts for each source
(Claude Code JSONL, Codex rollout JSONL, Cursor CLI JSONL and store.db), used
by both the security tests and the event-schema conformance tests.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


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
        "command": ["/bin/zsh", "-lc", "python3 -m unittest test_security"],
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
