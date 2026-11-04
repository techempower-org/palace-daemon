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
