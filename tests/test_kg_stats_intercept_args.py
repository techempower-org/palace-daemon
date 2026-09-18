"""`mempalace_kg_stats` with arguments must still hit the fast intercept (#286).

Incident 2026-09-17, 19:45-20:22 PDT. py-spy on the live daemon showed seven
`ThreadPoolExecutor-1_*` threads parked in the same stack:

    stats (mempalace/knowledge_graph_age.py:1046)   # with self._lock:
    tool_kg_stats (mempalace/mcp_server.py:5233)
    handle_request (mempalace/mcp_server.py:8179)

Those were `mempalace_kg_stats` MCP calls that reached the REAL tool, because
the /mcp fast intercept only fired when the call carried **no arguments**.
Each waited on the KG lock, and whichever won it ran a 10-20 s full-graph
Cypher walk under mine load — re-issued every 6-20 s for hours, long after the
2-5 s-budget hook clients had given up. The access log showed no /mcp traffic
for the last 20 minutes of the outage: the work outlived its callers.

The fast SQL payload ignores arguments today anyway, so a `kg_stats` call that
carries any is answered from the same counts rather than falling through.
`list_wings` / `get_taxonomy` deliberately keep the no-argument condition — a
future filtered variant of those should still fall through to the real tool.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_kg_stats_intercept_args.py -q
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import fast_intercept
import main  # noqa: E402


def _body(tool, arguments=None):
    params = {"name": tool}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params}


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class InterceptWithArgumentsTests(unittest.IsolatedAsyncioTestCase):
    async def _route(self, body):
        """Returns (payload_or_None, slow_path_called)."""
        with patch.object(main, "_check_auth", side_effect=lambda *_a, **_k: None), \
             patch.object(main, "_fast_mcp_kg_stats_payload", return_value={"fast": "kg"}), \
             patch.object(main, "_fast_mcp_status_payload", return_value={"fast": "status"}), \
             patch.object(main, "_fast_mcp_list_wings_payload", return_value={"fast": "wings"}), \
             patch.object(main, "_fast_mcp_get_taxonomy_payload", return_value={"fast": "tax"}), \
             patch.object(main, "_call", new_callable=AsyncMock) as slow:
            slow.return_value = {"jsonrpc": "2.0", "id": 7, "result": {"slow": True}}
            resp = await main.mcp_proxy(_Req(body), x_api_key=None)
        raw = json.loads(bytes(resp.body).decode())
        return raw, slow.called

    async def test_kg_stats_with_a_wing_argument_hits_the_fast_path(self):
        raw, slow_called = await self._route(_body("mempalace_kg_stats", {"wing": "2g"}))

        self.assertFalse(slow_called, "an argument must no longer bypass the intercept")
        text = raw["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text), {"fast": "kg"})

    async def test_kg_stats_without_arguments_still_hits_the_fast_path(self):
        """Control: the behaviour that already worked must be unchanged."""
        raw, slow_called = await self._route(_body("mempalace_kg_stats"))

        self.assertFalse(slow_called)
        self.assertEqual(json.loads(raw["result"]["content"][0]["text"]), {"fast": "kg"})

    async def test_status_with_arguments_hits_the_fast_path(self):
        raw, slow_called = await self._route(_body("mempalace_status", {"verbose": True}))

        self.assertFalse(slow_called)
        self.assertEqual(json.loads(raw["result"]["content"][0]["text"]), {"fast": "status"})

    async def test_list_wings_with_arguments_still_falls_through(self):
        """Deliberately unchanged: a future filtered list_wings must reach the
        real tool rather than be answered from an unfiltered fast payload."""
        _raw, slow_called = await self._route(_body("mempalace_list_wings", {"wing": "2g"}))

        self.assertTrue(slow_called, "list_wings keeps the no-argument condition")

    async def test_get_taxonomy_with_arguments_still_falls_through(self):
        _raw, slow_called = await self._route(_body("mempalace_get_taxonomy", {"x": 1}))

        self.assertTrue(slow_called)

    async def test_list_wings_without_arguments_still_intercepts(self):
        """Control for the pair above."""
        raw, slow_called = await self._route(_body("mempalace_list_wings"))

        self.assertFalse(slow_called)
        self.assertEqual(json.loads(raw["result"]["content"][0]["text"]), {"fast": "wings"})


if __name__ == "__main__":
    unittest.main()
