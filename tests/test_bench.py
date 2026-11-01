"""Keep benchmark setup outside the operations being measured."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import bench


class SessionListBenchmarkTests(unittest.TestCase):
    def test_cold_list_and_serialization_are_timed_without_extra_scans(self):
        for cold in (True, False):
            with self.subTest(cold=cold):
                clock = [0.0]
                calls = []
                sessions = [{"file": "fixture"}]

                def list_sessions():
                    clock[0] += 2.0 if not calls else 0.1
                    calls.append("list")
                    return sessions

                def dumps(payload):
                    self.assertEqual(calls, ["list", "list"])
                    self.assertEqual(payload, {"sessions": sessions})
                    clock[0] += 0.05
                    return "{}"

                def per_parser(args):
                    self.assertEqual(calls, ["list", "list"])
                    self.assertEqual(args, ["list-parsers", "cold" if cold else "warm"])
                    return {"fixture": {"sessions": 1, "secs": 2.0}}

                fake_server = SimpleNamespace(
                    list_sessions=list_sessions,
                    PARSERS={"fixture": SimpleNamespace(list_sessions=list_sessions)},
                    load_summary_caches=lambda: None,
                )
                with (
                    mock.patch.object(bench, "_server", return_value=fake_server),
                    mock.patch.object(bench, "rss_mb", return_value=1.0),
                    mock.patch.object(bench.time, "perf_counter", side_effect=lambda: clock[0]),
                    mock.patch.object(bench.json, "dumps", side_effect=dumps),
                    mock.patch.object(bench, "run_child", side_effect=per_parser),
                ):
                    result = bench.scenario_list(Path("unused"), cold)

                self.assertEqual(result["list_sessions_secs"], 2.0)
                self.assertEqual(result["list_sessions_repeat_secs"], 0.1)
                self.assertEqual(result["json_dumps_secs"], 0.05)
                self.assertEqual(result["per_parser_first_call"]["fixture"]["secs"], 2.0)
