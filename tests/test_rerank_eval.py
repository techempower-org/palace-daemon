"""Tests for the rerank eval harness (scripts/evals/rerank_eval.py).

Cover the pure scoring logic — relevance predicates, ordering
reconstruction, and metric aggregation — without touching the network or
the live palace. The in-process rerank path is exercised by the existing
TestLiveRerank cases in tests/test_rerank.py.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_rerank_eval.py -q
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_EVAL = _ROOT / "scripts" / "evals" / "rerank_eval.py"

_spec = importlib.util.spec_from_file_location("rerank_eval", _EVAL)
assert _spec and _spec.loader
ev = importlib.util.module_from_spec(_spec)
sys.modules["rerank_eval"] = ev
_spec.loader.exec_module(ev)


class TestRelevancePredicate(unittest.TestCase):
    def test_content_any_matches(self):
        rel = {"content_any": ["kill cascade", "ping-pong"]}
        self.assertTrue(ev.is_relevant({"text": "an infinite kill cascade happened"}, rel))
        self.assertFalse(ev.is_relevant({"text": "totally unrelated body"}, rel))

    def test_source_glob_gates(self):
        rel = {"source_glob": "deploy_arch.md", "content_any": ["system unit"]}
        self.assertTrue(ev.is_relevant(
            {"source_file": "project_deploy_arch.md", "text": "this is a system unit"}, rel))
        # right content, wrong file → not relevant
        self.assertFalse(ev.is_relevant(
            {"source_file": "other.md", "text": "this is a system unit"}, rel))

    def test_document_fallback_text(self):
        rel = {"content_any": ["Paris"]}
        self.assertTrue(ev.is_relevant({"document": "Paris is the capital"}, rel))


class TestOrdering(unittest.TestCase):
    def test_baseline_sorts_by_distance_asc(self):
        hits = [
            {"id": "far", "effective_distance": 0.9},
            {"id": "near", "effective_distance": 0.1},
            {"id": "mid", "effective_distance": 0.5},
        ]
        order = [h["id"] for h in ev.baseline_order(hits)]
        self.assertEqual(order, ["near", "mid", "far"])

    def test_baseline_falls_back_to_similarity(self):
        hits = [
            {"id": "a", "similarity": 0.2},
            {"id": "b", "similarity": 0.8},
        ]
        order = [h["id"] for h in ev.baseline_order(hits)]
        self.assertEqual(order, ["b", "a"])  # higher similarity = lower distance

    def test_reranked_sorts_by_rerank_score_desc_unscored_tail(self):
        hits = [
            {"id": "lo", "rerank_score": 0.1},
            {"id": "none"},
            {"id": "hi", "rerank_score": 0.9},
        ]
        order = [h["id"] for h in ev.reranked_order(hits)]
        self.assertEqual(order, ["hi", "lo", "none"])


class TestMetrics(unittest.TestCase):
    def test_first_relevant_rank(self):
        rel = {"content_any": ["target"]}
        ordering = [{"text": "no"}, {"text": "the target here"}, {"text": "no"}]
        self.assertEqual(ev.first_relevant_rank(ordering, rel), 2)
        self.assertIsNone(ev.first_relevant_rank([{"text": "no"}], rel))

    def test_recall_at_k(self):
        rel = {"content_any": ["target"]}
        ordering = [{"text": "no"}] * 6 + [{"text": "target"}]  # relevant at rank 7
        self.assertEqual(ev.recall_at_k(ordering, rel, 5), 0)
        self.assertEqual(ev.recall_at_k(ordering, rel, 10), 1)


class TestEvaluateCandidatesMode(unittest.TestCase):
    """End-to-end aggregate via the in-process candidates path.

    Builds a pool where the relevant hit is buried below noise in the
    baseline order; a working cross-encoder should pull it up. We assert
    the harness produces sane, comparable baseline/reranked metrics.
    """

    def setUp(self):
        try:
            import flashrank  # noqa: F401
        except Exception:
            self.skipTest("flashrank not installed")
        os.environ["PALACE_RERANK_ENABLED"] = "true"

    def test_buried_relevant_doc_metrics(self):
        queries = [{
            "id": "capital",
            "query": "What is the capital of France?",
            "relevant": {"content_any": ["Paris is the capital of France"]},
        }]
        candidates = {"capital": [
            {"drawer_id": "n1", "effective_distance": 0.10, "text": "The cat sat on the mat."},
            {"drawer_id": "n2", "effective_distance": 0.20, "text": "France borders Germany and Spain."},
            {"drawer_id": "rel", "effective_distance": 0.40, "text": "Paris is the capital of France."},
        ]}
        result = ev.evaluate(queries, mode="candidates", candidates=candidates)
        s = result["summary"]
        self.assertEqual(s["n_queries_usable"], 1)
        # Baseline ranks the relevant doc 3rd (worst distance); rerank should
        # move it to the top → reranked MRR strictly better than baseline.
        self.assertGreaterEqual(s["reranked"]["MRR"], s["baseline"]["MRR"])
        pq = result["per_query"][0]
        self.assertEqual(pq["baseline_rank"], 3)
        self.assertEqual(pq["reranked_rank"], 1)


if __name__ == "__main__":
    unittest.main()
