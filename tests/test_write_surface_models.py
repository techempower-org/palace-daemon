"""Unit tests for the #179 write-surface pydantic models.

These lock in the contracts that were verified by live-curl probes during
the #179 migration but had no permanent regression guard:

  * The **empty-wing semantics matrix** — each write surface means
    something different by "no wing given", and the difference is
    intentional (see palace-daemon#179). A future refactor that "unifies"
    them would silently break callers; the matrix test below makes that
    break loud.

  * The **#187 regression** — pydantic v2 skips field validators on
    *default* values unless ``model_config = {"validate_default": True}``.
    MemoryBody shipped (#186) without it, so a POST omitting ``room``
    arrived as ``""`` instead of coercing to ``"discoveries"`` and
    mempalace rejected it. ``test_memorybody_coerces_missing_*`` fails if
    that config is dropped.

Pure model construction — no HTTP layer, no daemon, no palace. Run with::

    python -m unittest tests.test_write_surface_models -v
"""
import os
import sys
import unittest

from pydantic import ValidationError

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from search_models import (  # noqa: E402
    BackfillAgeBody,
    MemoryBody,
    MineBody,
    SilentSaveBody,
)


class TestEmptyWingSemanticsMatrix(unittest.TestCase):
    """The heart of #179: each surface's empty-wing default is distinct
    and intentional. Assert all four side-by-side so a "let's unify these"
    refactor trips a test rather than a production surprise."""

    def test_empty_wing_defaults_are_per_surface(self):
        self.assertEqual(MemoryBody().wing, "unknown",
                         "/memory: missing wing → 'unknown'")
        self.assertEqual(MineBody(dir="/x").wing, "general",
                         "/mine: missing wing → 'general'")
        self.assertEqual(SilentSaveBody(entry="x").wing, "",
                         "/silent-save: missing wing stays '' (handler warns)")
        self.assertIsNone(BackfillAgeBody().wing,
                          "/backfill-age: missing wing → None (filter mode)")


class TestMemoryBody(unittest.TestCase):
    """Primary write surface — and the #187 regression guard."""

    def test_memorybody_coerces_missing_wing_to_unknown(self):
        # #187: without validate_default=True this returns "" and mempalace
        # rejects it. The validator MUST run on the default.
        self.assertEqual(MemoryBody().wing, "unknown")
        self.assertEqual(MemoryBody(content="x").wing, "unknown")

    def test_memorybody_coerces_missing_room_to_discoveries(self):
        # #187: the field that actually broke in production — a POST with
        # no room arrived as "" and was rejected "room is empty after
        # sanitization".
        self.assertEqual(MemoryBody().room, "discoveries")
        self.assertEqual(MemoryBody(content="x").room, "discoveries")

    def test_memorybody_coerces_whitespace_wing_and_room(self):
        m = MemoryBody(wing="   ", room="  \t ")
        self.assertEqual(m.wing, "unknown")
        self.assertEqual(m.room, "discoveries")

    def test_memorybody_normalizes_explicit_wing(self):
        self.assertEqual(MemoryBody(wing="Palace_Daemon").wing, "palace_daemon")

    def test_memorybody_accepts_canonical_room(self):
        self.assertEqual(MemoryBody(room="architecture").room, "architecture")

    def test_memorybody_rejects_noncanonical_room_with_400(self):
        # validate_room_or_raise raises fastapi.HTTPException (not a
        # pydantic ValueError), so it propagates with the structured 400
        # detail rather than being wrapped into a 422.
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            MemoryBody(room="bogus_room")
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("valid_rooms", ctx.exception.detail)

    def test_memorybody_content_permissive(self):
        # Pre-#179 did body.get("content", "") with no rejection; the
        # model preserves that — empty content is allowed.
        self.assertEqual(MemoryBody().content, "")


class TestMineBody(unittest.TestCase):

    def test_minebody_defaults(self):
        m = MineBody(dir="/x")
        self.assertEqual(m.wing, "general")
        self.assertEqual(m.mode, "convos")
        self.assertIsNone(m.extract)
        self.assertIsNone(m.limit)

    def test_minebody_normalizes_explicit_wing(self):
        self.assertEqual(MineBody(dir="/x", wing="Palace_Daemon").wing, "palace_daemon")

    def test_minebody_default_wing_already_canonical(self):
        # Why MineBody needs no validate_default: its default 'general' is
        # already canonical, so the skipped-validator path and the
        # run-validator path produce the same value. (Contrast MemoryBody,
        # whose '' default needed transforming — the #187 split.)
        self.assertEqual(MineBody(dir="/x").wing, "general")

    def test_minebody_rejects_bad_mode(self):
        with self.assertRaises(ValidationError):
            MineBody(dir="/x", mode="bogus")

    def test_minebody_rejects_bad_extract(self):
        with self.assertRaises(ValidationError):
            MineBody(dir="/x", extract="junk")

    def test_minebody_requires_dir(self):
        with self.assertRaises(ValidationError):
            MineBody()

    def test_minebody_rejects_empty_dir(self):
        with self.assertRaises(ValidationError):
            MineBody(dir="")


class TestSilentSaveBody(unittest.TestCase):

    def test_silentsave_preserves_empty_wing(self):
        # Distinct from MemoryBody: empty wing stays "" so the handler can
        # emit its themed warning rather than synthesizing 'unknown'.
        self.assertEqual(SilentSaveBody(entry="x").wing, "")
        self.assertEqual(SilentSaveBody(entry="x", wing="   ").wing, "")

    def test_silentsave_normalizes_explicit_wing(self):
        self.assertEqual(SilentSaveBody(entry="x", wing="Foo Bar").wing, "foo_bar")

    def test_silentsave_requires_entry(self):
        with self.assertRaises(ValidationError):
            SilentSaveBody()


class TestBackfillAgeBody(unittest.TestCase):

    def test_backfill_empty_wing_is_none(self):
        # Filter semantics: empty/None means "all wings", not a default
        # write target.
        self.assertIsNone(BackfillAgeBody().wing)
        self.assertIsNone(BackfillAgeBody(wing="").wing)

    def test_backfill_normalizes_explicit_wing(self):
        self.assertEqual(BackfillAgeBody(wing="Palace_Daemon").wing, "palace_daemon")

    def test_backfill_flag_defaults(self):
        b = BackfillAgeBody()
        self.assertFalse(b.skip_palace)
        self.assertFalse(b.skip_entities)
        self.assertFalse(b.restart)


if __name__ == "__main__":
    unittest.main()
