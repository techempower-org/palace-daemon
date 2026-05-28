"""Tests for the deployed-mempalace canary log line (issues #92, #116).

The daemon's `_log_mempalace_canary` helper reports the mtime + age of
the newest .py file in the deployed mempalace package at every restart
so silent-deploy-drift becomes visible in journalctl. The 2026-05-28
Syncthing outage made that drift invisible for ~1.5 hours; this helper
closes the gap.

#116 update: previously the canary used mempalace/__init__.py, but that
stable file produced false-positive WARNs on releases that touched
other modules but not __init__.py. The helper now walks the package
directory via `_newest_mempalace_mtime()` and reports whole-tree
freshness.

These tests verify:
  * fresh canary (age < threshold) logs INFO
  * stale canary (age > threshold) logs WARNING
  * env-tunable threshold via PALACE_CANARY_WARN_HOURS
  * probe failure (missing package / empty walk) doesn't crash
  * non-numeric / blank threshold falls back to the 24h default
  * the newest file's basename is in the log message

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_mempalace_canary.py -q
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402
import canaries  # noqa: E402  — #101 extraction; tests patch the new module's names


class TestMempalaceCanary(unittest.TestCase):
    """The helper logs INFO when fresh, WARNING when stale, never crashes."""

    def _patch_newest(self, mtime_offset_secs: float, filename: str = "searcher.py"):
        """Patch `newest_mempalace_mtime` in the canaries module so production-
        code intra-module calls see the mock. Patching main's re-export name
        won't intercept because log_mempalace_canary calls newest_mempalace_mtime
        from canaries' own namespace.
        """
        mtime = time.time() + mtime_offset_secs
        return patch.object(
            canaries, "newest_mempalace_mtime",
            return_value=(mtime, f"/fake/mempalace/{filename}"),
        )

    def test_fresh_canary_logs_info(self):
        """Just-deployed mempalace (age ~0s) → INFO, no WARNING."""
        logger = MagicMock()
        with self._patch_newest(-60):  # 1 min old
            main._log_mempalace_canary(logger, env={})
        logger.info.assert_called_once()
        logger.warning.assert_not_called()
        msg = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
        self.assertIn("mempalace canary", msg)
        self.assertIn("searcher.py", msg)
        self.assertIn("1m", msg)

    def test_stale_canary_logs_warning(self):
        """Two-day-old mempalace (default threshold 24h) → WARNING."""
        logger = MagicMock()
        with self._patch_newest(-2 * 86400):
            main._log_mempalace_canary(logger, env={})
        logger.warning.assert_called_once()
        logger.info.assert_not_called()
        msg = logger.warning.call_args.args[0] % logger.warning.call_args.args[1:]
        self.assertIn("stale", msg)
        self.assertIn("rsync-mempalace.sh", msg)
        self.assertIn("2.0d", msg)

    def test_custom_threshold_via_env(self):
        """PALACE_CANARY_WARN_HOURS overrides the 24h default."""
        logger = MagicMock()
        with self._patch_newest(-3 * 3600):
            main._log_mempalace_canary(logger, env={"PALACE_CANARY_WARN_HOURS": "1"})
        logger.warning.assert_called_once()
        logger.info.assert_not_called()

    def test_age_at_threshold_logs_info_not_warning(self):
        """Just under threshold (24h) — strict > comparison, so logs INFO."""
        logger = MagicMock()
        with self._patch_newest(-23.99 * 3600):
            main._log_mempalace_canary(logger, env={})
        logger.info.assert_called_once()
        logger.warning.assert_not_called()

    def test_non_numeric_threshold_falls_back_to_default(self):
        logger = MagicMock()
        with self._patch_newest(-3600):
            main._log_mempalace_canary(logger, env={"PALACE_CANARY_WARN_HOURS": "not-a-number"})
        logger.info.assert_called_once()
        logger.warning.assert_not_called()

    def test_blank_threshold_falls_back_to_default(self):
        logger = MagicMock()
        with self._patch_newest(-3600):
            main._log_mempalace_canary(logger, env={"PALACE_CANARY_WARN_HOURS": ""})
        logger.info.assert_called_once()

    def test_probe_returns_none_logs_skip(self):
        """If newest_mempalace_mtime returns None, log skip — don't crash."""
        logger = MagicMock()
        with patch.object(canaries, "newest_mempalace_mtime", return_value=None):
            main._log_mempalace_canary(logger, env={})
        logger.info.assert_called_once()
        msg = logger.info.call_args.args[0]
        self.assertIn("probe failed", msg)
        logger.warning.assert_not_called()

    def test_age_seconds_renders_minutes(self):
        logger = MagicMock()
        with self._patch_newest(-90):
            main._log_mempalace_canary(logger, env={})
        msg = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
        self.assertIn("age 1m", msg)

    def test_age_hours_renders_hours(self):
        logger = MagicMock()
        with self._patch_newest(-5 * 3600):
            main._log_mempalace_canary(logger, env={})
        msg = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
        self.assertIn("age 5.0h", msg)

    def test_newest_file_basename_in_log(self):
        """Log reports the newest file's basename so operator can confirm signal source."""
        logger = MagicMock()
        with self._patch_newest(-60, filename="cross_encoder_rerank.py"):
            main._log_mempalace_canary(logger, env={})
        msg = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
        self.assertIn("cross_encoder_rerank.py", msg)
        # Should NOT report the directory prefix — just basename
        self.assertNotIn("/fake/mempalace/cross", msg)


class TestNewestMempalaceMtime(unittest.TestCase):
    """`_newest_mempalace_mtime` walks the tree and picks the newest .py file."""

    def test_returns_none_when_no_init_path(self):
        fake_pkg = MagicMock()
        fake_pkg.__file__ = None
        with patch.dict(sys.modules, {"mempalace": fake_pkg}):
            result = main._newest_mempalace_mtime()
        self.assertIsNone(result)

    def test_returns_none_when_no_py_files(self):
        """Empty package dir or only non-.py files → None."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some non-.py files
            for name in ["README.md", "data.json"]:
                with open(os.path.join(tmpdir, name), "w") as f:
                    f.write("")
            fake_pkg = MagicMock()
            fake_pkg.__file__ = os.path.join(tmpdir, "__init__.py")  # doesn't exist
            with patch.dict(sys.modules, {"mempalace": fake_pkg}):
                result = main._newest_mempalace_mtime()
        self.assertIsNone(result)

    def test_picks_newest_py_in_tree(self):
        """When several .py files exist with different mtimes, return the newest."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create three .py files with controlled mtimes
            for name, age_secs in [
                ("__init__.py", 1000),     # oldest
                ("middle.py", 500),
                ("newest.py", 10),         # most recent
            ]:
                path = os.path.join(tmpdir, name)
                with open(path, "w") as f:
                    f.write("")
                ts = time.time() - age_secs
                os.utime(path, (ts, ts))
            fake_pkg = MagicMock()
            fake_pkg.__file__ = os.path.join(tmpdir, "__init__.py")
            with patch.dict(sys.modules, {"mempalace": fake_pkg}):
                result = main._newest_mempalace_mtime()
        self.assertIsNotNone(result)
        mtime, path = result
        self.assertTrue(path.endswith("newest.py"))

    def test_walks_subdirectories(self):
        """Newer .py file in a subdirectory wins over older top-level."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            init_path = os.path.join(tmpdir, "__init__.py")
            with open(init_path, "w") as f:
                f.write("")
            os.utime(init_path, (time.time() - 1000, time.time() - 1000))

            subdir = os.path.join(tmpdir, "sub")
            os.makedirs(subdir)
            new_file = os.path.join(subdir, "fresh.py")
            with open(new_file, "w") as f:
                f.write("")
            # fresh.py is naturally newer (just created)

            fake_pkg = MagicMock()
            fake_pkg.__file__ = init_path
            with patch.dict(sys.modules, {"mempalace": fake_pkg}):
                result = main._newest_mempalace_mtime()
        self.assertIsNotNone(result)
        _, path = result
        self.assertTrue(path.endswith("fresh.py"))


if __name__ == "__main__":
    unittest.main()
