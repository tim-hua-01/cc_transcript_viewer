#!/usr/bin/env python3
"""Measure the viewer's hot paths against the transcripts on this machine.

Read-only: the summary cache is copied into a temp dir (or started empty for
--cold), so the real ~/.cache file is never written. Each scenario runs in a
fresh interpreter so in-process caches don't leak between them.

    python3 scripts/bench.py                  # every scenario
    python3 scripts/bench.py list search      # just these
    python3 scripts/bench.py open --top 5     # the 5 largest transcripts

Numbers are wall-clock seconds and resident memory (RSS) in MB.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SEARCH_QUERIES = ("the", "function", "zzqxj-no-match")


def rss_mb() -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip()) / 1024


def timed(fn, *args):
    start = time.perf_counter()
    result = fn(*args)
    return result, time.perf_counter() - start


def _server(cache_dir: Path, cold: bool):
    sys.path.insert(0, str(REPO))
    import server
    real_cache = server.CACHE_FILE
    server.CACHE_FILE = cache_dir / "summaries.json"
    if not cold and real_cache.exists():
        shutil.copy(real_cache, server.CACHE_FILE)
    server.configure_mica(server.micastore.DEFAULT_STORE_DIR)
    return server


def scenario_list(cache_dir: Path, cold: bool) -> dict:
    server = _server(cache_dir, cold)
    base = rss_mb()
    per_parser = {}
    for name, parser in server.PARSERS.items():
        server.load_summary_caches()
        rows, secs = timed(parser.list_sessions)
        per_parser[name] = {"sessions": len(rows), "secs": round(secs, 3)}
    sessions, first = timed(server.list_sessions)
    _, again = timed(server.list_sessions)
    body, dump = timed(lambda: json.dumps({"sessions": server.list_sessions()}).encode())
    return {
        "cold": cold, "sessions": len(sessions), "per_parser_first_call": per_parser,
        "list_sessions_secs": round(first, 3), "list_sessions_repeat_secs": round(again, 3),
        "payload_mb": round(len(body) / 1e6, 2), "json_dumps_secs": round(dump, 3),
        "rss_mb": round(rss_mb() - base, 1),
    }


def scenario_open(cache_dir: Path, path: str) -> dict:
    server = _server(cache_dir, cold=False)
    base = rss_mb()
    data, parse = timed(server.load_session, path)
    body, dump = timed(lambda: json.dumps(data).encode())
    return {
        "file": path, "file_mb": round(os.path.getsize(path) / 1e6, 1),
        "load_secs": round(parse, 2), "json_dumps_secs": round(dump, 2),
        "payload_mb": round(len(body) / 1e6, 1), "events": len(data["events"]),
        "rss_mb_after": round(rss_mb() - base, 1),
    }


def scenario_search(cache_dir: Path) -> dict:
    server = _server(cache_dir, cold=False)
    server.list_sessions()
    base = rss_mb()
    out = {"queries": []}
    for i, q in enumerate(SEARCH_QUERIES * 2):
        matches, secs = timed(server.search_sessions, q)
        out["queries"].append({"q": q, "pass": 1 + i // len(SEARCH_QUERIES),
                               "matches": len(matches), "secs": round(secs, 2)})
    texts = [s for entry in server._TEXT_CACHE.values() for s in entry[1:]]
    out["text_cache"] = {
        "sessions": len(server._TEXT_CACHE),
        "chars_m": round(sum(map(len, texts)) / 1e6, 1),
        "bytes_mb": round(sum(map(sys.getsizeof, texts)) / 1e6, 1),
        "wide_strings": sum(1 for s in texts if s and max(s) > "￿"),
        "rss_mb_growth": round(rss_mb() - base, 1),
    }
    _, list_secs = timed(server.list_sessions)
    out["list_sessions_inside_search_secs"] = round(list_secs, 3)
    return out


def largest_transcripts(n: int) -> list[str]:
    sys.path.insert(0, str(REPO))
    import claude_parser, codex_parser
    roots = [claude_parser.DEFAULT_PROJECTS_DIR, codex_parser.DEFAULT_CODEX_HOME / "sessions"]
    files = [p for root in roots if root.exists() for p in root.rglob("*.jsonl")]
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return [str(p) for p in files[:n]]


def run_child(args: list[str]) -> dict:
    out = subprocess.run([sys.executable, __file__, "--child", *args],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out.strip().splitlines()[-1])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="*", default=["list", "open", "search"])
    ap.add_argument("--top", type=int, default=3, help="largest transcripts to open")
    ap.add_argument("--child", nargs="+", help=argparse.SUPPRESS)
    args = ap.parse_intermixed_args()

    if args.child:
        with tempfile.TemporaryDirectory() as td:
            kind, *rest = args.child
            if kind == "list":
                result = scenario_list(Path(td), cold=rest == ["cold"])
            elif kind == "open":
                result = scenario_open(Path(td), rest[0])
            else:
                result = scenario_search(Path(td))
        print(json.dumps(result))
        return

    for name in args.scenarios:
        if name == "list":
            jobs = [["list", "cold"], ["list", "warm"]]
        elif name == "open":
            jobs = [["open", path] for path in largest_transcripts(args.top)]
        elif name == "search":
            jobs = [["search"]]
        else:
            ap.error(f"unknown scenario {name!r}")
        for job in jobs:
            label = job[0] if job[0] == "open" else " ".join(job)
            print(json.dumps({"scenario": label, **run_child(job)}, indent=1), flush=True)


if __name__ == "__main__":
    main()
