"""Tests for the guarded canonical write pass (kg_canonical_writepass.py, #72a).

The write pass is the seam mempalace's triple worker would call before
persisting a RELATION edge. These tests assert:
  * default OFF → byte-for-byte pass-through (no prod behavior change on merge)
  * ON → canonical mapping with the raw retained for reversibility
  * code tokens flagged dropped; the flag is read live

Embedding model is NOT loaded — we inject a fake CanonicalMapper so the
mapping decision is deterministic.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_kg_canonical_writepass.py -q
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import kg_canonical_writepass as wp  # noqa: E402


class _FakeMapper:
    """Deterministic stand-in for CanonicalMapper."""

    def __init__(self, table):
        # table: raw -> (canonical_or_other_or_None, score)
        self.table = table

    def map_predicate(self, raw):
        return self.table.get(raw, ("other", 0.0))


class _FlagGuard(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(wp._FLAG)
        wp.reset_mapper_cache()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(wp._FLAG, None)
        else:
            os.environ[wp._FLAG] = self._saved
        wp.reset_mapper_cache()

    def _enable(self):
        os.environ[wp._FLAG] = "1"

    def _disable(self):
        os.environ.pop(wp._FLAG, None)


class TestDisabledPassthrough(_FlagGuard):
    def test_default_is_disabled(self):
        self._disable()
        self.assertFalse(wp.mapping_enabled())

    def test_passthrough_keeps_raw_verbatim(self):
        self._disable()
        r = wp.map_for_write("some_freeform_predicate")
        self.assertEqual(r.relation_type, "some_freeform_predicate")
        self.assertIsNone(r.raw_relation_type)
        self.assertFalse(r.dropped)
        self.assertFalse(r.mapped)

    def test_passthrough_never_drops_code_token(self):
        # disabled mode must not change behavior even for code tokens
        self._disable()
        r = wp.map_for_write("appendchild")
        self.assertEqual(r.relation_type, "appendchild")
        self.assertFalse(r.dropped)


class TestEnabledMapping(_FlagGuard):
    def _patch_mapper(self, table):
        wp._mapper = _FakeMapper(table)

    def test_flag_truthy_values(self):
        for v in ("1", "true", "on", "YES", "True"):
            os.environ[wp._FLAG] = v
            self.assertTrue(wp.mapping_enabled(), v)
        for v in ("0", "false", "off", "", "no"):
            os.environ[wp._FLAG] = v
            self.assertFalse(wp.mapping_enabled(), v)

    def test_canonical_mapping_retains_raw(self):
        self._enable()
        self._patch_mapper({"is": ("is_a", 1.0)})
        r = wp.map_for_write("is")
        self.assertEqual(r.relation_type, "is_a")
        self.assertEqual(r.raw_relation_type, "is")  # reversibility
        self.assertTrue(r.mapped)
        self.assertFalse(r.dropped)

    def test_unchanged_canonical_no_raw_retained(self):
        self._enable()
        self._patch_mapper({"is_a": ("is_a", 1.0)})
        r = wp.map_for_write("is_a")
        self.assertEqual(r.relation_type, "is_a")
        self.assertIsNone(r.raw_relation_type)  # nothing changed
        self.assertFalse(r.mapped)

    def test_code_token_flagged_dropped(self):
        self._enable()
        self._patch_mapper({"appendchild": (None, 0.0)})
        r = wp.map_for_write("appendchild")
        self.assertTrue(r.dropped)
        self.assertIsNone(r.relation_type)
        self.assertEqual(r.raw_relation_type, "appendchild")

    def test_other_bucket_retains_raw(self):
        self._enable()
        self._patch_mapper({"weird_one_off": ("other", 0.1)})
        r = wp.map_for_write("weird_one_off")
        self.assertEqual(r.relation_type, "other")
        self.assertEqual(r.raw_relation_type, "weird_one_off")
        self.assertTrue(r.mapped)


if __name__ == "__main__":
    unittest.main()
