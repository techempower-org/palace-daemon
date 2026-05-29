"""Tests for the hard auto-mine kill-switch (palace-daemon#190).

`bench_lock.automine_disabled()` is the absolute env gate the watcher's
_internal_mine checks before spawning. Distinct from the advisory
`.bench-active.lock` (bench_lock_active), which only gates newly-spawned
mines and has a finish→next-tick window a fresh mine can slip through —
the window that let #190's mid-bench SIGTERM happen.

Run with::

    python -m unittest tests.test_automine_disable -v
"""
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import bench_lock  # noqa: E402


class TestAutomineDisabled(unittest.TestCase):

    def _disabled(self, value):
        with patch.dict(os.environ, {"PALACE_DISABLE_AUTOMINE": value}, clear=False):
            return bench_lock.automine_disabled()

    def test_unset_is_enabled(self):
        # Absence of the var means auto-mine runs (default behavior).
        env = dict(os.environ)
        env.pop("PALACE_DISABLE_AUTOMINE", None)
        with patch.dict(os.environ, env, clear=True):
            disabled, reason = bench_lock.automine_disabled()
        self.assertFalse(disabled)
        self.assertEqual(reason, "")

    def test_truthy_values_disable(self):
        for v in ("1", "true", "True", "TRUE", "yes", "on", "  on  "):
            disabled, reason = self._disabled(v)
            self.assertTrue(disabled, f"{v!r} should disable auto-mine")
            self.assertIn("PALACE_DISABLE_AUTOMINE", reason)

    def test_falsey_values_keep_enabled(self):
        for v in ("", "0", "false", "no", "off", "garbage"):
            disabled, reason = self._disabled(v)
            self.assertFalse(disabled, f"{v!r} should NOT disable auto-mine")
            self.assertEqual(reason, "")

    def test_return_shape_matches_bench_lock_active(self):
        # The watcher logs either gate uniformly, so both must return
        # (bool, str).
        disabled, reason = self._disabled("1")
        self.assertIsInstance(disabled, bool)
        self.assertIsInstance(reason, str)


if __name__ == "__main__":
    unittest.main()
