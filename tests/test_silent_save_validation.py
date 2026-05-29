"""Regression tests for /silent-save body validation (palace-daemon #36).

Before #36, ``wing`` defaulted to ``""`` and was silently passed to
``tool_diary_write(wing="")``. Per AGENTS.md the daemon promises every
write response carries ``warnings``/``errors``, but stock mempalace did
not emit a warning for the empty-wing case — meaning the operator never
saw the broken default.

The fix lives in ``main.silent_save``: when ``wing`` is missing, ``None``,
or whitespace, the daemon prepends its own warning to the response so
``messages.save_ok`` renders the ⚠ glyph and the warning text on the
themed systemMessage.

Run with::

    cd /home/jp/Projects/palace-daemon
    python -m unittest tests.test_silent_save_validation -v
"""
import os
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


_EMPTY_WING_WARNING = "wing is empty — diary entry will have no wing association"


async def _ok_write(payload):
    """Stand-in for ``_do_silent_save_write`` — succeeds, no extra warnings."""
    return {"success": True, "entry_id": "drawer_test"}


class TestSilentSaveEmptyWingWarning(unittest.TestCase):
    """Lock in the empty-wing warning behavior."""

    def setUp(self):
        # Clear PALACE_API_KEY so _check_auth is a no-op; this test does
        # not need to exercise auth — that lives in test_hook_auth.
        self._env_patch = patch.dict(os.environ, {"PALACE_API_KEY": ""}, clear=False)
        self._env_patch.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self._env_patch.stop()

    def test_missing_wing_field_emits_warning(self):
        with patch.object(main, "_do_silent_save_write", new=_ok_write):
            resp = self.client.post(
                "/silent-save",
                json={"entry": "test entry", "session_id": "s1"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn(_EMPTY_WING_WARNING, body["warnings"])

    def test_empty_string_wing_emits_warning(self):
        with patch.object(main, "_do_silent_save_write", new=_ok_write):
            resp = self.client.post(
                "/silent-save",
                json={"entry": "test entry", "wing": ""},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn(_EMPTY_WING_WARNING, body["warnings"])

    def test_whitespace_wing_emits_warning(self):
        with patch.object(main, "_do_silent_save_write", new=_ok_write):
            resp = self.client.post(
                "/silent-save",
                json={"entry": "test entry", "wing": "   \t  "},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn(_EMPTY_WING_WARNING, body["warnings"])

    def test_valid_wing_does_not_emit_warning(self):
        with patch.object(main, "_do_silent_save_write", new=_ok_write):
            resp = self.client.post(
                "/silent-save",
                json={"entry": "test entry", "wing": "diary_selene"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertNotIn(_EMPTY_WING_WARNING, body["warnings"])

    def test_mempalace_warnings_still_forwarded(self):
        # Daemon's own warnings must not clobber upstream warnings; both
        # surface together.
        async def _write_with_warnings(payload):
            return {
                "success": True,
                "entry_id": "drawer_test",
                "warnings": ["room 'foo' is not canonical. accepted as-is."],
            }

        with patch.object(main, "_do_silent_save_write", new=_write_with_warnings):
            resp = self.client.post(
                "/silent-save",
                json={"entry": "test entry"},  # no wing → daemon warns
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Both warnings present, daemon's first.
        self.assertEqual(body["warnings"][0], _EMPTY_WING_WARNING)
        self.assertIn(
            "room 'foo' is not canonical. accepted as-is.",
            body["warnings"],
        )

    def test_missing_entry_still_rejected(self):
        # The wing validation is a *warning*; the entry validation is
        # still a hard rejection. Post-#179 pydantic returns 422 for
        # missing-required-field rather than the previous inline 400 —
        # the contract that "entry is required" is preserved, just the
        # HTTP code surface changed to pydantic's standard.
        resp = self.client.post(
            "/silent-save",
            json={"wing": "diary_selene"},
        )
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
