"""Gzip-NCD novelty scoring for drawer writes.

Implements Normalized Compression Distance (NCD) as described in the True
Memory paper (arXiv:2605.04897, Section 5.3).  NCD uses gzip as a universal
compressor to measure information-theoretic similarity:

    NCD(a, b) = (gzip(a+b) - min(gzip(a), gzip(b))) / max(gzip(a), gzip(b))

Range is [0, 1+ε] — 0 means identical, 1 means maximally different.  The
paper reports AUC 0.788 for novelty scoring (vs 0.484 for cosine-similarity
inversion on embeddings), making NCD a strong complement to vector search
for detecting redundant content.

This module is called at ``POST /memory`` write time.  It fetches the N most
recent drawers in the same wing/room and computes NCD against each.  The
minimum NCD (i.e. distance to the *most similar* existing drawer) becomes
the ``novelty_score`` — low values mean the new content is redundant.

Gating: ``PALACE_NOVELTY_ENABLED`` env var (default ``"true"``).  Read live
per-request.  Window size: ``PALACE_NOVELTY_WINDOW`` (default ``20``).

This is a **tag, not a gate** — all drawers are stored regardless of score.
The score is informational metadata for downstream retrieval boosting or
curation UIs.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
from typing import Any

_log = logging.getLogger("palace-daemon.novelty")

_DEFAULT_WINDOW = 20
_DEFAULT_LEVEL = 6  # gzip compression level (1-9); 6 is the default


def is_enabled() -> bool:
    val = os.getenv("PALACE_NOVELTY_ENABLED", "true").strip().lower()
    return val in ("1", "true", "yes", "on")


def _window_size() -> int:
    try:
        return max(1, int(os.getenv("PALACE_NOVELTY_WINDOW", str(_DEFAULT_WINDOW))))
    except (ValueError, TypeError):
        return _DEFAULT_WINDOW


def ncd(a: str, b: str) -> float:
    """Compute Normalized Compression Distance between two strings.

    Returns a float in [0, ~1.0+ε].  0 = identical information content,
    1 = maximally different.
    """
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0

    a_bytes = a.encode("utf-8")
    b_bytes = b.encode("utf-8")

    ca = len(gzip.compress(a_bytes, compresslevel=_DEFAULT_LEVEL))
    cb = len(gzip.compress(b_bytes, compresslevel=_DEFAULT_LEVEL))
    cab = len(gzip.compress(a_bytes + b_bytes, compresslevel=_DEFAULT_LEVEL))

    denom = max(ca, cb)
    if denom == 0:
        return 0.0
    return (cab - min(ca, cb)) / denom


def score_novelty(
    content: str,
    existing_texts: list[str],
) -> dict[str, Any]:
    """Score the novelty of ``content`` against a window of existing texts.

    Returns a dict with:
      - ``novelty_score``: min NCD across the window (0=duplicate, 1=novel)
      - ``window_size``: how many texts were compared
      - ``most_similar_index``: index into ``existing_texts`` of closest match
      - ``status``: "ok", "skipped", or "no_window"
    """
    info: dict[str, Any] = {
        "enabled": is_enabled(),
        "novelty_score": 1.0,
        "window_size": 0,
        "most_similar_index": None,
        "status": "skipped",
    }

    if not info["enabled"]:
        info["reason"] = "PALACE_NOVELTY_ENABLED=false"
        return info

    if not content or not content.strip():
        info["reason"] = "empty content"
        return info

    if not existing_texts:
        info["status"] = "no_window"
        info["reason"] = "no existing drawers to compare against"
        return info

    min_ncd = 1.0
    min_idx = 0
    for i, text in enumerate(existing_texts):
        if not text:
            continue
        d = ncd(content, text)
        if d < min_ncd:
            min_ncd = d
            min_idx = i

    info["novelty_score"] = round(min_ncd, 4)
    info["window_size"] = len(existing_texts)
    info["most_similar_index"] = min_idx
    info["status"] = "ok"
    return info


async def compute_novelty_for_write(
    content: str,
    wing: str,
    room: str,
    call_fn,
) -> dict[str, Any]:
    """End-to-end novelty scoring for a ``POST /memory`` write.

    Fetches recent drawers in the same wing/room via ``call_fn`` (the
    daemon's ``_call`` wrapper), computes NCD against each, and returns
    the scoring info dict.

    ``call_fn`` must be the daemon's async ``_call(request_dict)`` function
    so we go through the same semaphore/retry path as everything else.
    """
    if not is_enabled():
        return {"enabled": False, "status": "skipped", "novelty_score": 1.0}

    window = _window_size()
    try:
        result = await call_fn({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mempalace_list_drawers",
                "arguments": {
                    "wing": wing,
                    "room": room,
                    "limit": window,
                    "offset": 0,
                },
            },
        })

        try:
            text = result["result"]["content"][0]["text"]
            unwrapped = json.loads(text)
        except (KeyError, TypeError, json.JSONDecodeError):
            unwrapped = result

        texts: list[str] = []
        if isinstance(unwrapped, dict):
            drawers = unwrapped.get("drawers") or unwrapped.get("results") or []
            for d in drawers:
                # mempalace_list_drawers emits the body as "content_preview".
                # Without it in the fallback chain the window is always empty,
                # so every write scored novelty_score=1.0 — the feature was a
                # silent no-op from #45 until this fix. Previews are truncated
                # to ~200 chars upstream; scoring against full neighbour content
                # is a tracked follow-up.
                text = (
                    d.get("text")
                    or d.get("content")
                    or d.get("content_preview")
                    or d.get("preview")
                    or ""
                )
                if text:
                    texts.append(text)

        return score_novelty(content, texts)

    except Exception as e:
        _log.warning("Novelty scoring failed: %s — returning default score", e)
        return {
            "enabled": True,
            "novelty_score": 1.0,
            "status": "failed",
            "reason": f"{type(e).__name__}: {e}",
        }
