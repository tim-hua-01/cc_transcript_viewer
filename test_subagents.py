"""Unit tests for sub-agent linkage and timeline placement (zero-dependency).

Run with:  python3 -m unittest test_subagents

These cover the new server-side logic that ties a parent session's Task/Agent
calls to the sub-agent transcripts they spawned, and that titles/places
fleet-style "teammate" agents whose first message is wrapped.
"""

import json
import tempfile
import unittest
from pathlib import Path

import server


class SubagentLinkageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sub = Path(self._tmp.name) / "proj" / "sid" / "subagents"
        self.sub.mkdir(parents=True)
        # Caches are module-global; isolate each test.
        server._SUBAGENT_INDEX_CACHE.clear()
        server._SUBAGENT_FILE_CACHE.clear()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_agent(self, agent_id, meta, content, ts="2026-01-01T00:00:00Z"):
        (self.sub / f"agent-{agent_id}.jsonl").write_text(
            json.dumps({
                "type": "user",
                "timestamp": ts,
                "message": {"role": "user", "content": content},
            }) + "\n",
            encoding="utf-8",
        )
        (self.sub / f"agent-{agent_id}.meta.json").write_text(
            json.dumps(meta), encoding="utf-8")
        return str(self.sub / f"agent-{agent_id}.jsonl")

    def test_subagents_dir_for(self):
        proj = Path(self._tmp.name) / "proj"
        self.assertEqual(server._subagents_dir_for(proj / "sid.jsonl"), self.sub)
        self.assertEqual(server._subagents_dir_for(self.sub / "agent-x.jsonl"), self.sub)

    def test_link_by_tool_use_id(self):
        f = self._write_agent(
            "a1",
            {"agentType": "Explore", "description": "map code", "toolUseId": "toolu_X"},
            "do the thing",
        )
        idx = server._subagent_index(self.sub)
        self.assertEqual(idx["by_id"].get("toolu_X"), f)
        self.assertEqual(server._resolve_subagent_file(idx, "toolu_X", ""), f)
        agent = idx["agents"][0]
        self.assertEqual(agent["agent_type"], "Explore")
        self.assertEqual(agent["title"], "map code")
        self.assertEqual(agent["first_ts"], "2026-01-01T00:00:00Z")

    def test_link_by_prompt_when_no_tool_use_id(self):
        f = self._write_agent("b2", {"agentType": "worker"}, "Fix the parser bug in module X")
        idx = server._subagent_index(self.sub)
        self.assertEqual(idx["by_id"], {})
        self.assertEqual(
            server._resolve_subagent_file(idx, "no-such-id", "Fix the parser bug in module X"), f)

    def test_teammate_wrapper_unwrapped_for_title_and_match(self):
        inner = "You are fixing issue 42 in the codebase"
        wrapped = f'<teammate-message teammate_id="lead" summary="Fix issue 42"> {inner} </teammate-message>'
        f = self._write_agent("c3", {"agentType": "survival"}, wrapped)
        idx = server._subagent_index(self.sub)
        # Title comes from the wrapper's summary, not the raw blob.
        self.assertEqual(idx["agents"][0]["title"], "Fix issue 42")
        # The spawning Task call carried the inner prompt, so it must match.
        self.assertEqual(server._resolve_subagent_file(idx, "x", inner), f)

    def test_unwrap_teammate(self):
        self.assertEqual(server._unwrap_teammate("plain prompt"), ("", ""))
        summary, body = server._unwrap_teammate(
            '<teammate-message summary="S"> the body </teammate-message>')
        self.assertEqual(summary, "S")
        self.assertEqual(body, "the body")

    def test_no_match_returns_empty(self):
        self._write_agent("d4", {"agentType": "worker"}, "completely unrelated prompt")
        idx = server._subagent_index(self.sub)
        self.assertEqual(server._resolve_subagent_file(idx, "missing", "something else entirely"), "")

    def test_missing_dir_is_empty(self):
        idx = server._subagent_index(self.sub.parent / "nonexistent")
        self.assertEqual(idx, {"by_id": {}, "by_prompt": [], "agents": []})


if __name__ == "__main__":
    unittest.main()
