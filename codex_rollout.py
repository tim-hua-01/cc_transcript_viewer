#!/usr/bin/env python3
"""Writing Codex rollout JSONL — the parts that aren't specific to any source.

Both exporters (``cursor_to_codex.py``, ``opencode_to_codex.py``) emit the same
``{timestamp, type, payload}`` line format under the same ``YYYY/MM/DD/
rollout-<local-time>-<id>.jsonl`` naming that Codex itself uses. That shape is
what makes an export readable by Codex-aware tooling, so it lives here once
rather than being reimplemented per source and quietly drifting.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def iso(value) -> str | None:
    """ISO-8601 UTC string from epoch-ms, or an already-ISO string unchanged."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)) and value > 0:
        stamp = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return None


def as_text(value) -> str:
    """Best-effort string for a payload field that may be structured."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def line(kind: str, timestamp: str, payload: dict) -> dict:
    """One rollout record."""
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def rollout_filename(session_id: str, start: str) -> str:
    """``rollout-<local-time>-<id>.jsonl``, matching Codex's own naming."""
    try:
        stamp = datetime.fromisoformat((start or "").replace("Z", "+00:00")).astimezone()
    except ValueError:
        stamp = datetime.now().astimezone()
    return f"rollout-{stamp.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"


def output_path(out_dir: Path, session_id: str, start: str, layout: str) -> Path:
    """Destination for one rollout: Codex's dated directories, or flat."""
    name = rollout_filename(session_id, start)
    if layout == "flat":
        return out_dir / name
    stamp = name[len("rollout-") : len("rollout-") + 10]
    return out_dir / stamp[:4] / stamp[5:7] / stamp[8:10] / name


def write_rollout(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in lines:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
