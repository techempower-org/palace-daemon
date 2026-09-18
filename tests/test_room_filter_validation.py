# ── prepared: palace-daemon tests/test_room_filter_validation.py ───────
"""Read-side room validation — a filter may name any room that EXISTS (#285).

`/list?wing=2g&room=diary` returned 400 "not in the canonical set" while the
daemon was serving those very drawers: 200 OK with all 50 rows `room=diary`
when the same request omitted the filter. Measured against production:
79,889 drawers (8.6% of 924,568) across 8 non-canonical rooms were
unreachable by filter, `diary` alone 38,781.

Root cause is structural, not a bad constant. `rooms.py` already splits read
from write for WINGS — `normalize_wing_slug` (write) vs
`normalize_wing_filter` (read) — and its docstring says why a filter and a
write cannot share a rule. Rooms had one function, so the write rule ("only
canonical rooms may be created") was enforced on reads, where it means
"data that exists may not be read".

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_room_filter_validation.py -v
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


class _CacheReset(unittest.TestCase):
    def setUp(self):
        rooms._canonical_rooms_cache = {"planning", "decisions"}
        rooms._present_rooms_cache = None

    def tearDown(self):
        rooms._canonical_rooms_cache = None
        rooms._present_rooms_cache = None


class TestReadFilterAcceptsRoomsThatExist(_CacheReset):
    def test_canonical_room_passes(self):
        rooms.validate_room_filter_or_raise("planning")

    def test_none_passes(self):
        rooms.validate_room_filter_or_raise(None)

    def test_a_non_canonical_room_that_EXISTS_passes(self):
        """The bug, as a test: 38,781 `diary` drawers were unreachable."""
        rooms._present_rooms_cache = {"diary", "general"}
        rooms.validate_room_filter_or_raise("diary")

    def test_a_room_that_exists_nowhere_still_raises_400(self):
        """Typo protection is the reason this check exists — keep it."""
        rooms._present_rooms_cache = {"diary"}
        with self.assertRaises(HTTPException) as ctx:
            rooms.validate_room_filter_or_raise("diaryy")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_the_error_lists_what_is_actually_queryable(self):
        """Today it advertises the canonical 7, excluding the biggest room."""
        rooms._present_rooms_cache = {"diary"}
        with self.assertRaises(HTTPException) as ctx:
            rooms.validate_room_filter_or_raise("nope")
        self.assertEqual(
            ctx.exception.detail["valid_rooms"], ["decisions", "diary", "planning"]
        )


class TestStaleCacheNeverRefusesARealRoom(_CacheReset):
    def test_a_miss_re_reads_before_raising(self):
        """A cache predating a new room must not 400 it.

        The present set grows when mempalace's miner writes straight to
        postgres, so there is no write hook to invalidate on — which is why
        a TTL is the wrong mechanism and re-read-on-miss is the right one.
        """
        rooms._present_rooms_cache = {"diary"}
        calls = []

        def fake_query():
            calls.append(1)
            return {"diary", "newroom"}

        with patch.object(rooms, "_read_present_rooms", side_effect=fake_query):
            rooms.validate_room_filter_or_raise("newroom")   # must not raise
        self.assertEqual(len(calls), 1, "should re-read exactly once on a miss")


class TestFailsOpenNotClosed(_CacheReset):
    def test_an_unreadable_corpus_passes_rather_than_refuses(self):
        """Asymmetric costs: a false pass is an empty page; a false refusal
        makes existing data unreachable, which is the bug being fixed.

        Note this differs deliberately from `canonical_rooms()`, which falls
        back to the spec's seven — correct for a WRITE guard, wrong here.
        """
        with patch.object(rooms, "present_rooms", return_value=None):
            rooms.validate_room_filter_or_raise("anything-at-all")


class TestWritesAreUntouched(_CacheReset):
    def test_the_write_guard_still_refuses_a_non_canonical_room(self):
        """The tempting one-line fix is to loosen validate_room_or_raise
        itself, which opens the WRITE path and converts a read bug into a
        data-quality bug. POST /memory and PATCH /memory/{id} use it."""
        rooms._present_rooms_cache = {"diary"}
        with self.assertRaises(HTTPException):
            rooms.validate_room_or_raise("diary")



class TestSearchBodiesFilterWhileMemoryBodyWrites(_CacheReset):
    """The POST models split the same way the GET dependency does (#285).

    `_canon_room` was shared by all four models, so the tempting fix —
    loosen the one helper — would have opened POST /memory and let the
    non-canonical set grow: a read bug converted into a data-quality bug.
    """

    def test_a_search_body_accepts_a_room_that_exists(self):
        import search_models
        rooms._present_rooms_cache = {"diary"}
        body = search_models.SearchHybridBody(query="x", room="diary")
        self.assertEqual(body.room, "diary")

    def test_the_memory_write_body_still_refuses_it(self):
        import search_models
        rooms._present_rooms_cache = {"diary"}
        with self.assertRaises(HTTPException):
            search_models.MemoryBody(content="x", wing="2g", room="diary")

    def test_a_search_body_still_refuses_a_typo(self):
        import search_models
        rooms._present_rooms_cache = {"diary"}
        with self.assertRaises(HTTPException):
            search_models.SearchHybridBody(query="x", room="diaryy")


if __name__ == "__main__":
    unittest.main()
