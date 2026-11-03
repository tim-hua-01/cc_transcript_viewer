#!/usr/bin/env python3
"""The session catalog: every transcript source as one list of sessions.

Owns where transcripts are read from (configure_sources), the unified,
time-sorted session list with sub-agents grouped under their parents,
loading a session by id under the allowed roots, viewer-owned custom names,
persistence of the parsers' summary caches, and the optional read-only Mica
decoration. server.py serves this over HTTP; search workers import only this
module to load sessions.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

import claude_parser as claude
import codex_parser as codex
import common
import cursor_parser as cursor
import opencode_parser as opencode
from mica import store as micastore

# The transcript sources, in sidebar merge order. Every parser module exposes
# configure() / list_sessions() / SUMMARY_CACHE; sessions that live in a
# database rather than a file on disk are additionally routed by the scheme
# table below, real transcript paths by _path_parser().
PARSERS = {
    "claude": claude,
    "codex": codex,
    "cursor": cursor,
    "opencode": opencode,
}

# Scheme prefix -> parse-by-id, for database-backed sessions.
SCHEME_PARSERS = {
    cursor.SESSION_SCHEME: cursor.parse_session_by_id,
    cursor.CLI_SESSION_SCHEME: cursor.parse_cli_store_by_id,
    opencode.SESSION_SCHEME: opencode.parse_session_by_id,
}
# Session ids that name a row in a database rather than a file on disk. These
# never resolve to a transcript path, so every path-based check skips them.
SYNTHETIC_SCHEMES = tuple(SCHEME_PARSERS)
DEFAULT_CUSTOM_NAMES_FILE = (
    Path.home() / ".config" / "cc_transcript_viewer" / "names.json"
)
# Set by main() so handlers can reach it.
CUSTOM_NAMES_FILE = DEFAULT_CUSTOM_NAMES_FILE
# Read-only view of Mica, or None when the feature is off (the
# default unless a store exists at micastore.DEFAULT_STORE_DIR or --mica).
MICA: micastore.StoreReader | None = None
MICA_ERROR: str | None = None  # last failure attaching store state to the list

# Full-session parsers for Mica's copies, by the parser name the store records.
MICA_PARSERS = {"claude": claude, "codex": codex}
CACHE_FILE = Path.home() / ".cache" / "transcript_viewer" / "summaries.json"


# ---------------------------------------------------------------------------
# Viewer-owned custom transcript names
# ---------------------------------------------------------------------------
_CUSTOM_NAMES_LOCK = threading.Lock()
_CUSTOM_NAMES_CACHE: tuple[int | None, dict[str, str]] | None = None


def _load_custom_names() -> dict[str, str]:
    """Load the custom-name map, refreshing when it changes on disk."""
    global _CUSTOM_NAMES_CACHE
    with _CUSTOM_NAMES_LOCK:
        try:
            mtime = CUSTOM_NAMES_FILE.stat().st_mtime_ns
        except OSError:
            mtime = None
        if _CUSTOM_NAMES_CACHE and _CUSTOM_NAMES_CACHE[0] == mtime:
            return _CUSTOM_NAMES_CACHE[1].copy()
        try:
            raw = json.loads(CUSTOM_NAMES_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        names = {
            str(key): value
            for key, value in raw.items()
            if isinstance(value, str) and value.strip()
        } if isinstance(raw, dict) else {}
        _CUSTOM_NAMES_CACHE = (mtime, names)
        return names.copy()


def _custom_name_key(session: dict) -> str:
    """Stable across transcript moves, including Codex archival."""
    parts = [session.get("agent", ""), session.get("id", "")]
    if session.get("is_subagent") and session.get("parent_id"):
        parts.insert(1, session["parent_id"])
    return ":".join(str(part) for part in parts)


def apply_custom_name(session: dict) -> dict:
    """Add original/custom title fields and select the effective title."""
    original = session.get("original_title") or session.get("title") or "(untitled session)"
    custom = _load_custom_names().get(_custom_name_key(session), "")
    session["original_title"] = original
    session["custom_title"] = custom
    session["title"] = custom or original
    return session


def set_custom_name(session: dict, name: str) -> None:
    """Persist one override using an atomic same-directory replacement."""
    global _CUSTOM_NAMES_CACHE
    key = _custom_name_key(session)
    with _CUSTOM_NAMES_LOCK:
        try:
            raw = json.loads(CUSTOM_NAMES_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        names = raw if isinstance(raw, dict) else {}
        if name:
            names[key] = name
        else:
            names.pop(key, None)
        CUSTOM_NAMES_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = CUSTOM_NAMES_FILE.with_name(CUSTOM_NAMES_FILE.name + ".tmp")
        temp.write_text(
            json.dumps(names, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.replace(CUSTOM_NAMES_FILE)
        _CUSTOM_NAMES_CACHE = (CUSTOM_NAMES_FILE.stat().st_mtime_ns, names.copy())


# ---------------------------------------------------------------------------
# Summary-cache persistence (the parsers own the caches; we own the file)
# ---------------------------------------------------------------------------
_CACHE_VERSION = 2
_CACHE_LOCK = threading.Lock()
_CACHE_LOADED = False
_PARSER_CACHES = {name: parser.SUMMARY_CACHE for name, parser in PARSERS.items()}


def load_summary_caches() -> None:
    """Load viewer-owned sidebar summaries from disk once, best effort."""
    global _CACHE_LOADED
    with _CACHE_LOCK:
        if _CACHE_LOADED:
            return
        _CACHE_LOADED = True
        try:
            payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("version") != _CACHE_VERSION:
            return
        for name, cache in _PARSER_CACHES.items():
            cache.load(payload.get(name) or {})


def save_summary_caches() -> None:
    """Atomically persist a snapshot of every parser's summary cache, best effort.

    Merged over what is already on disk, never a blind overwrite: several
    viewer processes can share this file (two servers, a CLI run), and each
    holds summaries only for what it has looked at. Overwriting with one
    process's view would silently discard the others' entries and force full
    rescans on their next start. A stale merged-in entry costs nothing — its
    fingerprint just misses and that one file is recomputed.
    """
    if not any(cache.dirty for cache in _PARSER_CACHES.values()):
        return
    snapshots = {name: cache.snapshot() for name, cache in _PARSER_CACHES.items()}
    try:
        existing = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if existing.get("version") != _CACHE_VERSION:
            existing = {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    payload: dict = {"version": _CACHE_VERSION}
    for name, (_gen, data) in snapshots.items():
        merged = dict(existing.get(name) or {})
        merged.update(data)
        payload[name] = merged
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
        temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        temp.replace(CACHE_FILE)
    except OSError:
        return
    # Clear each dirty flag only if nothing changed since its snapshot, so a
    # concurrent update is never marked as saved.
    for name, (generation, _data) in snapshots.items():
        _PARSER_CACHES[name].mark_saved(generation)


# ---------------------------------------------------------------------------
# Unified session list / dispatch
# ---------------------------------------------------------------------------
def list_sessions() -> list[dict]:
    """Flat list of every session from every source, newest first."""
    load_summary_caches()
    out: list[dict] = []
    for parser in PARSERS.values():
        try:
            out.extend(parser.list_sessions())
        except Exception:  # noqa: BLE001 — one broken source must not hide the rest
            pass

    _apply_mica(out)
    save_summary_caches()

    for session in out:
        apply_custom_name(session)
    out.sort(key=_recency, reverse=True)

    # Place each sub-agent directly under its parent session rather than at its
    # own mtime slot. An actively-updated parent floats to the top of the
    # time-sorted list while its sub-agents (last touched earlier) sink down and
    # appear to belong to whatever unrelated session precedes them. Grouping
    # keeps a sub-agent visually attached to the session that spawned it.
    parent_files = {s["file"] for s in out if not s.get("is_subagent")}
    subs_by_parent: dict[str, list] = {}
    for s in out:
        if s.get("is_subagent") and s.get("parent_file") in parent_files:
            subs_by_parent.setdefault(s["parent_file"], []).append(s)

    grouped: list[dict] = []
    for s in out:
        if s.get("is_subagent"):
            # Orphans (parent file not in the list) keep their own mtime slot;
            # everything else is emitted under its parent below.
            if s.get("parent_file") not in parent_files:
                grouped.append(s)
            continue
        grouped.append(s)
        grouped.extend(subs_by_parent.get(s["file"], []))
    return grouped


# ---------------------------------------------------------------------------
# Mica (optional, read-only)
# ---------------------------------------------------------------------------
def _apply_mica(sessions: list) -> None:
    """Attach each live session's mica state, and add a row, read from
    Mica's copy, for every captured transcript no live session matches."""
    global MICA_ERROR
    reader = MICA
    if reader is None:
        return
    try:
        live_keys = set()
        for s in sessions:
            path = s.get("file") or ""
            if not path or path.startswith(SYNTHETIC_SCHEMES):
                continue
            key = reader.key_for_path(path)
            if key:
                s["mica"] = reader.badge(key)
                live_keys.add(key)
        for key, entry in reader.index().items():
            if key not in live_keys:
                sessions.append(_mica_copy_row(reader, key, entry))
        MICA_ERROR = None
    except Exception as e:  # noqa: BLE001 - report it rather than break the list
        MICA_ERROR = f"could not read the store: {e}"


def _mica_copy_row(reader, key: str, entry: dict) -> dict:
    badge = dict(reader.badge(key), copy=True)
    parser = MICA_PARSERS.get(entry.get("parser"))
    record = reader.record(key)
    gen = reader.best_generation(record) if record else None
    gen_path = reader.generation_path(key, gen) if gen else None
    if parser is not None and gen_path is not None:
        try:
            summary = dict(parser.session_summary(gen_path))
            summary["mica"] = dict(badge, generation=gen["id"])
            return summary
        except (OSError, ValueError) as e:
            badge["error"] = f"capture could not be read: {e}"
    else:
        badge["error"] = "no readable capture"
    # Still list it, so nothing in the store goes unseen.
    return common.make_summary(agent=entry.get("parser") or "", id=key,
                               file=str(gen_path or reader.files_dir / key),
                               title=Path(str(entry.get("path") or key)).name, mica=badge)


def mica_status() -> dict:
    if MICA is None:
        return {"enabled": False}
    try:
        status = MICA.status()
    except Exception as e:  # noqa: BLE001
        return {"enabled": True, "running": False, "error": f"could not read the store: {e}"}
    if MICA_ERROR:
        status["error"] = MICA_ERROR
    return status


def mica_compare(file_id: str) -> dict:
    """How a session's live transcript compares with its captured copy.

    ``file_id`` may be a live transcript path or a store copy's path; either
    way the comparison is between the live file (if any) and the capture.
    """
    if MICA is None:
        return {"state": "disabled"}
    if file_id.startswith(SYNTHETIC_SCHEMES):
        return {"state": "unsupported"}
    target = resolve_transcript_file(file_id)
    located = MICA.locate(target)
    if located:
        key, gen, record = located
        live = record.get("path") if record.get("status") == "active" else None
        result = MICA.compare(key, live)
        result["viewing_generation"] = gen.get("id")
        return result
    key = MICA.key_for_path(str(target))
    if key is None:
        return {"state": "untracked"}
    return MICA.compare(key, target)


def _recency(s: dict) -> float:
    """When the conversation last had real activity — the sidebar's sort key.

    The last recorded timestamp, not the file mtime: Claude Code appends
    post-hoc bookkeeping (`last-prompt` records for its /resume picker) to old
    transcripts, which bumps their mtime days after the conversation ended and
    would shuffle them to the top of a mtime-sorted list while their visible
    "Nd ago" label still shows the real age.
    """
    ts = s.get("last_ts")
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return s.get("mtime") or 0


def is_within(target: Path, root: Path) -> bool:
    try:
        root = root.resolve()
    except OSError:
        return False
    return target == root or root in target.parents


def _path_parser(target: Path):
    """The parse function that owns a real transcript path, or None if the
    path lies outside every allowed transcript root."""
    if MICA is not None and is_within(target, MICA.files_dir):
        located = MICA.locate(target)
        parser = MICA_PARSERS.get(located[2].get("parser")) if located else None
        return parser.parse_session if parser else None
    if is_within(target, claude.PROJECTS_DIR):
        return claude.parse_session
    if is_within(target, codex.SESSIONS_DIR) or (
        codex.ARCHIVED_SESSIONS_DIR.exists() and is_within(target, codex.ARCHIVED_SESSIONS_DIR)
    ):
        return codex.parse_session
    if cursor.is_cli_transcript(target):
        return cursor.parse_cli_session
    return None


def parse_session(target: Path) -> dict | None:
    """Dispatch to the right parser based on which transcript root owns the file.

    Returns None if the file is outside every allowed root.
    """
    parse = _path_parser(target)
    return parse(target) if parse else None


def load_session(file_id: str) -> dict | None:
    """Resolve a session id to parsed data, for both `/api/session` and search.

    Cursor IDE sessions use ``cursordb:<composerId>``; Cursor CLI store.db
    sessions use ``cursorcli:<sessionId>``; opencode sessions use
    ``opencode:<sessionID>``. Everything else is a real transcript path that
    must resolve under an allowed root.
    """
    for scheme, parse_by_id in SCHEME_PARSERS.items():
        if file_id.startswith(scheme):
            data = parse_by_id(file_id[len(scheme):])
            return apply_custom_name(data) if data is not None else None
    target = Path(file_id).expanduser().resolve()
    if not target.exists():
        return None
    data = parse_session(target)
    if data is None:
        return None
    located = MICA.locate(target) if MICA is not None else None
    if located:
        key, gen, record = located
        data["mica_copy"] = {
            "key": key,
            "generation": gen.get("id"),
            "reason": gen.get("reason"),
            "started": gen.get("started"),
            "closed": gen.get("closed"),
            "close_reason": gen.get("close_reason"),
            "original_path": record.get("path"),
            "live": record.get("status") == "active",
        }
    return apply_custom_name(data)


def resolve_transcript_file(file_id: str) -> Path:
    """Resolve a session id to its on-disk transcript under an allowed root.

    Raises FileNotFoundError if the id is synthetic (Cursor and opencode
    database sessions have no transcript file) or missing, and PermissionError
    if the path lies outside every transcript root.
    """
    if file_id.startswith(SYNTHETIC_SCHEMES):
        raise FileNotFoundError(file_id)
    target = Path(file_id).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(file_id)
    if _path_parser(target) is None:
        raise PermissionError(file_id)
    return target


def session_file_mtime(file_id: str) -> float | None:
    """Return a real transcript's mtime without parsing any session content.

    Synthetic database ids return None; their change detection continues to use
    the regular session-list refresh.
    """
    if file_id.startswith(SYNTHETIC_SCHEMES):
        return None
    return resolve_transcript_file(file_id).stat().st_mtime


def source_settings() -> dict:
    """Where this process reads transcripts from, for configure_sources() in
    a search worker process (a spawned process doesn't inherit it)."""
    return {
        "projects_dir": str(claude.PROJECTS_DIR),
        "codex_home": str(codex.CODEX_HOME),
        "cursor_db": str(cursor.DB_PATH),
        "cursor_projects": str(cursor.PROJECTS_DIR),
        "cursor_chats": str(cursor.CHATS_DIR),
        "opencode_db": str(opencode.DB_PATH),
        "mica": str(MICA.root) if MICA is not None else None,
        "custom_names": str(CUSTOM_NAMES_FILE),
    }


def configure_sources(settings: dict) -> None:
    """Point every parser (and Mica and the custom-names file) at ``settings``."""
    global CUSTOM_NAMES_FILE
    CUSTOM_NAMES_FILE = Path(settings["custom_names"]).expanduser()
    claude.configure(Path(settings["projects_dir"]))
    codex.configure(Path(settings["codex_home"]))
    cursor.configure(
        Path(settings["cursor_db"]),
        projects_dir=Path(settings["cursor_projects"]),
        chats_dir=Path(settings["cursor_chats"]),
    )
    opencode.configure(Path(settings["opencode_db"]))
    mica = settings.get("mica")
    configure_mica(Path(mica).expanduser() if mica else None, enabled=bool(mica))


def configure_mica(path, enabled: bool = True) -> None:
    """Turn the read-only mica view on (when a store exists at ``path``) or off."""
    global MICA
    MICA = None
    if not enabled or path is None:
        return
    reader = micastore.StoreReader(path)
    if reader.available():
        MICA = reader
