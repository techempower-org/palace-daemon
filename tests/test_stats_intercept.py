"""GET /stats reaches tool_kg_stats through _call, bypassing the fast intercept (#299).

MEASURED ON FAMILIAR, 2026-09-18 19:4x PDT (daemon 1d9e351e, /health 1.0 s,
/status/fast 0.34 s): `GET /stats` did not answer in 30 s. py-spy: six
`palace-tool_N` threads parked in `knowledge_graph_age.stats` via
`mcp_server.tool_kg_stats`, reached through `handle_request` directly — not
through the `/mcp` HTTP handler where #287's intercept lives. The six were the
day's six `mempalace stats` / `GET /stats` probes. #286's mechanism, second
door.

THE INVARIANTS are about which code RUNS, not about wall clock: with
`handle_request` modelled as parked on the KG lock for the two interceptable
tools, `/stats` still completes (inv 1) and `handle_request` is invoked for
`mempalace_graph_stats` ONLY (inv 2 — the py-spy assertion at the seam). On the
base both fail: `/stats` never returns and all three tools reach the executor.
"""
import ast
import asyncio
import json
import os
import threading
import unittest
from typing import ClassVar
from unittest.mock import patch

import fast_intercept
import main


def _req(tool, rid=1, arguments=None):
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}}}


class _Harness(unittest.IsolatedAsyncioTestCase):
    """`handle_request` stub: parks the two KG-lock tools on an Event that is
    never set (released in tearDown so no thread outlives the test), answers
    every other tool at once, and records every tool name it is handed."""

    KG_PAYLOAD: ClassVar[dict] = {"entities": 1461223, "triples": 2060900,
                                  "relationship_types": ["MENTIONS"]}
    STATUS_PAYLOAD: ClassVar[dict] = {"total_drawers": 812345, "wings": {}, "rooms": {},
                                      "protocol": "p", "aaak_dialect": "a"}

    def setUp(self):
        fast_intercept.kg_stats_cache_clear()
        self.seen: list = []
        self.park = threading.Event()

        def _handle(request_dict):
            tool = request_dict["params"]["name"]
            self.seen.append(tool)
            if tool in ("mempalace_kg_stats", "mempalace_status"):
                self.park.wait(5.0)
            return {"jsonrpc": "2.0", "id": request_dict.get("id"),
                    "result": {"content": [{"type": "text", "text": json.dumps({"slow": tool})}]}}

        self._patches = [
            patch.object(main._mp, "handle_request", side_effect=_handle),
            patch.object(main, "_check_auth"),
            patch.object(main, "PALACE_MCP_FAST_INTERCEPT", True),
            patch.object(main, "_fast_mcp_kg_stats_cached",
                         return_value=(dict(self.KG_PAYLOAD), False)),
            patch.object(main, "_fast_mcp_status_payload", return_value=dict(self.STATUS_PAYLOAD)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        self.park.set()
        for p in self._patches:
            p.stop()


class TestStatsAnswersWhileTheKgLockIsHeld(_Harness):
    async def test_stats_completes_and_no_tool_thread_enters_kg_stats(self):
        """Inv 1 + inv 2. On the base: TimeoutError, and seen == all three."""
        before = main._tool_inflight
        result = await asyncio.wait_for(main.stats(x_api_key=None), 2.0)
        self.assertEqual(result["kg"], self.KG_PAYLOAD)
        self.assertEqual(result["status"], self.STATUS_PAYLOAD)
        self.assertEqual(result["graph"], {"slow": "mempalace_graph_stats"})
        self.assertEqual(self.seen, ["mempalace_graph_stats"],
                         f"handle_request was handed an interceptable tool: {self.seen}")
        self.assertEqual(main._tool_inflight, before, "an intercepted call took an in-flight slot")

    async def test_graphs_kg_stats_request_is_intercepted_too(self):
        """Inv 5 — the exact request /graph builds (main._mcp(..., 2))."""
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "mempalace_kg_stats", "arguments": {}}}
        env = await asyncio.wait_for(main._call(req), 2.0)
        self.assertEqual(env["id"], 2)
        self.assertEqual(main._unwrap(env), self.KG_PAYLOAD)
        self.assertEqual(self.seen, [])

    async def test_kg_stats_with_arguments_is_still_intercepted_from_call(self):
        """#286's rule applies at the internal door too: arguments never send
        kg_stats/status to the KG lock."""
        env = await asyncio.wait_for(
            main._call(_req("mempalace_kg_stats", 7, {"verbose": True})), 2.0)
        self.assertEqual(main._unwrap(env), self.KG_PAYLOAD)
        self.assertEqual(self.seen, [])


class TestTheTwoDoorsGiveOneAnswer(_Harness):
    async def test_mcp_and_stats_return_the_same_kg_payload(self):
        """Producer/consumer: one rule, one payload, two envelopes."""
        class _R:
            async def json(self_inner):
                return _req("mempalace_kg_stats", 11)
        mcp_env = json.loads((await main.mcp_proxy(_R(), x_api_key=None)).body)
        stats_result = await asyncio.wait_for(main.stats(x_api_key=None), 2.0)
        self.assertEqual(main._unwrap(mcp_env), stats_result["kg"])
        self.assertEqual(mcp_env["id"], 11)

    async def test_mcp_still_intercepts_before_call(self):
        """The HTTP door keeps its own intercept: `_call` is not even entered."""
        class _R:
            async def json(self_inner):
                return _req("mempalace_status", 3)
        with patch.object(main, "_call") as slow:
            env = json.loads((await main.mcp_proxy(_R(), x_api_key=None)).body)
        slow.assert_not_called()
        self.assertEqual(main._unwrap(env), self.STATUS_PAYLOAD)


class TestFallThroughIsPreserved(_Harness):
    async def test_a_failing_fast_payload_falls_to_the_slow_path(self):
        """Inv 4 — parity with upstream: the tool runs when SQL cannot answer."""
        self.park.set()  # let the slow path return
        with patch.object(main, "_fast_mcp_kg_stats_cached", side_effect=RuntimeError("pg down")):
            env = await asyncio.wait_for(main._call(_req("mempalace_kg_stats", 5)), 5.0)
        self.assertEqual(main._unwrap(env), {"slow": "mempalace_kg_stats"})
        self.assertEqual(self.seen, ["mempalace_kg_stats"])

    async def test_flag_off_means_slow_path_for_everything(self):
        self.park.set()
        with patch.object(main, "PALACE_MCP_FAST_INTERCEPT", False):
            await asyncio.wait_for(main.stats(x_api_key=None), 5.0)
        self.assertEqual(sorted(self.seen),
                         ["mempalace_graph_stats", "mempalace_kg_stats", "mempalace_status"])

    async def test_non_tools_call_methods_are_untouched(self):
        """`ping` and friends never consult the intercept."""
        self.park.set()
        with patch.object(main, "_fast_intercept_fn") as rule:
            await asyncio.wait_for(
                main._call({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}), 5.0)
        rule.assert_not_called()


class TestInterceptedCallsLeaveNoSlowPathTrace(_Harness):
    async def test_no_slow_path_log_line_for_an_intercepted_call(self):
        """Inv 6 — the journal must say "fast path", never "slow path", for it."""
        lines: list = []

        def _rec(m, *a):
            lines.append(m % a if a else m)

        with patch.object(main._log, "info", side_effect=_rec), \
             patch.object(main._log, "warning", side_effect=_rec):
            await asyncio.wait_for(main._call(_req("mempalace_kg_stats", 9)), 2.0)
        self.assertTrue(any("_call fast path: tool=mempalace_kg_stats" in ln for ln in lines), lines)
        self.assertFalse(any("slow path" in ln for ln in lines), lines)


class TestNoFourthDoor(unittest.TestCase):
    """Every path from the daemon into `handle_request` goes through `_call`,
    and `_call` consults the shared rule — asserted on the AST, so a new
    internal caller that dispatches an interceptable tool some other way is a
    red test, not a py-spy dump six months from now."""

    INTERCEPTABLE: ClassVar[set] = {"mempalace_status", "mempalace_kg_stats",
                                    "mempalace_list_wings", "mempalace_get_taxonomy",
                                    "mempalace_list_drawers"}
    # Sites that hand `handle_request` a request that can never name a tool.
    ALLOWED_DIRECT: ClassVar[set] = {
        "_warn_if_hnsw_threads_unset",   # chroma-only `ping` (main.py, #1161 check)
        "_call",                         # the door itself, and its HNSW retry
        "health",                        # `_ping` on the FAST executor
    }

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cls.trees = {}
        for name in os.listdir(root):
            if name.endswith(".py"):
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    cls.trees[name] = ast.parse(f.read())

    @staticmethod
    def _enclosing_function(tree, target):
        out = []
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    if node is target:
                        out.append(fn.name)
        return out

    def test_every_handle_request_site_is_call_or_a_ping(self):
        sites = []
        for fname, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "handle_request":
                    encl = self._enclosing_function(tree, node)
                    sites.append((fname, node.lineno, encl[-1] if encl else "<module>"))
        self.assertTrue(sites, "no handle_request site found — the walk is broken")
        offenders = [s for s in sites if s[2] not in self.ALLOWED_DIRECT]
        self.assertEqual(offenders, [], f"handle_request reached outside _call: {offenders}")
        self.assertTrue(any(s[2] == "_call" for s in sites))

    def test_call_consults_the_shared_rule_before_the_semaphore(self):
        tree = self.trees["main.py"]
        call_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.AsyncFunctionDef) and n.name == "_call")
        names_in_order = [n.func.id for n in ast.walk(call_fn)
                          if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn("_fast_intercept_fn", names_in_order)
        self.assertIn("_sem_for", names_in_order)
        self.assertLess(names_in_order.index("_fast_intercept_fn"), names_in_order.index("_sem_for"),
                        "the intercept must run before the semaphore is taken")

    def test_mcp_proxy_uses_the_same_rule(self):
        tree = self.trees["main.py"]
        proxy = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.AsyncFunctionDef) and n.name == "mcp_proxy")
        called = {n.func.id for n in ast.walk(proxy)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn("_fast_intercept_fn", called)
        # and no second copy of the dispatch table exists anywhere in main.py
        tables = [n for n in ast.walk(tree) if isinstance(n, ast.Dict)
                  and any(isinstance(k, ast.Constant) and k.value == "mempalace_kg_stats"
                          for k in n.keys)]
        self.assertEqual(len(tables), 1, "the tool→fast-payload table exists in exactly one place")

    def test_every_interceptable_tool_name_is_in_the_shared_rule(self):
        tree = self.trees["main.py"]
        rule = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_fast_intercept_fn")
        literals = {n.value for n in ast.walk(rule)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertTrue(self.INTERCEPTABLE <= literals, self.INTERCEPTABLE - literals)


if __name__ == "__main__":
    unittest.main()
