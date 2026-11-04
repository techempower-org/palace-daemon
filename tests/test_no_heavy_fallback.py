"""A failed fast intercept is an error naming the cause, never the heavy slow path (#323).

2026-11-04 on familiar: under mine load a fast SQL path hit its statement
timeout and the daemon fell back to the in-process full-corpus version of the
same tool, twice (12:16 kg_stats, 13:11 status). The daemon grew 1.2 -> 5.4 GB
and refused every save for 20 minutes. Fast-path failures are now counted on
/status/fast (``fast_path_timeouts``) as the leading signal.

Run with::

    python -m pytest tests/test_no_heavy_fallback.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import instruments  # noqa: E402
import main  # noqa: E402
import status_snapshot  # noqa: E402


class QueryCanceled(Exception):
    """Same class name as psycopg's statement-timeout error."""


def _req(tool, args=None):
    return {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": tool, "arguments": args if args is not None else {}}}


_INTERCEPTED = {
    "mempalace_status": "_fast_mcp_status_payload",
    "mempalace_kg_stats": "_fast_mcp_kg_stats_cached",
    "mempalace_list_wings": "_fast_mcp_list_wings_payload",
    "mempalace_get_taxonomy": "_fast_mcp_get_taxonomy_payload",
}


class NoHeavyFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        instruments._FAST_FAILURES.clear()
        self.slow_calls = []

        def _slow(request_dict):
            self.slow_calls.append(request_dict["params"]["name"])
            return {"jsonrpc": "2.0", "id": 9, "result": {"slow": True}}

        self._p = [
            patch.object(main, "PALACE_MCP_FAST_INTERCEPT", True),
            patch.object(main._mp, "handle_request", side_effect=_slow),
        ]
        for p in self._p:
            p.start()

    async def asyncTearDown(self):
        for p in self._p:
            p.stop()
        instruments._FAST_FAILURES.clear()

    async def test_every_intercepted_tool_answers_an_error_without_the_slow_path(self):
        for tool, attr in _INTERCEPTED.items():
            with self.subTest(tool=tool), \
                    patch.object(main, attr, side_effect=QueryCanceled(
                        "canceling statement due to statement timeout")):
                env = await asyncio.wait_for(main._call(_req(tool)), 5)
                self.assertEqual(env["error"]["code"], -32004, env)
                self.assertIn("statement timeout", env["error"]["message"])
                self.assertIn("#323", env["error"]["message"])
        with patch.object(main, "_fast_list_payload", side_effect=RuntimeError("pg down")):
            env = await asyncio.wait_for(main._call(_req("mempalace_list_drawers", {"limit": 5})), 5)
            self.assertEqual(env["error"]["code"], -32004)
        self.assertEqual(self.slow_calls, [], "a heavy slow path ran")

    async def test_failures_are_counted_and_timeouts_told_apart(self):
        with patch.object(main, "_fast_mcp_kg_stats_cached",
                          side_effect=QueryCanceled("canceling statement due to statement timeout")):
            await main._call(_req("mempalace_kg_stats"))
        with patch.object(main, "_fast_mcp_get_taxonomy_payload",
                          side_effect=RuntimeError("connection refused")):
            await main._call(_req("mempalace_get_taxonomy"))
        s = instruments.fast_failure_summary()
        self.assertEqual(s["timeouts_24h"], 1)
        self.assertEqual(s["failures_24h"], 2)
        self.assertEqual(s["by_tool"], {"mempalace_kg_stats": 1, "mempalace_get_taxonomy": 1})

    async def test_the_kill_switch_still_allows_the_slow_path(self):
        with patch.object(main, "PALACE_MCP_FAST_INTERCEPT", False):
            env = await asyncio.wait_for(main._call(_req("mempalace_status")), 5)
        self.assertEqual(env.get("result"), {"slow": True})
        self.assertEqual(self.slow_calls, ["mempalace_status"])

    async def test_status_is_served_from_the_snapshot_when_a_recompute_would_fail(self):
        calls = {"n": 0}

        def _compute():
            calls["n"] += 1
            if calls["n"] > 1:
                raise QueryCanceled("canceling statement due to statement timeout")
            return {"total_drawers": 7, "wings": {"w": 7}, "rooms": {}}

        failures = []
        snap = status_snapshot.SnapshotCache(_compute, ttl_s=0.0, on_failure=failures.append)
        with patch.object(main, "_STATUS_SNAPSHOT", snap), \
                patch.object(status_snapshot._log, "exception"):
            first = await main._call(_req("mempalace_status"))  # cold: computes
            second = await main._call(_req("mempalace_status"))  # stale: starts a refresh
            for _ in range(100):  # wait for that refresh to fail
                with snap._lock:
                    if not snap._refreshing and calls["n"] >= 2:
                        break
                await asyncio.sleep(0.02)
            third = await main._call(_req("mempalace_status"))
        self.assertGreaterEqual(calls["n"], 2, "the failing refresh never ran")
        self.assertTrue(failures, "the refresh failure was not reported")
        for env in (first, second, third):
            self.assertIn("result", env, env)
        self.assertEqual(self.slow_calls, [])


class StatsConsumer(unittest.IsolatedAsyncioTestCase):
    async def test_stats_reports_a_failed_intercept_in_its_field_and_runs_no_slow_path(self):
        slow = []

        def _slow(request_dict):
            slow.append(request_dict["params"]["name"])
            return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{}"}]}}

        snap = status_snapshot.SnapshotCache(lambda: {"total_drawers": 2, "wings": {}, "rooms": {}})
        with patch.object(main, "PALACE_MCP_FAST_INTERCEPT", True), \
                patch.object(main, "_STATUS_SNAPSHOT", snap), \
                patch.object(main, "_check_auth"), \
                patch.object(main, "_fast_mcp_kg_stats_cached",
                             side_effect=QueryCanceled("canceling statement due to statement timeout")), \
                patch.object(main._mp, "handle_request", side_effect=_slow):
            out = await asyncio.wait_for(main.stats(x_api_key=None), 10)
        text = str(out)
        self.assertIn("-32004", text)
        self.assertIn("statement timeout", text)
        self.assertNotIn("mempalace_kg_stats", slow)


class StatusFastCarriesTheCounter(unittest.TestCase):
    def test_status_fast_reports_fast_path_timeouts(self):
        from fastapi.testclient import TestClient

        instruments._FAST_FAILURES.clear()
        instruments.record_fast_failure("mempalace_status", QueryCanceled("statement timeout"))
        snap = status_snapshot.SnapshotCache(lambda: {"total_drawers": 1, "wings": {}, "rooms": {}})
        with patch.dict(os.environ, {"PALACE_API_KEY": ""}, clear=False), \
                patch.object(main, "_STATUS_SNAPSHOT", snap), \
                patch.object(main, "_postgres_dsn", lambda: "dsn"):
            body = TestClient(main.app).get("/status/fast").json()
        self.assertEqual(body["fast_path_timeouts"]["timeouts_24h"], 1)
        self.assertEqual(body["fast_path_timeouts"]["by_tool"], {"mempalace_status": 1})
        instruments._FAST_FAILURES.clear()


class CounterSaturation(unittest.TestCase):
    def test_saturated_only_while_the_window_is_full(self):
        now = time.time()
        with patch.object(instruments, "_FAST_FAILURES", instruments.collections.deque(maxlen=2)):
            instruments.record_fast_failure("a", QueryCanceled("x"), now - 25 * 3600)
            instruments.record_fast_failure("a", QueryCanceled("x"), now - 25 * 3600)
            self.assertFalse(instruments.fast_failure_summary(now)["saturated"])
            instruments.record_fast_failure("a", QueryCanceled("x"), now - 5)
            instruments.record_fast_failure("a", QueryCanceled("x"), now - 4)
            self.assertTrue(instruments.fast_failure_summary(now)["saturated"])


class CounterWindow(unittest.TestCase):
    def test_old_failures_age_out(self):
        instruments._FAST_FAILURES.clear()
        now = time.time()
        instruments.record_fast_failure("a", QueryCanceled("x"), now - 25 * 3600)
        instruments.record_fast_failure("b", QueryCanceled("x"), now - 60)
        self.assertEqual(instruments.fast_failure_summary(now)["timeouts_24h"], 1)
        instruments._FAST_FAILURES.clear()


if __name__ == "__main__":
    unittest.main()
