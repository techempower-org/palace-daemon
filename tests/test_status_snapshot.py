"""status_snapshot.SnapshotCache: /status/fast served from memory.

Run with::

    python -m unittest tests.test_status_snapshot -v
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import status_snapshot  # noqa: E402


class _Compute:
    def __init__(self, delay=0.0, fail_after=None):
        self.calls = 0
        self.delay = delay
        self.fail_after = fail_after
        self.gate = None

    def __call__(self):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("db down")
        return {"n": self.calls}


def _settle(cache, timeout=5.0):
    """Wait for any background refresh to finish."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with cache._lock:
            if not cache._refreshing:
                return
        time.sleep(0.005)
    raise AssertionError("background refresh did not finish")


class TestSnapshotCache(unittest.TestCase):
    def test_first_get_computes_then_serves_from_memory(self):
        c = _Compute()
        cache = status_snapshot.SnapshotCache(c, ttl_s=60)
        self.assertEqual(cache.get()[0], {"n": 1})
        value, age = cache.get()
        self.assertEqual(value, {"n": 1})
        self.assertEqual(c.calls, 1)
        self.assertLess(age, 1)

    def test_past_ttl_answers_at_once_and_refreshes_once_in_background(self):
        c = _Compute()
        cache = status_snapshot.SnapshotCache(c, ttl_s=0.01)
        cache.get()
        time.sleep(0.02)
        c.gate = threading.Event()  # hold the refresh open
        t0 = time.monotonic()
        for _ in range(5):
            self.assertEqual(cache.get()[0], {"n": 1})
        self.assertLess(time.monotonic() - t0, 0.5)
        c.gate.set()
        _settle(cache)
        self.assertEqual(c.calls, 2)  # five stale reads, one refresh
        self.assertEqual(cache.get()[0], {"n": 2})

    def test_invalidate_refreshes_without_blocking(self):
        c = _Compute()
        cache = status_snapshot.SnapshotCache(c, ttl_s=60)
        cache.get()
        cache.invalidate()
        self.assertEqual(cache.get()[0], {"n": 1})
        _settle(cache)
        self.assertEqual(cache.get()[0], {"n": 2})

    def test_invalidate_during_a_refresh_stays_stale(self):
        c = _Compute()
        cache = status_snapshot.SnapshotCache(c, ttl_s=60)
        cache.get()
        c.gate = threading.Event()
        cache.invalidate()
        cache.get()  # starts refresh #2, held at the gate
        cache.invalidate()  # a mine finishes while it runs
        c.gate.set()
        _settle(cache)
        c.gate = None
        cache.get()  # still stale: starts refresh #3
        _settle(cache)
        self.assertEqual(c.calls, 3)

    def test_concurrent_first_gets_compute_once(self):
        c = _Compute(delay=0.05)
        cache = status_snapshot.SnapshotCache(c)
        out = []
        threads = [threading.Thread(target=lambda: out.append(cache.get()[0])) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(c.calls, 1)
        self.assertEqual(out, [{"n": 1}] * 8)

    def test_failed_refresh_keeps_the_last_good_snapshot(self):
        c = _Compute(fail_after=1)
        cache = status_snapshot.SnapshotCache(c, ttl_s=0.0)
        cache.get()
        with patch.object(status_snapshot._log, "exception") as logged:
            cache.get()
            _settle(cache)
            self.assertEqual(cache.get()[0], {"n": 1})
            _settle(cache)
        self.assertTrue(logged.called)

    def test_past_max_age_recomputes_synchronously(self):
        c = _Compute()
        cache = status_snapshot.SnapshotCache(c, ttl_s=0.0, max_age_s=0.01)
        cache.get()
        time.sleep(0.02)
        _settle(cache)
        value, age = cache.get()
        self.assertGreaterEqual(value["n"], 2)
        self.assertEqual(age, 0.0)

    def test_no_snapshot_and_compute_fails_raises(self):
        cache = status_snapshot.SnapshotCache(_Compute(fail_after=0))
        with self.assertRaises(RuntimeError):
            cache.get()

    def test_warm_fills_it_in_the_background(self):
        c = _Compute()
        cache = status_snapshot.SnapshotCache(c)
        cache.warm()
        _settle(cache)
        self.assertEqual(cache.get()[0], {"n": 1})
        self.assertEqual(c.calls, 1)

    # 2026-11-17 20:00-22:0x on the production palace host: under I/O load every refresh timed out; once the last good
    # snapshot passed max_age_s, every /status/fast computed synchronously, timed out again and
    # answered 500 to every host's SessionStart line.
    def test_past_max_age_a_failed_recompute_serves_the_last_good_snapshot_with_its_age(self):
        c = _Compute(fail_after=1)
        failures = []
        cache = status_snapshot.SnapshotCache(c, ttl_s=60, max_age_s=0.01, on_failure=failures.append)
        cache.get()
        time.sleep(0.03)
        with patch.object(status_snapshot._log, "exception"), patch.object(status_snapshot._log, "warning"):
            value, age = cache.get()
            _settle(cache)
        self.assertEqual(value, {"n": 1})
        self.assertGreaterEqual(age, 0.03)
        self.assertTrue(failures)

    def test_past_max_age_a_failed_recompute_starts_a_background_refresh(self):
        sync = _Compute(fail_after=1)
        bg = _Compute()
        cache = status_snapshot.SnapshotCache(sync, ttl_s=60, max_age_s=0.01, refresh_compute=bg)
        cache.get()
        time.sleep(0.03)
        with patch.object(status_snapshot._log, "warning"):
            cache.get()
        _settle(cache)
        self.assertEqual(bg.calls, 1)
        self.assertEqual(cache.get()[0], {"n": 1})  # bg's first value
        self.assertLess(cache.get()[1], 1)

    def test_requests_queued_behind_a_failed_recompute_do_not_each_retry_it(self):
        c = _Compute(fail_after=1)
        cache = status_snapshot.SnapshotCache(c, ttl_s=60, max_age_s=0.01)
        cache.get()
        time.sleep(0.03)
        c.gate = threading.Event()
        out = []
        with patch.object(status_snapshot._log, "warning"), patch.object(status_snapshot._log, "exception"):
            ts = [threading.Thread(target=lambda: out.append(cache.get())) for _ in range(4)]
            for t in ts:
                t.start()
            time.sleep(0.1)
            c.gate.set()
            for t in ts:
                t.join(5)
            _settle(cache)
        self.assertEqual([v for v, _ in out], [{"n": 1}] * 4)
        # one synchronous attempt and at most one background one, not one per request
        self.assertLessEqual(c.calls, 3)

    def test_the_background_refresh_uses_its_own_compute(self):
        sync = _Compute()
        bg = _Compute()
        cache = status_snapshot.SnapshotCache(sync, ttl_s=0.0, refresh_compute=bg)
        cache.get()
        cache.get()
        _settle(cache)
        self.assertEqual((sync.calls, bg.calls), (1, 1))

    def test_no_refresh_compute_means_the_background_uses_compute(self):
        c = _Compute()
        cache = status_snapshot.SnapshotCache(c, ttl_s=0.0)
        cache.get()
        cache.get()
        _settle(cache)
        self.assertEqual(c.calls, 2)


class TestStatusFastRoute(unittest.TestCase):
    def test_route_serves_the_snapshot_with_its_age(self):
        from fastapi.testclient import TestClient

        import main

        c = _Compute()
        fake = status_snapshot.SnapshotCache(lambda: {"total_drawers": 3, "wings": {"w": 3}, **c()})
        with patch.dict(os.environ, {"PALACE_API_KEY": ""}, clear=False), \
                patch.object(main, "_STATUS_SNAPSHOT", fake), \
                patch.object(main, "_postgres_dsn", lambda: "dsn"):
            client = TestClient(main.app)
            a = client.get("/status/fast").json()
            b = client.get("/status/fast").json()
        self.assertEqual(a["total_drawers"], 3)
        self.assertIn("snapshot_age_s", b)
        self.assertIn("auth_rejections", b)
        self.assertEqual(c.calls, 1)
        self.assertIs(b["snapshot_stale"], False)

    def test_route_never_500s_on_a_slow_query_while_a_last_good_snapshot_exists(self):
        from fastapi.testclient import TestClient

        import main

        c = _Compute(fail_after=1)
        fake = status_snapshot.SnapshotCache(lambda: {"total_drawers": 3, **c()}, ttl_s=60, max_age_s=0.01)
        with patch.dict(os.environ, {"PALACE_API_KEY": ""}, clear=False), \
                patch.object(main, "_STATUS_SNAPSHOT", fake), \
                patch.object(main, "_postgres_dsn", lambda: "dsn"), \
                patch.object(status_snapshot._log, "warning"), patch.object(status_snapshot._log, "exception"):
            client = TestClient(main.app)
            client.get("/status/fast")
            time.sleep(0.03)
            r = client.get("/status/fast")
            _settle(fake)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["total_drawers"], 3)
        self.assertIs(body["snapshot_stale"], True)
        self.assertGreaterEqual(body["snapshot_age_s"], 0.0)


class TestStatementTimeouts(unittest.TestCase):
    """The synchronous path keeps 3 s; only the background refresh waits up to 30 s."""

    def _run(self, **kw):
        import fast_intercept

        executed = []

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, *a):
                executed.append(sql)

            def fetchall(self):
                return []

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return Cur()

            def close(self):
                pass

        with patch("postgres.postgres_dsn", lambda: "dsn"), \
                patch("psycopg2.connect", lambda *a, **k: Conn()):
            fast_intercept.fast_status_payload(**kw)
        return [s for s in executed if "statement_timeout" in s]

    def test_default_is_three_seconds(self):
        self.assertEqual(self._run(), ["SET LOCAL statement_timeout = '3000ms'"])

    def test_a_longer_timeout_is_passed_through(self):
        self.assertEqual(self._run(statement_timeout_s=30), ["SET LOCAL statement_timeout = '30000ms'"])

    def test_a_non_number_timeout_is_refused(self):
        with self.assertRaises((TypeError, ValueError)):
            self._run(statement_timeout_s="3s; DROP TABLE x")

    def test_main_wires_3s_for_requests_and_30s_for_the_background_refresh(self):
        import main

        seen = []
        with patch.object(main, "_fast_status_payload", lambda **kw: seen.append(kw) or {}):
            main._STATUS_SNAPSHOT._compute()
            main._STATUS_SNAPSHOT._refresh_compute()
        self.assertEqual(seen[0].get("statement_timeout_s", 3), 3)
        self.assertEqual(seen[1]["statement_timeout_s"], 30)


class TestTotalIsTheSumOfWings(unittest.TestCase):
    def test_no_separate_count_query(self):
        import fast_intercept

        executed = []

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, *a):
                executed.append(sql)
                self.last = sql

            def fetchall(self):
                if "GROUP BY wing" in self.last:
                    return [("a", 5), ("b", 2), (None, 1)]
                return [("r", 8)]

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def cursor(self):
                return Cur()

            def close(self):
                pass

        with patch("postgres.postgres_dsn", lambda: "dsn"), \
                patch("psycopg2.connect", lambda *a, **k: Conn()):
            out = fast_intercept.fast_status_payload()
        self.assertEqual(out["total_drawers"], 8)
        self.assertFalse(any(s.strip().startswith("SELECT count(*) FROM") for s in executed))


if __name__ == "__main__":
    unittest.main()
