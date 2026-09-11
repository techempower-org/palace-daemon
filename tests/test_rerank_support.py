"""The live-rerank availability probe itself (daemon#273, #278).

The probe decides whether six live tests run or skip, so its own failure
modes are worth pinning: it must not report "cached" for a directory the
loader cannot actually load, and it must not report "not cached" for a
complete one.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests._rerank_support import _required_files, rerank_model_status  # noqa: E402

_MODEL = "ms-marco-TinyBERT-L-2-v2"


def _complete_model(root: Path, model: str = _MODEL) -> Path:
    d = root / model
    d.mkdir(parents=True)
    for name in _required_files(model):
        (d / name).write_text("{}")
    return d


class TestRerankModelStatus(unittest.TestCase):
    def test_a_complete_cache_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            _complete_model(Path(tmp))
            ok, why = rerank_model_status(cache_dir=tmp, model=_MODEL)
        self.assertTrue(ok, why)

    def test_an_absent_model_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok, why = rerank_model_status(cache_dir=tmp, model=_MODEL)
        self.assertFalse(ok)
        self.assertIn("not cached", why)

    def test_a_partial_extract_is_unavailable(self):
        """The #278 case: an interrupted unzip left some of the files.

        "Any file present" counted that as cached, so the live test loaded a
        model that could not load and failed instead of skipping — the #273
        flake again, one step narrower.
        """
        with tempfile.TemporaryDirectory() as tmp:
            d = _complete_model(Path(tmp))
            os.remove(d / "tokenizer.json")
            ok, why = rerank_model_status(cache_dir=tmp, model=_MODEL)
        self.assertFalse(ok, "a model missing a file the loader opens is not usable")
        self.assertIn("incomplete", why)
        self.assertIn("tokenizer.json", why, "say WHICH file is missing")

    def test_an_empty_directory_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / _MODEL).mkdir()
            ok, why = rerank_model_status(cache_dir=tmp, model=_MODEL)
        self.assertFalse(ok)

    def test_optional_vocab_txt_is_not_required(self):
        """`vocab.txt` is loaded behind `if vocab_file.exists()`.

        Requiring it would refuse a complete model — the opposite error, and
        the one that would quietly stop the live tests ever running.
        """
        self.assertNotIn("vocab.txt", _required_files(_MODEL))
        with tempfile.TemporaryDirectory() as tmp:
            _complete_model(Path(tmp))  # no vocab.txt written
            ok, why = rerank_model_status(cache_dir=tmp, model=_MODEL)
        self.assertTrue(ok, why)

    def test_the_required_set_matches_what_flashrank_opens(self):
        """Derived from the loader, not hand-maintained."""
        names = set(_required_files(_MODEL))
        self.assertIn("tokenizer.json", names)
        self.assertIn("config.json", names)
        try:
            from flashrank.Config import model_file_map
        except Exception:
            self.skipTest("flashrank not installed")
        self.assertIn(model_file_map[_MODEL], names, "the weights file must be required")

    def test_the_real_cache_agrees_with_a_copy_of_itself(self):
        """A true positive against the model actually on this machine."""
        ok, _why = rerank_model_status()
        if not ok:
            self.skipTest("no real model cached here to copy")
        from flashrank.Ranker import default_cache_dir

        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(
                Path(default_cache_dir) / _MODEL, Path(tmp) / _MODEL
            )
            self.assertEqual(rerank_model_status(cache_dir=tmp, model=_MODEL)[0], True)
