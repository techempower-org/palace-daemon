"""Tests for /mcp fast intercept + per-tool timeout (issue #49).

At production scale (~370k drawers, ~1M AGE entities, ~5.6M MENTIONS edges)
``mempalace_status`` and ``mempalace_kg_stats`` take 29s and 9s respectively
when proxied through the MCP machinery upstream — long enough that callers
hit their TCP-level read timeout and report it as a hang. Two fixes land
here:

  1. ``_call`` wraps every MCP tool (except ``mempalace_mine``) in
     ``asyncio.wait_for`` so a stalled handler surfaces as a JSON-RPC
     ``-32001`` error envelope instead of a silent TCP timeout.
  2. ``/mcp`` short-circuits ``mempalace_status`` / ``mempalace_kg_stats``
     to direct-SQL fast paths that match the upstream envelope shape but
     return in sub-millisecond time, with an opt-out env flag.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m unittest tests.test_mcp_fast_intercept -v
"""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class TestPerToolTimeout(unittest.IsolatedAsyncioTestCase):
    """``_call`` should bound every tool except ``mempalace_mine``."""

    async def test_timeout_returns_jsonrpc_error_envelope(self):
        # Pretend the underlying tool will never return. We shave the
        # ceiling down to 50ms so the test stays fast. The fake handler
        # only needs to outlast the timeout; 0.5s leaves comfortable
        # headroom without dragging the test suite.
        def _hang(_req):
            import time

            time.sleep(0.5)
            return {"jsonrpc": "2.0", "id": _req.get("id"), "result": {}}

        req = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "mempalace_search", "arguments": {"query": "x"}}}

        with patch.object(main, "PALACE_MCP_TOOL_TIMEOUT_SECONDS", 0.05), \
             patch.object(main._mp, "handle_request", side_effect=_hang):
            resp = await main._call(req)

        self.assertEqual(resp.get("jsonrpc"), "2.0")
        self.assertEqual(resp.get("id"), 7)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32001)
        self.assertIn("mempalace_search", resp["error"]["message"])

    async def test_mine_is_exempt_from_timeout(self):
        # mempalace_mine legitimately runs minutes — must NOT be wrapped.
        # We assert this by patching wait_for to fail loudly if called.
        def _quick(_req):
            return {"jsonrpc": "2.0", "id": _req.get("id"), "result": {"ok": True}}

        async def _wait_for_bomb(*_a, **_kw):
            raise AssertionError("wait_for must not wrap mempalace_mine")

        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "mempalace_mine", "arguments": {}}}

        with patch.object(main, "PALACE_MCP_TOOL_TIMEOUT_SECONDS", 0.01), \
             patch.object(main._mp, "handle_request", side_effect=_quick), \
             patch.object(main.asyncio, "wait_for", side_effect=_wait_for_bomb):
            resp = await main._call(req)

        self.assertEqual(resp.get("result"), {"ok": True})

    async def test_timeout_disabled_when_set_zero(self):
        # PALACE_MCP_TOOL_TIMEOUT_SECONDS=0 → never wrap.
        def _quick(_req):
            return {"jsonrpc": "2.0", "id": _req.get("id"), "result": {"ok": True}}

        async def _wait_for_bomb(*_a, **_kw):
            raise AssertionError("wait_for must not run when timeout=0")

        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "mempalace_search", "arguments": {}}}

        with patch.object(main, "PALACE_MCP_TOOL_TIMEOUT_SECONDS", 0), \
             patch.object(main._mp, "handle_request", side_effect=_quick), \
             patch.object(main.asyncio, "wait_for", side_effect=_wait_for_bomb):
            resp = await main._call(req)

        self.assertEqual(resp.get("result"), {"ok": True})


class TestFastInterceptStatus(unittest.TestCase):
    """``_fast_mcp_status_payload`` wraps the SQL counts in tool_status shape."""

    def test_shape_matches_tool_status(self):
        with patch.object(main, "_fast_status_payload",
                          return_value={"total_drawers": 42, "wings": {"a": 30, "b": 12}, "rooms": {"sessions": 42}}):
            payload = main._fast_mcp_status_payload()

        # Required tool_status keys.
        for key in ("total_drawers", "wings", "rooms", "protocol", "aaak_dialect"):
            self.assertIn(key, payload, f"missing {key}")
        self.assertEqual(payload["total_drawers"], 42)
        self.assertEqual(payload["wings"], {"a": 30, "b": 12})

    def test_falls_back_when_constants_missing(self):
        # If mempalace.mcp_server somehow lacks PALACE_PROTOCOL / AAAK_SPEC
        # we still return the keys so the response shape stays stable.
        # Setting the module to None in sys.modules makes `import
        # mempalace.mcp_server` raise ImportError without monkeypatching
        # the global __import__ (which is brittle and can break the
        # test runner's own imports).
        with patch.object(main, "_fast_status_payload",
                          return_value={"total_drawers": 1, "wings": {}, "rooms": {}}), \
             patch.dict("sys.modules", {"mempalace.mcp_server": None}):
            payload = main._fast_mcp_status_payload()

        self.assertEqual(payload["protocol"], "")
        self.assertEqual(payload["aaak_dialect"], "")


class TestFastInterceptKgStats(unittest.TestCase):
    """``_fast_mcp_kg_stats_payload`` maps AGE backing-table counts to
    the ``tool_kg_stats`` envelope keys."""

    def test_shape_matches_tool_kg_stats(self):
        with patch.object(main, "_read_kg_postgres_stats",
                          return_value={"entities": 1000, "triples": 5,
                                        "mentions": 6000, "relationship_types": ["RELATION", "MENTIONS"]}):
            payload = main._fast_mcp_kg_stats_payload()

        for key in ("entities", "triples", "current_facts", "expired_facts", "relationship_types"):
            self.assertIn(key, payload, f"missing {key}")
        self.assertEqual(payload["entities"], 1000)
        self.assertEqual(payload["triples"], 5)
        # Fast path treats all triples as current (no property-level filter).
        self.assertEqual(payload["current_facts"], 5)
        self.assertEqual(payload["expired_facts"], 0)
        self.assertEqual(payload["relationship_types"], ["RELATION", "MENTIONS"])

    def test_raises_when_age_unreachable(self):
        with patch.object(main, "_read_kg_postgres_stats", return_value=None):
            with self.assertRaises(RuntimeError):
                main._fast_mcp_kg_stats_payload()


class TestMcpProxyInterception(unittest.IsolatedAsyncioTestCase):
    """``/mcp`` should route the two slow tools through the fast helpers
    when ``PALACE_MCP_FAST_INTERCEPT`` is on, and fall through otherwise."""

    def _make_request(self, body: dict):
        # FastAPI's Request only needs .json() for mcp_proxy.
        class _R:
            async def json(self_inner):
                return body
        return _R()

    async def _call_proxy(self, body):
        resp = await main.mcp_proxy(self._make_request(body), x_api_key=None)
        # JSONResponse.body is bytes; decode + json-parse.
        return json.loads(resp.body)

    async def test_intercepts_mempalace_status(self):
        body = {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                "params": {"name": "mempalace_status", "arguments": {}}}

        fake_payload = {"total_drawers": 99, "wings": {}, "rooms": {}, "protocol": "p", "aaak_dialect": "a"}
        with patch.object(main, "PALACE_MCP_FAST_INTERCEPT", True), \
             patch.object(main, "_check_auth"), \
             patch.object(main, "_fast_mcp_status_payload", return_value=fake_payload), \
             patch.object(main, "_call") as slow:
            envelope = await self._call_proxy(body)

        slow.assert_not_called()
        self.assertEqual(envelope["jsonrpc"], "2.0")
        self.assertEqual(envelope["id"], 11)
        # JSON-RPC tool-result wrapping: result.content[0].text holds the json string.
        text = envelope["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text), fake_payload)

    async def test_intercepts_mempalace_kg_stats(self):
        body = {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                "params": {"name": "mempalace_kg_stats", "arguments": {}}}

        fake = {"entities": 1, "triples": 0, "current_facts": 0, "expired_facts": 0, "relationship_types": []}
        with patch.object(main, "PALACE_MCP_FAST_INTERCEPT", True), \
             patch.object(main, "_check_auth"), \
             patch.object(main, "_fast_mcp_kg_stats_payload", return_value=fake), \
             patch.object(main, "_call") as slow:
            envelope = await self._call_proxy(body)

        slow.assert_not_called()
        self.assertEqual(json.loads(envelope["result"]["content"][0]["text"]), fake)

    async def test_disabled_flag_falls_through_to_slow(self):
        body = {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                "params": {"name": "mempalace_status", "arguments": {}}}

        async def _slow(_b):
            return {"jsonrpc": "2.0", "id": 13, "result": {"slow": True}}

        with patch.object(main, "PALACE_MCP_FAST_INTERCEPT", False), \
             patch.object(main, "_check_auth"), \
             patch.object(main, "_fast_mcp_status_payload",
                          side_effect=AssertionError("fast path must not run")), \
             patch.object(main, "_call", side_effect=_slow):
            envelope = await self._call_proxy(body)

        self.assertEqual(envelope["result"], {"slow": True})

    async def test_fast_path_exception_falls_back_to_slow(self):
        # If the AGE fast path raises (e.g. AGE down), the slow proxy
        # path is the safety net so behaviour matches upstream.
        body = {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                "params": {"name": "mempalace_kg_stats", "arguments": {}}}

        async def _slow(_b):
            return {"jsonrpc": "2.0", "id": 14, "result": {"slow_ok": True}}

        with patch.object(main, "PALACE_MCP_FAST_INTERCEPT", True), \
             patch.object(main, "_check_auth"), \
             patch.object(main, "_fast_mcp_kg_stats_payload",
                          side_effect=RuntimeError("AGE down")), \
             patch.object(main, "_call", side_effect=_slow):
            envelope = await self._call_proxy(body)

        self.assertEqual(envelope["result"], {"slow_ok": True})

    async def test_other_tools_are_not_intercepted(self):
        # mempalace_search must go through _call so semaphores + auto-repair
        # + the timeout wrapper all keep working.
        body = {"jsonrpc": "2.0", "id": 15, "method": "tools/call",
                "params": {"name": "mempalace_search", "arguments": {"query": "x"}}}

        async def _slow(_b):
            return {"jsonrpc": "2.0", "id": 15, "result": {"hits": []}}

        with patch.object(main, "PALACE_MCP_FAST_INTERCEPT", True), \
             patch.object(main, "_check_auth"), \
             patch.object(main, "_call", side_effect=_slow) as slow:
            envelope = await self._call_proxy(body)

        slow.assert_called_once()
        self.assertEqual(envelope["result"], {"hits": []})


if __name__ == "__main__":
    unittest.main()
