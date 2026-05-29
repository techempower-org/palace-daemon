"""Tests for ``rooms.validate_room_or_raise`` — the shared room-validation
helper that consolidates two inline blocks previously in /search/hybrid and
/search/age-fused (they had drifted apart on error-message text).

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_room_validation.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import rooms  # noqa: E402


class TestValidateRoomOrRaise(unittest.TestCase):
    def setUp(self):
        # Reset the cache so each test starts from a known state.
        rooms._canonical_rooms_cache = None

    def tearDown(self):
        rooms._canonical_rooms_cache = None

    def test_none_passes_through(self):
        """`room=None` is a no-op — the parameter was optional."""
        # No exception raised.
        rooms.validate_room_or_raise(None)

    def test_canonical_room_passes(self):
        """A canonical room name returns without raising."""
        rooms._canonical_rooms_cache = {"planning", "decisions"}
        rooms.validate_room_or_raise("planning")
        rooms.validate_room_or_raise("decisions")

    def test_non_canonical_raises_400(self):
        """A non-canonical name raises HTTP 400 with valid_rooms enumerated."""
        rooms._canonical_rooms_cache = {"planning", "decisions"}
        with self.assertRaises(HTTPException) as ctx:
            rooms.validate_room_or_raise("not-a-room")
        self.assertEqual(ctx.exception.status_code, 400)
        detail = ctx.exception.detail
        self.assertIn("not-a-room", detail["error"])
        self.assertEqual(detail["valid_rooms"], ["decisions", "planning"])  # sorted

    def test_valid_rooms_sorted_in_error(self):
        """Operators get a stable order for error-body comparison."""
        rooms._canonical_rooms_cache = {"zebra", "alpha", "mike"}
        with self.assertRaises(HTTPException) as ctx:
            rooms.validate_room_or_raise("bogus")
        self.assertEqual(ctx.exception.detail["valid_rooms"], ["alpha", "mike", "zebra"])


if __name__ == "__main__":
    unittest.main()
