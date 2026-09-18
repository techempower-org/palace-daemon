"""kg_stats: cheap cold query, single-flight TTL cache, honest log line (#290).

MEASURED ON THE LIVE PALACE, 2026-09-18, read-only:

    count(*) Entity      40.7 ms   n = 1,461,223
    count(*) RELATION   247.5 ms   n = 2,060,900
    count(*) MENTIONS  27,622 ms   n = 51,539,744   <- the whole cost
    EXISTS  MENTIONS      0.5 ms

The payload never returns the MENTIONS count; it uses it only to decide whether
"MENTIONS" belongs in `relationship_types`. So the cold path was paying 27.6 s —
more than the 30 s statement timeout allows under load, in which case the count
raises and EVERY count is lost — to answer a question no caller on this path
asks. `exact_mentions=False` answers it with EXISTS instead.

The cache is then about repeat and concurrency, not about the 27 s: exact counts
memoised behind a single flight, never `reltuples` estimates. Exactness is
preserved.

PRODUCER AND CONSUMER TOGETHER (successor rule 3): the payload's producer is
`fast_mcp_kg_stats_*`; its consumer is the `/mcp` dispatch that both unwraps the
`(payload, was_cached)` tuple and decides which log line to emit. A cache that
changes the producer's return shape changes what the consumer logs, so the
envelope test drives the real route rather than the payload alone.
"""
import json
import logging
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

import fast_intercept
import kg_reader
import main


def _stats(**over):
    d = {"entities": 1_461_223, "triples": 2_060_900,
         "relationship_types": ["RELATION", "MENTIONS"]}
    d.update(over)
    return d


class TestCheapColdQuery(unittest.TestCase):
    """The producer asks postgres for EXISTS, not count(*), on the cheap path."""

    def _cursor_spy(self):
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = lambda s, *a: False
        cur.fetchone.side_effect = lambda: (1,)
        return cur

    def _run(self, exact):
        cur = self._cursor_spy()
        conn = MagicMock()
        conn.cursor.return_value = cur
        kg = MagicMock(GRAPH_NAME="mempalace_kg", _conn=conn)
        with patch.object(kg_reader, "_config", lambda: MagicMock(postgres_dsn="postgresql://x/y")), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql://x/y"}), \
             patch("mempalace.knowledge_graph_age.KnowledgeGraphAGE", return_value=kg):
            out = kg_reader.read_kg_postgres_stats(exact_mentions=exact)
        sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
        return out, sql

    def test_cheap_path_uses_EXISTS_and_never_counts_mentions(self):
        out, sql = self._run(exact=False)
        self.assertIn('EXISTS(SELECT 1 FROM mempalace_kg."MENTIONS")', sql)
        self.assertNotIn('count(*) FROM mempalace_kg."MENTIONS"', sql)
        self.assertNotIn("mentions", out, "the number must be OMITTED, not zeroed")

    def test_exact_path_is_unchanged_for_existing_consumers(self):
        """/ontology asserts the exact number — the default must not move."""
        out, sql = self._run(exact=True)
        self.assertIn('count(*) FROM mempalace_kg."MENTIONS"', sql)
        self.assertIn("mentions", out)

    def test_both_paths_still_count_entities_and_relations_exactly(self):
        for exact in (True, False):
            _out, sql = self._run(exact=exact)
            self.assertIn('count(*) FROM mempalace_kg."Entity"', sql)
            self.assertIn('count(*) FROM mempalace_kg."RELATION"', sql)


class TestCacheAndSingleFlight(unittest.TestCase):
    def setUp(self):
        fast_intercept.kg_stats_cache_clear()
        fast_intercept._kg_stats_queries = 0
        self._env = patch.dict(os.environ, {"PALACE_KG_STATS_TTL": "120"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        fast_intercept.kg_stats_cache_clear()

    def test_the_payload_asks_for_the_cheap_variant(self):
        spy = MagicMock(return_value=_stats())
        with patch.object(main, "_read_kg_postgres_stats", spy):
            fast_intercept.fast_mcp_kg_stats_payload()
        self.assertEqual(spy.call_args.kwargs.get("exact_mentions"), False)

    def test_second_call_is_served_from_cache_and_issues_no_query(self):
        spy = MagicMock(return_value=_stats())
        with patch.object(main, "_read_kg_postgres_stats", spy):
            first, cached1 = fast_intercept.fast_mcp_kg_stats_cached()
            second, cached2 = fast_intercept.fast_mcp_kg_stats_cached()
        self.assertFalse(cached1)
        self.assertTrue(cached2)
        self.assertEqual(spy.call_count, 1, "the warm call must not query")
        self.assertEqual(first, second, "cached payload must be identical")

    def test_cached_payload_is_exact_not_an_estimate(self):
        """No reltuples anywhere: the cache stores the real counts."""
        with patch.object(main, "_read_kg_postgres_stats", MagicMock(return_value=_stats())):
            payload, _ = fast_intercept.fast_mcp_kg_stats_cached()
        self.assertEqual(payload["entities"], 1_461_223)
        self.assertEqual(payload["triples"], 2_060_900)
        self.assertNotIn("estimated", payload)

    def test_a_mutated_cache_entry_cannot_leak_into_the_next_caller(self):
        """Mutate what a CACHED read returned — not what the cold read returned.

        The first version mutated the cold result, which is already a copy of
        the stored dict, so the assertion held even when the cached path handed
        out the stored object itself. Mutation testing caught it: removing
        `dict(...)` from the cached return left this test green. The cached
        read is the one that can leak, so it is the one to poke.
        """
        with patch.object(main, "_read_kg_postgres_stats", MagicMock(return_value=_stats())):
            cold, was_cached = fast_intercept.fast_mcp_kg_stats_cached()
            self.assertFalse(was_cached)
            warm1, c1 = fast_intercept.fast_mcp_kg_stats_cached()
            self.assertTrue(c1)
            warm1["entities"] = -1              # poison what the CACHE handed back
            warm2, c2 = fast_intercept.fast_mcp_kg_stats_cached()
        self.assertTrue(c2)
        self.assertEqual(warm2["entities"], 1_461_223, "cached readers must get a copy")
        self.assertEqual(cold["entities"], 1_461_223)

    def test_ttl_zero_disables_the_cache_entirely(self):
        spy = MagicMock(return_value=_stats())
        with patch.dict(os.environ, {"PALACE_KG_STATS_TTL": "0"}), \
             patch.object(main, "_read_kg_postgres_stats", spy):
            fast_intercept.fast_mcp_kg_stats_cached()
            fast_intercept.fast_mcp_kg_stats_cached()
        self.assertEqual(spy.call_count, 2)

    def test_N_concurrent_cold_callers_cost_ONE_query(self):
        """Single flight, counted at the seam — not inferred from wall clock."""
        started = threading.Barrier(8)
        calls = []

        def _slow(**_kw):
            calls.append(1)
            threading.Event().wait(0.05)
            return _stats()

        with patch.object(main, "_read_kg_postgres_stats", side_effect=_slow):
            def _worker():
                started.wait()
                fast_intercept.fast_mcp_kg_stats_cached()
            ts = [threading.Thread(target=_worker) for _ in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(len(calls), 1, f"8 concurrent cold callers issued {len(calls)} queries")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class TestOnlySuccessesAreCached(unittest.TestCase):
    """A failure must not be served for a TTL, and must not hide behind one.

    The dangerous case is NOT an exception. `read_kg_postgres_stats` swallows a
    count failure and returns ZEROS — indistinguishable from a real empty
    graph — so without a marker the cache would store `entities: 0` and serve
    it confidently for 120 s. That is the shape this class pins.
    """

    def setUp(self):
        fast_intercept.kg_stats_cache_clear()
        self._env = patch.dict(os.environ, {"PALACE_KG_STATS_TTL": "120"})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        fast_intercept.kg_stats_cache_clear()

    def test_a_failing_payload_is_retried_on_the_very_next_call(self):
        spy = MagicMock(side_effect=RuntimeError("AGE unreachable"))
        with patch.object(main, "_read_kg_postgres_stats", spy):
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    fast_intercept.fast_mcp_kg_stats_cached()
        self.assertEqual(spy.call_count, 3, "a failure must not be cached")

    def test_a_failure_invalidates_a_previously_cached_success(self):
        ok = MagicMock(return_value=_stats())
        with patch.object(main, "_read_kg_postgres_stats", ok):
            fast_intercept.fast_mcp_kg_stats_cached()          # cache a success
        # TTL has not expired, but the next real call fails: the stale success
        # must not be what the caller gets once we have learned it is stale.
        with patch.object(main, "_read_kg_postgres_stats",
                          MagicMock(side_effect=RuntimeError("AGE down"))), \
             patch.dict(os.environ, {"PALACE_KG_STATS_TTL": "0"}):
            with self.assertRaises(RuntimeError):
                fast_intercept.fast_mcp_kg_stats_cached()
        self.assertIsNone(fast_intercept._kg_stats_cached,
                          "the failure must clear the cached success")

    def test_a_failed_count_query_yields_None_and_is_never_cached(self):
        """The mechanism #290 removed the trigger for, but not the mechanism.

        A statement_timeout used to be swallowed and a payload built from the
        initialised zeros — plausible, wrong, and cached for a TTL. The read
        returns None now; the intercept raises; /mcp falls to the slow path.
        """
        spy = MagicMock(return_value=None)
        with patch.object(main, "_read_kg_postgres_stats", spy):
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    fast_intercept.fast_mcp_kg_stats_cached()
        self.assertEqual(spy.call_count, 2, "a failed read must be retried, not cached")
        self.assertIsNone(fast_intercept._kg_stats_cached)

    def test_zeros_are_never_what_a_failed_count_returns(self):
        """Driven through the REAL reader: a raising count must not become 0s."""
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = lambda s, *a: False
        calls = {"n": 0}

        def _exec(sql, *a, **k):
            calls["n"] += 1
            if "RELATION" in str(sql):
                raise RuntimeError("canceling statement due to statement timeout")

        cur.execute.side_effect = _exec
        cur.fetchone.side_effect = lambda: (10,)
        conn = MagicMock(); conn.cursor.return_value = cur
        kg = MagicMock(GRAPH_NAME="mempalace_kg", _conn=conn)
        with patch.object(kg_reader, "_config",
                          lambda: MagicMock(postgres_dsn="postgresql://x/y")), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql://x/y"}), \
             patch("mempalace.knowledge_graph_age.KnowledgeGraphAGE", return_value=kg):
            out = kg_reader.read_kg_postgres_stats(exact_mentions=False)
        self.assertIsNone(out, "a failed count must not be reported as a count")

    def test_a_genuinely_empty_graph_is_still_a_valid_cacheable_answer(self):
        """The negative control: zeros WITHOUT the marker are a real result."""
        empty = {"entities": 0, "triples": 0, "relationship_types": []}
        spy = MagicMock(return_value=empty)
        with patch.object(main, "_read_kg_postgres_stats", spy):
            first, c1 = fast_intercept.fast_mcp_kg_stats_cached()
            _second, c2 = fast_intercept.fast_mcp_kg_stats_cached()
        self.assertFalse(c1)
        self.assertTrue(c2, "an empty graph is an answer, not a failure")
        self.assertEqual(first["entities"], 0)
        self.assertEqual(spy.call_count, 1)


class TestDegradedReadIsVisible(unittest.TestCase):
    """The count failure must reach the daemon's logger, not the root one."""

    def test_count_failure_logs_on_palace_daemon_and_marks_the_payload(self):
        cap = _Capture()
        logger = logging.getLogger("palace-daemon")
        logger.addHandler(cap)
        lvl = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            cur = MagicMock()
            cur.__enter__ = lambda s: s
            cur.__exit__ = lambda s, *a: False
            cur.execute.side_effect = RuntimeError("statement timeout")
            conn = MagicMock(); conn.cursor.return_value = cur
            kg = MagicMock(GRAPH_NAME="mempalace_kg", _conn=conn)
            with patch.object(kg_reader, "_config",
                              lambda: MagicMock(postgres_dsn="postgresql://x/y")), \
                 patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql://x/y"}), \
                 patch("mempalace.knowledge_graph_age.KnowledgeGraphAGE", return_value=kg):
                out = kg_reader.read_kg_postgres_stats(exact_mentions=False)
        finally:
            logger.removeHandler(cap)
            logger.setLevel(lvl)
        self.assertIsNone(out, "a failed count must return None, never zeros")
        self.assertTrue([m for m in cap.messages if "count queries failed" in m],
                        f"not on the daemon logger: {cap.messages}")


class TestMcpRouteLogsWhichPathItTook(unittest.IsolatedAsyncioTestCase):
    """The consumer half: the envelope AND the log line, through the real route.

    #290's complaint was that the journal carried only `/mcp slow path:` lines,
    so an intercepted-but-slow call looked like one that was never intercepted.
    """

    async def asyncSetUp(self):
        fast_intercept.kg_stats_cache_clear()
        self.cap = _Capture()
        self.logger = logging.getLogger("palace-daemon")
        self.logger.addHandler(self.cap)
        self._lvl = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self._p = patch.object(main, "_check_auth", side_effect=lambda *_a, **_k: None)
        self._p.start()

    async def asyncTearDown(self):
        self._p.stop()
        self.logger.removeHandler(self.cap)
        self.logger.setLevel(self._lvl)
        fast_intercept.kg_stats_cache_clear()

    async def _call(self):
        req = MagicMock()
        req.json = MagicMock()
        async def _json():
            return {"jsonrpc": "2.0", "id": 1,
                    "params": {"name": "mempalace_kg_stats", "arguments": {"wing": "memorypalace"}}}
        req.json = _json
        resp = await main.mcp_proxy(req, x_api_key=None)
        return json.loads(bytes(resp.body).decode())

    async def test_cold_then_cached_are_logged_differently_and_both_say_fast(self):
        with patch.object(main, "_read_kg_postgres_stats", MagicMock(return_value=_stats())):
            first = await self._call()
            second = await self._call()

        payload = json.loads(first["result"]["content"][0]["text"])
        self.assertEqual(payload["entities"], 1_461_223,
                         "the tuple must be unwrapped before it is serialised")
        self.assertEqual(payload, json.loads(second["result"]["content"][0]["text"]))

        fast = [m for m in self.cap.messages if "/mcp fast path" in m]
        self.assertTrue(any("cold" in m for m in fast), f"no cold line: {self.cap.messages}")
        self.assertTrue(any("cached" in m for m in fast), f"no cached line: {self.cap.messages}")
        self.assertFalse([m for m in self.cap.messages if "/mcp slow path" in m],
                         "an intercepted call must not be logged as the slow path")


if __name__ == "__main__":
    unittest.main()
