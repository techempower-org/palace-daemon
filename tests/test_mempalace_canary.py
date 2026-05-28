"""Tests for the deployed-mempalace canary log line (issue #92).

The daemon's `_log_mempalace_canary` helper reports the mtime + age of
the deployed mempalace package at every restart so silent-deploy-drift
becomes visible in journalctl. The 2026-05-28 Syncthing outage made
that drift invisible for ~1.5 hours; this helper closes the gap.

These tests verify:
  * fresh canary (age < threshold) logs INFO
  * stale canary (age > threshold) logs WARNING
  * env-tunable threshold via PALACE_CANARY_WARN_HOURS
  * probe failure (missing __file__ / unreadable file) doesn't crash
  * non-numeric / blank threshold falls back to the 24h default

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_mempalace_canary.py -q
"""
from __future__ import annotations

import logging
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


class TestMempalaceCanary(unittest.TestCase):
    """The helper logs INFO when fresh, WARNING when stale, never crashes."""

    def _fake_mempalace(self, mtime_offset_secs: float, file_path="/fake/mempalace/__init__.py"):
        """Build a fake mempalace module and a mocked os.path.getmtime."""
        fake_pkg = MagicMock()
        fake_pkg.__file__ = file_path
        fake_mtime = time.time() + mtime_offset_secs  # negative for "in the past"
        return fake_pkg, fake_mtime

    def test_fresh_canary_logs_info(self):
        """Just-deployed mempalace (age ~0s) → INFO, no WARNING."""
        logger = MagicMock()
        fake_pkg, fake_mtime = self._fake_mempalace(mtime_offset_secs=-60)  # 1 min old
        with patch.dict(sys.modules, {"mempalace": fake_pkg}), \
             patch("os.path.getmtime", return_value=fake_mtime):
            main._log_mempalace_canary(logger, env={})
        logger.info.assert_called_once()
        logger.warning.assert_not_called()
        msg = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
        self.assertIn("mempalace canary", msg)
        self.assertIn("/fake/mempalace/__init__.py", msg)
        self.assertIn("1m", msg)  # age 1m

    def test_stale_canary_logs_warning(self):
        """Two-day-old mempalace (default threshold 24h) → WARNING."""
        logger = MagicMock()
        # 2 days old
        fake_pkg, fake_mtime = self._fake_mempalace(mtime_offset_secs=-2 * 86400)
        with patch.dict(sys.modules, {"mempalace": fake_pkg}), \
             patch("os.path.getmtime", return_value=fake_mtime):
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
        # 3 hours old; default 24h would log INFO, but with threshold 1h → WARNING
        fake_pkg, fake_mtime = self._fake_mempalace(mtime_offset_secs=-3 * 3600)
        with patch.dict(sys.modules, {"mempalace": fake_pkg}), \
             patch("os.path.getmtime", return_value=fake_mtime):
            main._log_mempalace_canary(logger, env={"PALACE_CANARY_WARN_HOURS": "1"})
        logger.warning.assert_called_once()
        logger.info.assert_not_called()

    def test_age_at_threshold_logs_info_not_warning(self):
        """Exactly at threshold (24h) — strict > comparison, so logs INFO."""
        logger = MagicMock()
        # Just under 24h (e.g., 23h59m)
        fake_pkg, fake_mtime = self._fake_mempalace(mtime_offset_secs=-23.99 * 3600)
        with patch.dict(sys.modules, {"mempalace": fake_pkg}), \
             patch("os.path.getmtime", return_value=fake_mtime):
            main._log_mempalace_canary(logger, env={})
        logger.info.assert_called_once()
        logger.warning.assert_not_called()

    def test_non_numeric_threshold_falls_back_to_default(self):
        """A garbage env value reverts to 24h, doesn't crash."""
        logger = MagicMock()
        fake_pkg, fake_mtime = self._fake_mempalace(mtime_offset_secs=-3600)  # 1h
        with patch.dict(sys.modules, {"mempalace": fake_pkg}), \
             patch("os.path.getmtime", return_value=fake_mtime):
            main._log_mempalace_canary(logger, env={"PALACE_CANARY_WARN_HOURS": "not-a-number"})
        # 1h is well under default 24h → INFO
        logger.info.assert_called_once()
        logger.warning.assert_not_called()

    def test_blank_threshold_falls_back_to_default(self):
        """Empty string treated as unset — uses 24h."""
        logger = MagicMock()
        fake_pkg, fake_mtime = self._fake_mempalace(mtime_offset_secs=-3600)
        with patch.dict(sys.modules, {"mempalace": fake_pkg}), \
             patch("os.path.getmtime", return_value=fake_mtime):
            main._log_mempalace_canary(logger, env={"PALACE_CANARY_WARN_HOURS": ""})
        logger.info.assert_called_once()

    def test_missing_dunder_file_logs_skip(self):
        """If mempalace.__file__ is None, log skip — don't crash."""
        logger = MagicMock()
        fake_pkg = MagicMock()
        fake_pkg.__file__ = None
        with patch.dict(sys.modules, {"mempalace": fake_pkg}):
            main._log_mempalace_canary(logger, env={})
        logger.info.assert_called_once()
        msg = logger.info.call_args.args[0]
        self.assertIn("no __file__", msg)
        logger.warning.assert_not_called()

    def test_getmtime_failure_logs_skip(self):
        """If stat fails (file gone between import and stat), log skip — don't crash."""
        logger = MagicMock()
        fake_pkg, _ = self._fake_mempalace(mtime_offset_secs=0)
        with patch.dict(sys.modules, {"mempalace": fake_pkg}), \
             patch("os.path.getmtime", side_effect=FileNotFoundError("gone")):
            main._log_mempalace_canary(logger, env={})
        logger.info.assert_called_once()
        msg = logger.info.call_args.args[0]
        self.assertIn("probe failed", msg)
        logger.warning.assert_not_called()

    def test_age_seconds_renders_minutes(self):
        """Sub-hour age renders as Nm (minutes)."""
        logger = MagicMock()
        fake_pkg, fake_mtime = self._fake_mempalace(mtime_offset_secs=-90)  # 1.5min → 1m
        with patch.dict(sys.modules, {"mempalace": fake_pkg}), \
             patch("os.path.getmtime", return_value=fake_mtime):
            main._log_mempalace_canary(logger, env={})
        msg = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
        self.assertIn("age 1m", msg)

    def test_age_hours_renders_hours(self):
        """Sub-day age renders as N.Nh (hours)."""
        logger = MagicMock()
        fake_pkg, fake_mtime = self._fake_mempalace(mtime_offset_secs=-5 * 3600)  # 5h
        with patch.dict(sys.modules, {"mempalace": fake_pkg}), \
             patch("os.path.getmtime", return_value=fake_mtime):
            main._log_mempalace_canary(logger, env={})
        msg = logger.info.call_args.args[0] % logger.info.call_args.args[1:]
        self.assertIn("age 5.0h", msg)


if __name__ == "__main__":
    unittest.main()
