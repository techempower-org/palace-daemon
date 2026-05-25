"""FlashRank cross-encoder reranking for /search* responses.

Spike for techempower-org/familiar.realm.watch#43. Hybrid (vector + BM25 +
graph) retrieval gets a neural-rerank pass before results leave the daemon
so callers don't have to wire up a cross-encoder themselves.

Model: ``ms-marco-TinyBERT-L-2-v2`` ("nano", ~4 MB ONNX) — chosen because
the daemon's production host (``familiar``) is CPU-only. Typical rerank
latency is ~15–40 ms for n<=20 passages on commodity hardware; the model
load is one-shot at first call and cached for the daemon's lifetime.

Failure mode: if FlashRank import fails, model download fails, or the
ranker raises mid-request, the original ordering is returned unchanged
with a logged warning. The endpoint contract is preserved either way.

Gated by ``PALACE_RERANK_ENABLED`` (default: ``"true"``).
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time
from typing import Any

_log = logging.getLogger("palace-daemon.rerank")

# Singleton ranker — FlashRank loads ONNX weights and a tokenizer; the
# load is too expensive to repeat per-request (~1 s cold) so we cache for
# the daemon's lifetime. Locked because Bun.spawn-style concurrent first
# calls could otherwise double-load.
_ranker: Any = None
_ranker_lock = threading.Lock()
_ranker_load_error: str | None = None
_RERANK_MODEL = os.getenv("PALACE_RERANK_MODEL", "ms-marco-TinyBERT-L-2-v2")
_RERANK_MAX_LENGTH = int(os.getenv("PALACE_RERANK_MAX_LENGTH", "512"))


def is_enabled() -> bool:
    """Return True if reranking should run for this request.

    Reads ``PALACE_RERANK_ENABLED`` live (not at import) so an operator
    can flip the toggle by editing the systemd unit's Environment= line
    and restarting without a code change.
    """
    val = os.getenv("PALACE_RERANK_ENABLED", "true").strip().lower()
    return val in ("1", "true", "yes", "on")


def _get_ranker():
    """Lazy-load and cache the FlashRank ranker.

    Returns ``None`` on permanent failure (model not downloadable, import
    error, etc.) — callers must handle that and fall through.
    """
    global _ranker, _ranker_load_error
    if _ranker is not None:
        return _ranker
    if _ranker_load_error is not None:
        # Cache the failure too — don't retry on every request and pay
        # the import/network cost over and over.
        return None
    with _ranker_lock:
        if _ranker is not None:
            return _ranker
        if _ranker_load_error is not None:
            return None
        try:
            from flashrank import Ranker  # type: ignore[import-not-found]
            t0 = _time.monotonic()
            _ranker = Ranker(
                model_name=_RERANK_MODEL,
                max_length=_RERANK_MAX_LENGTH,
            )
            _log.info(
                "FlashRank loaded model=%s max_length=%d in %.0fms",
                _RERANK_MODEL, _RERANK_MAX_LENGTH,
                (_time.monotonic() - t0) * 1000.0,
            )
            return _ranker
        except Exception as e:
            _ranker_load_error = f"{type(e).__name__}: {e}"
            _log.warning(
                "FlashRank disabled — could not load model %r: %s",
                _RERANK_MODEL, _ranker_load_error,
            )
            return None


def _passage_text(hit: dict) -> str:
    """Pull the rerankable string out of a result hit.

    Mempalace search hits put the body in ``text``; the daemon's
    /search/age-fused graph-only stubs put it in ``document`` (often
    ``None``). Falls back through both so the same reranker works for
    every endpoint.
    """
    for key in ("text", "document"):
        v = hit.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def rerank_hits(query: str, hits: list[dict]) -> tuple[list[dict], dict]:
    """Reorder ``hits`` by FlashRank score against ``query``.

    Returns ``(reranked_hits, info)`` where ``info`` is a small dict with
    timing + status that callers can attach to a response trace block.
    Original hit dicts are preserved — only the order changes and each
    hit gains a ``rerank_score`` float field.

    Hits with empty rerankable text are kept in their original positions
    *behind* the reranked block (graph-only stubs shouldn't outrank a
    proper vector hit just because the cross-encoder couldn't score them).

    On any failure the original list is returned unchanged and ``info``
    carries ``status=skipped|failed`` with the reason.
    """
    info: dict = {
        "enabled": is_enabled(),
        "model": _RERANK_MODEL,
        "n_input": len(hits),
        "n_reranked": 0,
        "latency_ms": 0.0,
        "status": "skipped",
    }
    if not info["enabled"]:
        info["reason"] = "PALACE_RERANK_ENABLED=false"
        return hits, info
    if not hits:
        info["status"] = "noop"
        return hits, info
    if not query or not query.strip():
        info["status"] = "skipped"
        info["reason"] = "empty query"
        return hits, info

    rankable: list[tuple[int, dict]] = []
    unrankable: list[tuple[int, dict]] = []
    for i, h in enumerate(hits):
        if _passage_text(h):
            rankable.append((i, h))
        else:
            unrankable.append((i, h))
    if not rankable:
        info["status"] = "skipped"
        info["reason"] = "no passages have rerankable text"
        return hits, info

    ranker = _get_ranker()
    if ranker is None:
        info["status"] = "failed"
        info["reason"] = _ranker_load_error or "ranker unavailable"
        return hits, info

    try:
        from flashrank import RerankRequest  # type: ignore[import-not-found]
        passages = [
            {"id": idx, "text": _passage_text(h)}
            for idx, h in rankable
        ]
        t0 = _time.monotonic()
        scored = ranker.rerank(RerankRequest(query=query, passages=passages))
        info["latency_ms"] = round((_time.monotonic() - t0) * 1000.0, 2)
        info["n_reranked"] = len(scored)
        info["status"] = "ok"

        # Build reordered list. ``scored`` is sorted by score desc;
        # each entry's ``id`` is the original index into ``hits``.
        # Coerce numpy scalars to plain floats so the response is JSON-safe.
        out: list[dict] = []
        seen: set[int] = set()
        for s in scored:
            idx = s.get("id")
            if not isinstance(idx, int) or idx in seen or idx >= len(hits):
                continue
            seen.add(idx)
            hit = hits[idx]
            try:
                hit["rerank_score"] = float(s.get("score", 0.0))
            except (TypeError, ValueError):
                hit["rerank_score"] = 0.0
            out.append(hit)
        # Append rankable hits the cross-encoder somehow dropped, then
        # unrankable stubs at the tail in original order.
        for idx, h in rankable:
            if idx not in seen:
                out.append(h)
        for _, h in unrankable:
            out.append(h)
        return out, info
    except Exception as e:
        info["status"] = "failed"
        info["reason"] = f"{type(e).__name__}: {e}"
        _log.warning("FlashRank rerank failed: %s — returning original order", info["reason"])
        return hits, info


def rerank_response(query: str, response: Any) -> Any:
    """Apply :func:`rerank_hits` to the ``results`` list of a search response.

    No-op when ``response`` isn't a dict or doesn't carry a ``results``
    list. Attaches a ``rerank`` block to the response with timing/status
    so callers (and tests) can observe what happened.
    """
    if not isinstance(response, dict):
        return response
    results = response.get("results")
    if not isinstance(results, list):
        return response
    reordered, info = rerank_hits(query, results)
    response["results"] = reordered
    response["rerank"] = info
    return response
