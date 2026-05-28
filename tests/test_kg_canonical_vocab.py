"""Tests for the closed-vocabulary predicate mapper (kg_canonical_vocab.py, #72).

These are pure / deterministic — they do NOT load the ONNX embedding model.
The embedding path is exercised by injecting a fake gloss-vector matrix and a
fake embedding function so the nearest-canonical math is verified without the
model. The lexical fallback is tested directly.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_kg_canonical_vocab.py -q
"""
import unittest

from mempalace.kg_canonical_vocab import (
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


class TestBatchedMapPredicates(unittest.TestCase):
    """Byte-identity regression for the batched ``map_predicates`` API (#84).

    Asserts that ``[m.map_predicate(r) for r in raws] == m.map_predicates(raws)``
    on hand-curated input sets that cover every branch the per-call API takes:

    * normal raws that route through nearest-canonical embedding scoring
    * code-token drops (``normalize_predicate`` returns ``None``)
    * canonical short-circuit hits (normalized form is exactly a canonical name)
    * below-threshold → ``"other"`` (long-tail bucket)
    * synonym normalization that lands on a canonical (e.g. ``"is" → "is_a"``)

    Covered in both lexical (no embedding model) and injected-embedding modes.
    """

    # ---------- lexical mode ----------------------------------------------
    def test_lexical_byte_identity(self):
        m = CanonicalMapper(threshold=0.4, use_embeddings=False)
        raws = [
            "is_a",                # canonical short-circuit
            "is",                  # synonym → "is_a" short-circuit
            "appendchild",         # code-token drop → (None, 0.0)
            "xyzzy_quux",          # below threshold → "other"
            "works_on",            # canonical short-circuit
            "contains_a_lot_of",   # token overlap, may land "contains" or below
            "",                    # empty → normalize returns None
        ]
        single = [m.map_predicate(r) for r in raws]
        batched = m.map_predicates(raws)
        self.assertEqual(single, batched)

    def test_lexical_empty_input(self):
        m = CanonicalMapper(threshold=0.4, use_embeddings=False)
        self.assertEqual(m.map_predicates([]), [])

    def test_lexical_accepts_iterable(self):
        # Iterable[str] (not just list[str]) — generator must work.
        m = CanonicalMapper(threshold=0.4, use_embeddings=False)
        raws = ["is_a", "appendchild", "works_on"]
        self.assertEqual(m.map_predicates(iter(raws)), [m.map_predicate(r) for r in raws])

    # ---------- embedding mode (injected fake EF) -------------------------
    def _embedding_mapper(self, threshold=0.7):
        # Mirror TestEmbeddingPathInjected._mapper exactly so the embedding
        # path is exercised without loading the ONNX model.
        m = CanonicalMapper(threshold=threshold, use_embeddings=False)
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

    def test_embedding_byte_identity(self):
        m = self._embedding_mapper(threshold=0.7)
        raws = [
            "utilises_library",   # nearest "uses" above threshold
            "is_imported_from",   # nearest "imports" above threshold
            "totally_unrelated",  # below threshold → "other"
            "contains",           # exact-canonical short-circuit (1.0)
            "appendchild",        # code-token → None
            "",                   # empty → None
        ]
        single = [m.map_predicate(r) for r in raws]
        batched = m.map_predicates(raws)
        self.assertEqual(single, batched)

    def test_embedding_all_short_circuit_skips_ef(self):
        # If every input normalizes to a canonical name, no ef() call is
        # needed. The output must still align 1:1 with input.
        m = self._embedding_mapper(threshold=0.7)
        # Wrap the EF so we can detect any call.
        calls: list[int] = []
        real_ef = m._ef

        def counting_ef(inputs):
            calls.append(len(inputs))
            return real_ef(inputs)

        m._ef = counting_ef
        out = m.map_predicates(["uses", "imports", "contains"])
        self.assertEqual(out, [("uses", 1.0), ("imports", 1.0), ("contains", 1.0)])
        self.assertEqual(calls, [])  # short-circuit path took zero ef() calls

    def test_embedding_batches_in_chunks(self):
        # batch_size < pending count → ef() is called multiple times. Verify
        # output alignment + agreement with per-call API across the boundary.
        m = self._embedding_mapper(threshold=0.7)
        calls: list[int] = []
        real_ef = m._ef

        def counting_ef(inputs):
            calls.append(len(inputs))
            return real_ef(inputs)

        m._ef = counting_ef
        raws = ["utilises_library", "is_imported_from", "totally_unrelated"] * 3
        # Re-derive single under the same wrapped ef so the byte-identity
        # check stays meaningful (per-call also routes through _ef).
        single = [m.map_predicate(r) for r in raws]
        calls.clear()
        batched = m.map_predicates(raws, batch_size=2)
        self.assertEqual(single, batched)
        # 9 pending raws (none short-circuit) at batch_size=2 → 5 batches of
        # sizes [2,2,2,2,1]. Order doesn't matter; the total + max do.
        self.assertEqual(sum(calls), 9)
        self.assertLessEqual(max(calls), 2)

    def test_embedding_failed_normalize_keeps_alignment(self):
        # A None-after-normalize in the middle of the input must NOT shift
        # later results. Aligned-tuple output, one per input.
        m = self._embedding_mapper(threshold=0.7)
        raws = [
            "utilises_library",
            "appendchild",         # → None
            "is_imported_from",
            "executemany",         # → None
            "contains",
        ]
        out = m.map_predicates(raws)
        self.assertEqual(len(out), len(raws))
        self.assertEqual(out[1], (None, 0.0))
        self.assertEqual(out[3], (None, 0.0))
        self.assertEqual(out[4], ("contains", 1.0))
        # The two real embedding queries must agree with per-call.
        self.assertEqual(out[0], m.map_predicate("utilises_library"))
        self.assertEqual(out[2], m.map_predicate("is_imported_from"))


if __name__ == "__main__":
    unittest.main()
