"""Tests for instruments.py: the 401 counter, the over-cap check, source status.

Run with::

    python -m unittest tests.test_instruments -v
"""
import asyncio
import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import instruments  # noqa: E402


class TestAuthRejectionCounter(unittest.TestCase):
    def setUp(self):
        instruments._REJECTIONS.clear()

    def _run(self, status, path="/mine"):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        sent = []

        async def send(message):
            sent.append(message)

        mw = instruments.AuthRejectionCounter(app)
        asyncio.run(mw({"type": "http", "path": path}, None, send))
        return sent

    def test_counts_401_and_passes_messages_through(self):
        sent = self._run(401, "/mine")
        self.assertEqual([m["type"] for m in sent], ["http.response.start", "http.response.body"])
        s = instruments.auth_rejection_summary()
        self.assertEqual(s["last_24h"], 1)
        self.assertEqual(s["by_path"], {"/mine": 1})
        self.assertIsNotNone(s["last_at"])

    def test_recorded_path_is_bounded(self):
        self._run(401, "/" + "a" * 100_000)
        (path,) = instruments.auth_rejection_summary()["by_path"]
        self.assertEqual(len(path), instruments._PATH_MAX)

    def test_other_statuses_are_not_counted(self):
        for status in (200, 202, 403, 404, 500):
            self._run(status)
        self.assertEqual(instruments.auth_rejection_summary()["last_24h"], 0)

    def test_non_http_scopes_pass_through(self):
        called = []

        async def app(scope, receive, send):
            called.append(scope["type"])

        asyncio.run(instruments.AuthRejectionCounter(app)({"type": "lifespan"}, None, None))
        self.assertEqual(called, ["lifespan"])

    def test_window_drops_rejections_older_than_24h(self):
        now = time.time()
        instruments.record_rejection("/old", now - 25 * 3600)
        instruments.record_rejection("/new", now - 60)
        s = instruments.auth_rejection_summary(now)
        self.assertEqual(s["last_24h"], 1)
        self.assertEqual(s["by_path"], {"/new": 1})

    def test_window_incomplete_for_a_young_daemon(self):
        with patch.object(instruments, "_PROCESS_START", time.time() - 60):
            self.assertFalse(instruments.auth_rejection_summary()["window_complete"])
        with patch.object(instruments, "_PROCESS_START", time.time() - 25 * 3600):
            self.assertTrue(instruments.auth_rejection_summary()["window_complete"])


def _fake_convo_miner(**attrs):
    mod = types.ModuleType("mempalace.convo_miner")
    for k, v in attrs.items():
        setattr(mod, k, v)
    pkg = types.ModuleType("mempalace")
    pkg.convo_miner = mod
    return {"mempalace": pkg, "mempalace.convo_miner": mod}


class TestTranscriptOverCap(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "t.jsonl")
        with open(self.path, "wb") as f:
            f.write(b"x" * 100)

    def tearDown(self):
        self._tmp.cleanup()

    def test_over_max_file_size(self):
        with patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=50)):
            self.assertEqual(instruments.transcript_over_cap(self.path), (100, 50))

    def test_under_cap(self):
        with patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=500)):
            self.assertIsNone(instruments.transcript_over_cap(self.path))

    def test_per_format_rule_wins_over_max_file_size(self):
        mods = _fake_convo_miner(MAX_FILE_SIZE=50, size_cap_for=lambda p: None)
        with patch.dict(sys.modules, mods):
            self.assertIsNone(instruments.transcript_over_cap(self.path))

    def test_missing_file_and_directory_are_not_over_cap(self):
        with patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=1)):
            self.assertIsNone(instruments.transcript_over_cap(self.path + ".nope"))
            self.assertIsNone(instruments.transcript_over_cap(self._tmp.name))


class TestResolveSourcePath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "projects")
        os.makedirs(os.path.join(self.root, "p"))
        self._p = patch.object(instruments, "SOURCE_ROOT", self.root)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_accepts_a_transcript_under_the_root(self):
        path = os.path.join(self.root, "p", "s.jsonl")
        self.assertEqual(instruments.resolve_source_path(path), path)

    def test_refuses_outside_traversal_relative_and_other_suffixes(self):
        for path in (
            "/etc/passwd.jsonl",
            os.path.join(self.root, "..", "x.jsonl"),
            "p/s.jsonl",
            os.path.join(self.root, "p", "s.json"),
        ):
            self.assertIsNone(instruments.resolve_source_path(path), path)

    def test_refuses_a_symlink_that_escapes_the_root(self):
        outside = os.path.join(self._tmp.name, "secret.jsonl")
        open(outside, "w").close()
        link = os.path.join(self.root, "p", "link.jsonl")
        os.symlink(outside, link)
        self.assertIsNone(instruments.resolve_source_path(link))


class TestSaturation(unittest.TestCase):
    def setUp(self):
        instruments._REJECTIONS.clear()

    def tearDown(self):
        instruments._REJECTIONS.clear()

    def test_saturated_only_while_the_window_is_full(self):
        now = time.time()
        with patch.object(instruments, "_REJECTIONS",
                          instruments.collections.deque(maxlen=3)):
            for _ in range(3):
                instruments.record_rejection("/old", now - 25 * 3600)
            self.assertFalse(instruments.auth_rejection_summary(now)["saturated"])
            for _ in range(3):
                instruments.record_rejection("/new", now - 10)
            self.assertTrue(instruments.auth_rejection_summary(now)["saturated"])


class _Cur:
    def __init__(self, row):
        self.row = row
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.cur = _Cur(row)
        self.closed = False

    def cursor(self):
        return self.cur

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class TestSourceStatus(unittest.TestCase):
    def setUp(self):
        instruments._SOURCE_CACHE.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "s.jsonl")
        with open(self.path, "wb") as f:
            f.write(b"x" * 10)
        self.mtime = time.time()
        os.utime(self.path, (self.mtime, self.mtime))

    def tearDown(self):
        self._tmp.cleanup()

    def test_lag_against_utc_timestamp(self):
        newest = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.mtime - 7200))
        conn = _Conn([(newest,)] + [(None,)] * 4)
        with patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=1000)):
            out = instruments.source_status(self.path, lambda: conn)
        self.assertTrue(out["exists"])
        self.assertFalse(out["over_cap"])
        self.assertEqual(out["drawers"], 5)
        self.assertAlmostEqual(out["lag_s"], 7200, delta=2)
        self.assertTrue(conn.closed)
        self.assertEqual(conn.cur.executed[-1][1], (self.path,))

    def test_lag_against_naive_local_timestamp(self):
        newest = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.mtime - 600))
        with patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=1000)):
            out = instruments.source_status(self.path, lambda: _Conn([(newest,)]))
        self.assertAlmostEqual(out["lag_s"], 600, delta=2)

    def test_newest_is_the_later_instant_across_formats(self):
        tz_newer = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.mtime - 60))
        naive_older = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.mtime - 7200))
        with patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=1000)):
            out = instruments.source_status(
                self.path, lambda: _Conn([(naive_older,), (tz_newer,)] + [(None,)] * 4)
            )
        self.assertAlmostEqual(out["lag_s"], 60, delta=2)
        self.assertEqual(out["drawers"], 6)

    def test_malformed_values_are_ignored_not_fatal(self):
        good = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.mtime - 300))
        later_bad_same_shape = "2999-11-04T99:00:00Z"
        rows = [("2026-02-30T12:00:00Z",), (later_bad_same_shape,), ("junk",), (good,), (good,)]
        with patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=1000)):
            out = instruments.source_status(self.path, lambda: _Conn(rows))
        self.assertAlmostEqual(out["lag_s"], 300, delta=2)
        self.assertEqual(out["drawers"], 5)

    def test_space_and_t_separators_compare_as_instants(self):
        later = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(self.mtime - 30))
        earlier = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.mtime - 9000))
        with patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=1000)):
            out = instruments.source_status(self.path, lambda: _Conn([(earlier,), (later,)]))
        self.assertAlmostEqual(out["lag_s"], 30, delta=2)

    def test_over_cap_and_no_drawers(self):
        with patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=5)):
            out = instruments.source_status(self.path, lambda: _Conn([]))
        self.assertTrue(out["over_cap"])
        self.assertEqual(out["cap_bytes"], 5)
        self.assertEqual(out["drawers"], 0)
        self.assertIsNone(out["lag_s"])

    def test_missing_file(self):
        out = instruments.source_status(self.path + ".gone", lambda: _Conn([]))
        self.assertFalse(out["exists"])


if __name__ == "__main__":
    unittest.main()


class TestStatusSourceRoute(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        import main

        self.main = main
        self._env = patch.dict(os.environ, {"PALACE_API_KEY": "k"}, clear=False)
        self._env.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self._env.stop()

    def test_requires_the_key_and_is_counted_as_a_401(self):
        instruments._REJECTIONS.clear()
        r = self.client.get("/status/source", params={"path": "/x.jsonl"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(instruments.auth_rejection_summary()["by_path"], {"/status/source": 1})

    def test_refuses_paths_outside_the_transcript_tree(self):
        r = self.client.get(
            "/status/source", params={"path": "/etc/passwd.jsonl"}, headers={"X-API-Key": "k"}
        )
        self.assertEqual(r.status_code, 422)

    def test_returns_the_source_status(self):
        path = os.path.join(instruments.SOURCE_ROOT, "p", "s.jsonl")
        with patch.object(self.main, "_postgres_dsn", lambda: "dsn"), \
                patch.object(self.main._instruments, "source_status",
                             lambda p, connect: {"path": p, "drawers": 3}):
            r = self.client.get(
                "/status/source", params={"path": path}, headers={"X-API-Key": "k"}
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"path": path, "drawers": 3})


class TestSourceCache(unittest.TestCase):
    def setUp(self):
        instruments._SOURCE_CACHE.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "c.jsonl")
        with open(self.path, "wb") as f:
            f.write(b"x")
        self.calls = 0
        self.newest = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 60))

    def tearDown(self):
        instruments._SOURCE_CACHE.clear()
        self._tmp.cleanup()

    def _connect(self):
        self.calls += 1
        return _Conn([(self.newest,)])

    def _status(self):
        with patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=1000)):
            return instruments.source_status(self.path, self._connect)

    def test_second_call_is_served_from_the_cache(self):
        a, b = self._status(), self._status()
        self.assertEqual(self.calls, 1)
        self.assertFalse(a["cached"])
        self.assertTrue(b["cached"])
        self.assertEqual(a["drawers"], b["drawers"])

    def test_a_mine_of_the_path_or_its_directory_invalidates(self):
        self._status()
        instruments.invalidate_source(self.path)
        self._status()
        instruments.invalidate_source(self._tmp.name)
        self._status()
        self.assertEqual(self.calls, 3)

    def test_unrelated_and_sibling_prefix_paths_keep_their_entry(self):
        self._status()
        instruments.invalidate_source(self.path + ".other")
        instruments.invalidate_source(self._tmp.name + "x")
        self._status()
        self.assertEqual(self.calls, 1)

    def test_ttl_expiry_refreshes(self):
        self._status()
        with patch.object(instruments, "_SOURCE_CACHE_TTL_S", 0):
            self._status()
        self.assertEqual(self.calls, 2)

    def test_file_side_is_never_cached(self):
        self._status()
        with open(self.path, "ab") as f:
            f.write(b"more")
        self.assertEqual(self._status()["size"], 5)

    def test_cache_is_bounded(self):
        with patch.object(instruments, "_SOURCE_CACHE_MAX", 3), \
                patch.dict(sys.modules, _fake_convo_miner(MAX_FILE_SIZE=1000)):
            for i in range(5):
                instruments.source_status(self.path + str(i), self._connect)
        self.assertEqual(len(instruments._SOURCE_CACHE), 3)
