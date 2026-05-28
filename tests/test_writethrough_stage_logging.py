"""Tests for the KG write-through stage logging (issue #76).

Today (2026-05-27) we discovered ~12,300 drawers had been written without
ever being enqueued for LLM triple extraction — ``MEMPALACE_KG_EXTRACTION_QUEUE``
was silently OFF in the daemon env, but mempalace's single "KG write-through
attached" log line gave no visibility into which composer stages actually
attached.

The fix in ``main.py`` adds ``_log_kg_writethrough_stages(env, logger)`` that
logs each stage's on/off state by env flag at startup. These tests verify the
helper's behavior so the silent-OFF condition can never recur invisibly.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_writethrough_stage_logging.py -q
"""
from __future__ import annotations

import logging
import os
import sys
import unittest
from unittest.mock import MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


def _format_log(call_args) -> str:
    """Reconstruct what the logger would actually emit."""
    template = call_args.args[0]
    fmt_args = call_args.args[1:]
    return template % fmt_args if fmt_args else template


class TestWritethroughStageLogging(unittest.TestCase):
    def _capture(self, env: dict) -> str:
        logger = MagicMock()
        main._log_kg_writethrough_stages(env, logger)
        logger.info.assert_called_once()
        return _format_log(logger.info.call_args)

    def test_both_stages_on(self):
        msg = self._capture({
            "MEMPALACE_KG_WRITETHROUGH": "1",
            "MEMPALACE_KG_EXTRACTION_QUEUE": "1",
        })
        self.assertIn("MENTIONS=on", msg)
        self.assertIn("EXTRACTION_QUEUE=on", msg)
        # Env flag names appear in the message so operators can grep for them.
        self.assertIn("MEMPALACE_KG_WRITETHROUGH", msg)
        self.assertIn("MEMPALACE_KG_EXTRACTION_QUEUE", msg)

    def test_extraction_queue_silently_off(self):
        """The exact regression scenario: WRITETHROUGH on, EXTRACTION_QUEUE unset."""
        msg = self._capture({"MEMPALACE_KG_WRITETHROUGH": "1"})
        self.assertIn("MENTIONS=on", msg)
        self.assertIn("EXTRACTION_QUEUE=OFF", msg)

    def test_mentions_off_extraction_on(self):
        msg = self._capture({"MEMPALACE_KG_EXTRACTION_QUEUE": "true"})
        self.assertIn("MENTIONS=OFF", msg)
        self.assertIn("EXTRACTION_QUEUE=on", msg)

    def test_both_off_default(self):
        msg = self._capture({})
        self.assertIn("MENTIONS=OFF", msg)
        self.assertIn("EXTRACTION_QUEUE=OFF", msg)

    def test_accepts_alternate_truthy_spellings(self):
        for val in ("1", "true", "yes", "on", "TRUE", "Yes", "On"):
            with self.subTest(val=val):
                msg = self._capture({"MEMPALACE_KG_WRITETHROUGH": val})
                self.assertIn("MENTIONS=on", msg, f"value {val!r} should be truthy")

    def test_off_for_falsy_and_blank(self):
        for val in ("0", "false", "no", "off", "", "   ", "garbage"):
            with self.subTest(val=val):
                msg = self._capture({"MEMPALACE_KG_WRITETHROUGH": val})
                self.assertIn("MENTIONS=OFF", msg, f"value {val!r} should not be truthy")

    def test_log_level_is_info(self):
        logger = MagicMock()
        main._log_kg_writethrough_stages({}, logger)
        # Must use INFO (not DEBUG), so journalctl -u palace-daemon surfaces it
        # at default log levels.
        logger.info.assert_called_once()
        logger.debug.assert_not_called()
        logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
