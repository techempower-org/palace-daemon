"""Regression test: suppressed Stop invocations must leave a log trace.

When the harness passes ``stop_hook_active: true``, ``hook_stop`` bails
before doing any work — by design. But before 2026-06-10 it bailed
before its first ``_log`` call too, making "harness never invoked the
hook" and "harness invoked it with stop_hook_active" indistinguishable
in hook.log. A session went silent for 3.5 hours and the log couldn't
say which of the two had happened.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_hook_stop_suppression.py -q
"""
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENTS = os.path.join(os.path.dirname(_HERE), "clients")
if _CLIENTS not in sys.path:
    sys.path.insert(0, _CLIENTS)

import hook  # noqa: E402


class TestStopSuppressionLogging(unittest.TestCase):
    def _run(self, stop_hook_active):
        logged = []
        data = {
            "session_id": "abc12345-0000-0000-0000-000000000000",
            "transcript_path": "/nonexistent/transcript.jsonl",
            "stop_hook_active": stop_hook_active,
        }
        with patch.object(hook, "_log", logged.append), \
             patch.object(hook, "_output") as output, \
             patch.object(hook, "_count_human_messages") as counter:
            counter.return_value = 0
            hook.hook_stop(data, "claude-code")
        return logged, output, counter

    def test_suppressed_stop_logs_before_exit(self):
        for active in (True, "true", "1", "yes"):
            logged, output, counter = self._run(active)
            self.assertTrue(
                any("stop suppressed" in m for m in logged),
                f"no suppression log for stop_hook_active={active!r}: {logged}",
            )
            output.assert_called_once_with({})
            counter.assert_not_called()

    def test_active_false_proceeds_past_the_gate(self):
        logged, _, counter = self._run(False)
        self.assertFalse(any("stop suppressed" in m for m in logged))
        counter.assert_called_once()


if __name__ == "__main__":
    unittest.main()
