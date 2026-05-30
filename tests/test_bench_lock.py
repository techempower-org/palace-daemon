"""Tests for the bench-active lock (#104).

External bench runners (SME LongMemEval, etc.) touch a lock file to pause
palace-daemon's WatcherService-spawned auto-mine while they're driving the
daemon hard. These tests verify the daemon-side check: lock detection,
stale-lock auto-ignore, env override, and graceful degradation when the
palace path isn't resolvable.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_bench_lock.py -q
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class TestBenchLockPath(unittest.TestCase):
    def test_env_override_wins(self):
        with patch.dict(os.environ, {"PALACE_BENCH_LOCK_PATH": "/tmp/x.lock"}):
            self.assertEqual(main._bench_lock_path(), "/tmp/x.lock")

    def test_default_uses_mp_config_palace_path(self):
        os.environ.pop("PALACE_BENCH_LOCK_PATH", None)
        fake_config = type("FakeConfig", (), {"palace_path": "/srv/test-palace"})()
        with patch.object(main._mp, "_config", fake_config):
            self.assertEqual(
                main._bench_lock_path(),
                "/srv/test-palace/.bench-active.lock",
            )

    def test_falls_back_when_config_unavailable(self):
        """If the config provider throws, we still return a (fallback) path."""
        os.environ.pop("PALACE_BENCH_LOCK_PATH", None)
        # Use the _config_provider injection point that the refactored
        # bench_lock_path() exposes — simpler than patching the lazy import.
        def _broken_provider():
            raise RuntimeError("config not initialized")
        import bench_lock
        path = bench_lock.bench_lock_path(_config_provider=_broken_provider)
        self.assertTrue(path)
        self.assertIn(".palace-bench-active.lock", path)


class TestBenchLockActive(unittest.TestCase):
    def setUp(self):
        self.lock_path = "/tmp/test-bench-active.lock"
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass
        os.environ["PALACE_BENCH_LOCK_PATH"] = self.lock_path

    def tearDown(self):
        try:
            os.unlink(self.lock_path)
        except FileNotFoundError:
            pass
        os.environ.pop("PALACE_BENCH_LOCK_PATH", None)
        os.environ.pop("PALACE_BENCH_LOCK_MAX_AGE_SECONDS", None)

    def test_no_lock_returns_false(self):
        active, reason = main._bench_lock_active()
        self.assertFalse(active)
        self.assertIn("no lock", reason)

    def test_fresh_lock_returns_true(self):
        with open(self.lock_path, "w") as f:
            f.write("")
        active, reason = main._bench_lock_active()
        self.assertTrue(active)
        self.assertIn(self.lock_path, reason)
        self.assertIn("age=", reason)

    def test_stale_lock_returns_false(self):
        with open(self.lock_path, "w") as f:
            f.write("")
        old_ts = time.time() - 7 * 3600
        os.utime(self.lock_path, (old_ts, old_ts))
        active, reason = main._bench_lock_active()
        self.assertFalse(active)
        self.assertIn("stale", reason)

    def test_custom_max_age(self):
        with open(self.lock_path, "w") as f:
            f.write("")
        old_ts = time.time() - 300
        os.utime(self.lock_path, (old_ts, old_ts))
        os.environ["PALACE_BENCH_LOCK_MAX_AGE_SECONDS"] = "60"
        active, reason = main._bench_lock_active()
        self.assertFalse(active)
        self.assertIn("stale", reason)

    def test_non_numeric_max_age_falls_back_to_default(self):
        with open(self.lock_path, "w") as f:
            f.write("")
        os.environ["PALACE_BENCH_LOCK_MAX_AGE_SECONDS"] = "not-a-number"
        active, _ = main._bench_lock_active()
        self.assertTrue(active)

    def test_unreadable_path_returns_inactive(self):
        """A bad path shouldn't block auto-mine — fall back to inactive."""
        os.environ["PALACE_BENCH_LOCK_PATH"] = "/proc/nonexistent/sub/path/file"
        active, _ = main._bench_lock_active()
        self.assertFalse(active)


class TestBenchLockRefcount(unittest.TestCase):
    """Refcount mode (#196): the lock path is a directory of PID markers.

    Active while ≥1 non-stale marker exists; stale (by-age) markers are
    reaped. PID-liveness is deliberately NOT a reap criterion (SME benches
    SSH in and record a remote PID), so these tests don't rely on it.
    """

    def setUp(self):
        import tempfile
        self.dir_path = tempfile.mkdtemp(prefix="bench-refcount-")
        os.environ["PALACE_BENCH_LOCK_PATH"] = self.dir_path

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir_path, ignore_errors=True)
        os.environ.pop("PALACE_BENCH_LOCK_PATH", None)
        os.environ.pop("PALACE_BENCH_LOCK_MAX_AGE_SECONDS", None)

    def _marker(self, pid, age_s=0):
        path = os.path.join(self.dir_path, f"{pid}.marker")
        with open(path, "w") as f:
            f.write("")
        if age_s:
            ts = time.time() - age_s
            os.utime(path, (ts, ts))
        return path

    def test_empty_dir_is_inactive(self):
        active, reason = main._bench_lock_active()
        self.assertFalse(active)
        self.assertIn("no live markers", reason)

    def test_one_marker_is_active(self):
        self._marker(11111)
        active, reason = main._bench_lock_active()
        self.assertTrue(active)
        self.assertIn("active=1", reason)

    def test_two_markers_refcount_two(self):
        self._marker(11111)
        self._marker(22222)
        active, reason = main._bench_lock_active()
        self.assertTrue(active)
        self.assertIn("active=2", reason)

    def test_stale_marker_reaped_age_only(self):
        os.environ["PALACE_BENCH_LOCK_MAX_AGE_SECONDS"] = "60"
        self._marker(11111, age_s=0)          # fresh
        stale = self._marker(99999, age_s=300)  # stale by age
        active, reason = main._bench_lock_active()
        self.assertTrue(active)               # the fresh one keeps it active
        self.assertIn("active=1", reason)
        self.assertIn("reaped=1", reason)
        self.assertFalse(os.path.exists(stale))  # stale marker was reaped

    def test_all_stale_is_inactive(self):
        os.environ["PALACE_BENCH_LOCK_MAX_AGE_SECONDS"] = "60"
        self._marker(11111, age_s=300)
        self._marker(22222, age_s=300)
        active, reason = main._bench_lock_active()
        self.assertFalse(active)
        self.assertIn("no live markers", reason)

    def test_live_marker_with_dead_pid_not_reaped(self):
        """A fresh marker whose PID is dead/non-local must NOT be reaped —
        the SSH'd-bench case. Age is the only reap criterion."""
        # PID 1 is alive but we also test a clearly-not-on-this-host-style id;
        # either way, a fresh marker stays live regardless of PID liveness.
        self._marker(2147480000, age_s=0)  # implausible PID, fresh
        active, reason = main._bench_lock_active()
        self.assertTrue(active)
        self.assertIn("active=1", reason)

    def test_non_marker_files_ignored(self):
        # Files without the .marker suffix don't count toward the refcount.
        with open(os.path.join(self.dir_path, "README.txt"), "w") as f:
            f.write("not a marker")
        active, reason = main._bench_lock_active()
        self.assertFalse(active)
        self.assertIn("no live markers", reason)


if __name__ == "__main__":
    unittest.main()
