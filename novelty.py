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

``mempalace_list_drawers`` returns each drawer body as ``content_preview``,
truncated to ~200 chars upstream.  Scoring against truncated neighbours
understates similarity for longer drawers (#65), so by default we fetch the
*full* content of each window member via ``mempalace_get_drawer`` and score
against that.  Knobs:

  - ``PALACE_NOVELTY_FULL_CONTENT`` (default ``"true"``) — fetch full neighbour
    content.  When off (or when a fetch fails), fall back to the truncated
    ``content_preview`` for that entry.
  - ``PALACE_NOVELTY_FULL_CONTENT_WINDOW`` (default = ``PALACE_NOVELTY_WINDOW``)
    — cap how many of the window's drawers get a full-content fetch, so the
    extra read cost is bounded independently of the scoring window.  Entries
    beyond the cap use their preview.

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


def _full_content_enabled() -> bool:
    val = os.getenv("PALACE_NOVELTY_FULL_CONTENT", "true").strip().lower()
    return val in ("1", "true", "yes", "on")


def _full_content_window() -> int:
    """How many window drawers to fetch full content for.

    Bounds the extra ``get_drawer`` read cost separately from the scoring
    window. Defaults to the full scoring window; clamps to >= 0.
    """
    raw = os.getenv("PALACE_NOVELTY_FULL_CONTENT_WINDOW")
    if raw is None:
        return _window_size()
    try:
        return max(0, int(raw))
    except (ValueError, TypeError):
        return _window_size()


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


def _unwrap_tool_result(result: Any) -> Any:
    """Unwrap an MCP tools/call envelope to the inner JSON payload."""
    try:
        text = result["result"]["content"][0]["text"]
        return json.loads(text)
    except (KeyError, TypeError, IndexError, json.JSONDecodeError):
        return result


def _parse_window(unwrapped: Any) -> list[dict[str, str]]:
    """Extract the rolling window as ``[{"drawer_id", "preview"}, ...]``.

    ``preview`` is the truncated body from ``mempalace_list_drawers`` (under
    ``content_preview``), with ``text``/``content``/``preview`` as fallbacks.
    Malformed (non-dict) rows are skipped so they can't raise.
    """
    out: list[dict[str, str]] = []
    if not isinstance(unwrapped, dict):
        return out
    drawers = unwrapped.get("drawers") or unwrapped.get("results") or []
    for d in drawers:
        if not isinstance(d, dict):
            continue
        preview = (
            d.get("text")
            or d.get("content")
            or d.get("content_preview")
            or d.get("preview")
            or ""
        )
        if not isinstance(preview, str):
            preview = ""
        out.append({"drawer_id": d.get("drawer_id") or "", "preview": preview})
    return out


async def _fetch_full_content(call_fn, drawer_id: str) -> str | None:
    """Fetch a drawer's full content via ``mempalace_get_drawer``.

    Returns the content string, or ``None`` if the fetch fails or yields no
    usable content — the caller falls back to the truncated preview.
    """
    if not drawer_id:
        return None
    try:
        result = await call_fn({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mempalace_get_drawer",
                "arguments": {"drawer_id": drawer_id},
            },
        })
        unwrapped = _unwrap_tool_result(result)
        if isinstance(unwrapped, dict):
            full = unwrapped.get("content") or unwrapped.get("text")
            if isinstance(full, str) and full:
                return full
    except Exception as e:  # noqa: BLE001 — one bad fetch must not abort scoring
        _log.debug("get_drawer(%s) failed: %s — using preview", drawer_id, e)
    return None


async def compute_novelty_for_write(
    content: str,
    wing: str,
    room: str,
    call_fn,
) -> dict[str, Any]:
    """End-to-end novelty scoring for a ``POST /memory`` write.

    Fetches recent drawers in the same wing/room via ``call_fn`` (the
    daemon's ``_call`` wrapper), resolves each window member's text (full
    content by default, truncated preview as fallback — see #65), computes
    NCD against each, and returns the scoring info dict.

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

        members = _parse_window(_unwrap_tool_result(result))

        # Resolve each window member's text. content_preview is truncated to
        # ~200 chars (#65), which understates NCD similarity for longer
        # drawers, so by default fetch full content per member. Cap the number
        # of full fetches (PALACE_NOVELTY_FULL_CONTENT_WINDOW) to bound the
        # extra read cost; entries beyond the cap, fetch failures, and the
        # knob being off all fall back to the truncated preview.
        full_enabled = _full_content_enabled()
        full_cap = _full_content_window() if full_enabled else 0
        full_used = 0
        texts: list[str] = []
        for i, m in enumerate(members):
            text = m["preview"]
            if i < full_cap:
                full = await _fetch_full_content(call_fn, m["drawer_id"])
                if full is not None:
                    text = full
                    full_used += 1
            if text:
                texts.append(text)

        info = score_novelty(content, texts)
        info["full_content"] = full_enabled
        info["full_content_used"] = full_used
        return info

    except Exception as e:
        _log.warning("Novelty scoring failed: %s — returning default score", e)
        return {
            "enabled": True,
            "novelty_score": 1.0,
            "status": "failed",
            "reason": f"{type(e).__name__}: {e}",
        }
