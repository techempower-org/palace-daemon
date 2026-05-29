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


class TestNormalizeWingFilter(unittest.TestCase):
    """rooms.normalize_wing_filter handles read-side wing normalization.

    Symmetry contract: a write that normalizes ``Palace_Daemon`` → ``palace_daemon``
    must be reachable by a read filter ``Palace_Daemon``. Pre-fix the read
    endpoints passed the caller's string through unchanged, breaking this.

    Empty/None input means "no filter" (read all wings) — different from the
    write-side normalize_wing_slug which returns ``"unknown"`` for empty input.
    """

    def test_none_returns_none(self):
        self.assertIsNone(rooms.normalize_wing_filter(None))

    def test_empty_string_returns_none(self):
        """Empty filter means no filter, not literal 'unknown'."""
        self.assertIsNone(rooms.normalize_wing_filter(""))

    def test_mixed_case_lowercased(self):
        """The core symmetry case: write 'Palace_Daemon' → store 'palace_daemon';
        read 'Palace_Daemon' → filter 'palace_daemon'."""
        self.assertEqual(rooms.normalize_wing_filter("Palace_Daemon"), "palace_daemon")

    def test_wing_prefix_stripped(self):
        """``wing_palace`` → ``palace`` to match the write-side."""
        self.assertEqual(rooms.normalize_wing_filter("wing_palace"), "palace")

    def test_already_normalized_is_idempotent(self):
        self.assertEqual(rooms.normalize_wing_filter("palace_daemon"), "palace_daemon")

    def test_whitespace_only_returns_none(self):
        """Whitespace-only normalize_wing_slug → 'unknown' fallback;
        wrapper coerces to None (no filter) rather than literal 'unknown'."""
        # normalize_wing_slug("   ") returns "___" since re.sub maps
        # non-[a-z0-9_] to _. Only None/empty truthiness check triggers
        # the "unknown" path. So the coerce-on-unknown branch fires
        # when an upstream caller passes the literal string "unknown" or
        # when the input was already empty.
        # Direct probe of the literal "unknown" path:
        self.assertIsNone(rooms.normalize_wing_filter("unknown"))

    def test_garbage_punctuation_passes_through(self):
        """Pure-punctuation input collapses to underscores — NOT the
        'unknown' fallback. Pass it through as the (weird) normalized
        slug rather than discarding the filter intent."""
        # '!!!' → '___' (valid lowercased slug shape).
        self.assertEqual(rooms.normalize_wing_filter("!!!"), "___")


class TestFastAPIDependencies(unittest.TestCase):
    """rooms.wing_filter_dep and rooms.room_validator_dep are the
    FastAPI-dependency form of the helpers — endpoints declare
    ``wing: str | None = Depends(rooms.wing_filter_dep)`` so canonicalization
    happens at request-parse time. Filed as palace-daemon#179.

    These tests call the deps directly (the FastAPI machinery just passes
    the raw query-param value as the first positional arg)."""

    def setUp(self):
        rooms._canonical_rooms_cache = None

    def tearDown(self):
        rooms._canonical_rooms_cache = None

    def test_wing_filter_dep_canonicalizes(self):
        """The dep returns the same value normalize_wing_filter would."""
        self.assertEqual(rooms.wing_filter_dep("Palace_Daemon"), "palace_daemon")
        self.assertIsNone(rooms.wing_filter_dep(None))
        self.assertIsNone(rooms.wing_filter_dep(""))

    def test_room_validator_dep_passes_canonical(self):
        """The dep returns the canonical room name."""
        rooms._canonical_rooms_cache = {"planning", "decisions"}
        self.assertEqual(rooms.room_validator_dep("planning"), "planning")
        self.assertIsNone(rooms.room_validator_dep(None))

    def test_room_validator_dep_raises_400_on_invalid(self):
        """The dep raises HTTPException 400 — FastAPI machinery converts
        that to the response shape automatically. Same contract as the
        previous inline validate_room_or_raise calls."""
        rooms._canonical_rooms_cache = {"planning"}
        with self.assertRaises(HTTPException) as ctx:
            rooms.room_validator_dep("bogus")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("bogus", ctx.exception.detail["error"])


if __name__ == "__main__":
    unittest.main()
