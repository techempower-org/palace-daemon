"""Tests for the per-request rerank override (palace-daemon#189).

rerank_hits / rerank_response gained an ``enabled`` param: None defers to
PALACE_RERANK_ENABLED (unchanged), True/False forces the cross-encoder
on/off for that call only — so ablation benches (SME #75 leg A vs B) can
A/B rerank within one pass without mutating daemon-global env state that
concurrent callers share.

These assert the GATING decision (enabled flag + reason + source) without
needing a real FlashRank model: when the gate is off the function returns
early with status=skipped, which is observable regardless of model
availability.

Run with::

    python -m unittest tests.test_rerank_per_request -v
"""
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import rerank  # noqa: E402


class TestRerankPerRequestOverride(unittest.TestCase):

    def test_explicit_false_disables_regardless_of_env(self):
        # Env says enabled, but the per-request False wins for this call.
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "true"}, clear=False):
            out, info = rerank.rerank_hits("q", [{"text": "x"}], enabled=False)
        self.assertFalse(info["enabled"])
        self.assertEqual(info["enabled_source"], "per-request")
        self.assertIn("per-request", info["reason"])

    def test_explicit_true_overrides_env_false(self):
        # Env disables globally, but the per-request True forces it on, so
        # the function proceeds past the gate (status != the env-disabled
        # early return). It may still skip/fail later if no model is
        # installed — what we assert is the gate decision + source.
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "false"}, clear=False):
            out, info = rerank.rerank_hits("q", [{"text": "x"}], enabled=True)
        self.assertTrue(info["enabled"])
        self.assertEqual(info["enabled_source"], "per-request")
        # The env-disabled reason must NOT be present — we forced it on.
        self.assertNotEqual(info.get("reason"), "PALACE_RERANK_ENABLED=false")

    def test_none_defers_to_env_enabled(self):
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "false"}, clear=False):
            out, info = rerank.rerank_hits("q", [{"text": "x"}], enabled=None)
        self.assertFalse(info["enabled"])
        self.assertEqual(info["enabled_source"], "env")
        self.assertEqual(info["reason"], "PALACE_RERANK_ENABLED=false")

    def test_default_arg_is_none_backcompat(self):
        # Existing callers that don't pass enabled get env behavior.
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "false"}, clear=False):
            out, info = rerank.rerank_hits("q", [{"text": "x"}])
        self.assertEqual(info["enabled_source"], "env")
        self.assertFalse(info["enabled"])

    def test_rerank_response_threads_enabled_into_block(self):
        # The response's rerank block must reflect the per-request decision
        # so callers can confirm which path ran (#189 requirement).
        resp = {"results": [{"text": "x"}]}
        out = rerank.rerank_response("q", resp, enabled=False)
        self.assertFalse(out["rerank"]["enabled"])
        self.assertEqual(out["rerank"]["enabled_source"], "per-request")

    def test_rerank_response_none_is_backcompat(self):
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "false"}, clear=False):
            out = rerank.rerank_response("q", {"results": [{"text": "x"}]})
        self.assertEqual(out["rerank"]["enabled_source"], "env")


if __name__ == "__main__":
    unittest.main()
