"""Does the live-rerank model already exist on disk? (daemon#273)

`tests.yml` states the suite's contract in its own comment: *"The suite is
self-contained: no postgres, no live palace, no network. Tests that would
need a service already skip themselves with a reason, so nothing here fakes
one."* The rerank tests broke that promise — they gated on whether
`flashrank` **imports**, then loaded a model that FlashRank downloads from
huggingface.co on first use.

So a transient `429 Too Many Requests` produced five red tests rather than
five skips (2026-09-11), on a suite that is supposed to need no network at
all. An import probe is cheaper than what the test consumes, which is the
same shape as a guard that cannot see the failure it guards.

The probe here asks the question the test actually depends on — *is the
model already cached?* — and takes BOTH the model name and the cache
directory from the code under test, so it cannot drift from what
`rerank.py` will load:

* model  — ``rerank._RERANK_MODEL`` (honours ``PALACE_RERANK_MODEL``)
* cache  — ``flashrank.Ranker.default_cache_dir``, because ``rerank.py``
  constructs ``Ranker()`` without a ``cache_dir`` and therefore gets that one

⚠️ That default is ``/tmp``, which on a tmpfs host is RAM and on a CI runner
is empty at the start of every job. A live rerank test in CI therefore
downloads the model every run by construction — see the PR for why the fix
is to skip rather than to cache it.
"""
import os
from pathlib import Path


def rerank_model_status(cache_dir=None, model=None):
    """``(available, reason)`` — can a live rerank test run without a network?

    ``cache_dir`` / ``model`` exist for tests of this predicate; production
    callers pass neither, so the answer always describes what ``rerank.py``
    would actually load.
    """
    try:
        import flashrank  # noqa: F401
    except Exception as exc:
        return False, f"flashrank not installed ({type(exc).__name__})"

    if model is None:
        try:
            from rerank import _RERANK_MODEL as model
        except Exception:
            model = os.getenv("PALACE_RERANK_MODEL", "ms-marco-TinyBERT-L-2-v2")

    if cache_dir is None:
        try:
            from flashrank.Ranker import default_cache_dir as cache_dir
        except Exception as exc:
            return False, f"cannot resolve FlashRank's cache dir ({type(exc).__name__})"

    model_dir = Path(cache_dir) / model
    if not model_dir.is_dir():
        return False, (
            f"rerank model {model!r} is not cached in {cache_dir} — running this "
            "would download it, and this suite is network-free (daemon#273)"
        )

    missing = [name for name in _required_files(model) if not (model_dir / name).is_file()]
    if missing:
        # A directory that exists but is incomplete is the interrupted-download
        # case: `_download_model_files` unzips in place, so a killed job or a
        # full disk leaves a partial tree behind. "Any file present" counted
        # that as cached, and the live test then failed on load instead of
        # skipping — the same flake #273 removed, one step narrower (#278).
        return False, (
            f"rerank model {model!r} is cached but incomplete in {cache_dir} "
            f"(missing {', '.join(sorted(missing))}) — an interrupted download; "
            "delete the directory to refetch (daemon#278)"
        )
    return True, ""


def _required_files(model):
    """Every file FlashRank opens unconditionally when loading ``model``.

    Read off its loader rather than guessed: ``Ranker.__init__`` builds an
    ``InferenceSession`` on ``model_file_map[model_name]``, and
    ``_get_tokenizer`` opens ``config.json``, ``tokenizer_config.json``,
    ``special_tokens_map.json`` and ``tokenizer.json``. ``vocab.txt`` is
    deliberately absent: that one is guarded by ``if vocab_file.exists()``,
    so requiring it would refuse a complete model.
    """
    names = [
        "config.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "tokenizer.json",
    ]
    try:
        from flashrank.Config import model_file_map

        weights = model_file_map.get(model)
        if weights:
            names.append(weights)
    except Exception:
        # Unknown layout: fall back to the tokenizer set rather than
        # inventing a weights filename that would refuse a good cache.
        pass
    return names
