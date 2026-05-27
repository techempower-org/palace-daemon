"""Tests for the closed-vocabulary predicate mapper (kg_canonical_vocab.py, #72).

These are pure / deterministic — they do NOT load the ONNX embedding model.
The embedding path is exercised by injecting a fake gloss-vector matrix and a
fake embedding function so the nearest-canonical math is verified without the
model. The lexical fallback is tested directly.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_kg_canonical_vocab.py -q
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kg_canonical_vocab import (  # noqa: E402
    CANONICAL_RELATIONS,
    Canonical,
    CanonicalMapper,
    lexical_similarity,
)


class TestCanonicalSet(unittest.TestCase):
    def test_names_unique(self):
        names = [c.name for c in CANONICAL_RELATIONS]
        self.assertEqual(len(names), len(set(names)))

    def test_all_have_glosses(self):
        for c in CANONICAL_RELATIONS:
            self.assertIsInstance(c, Canonical)
            self.assertTrue(c.gloss.strip())

    def test_reasonable_size(self):
        # the spike targets "dozens" — guard against accidental bloat
        self.assertGreaterEqual(len(CANONICAL_RELATIONS), 20)
        self.assertLessEqual(len(CANONICAL_RELATIONS), 80)


class TestLexicalSimilarity(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(lexical_similarity("works_on", "works_on"), 1.0)

    def test_disjoint(self):
        self.assertEqual(lexical_similarity("foo", "bar"), 0.0)

    def test_partial_overlap(self):
        # {is, located, at} vs {located, in} → 1 shared / 4 union
        self.assertAlmostEqual(
            lexical_similarity("is_located_at", "located_in"), 1 / 4
        )

    def test_empty(self):
        self.assertEqual(lexical_similarity("", "x"), 0.0)


class TestLexicalMapper(unittest.TestCase):
    def setUp(self):
        self.m = CanonicalMapper(threshold=0.4, use_embeddings=False)

    def test_mode_is_lexical(self):
        self.assertEqual(self.m.mode, "lexical")

    def test_exact_canonical_hit(self):
        canon, score = self.m.map_predicate("is_a")
        self.assertEqual(canon, "is_a")
        self.assertEqual(score, 1.0)

    def test_synonym_normalizes_then_exact_hits(self):
        # normalize_predicate folds "is" → "is_a", which is an exact canonical
        canon, score = self.m.map_predicate("is")
        self.assertEqual(canon, "is_a")
        self.assertEqual(score, 1.0)

    def test_code_token_dropped(self):
        canon, score = self.m.map_predicate("appendchild")
        self.assertIsNone(canon)

    def test_low_overlap_goes_to_other(self):
        canon, _ = self.m.map_predicate("xyzzy_quux")
        self.assertEqual(canon, "other")


class _FakeEF:
    """Deterministic fake embedding function for the embedding-path test.

    Maps a fixed set of strings to orthonormal-ish 3-vectors so cosine
    nearest-neighbour is predictable.
    """

    _VECS = {
        # canonical glosses we care about (only the leading token matters here)
        "uses": [1.0, 0.0, 0.0],
        "imports": [0.0, 1.0, 0.0],
        "contains": [0.0, 0.0, 1.0],
        # query terms
        "utilises_library": [0.9, 0.1, 0.0],   # → uses
        "is_imported_from": [0.05, 0.99, 0.0],  # → imports
        "totally_unrelated": [0.4, 0.4, 0.4],   # ~equidistant, below thresh
    }

    def __call__(self, inputs):
        out = []
        for s in inputs:
            if s in self._VECS:
                out.append(self._VECS[s])
            else:
                # canonical glosses we don't pin: park them far away on a 4th axis
                out.append([0.0, 0.0, 0.0])
        return out


class TestEmbeddingPathInjected(unittest.TestCase):
    """Verify nearest-canonical math via an injected fake EF + gloss matrix."""

    def _mapper(self, threshold=0.7):
        m = CanonicalMapper(threshold=threshold, use_embeddings=False)
        # Force embedding mode with a controlled 3-canonical universe.
        m.mode = "embedding"
        m._ef = _FakeEF()
        m.canonicals = (
            Canonical("uses", "uses"),
            Canonical("imports", "imports"),
            Canonical("contains", "contains"),
        )
        m._names = [c.name for c in m.canonicals]
        m._glosses = [c.gloss for c in m.canonicals]
        m._gloss_vecs = [list(v) for v in m._ef(m._glosses)]
        return m

    def test_nearest_canonical_above_threshold(self):
        m = self._mapper(threshold=0.7)
        canon, score = m.map_predicate("utilises_library")
        self.assertEqual(canon, "uses")
        self.assertGreater(score, 0.7)

    def test_second_canonical(self):
        m = self._mapper(threshold=0.7)
        canon, _ = m.map_predicate("is_imported_from")
        self.assertEqual(canon, "imports")

    def test_below_threshold_is_other(self):
        m = self._mapper(threshold=0.95)
        canon, score = m.map_predicate("totally_unrelated")
        self.assertEqual(canon, "other")
        self.assertLess(score, 0.95)

    def test_exact_canonical_short_circuits(self):
        m = self._mapper(threshold=0.7)
        canon, score = m.map_predicate("contains")
        self.assertEqual(canon, "contains")
        self.assertEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
