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


class TestRetryableHttp(unittest.TestCase):
    """5xx is transient (retry); 4xx is permanent (propagate). Gemini #64."""

    def _http_error(self, code: int):
        import requests
        resp = requests.Response()
        resp.status_code = code
        return requests.HTTPError(response=resp)

    def test_5xx_is_retryable(self):
        for code in (500, 502, 503, 504):
            self.assertTrue(ev._is_retryable_http(self._http_error(code)), code)

    def test_4xx_not_retryable(self):
        for code in (400, 401, 403, 404):
            self.assertFalse(ev._is_retryable_http(self._http_error(code)), code)

    def test_missing_response_not_retryable(self):
        import requests
        self.assertFalse(ev._is_retryable_http(requests.HTTPError("no response")))


class TestFetchLiveRetry(unittest.TestCase):
    """fetch_live retries transient 5xx then succeeds; 4xx fails fast."""

    def setUp(self):
        import requests
        self.requests = requests

    def _resp(self, code: int, json_body=None):
        r = self.requests.Response()
        r.status_code = code
        if json_body is not None:
            import json as _json
            r._content = _json.dumps(json_body).encode()
        return r

    def test_retries_on_503_then_succeeds(self):
        from unittest.mock import patch
        calls = [self._resp(503), self._resp(503), self._resp(200, {"results": []})]
        with patch.object(ev.requests, "get", side_effect=calls) as mget, \
                patch.object(ev.time, "sleep"):
            out = ev.fetch_live("http://x", "k", "q", 5, 1.0, retries=3, backoff=0.0)
        self.assertEqual(out, {"results": []})
        self.assertEqual(mget.call_count, 3)

    def test_4xx_propagates_without_retry(self):
        from unittest.mock import patch
        with patch.object(ev.requests, "get", side_effect=[self._resp(401)]) as mget, \
                patch.object(ev.time, "sleep"):
            with self.assertRaises(self.requests.HTTPError):
                ev.fetch_live("http://x", "k", "q", 5, 1.0, retries=3, backoff=0.0)
        # 4xx must not consume retries — exactly one attempt.
        self.assertEqual(mget.call_count, 1)


if __name__ == "__main__":
    unittest.main()
