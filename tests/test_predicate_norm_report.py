"""Tests for the dry-run report's vocab-loading error handling (#61 r2).

Gemini flagged that ``_load_vocab`` / ``_fetch_live`` would crash with a
traceback on a missing file, bad JSON, or an unexpected JSON shape. These
tests assert each failure mode exits cleanly via ``SystemExit`` with a
message, and that valid list / graph_stats-object inputs still parse.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_predicate_norm_report.py -q
"""
import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPT = os.path.join(_ROOT, "scripts", "predicate_norm_report.py")

_spec = importlib.util.spec_from_file_location("predicate_norm_report", _SCRIPT)
report = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(report)


def _args(**kw) -> argparse.Namespace:
    base = {"vocab_file": None, "live": False, "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _write(content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


class TestLoadVocabErrors(unittest.TestCase):
    def test_missing_file_exits_clean(self):
        with self.assertRaises(SystemExit) as cm:
            report._load_vocab(_args(vocab_file="/no/such/file.json"))
        self.assertIn("cannot read vocab file", str(cm.exception))

    def test_invalid_json_exits_clean(self):
        path = _write("{not valid json")
        try:
            with self.assertRaises(SystemExit) as cm:
                report._load_vocab(_args(vocab_file=path))
            self.assertIn("invalid JSON", str(cm.exception))
        finally:
            os.unlink(path)

    def test_wrong_top_level_type_exits_clean(self):
        path = _write("42")
        try:
            with self.assertRaises(SystemExit) as cm:
                report._load_vocab(_args(vocab_file=path))
            self.assertIn("expected a JSON list or object", str(cm.exception))
        finally:
            os.unlink(path)

    def test_object_without_known_key_exits_clean(self):
        path = _write(json.dumps({"total_edges": 5}))
        try:
            with self.assertRaises(SystemExit) as cm:
                report._load_vocab(_args(vocab_file=path))
            self.assertIn("relationship_types", str(cm.exception))
        finally:
            os.unlink(path)

    def test_key_present_but_not_a_list_exits_clean(self):
        path = _write(json.dumps({"relationship_types": "is_a"}))
        try:
            with self.assertRaises(SystemExit) as cm:
                report._load_vocab(_args(vocab_file=path))
            self.assertIn("must be", str(cm.exception))
        finally:
            os.unlink(path)


class TestLoadVocabValid(unittest.TestCase):
    def test_bare_list(self):
        path = _write(json.dumps(["is", "is_a", "appendchild"]))
        try:
            vocab, src = report._load_vocab(_args(vocab_file=path))
            self.assertEqual(vocab, ["is", "is_a", "appendchild"])
            self.assertTrue(src.startswith("file:"))
        finally:
            os.unlink(path)

    def test_graph_stats_object(self):
        path = _write(json.dumps({"relationship_types": ["is", "uses"]}))
        try:
            vocab, _ = report._load_vocab(_args(vocab_file=path))
            self.assertEqual(vocab, ["is", "uses"])
        finally:
            os.unlink(path)

    def test_predicates_key_alias(self):
        path = _write(json.dumps({"predicates": ["a", "b"]}))
        try:
            vocab, _ = report._load_vocab(_args(vocab_file=path))
            self.assertEqual(vocab, ["a", "b"])
        finally:
            os.unlink(path)

    def test_default_bundled_sample(self):
        vocab, src = report._load_vocab(_args())
        self.assertTrue(len(vocab) > 0)
        self.assertIn("bundled-sample", src)


class TestFetchLiveErrors(unittest.TestCase):
    def test_missing_env_exits_clean(self):
        saved = {k: os.environ.pop(k, None)
                 for k in ("PALACE_API_KEY", "PALACE_DAEMON_URL")}
        try:
            with self.assertRaises(SystemExit) as cm:
                report._fetch_live()
            self.assertIn("PALACE_API_KEY", str(cm.exception))
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def _with_env(self, fn):
        """Run fn with dummy live env vars set, restoring afterward."""
        saved = {k: os.environ.get(k)
                 for k in ("PALACE_API_KEY", "PALACE_DAEMON_URL")}
        os.environ["PALACE_API_KEY"] = "dummy"
        os.environ["PALACE_DAEMON_URL"] = "http://example.invalid:9"
        try:
            return fn()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_timeout_exits_clean_no_traceback(self):
        # A bare read timeout surfaces as TimeoutError (an OSError), which an
        # HTTPError/URLError-only handler would let escape as a traceback.
        def run():
            with mock.patch(
                "urllib.request.urlopen", side_effect=TimeoutError("timed out")
            ):
                with self.assertRaises(SystemExit) as cm:
                    report._fetch_live()
            self.assertIn("timeout", str(cm.exception).lower())
        self._with_env(run)

    def test_urlerror_exits_clean(self):
        import urllib.error

        def run():
            with mock.patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("refused"),
            ):
                with self.assertRaises(SystemExit) as cm:
                    report._fetch_live()
            self.assertIn("fetch failed", str(cm.exception))
        self._with_env(run)

    def test_httperror_mentions_postgres_hint(self):
        import urllib.error

        def run():
            err = urllib.error.HTTPError(
                "http://x", 503, "Service Unavailable", {}, None
            )
            with mock.patch("urllib.request.urlopen", side_effect=err):
                with self.assertRaises(SystemExit) as cm:
                    report._fetch_live()
            self.assertIn("postgres", str(cm.exception).lower())
        self._with_env(run)

    def test_parses_cypher_rows(self):
        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return json.dumps({
                    "rows": [{"rt": "is_a"}, {"rt": "uses"}, {"rt": None}]
                }).encode()

        def run():
            with mock.patch("urllib.request.urlopen", return_value=_Resp()):
                out = report._fetch_live()
            # None rt is skipped
            self.assertEqual(out, ["is_a", "uses"])
        self._with_env(run)


class TestBuildReport(unittest.TestCase):
    def test_known_shape(self):
        rep = report.build_report(
            ["appendchild", "is", "is_a", "don't_adapt", "works_on"]
        )
        # appendchild dropped; is+is_a collapse to is_a; don't_adapt → not_adapt
        self.assertEqual(rep["original_cardinality"], 5)
        self.assertEqual(rep["dropped_count"], 1)
        self.assertIn("appendchild", rep["dropped"])
        self.assertIn("is_a", rep["collapses"])
        self.assertIn(("don't_adapt", "not_adapt"), rep["negation_rewrites"])


if __name__ == "__main__":
    unittest.main()
