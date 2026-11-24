"""Search route handlers — extracted from main.py per palace-daemon#101 (#3).

The five MCP-backed search endpoints (/search, /search/hybrid,
/search/keyword, /search/age-fused, /context) live here on an
``APIRouter`` that main.py mounts via ``app.include_router``.

Why the handlers reference ``main.X`` rather than importing the symbols
directly: the daemon's core dispatch (``_call``), response plumbing
(``_unwrap``, ``_search_args``), the mempalace instance (``_mp``), the
reranker (``_rerank``), the auth gate (``_check_auth``) and the AGE
extractor loader (``_load_age_extractor``) all live in main.py, and the
test suite patches them as ``main._call`` / ``main._load_age_extractor``
/ etc. (see tests/test_search_age_fused_hydration.py,
tests/test_search_hybrid_fusion_mode.py, which call
``main.search_age_fused`` / ``main.search_hybrid`` directly with those
symbols mocked). Looking them up through ``main`` at request time keeps
those patches effective and the tests unmodified — the same lazy-
``import main`` pattern used by daemon_tools.py and fast_intercept.py
(#101 slices 5/6). main.py re-exports the handler functions so the
direct-call tests resolve ``main.search_age_fused`` etc.

``rooms`` is imported directly (not via main) because the FastAPI
``Depends(...)`` defaults are evaluated at function-definition time, when
main may not be fully initialized; rooms.py does not import main, so this
is safe and non-circular.
"""
from __future__ import annotations

import asyncio
import logging
import time

_log = logging.getLogger("palace-daemon.search_routes")  # #292: never the root logger
import os

from fastapi import APIRouter, Depends, Header, HTTPException

import rooms
from search_models import (  # noqa: F401
    SearchAgeFusedBody,
    SearchHybridBody,
    SearchKeywordBody,
)

router = APIRouter()

# ── judged classes (mempalace docs/designs/judged-drawer-classes.md) ────────
#
# A drawer the judged-class run flagged ``judged_class`` chatter or stale stays
# in the palace and is left out of default search; ``all_classes=true`` returns
# every drawer, so a flagged drawer is never unreachable. Routes that go through
# the ``mempalace_search`` tool pass the flag on (mempalace filters); routes with
# their own SQL filter here. Lookups fail open: a drawer is hidden only when its
# row is read and proven flagged.

JUDGED_SEARCH_EXCLUDED = ("chatter", "stale")


def _judged_hidden_ids(ids, dsn):
    ids = sorted({i for i in ids if isinstance(i, str) and i})
    if not ids or not dsn:
        return set()
    try:
        import psycopg2

        with psycopg2.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '2s'")
                cur.execute(
                    "SELECT id FROM mempalace_drawers WHERE id = ANY(%s) "
                    "AND metadata->>'judged_class' = ANY(%s)",
                    (ids, list(JUDGED_SEARCH_EXCLUDED)),
                )
                return {r[0] for r in cur.fetchall()}
    except Exception as e:  # noqa: BLE001 -- fail open: never hide what we cannot prove is flagged
        _log.warning("judged-class lookup failed; not filtering: %s", e)
        return set()


def _drop_judged(response, dsn, limit=None):
    """Remove judged chatter/stale hits from a search response; count them in ``judged_excluded``."""
    if not isinstance(response, dict) or not response.get("results"):
        return response
    hits = response["results"]
    hidden = _judged_hidden_ids([h.get("drawer_id") or h.get("id") for h in hits], dsn)
    kept = [h for h in hits if (h.get("drawer_id") or h.get("id")) not in hidden]
    response["results"] = kept[:limit] if limit else kept
    _note_judged(response, len(hits) - len(kept))
    return response


def _note_judged(response, n):
    """Count left-out hits in ``judged_excluded`` (the candidates this route examined) and say how to get them."""
    if not n:
        return
    response["judged_excluded"] = response.get("judged_excluded", 0) + n
    warnings = response.setdefault("warnings", [])
    if isinstance(warnings, list):
        warnings.append(
            f"{n} hit(s) judged chatter/stale were left out; add all_classes=true to include them"
        )


# ── kind= filter (palace-daemon#194) ─────────────────────────────────────────
#
# Stop-hook auto-save checkpoint diary entries (``CHECKPOINT:…`` bodies,
# ``topic=checkpoint``) are session-summary noise that drowns out real content
# under vector similarity. ``kind="content"`` (the default) excludes them;
# ``kind="all"`` returns the unfiltered superset; ``kind="checkpoint"`` returns
# only the checkpoints (recovery/audit lookups).
#
# History: commit 4a318d3 (2026-04-27) RETIRED this filter when the Phase A–E
# checkpoint-collection split moved every checkpoint out of
# ``mempalace_drawers`` into ``mempalace_session_recovery`` — the filter was
# filtering nothing. The 2026-05-29 DB rebackfill re-merged checkpoint drawers
# back into the main collection, restoring the noise the filter was built for,
# so the filter is back.
#
# Why match on TWO signals (topic OR text prefix) rather than the canonical
# ``topic`` metadata alone: #194 reports the rebackfilled checkpoint hits come
# back with empty/dropped metadata (no ``topic``). The ``CHECKPOINT:`` body
# prefix (mempalace.hooks_cli line ~812) is the resilient fallback that
# survives the metadata loss. Either signal classifies a hit as a checkpoint.
_VALID_KINDS = ("content", "checkpoint", "all")
# Mirror main.py's CHECKPOINT_TOPIC + CHECKPOINT_TOPIC_SYNONYMS.
_CHECKPOINT_TOPICS = frozenset({"checkpoint", "auto-save"})
# Bodies of Stop-hook checkpoint diary entries start with this literal
# (see mempalace.hooks_cli: ``f"CHECKPOINT:{date}|session:{id}…"``).
_CHECKPOINT_TEXT_PREFIX = "CHECKPOINT:"


def _hit_is_checkpoint(hit: dict) -> bool:
    """True if a search hit is a Stop-hook auto-save checkpoint.

    Two independent signals, OR'd so the classification survives the
    metadata loss described in palace-daemon#194:

      1. canonical ``topic`` metadata in {checkpoint, auto-save}
         (the original read-side filter key), or
      2. the drawer body begins with ``CHECKPOINT:`` (resilient fallback
         when the rebackfill dropped the topic metadata).

    ``topic`` may live at the hit top level (the /search shape) or under a
    nested ``metadata`` dict; check both.
    """
    if not isinstance(hit, dict):
        return False
    meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    topic = (hit.get("topic") or meta.get("topic") or "")
    if isinstance(topic, str) and topic.strip().lower() in _CHECKPOINT_TOPICS:
        return True
    text = hit.get("text") or hit.get("document") or ""
    if isinstance(text, str) and text.lstrip().startswith(_CHECKPOINT_TEXT_PREFIX):
        return True
    return False


def _apply_kind_filter(response, kind: str):
    """Filter a search response's ``results`` by ``kind`` (palace-daemon#194).

    * ``"all"``        — return unchanged (the superset).
    * ``"content"``    — drop checkpoint hits.
    * ``"checkpoint"`` — keep only checkpoint hits.

    No-op when ``response`` isn't a dict or carries no list ``results``.
    Echoes the applied ``kind`` into the ``filters`` block (the regression
    in #194 was that ``kind`` never appeared as an applied filter). Returns
    the same object it was given (mutated in place) for caller convenience.

    NOTE — this is a post-fetch filter over the ``limit`` rows mempalace
    already returned, not a push-down into mempalace's over-fetched
    candidate pool (mempalace's read-side kind= machinery was retired in
    7ba28dc and no longer exists to push down to). Consequence: a
    ``kind="content"`` response can come back with fewer than ``limit``
    rows when checkpoints rank inside the top ``limit`` — the excluded
    checkpoints are dropped, not back-filled from deeper in the pool. The
    correctness invariant (content ⊊ all, no checkpoints in content) holds;
    the count is best-effort. Over-fetch-then-filter would need either a
    larger ``limit`` from the caller or restoring the push-down filter.
    """
    if kind == "all" or not isinstance(response, dict):
        return response
    results = response.get("results")
    if not isinstance(results, list):
        return response
    if kind == "checkpoint":
        kept = [h for h in results if _hit_is_checkpoint(h)]
    else:  # "content"
        kept = [h for h in results if not _hit_is_checkpoint(h)]
    response["results"] = kept
    filters = response.get("filters")
    if isinstance(filters, dict):
        filters["kind"] = kind
    return response


@router.get("/search")
async def search(
    q: str,
    limit: int = 15,
    # palace-daemon#194: kind= checkpoint filter, re-added after the
    # 2026-05-29 DB rebackfill re-merged checkpoint drawers into the main
    # collection (4a318d3 had retired it when the collection split emptied
    # them out). Default "content" excludes Stop-hook auto-save checkpoints.
    kind: str = "content",
    # palace-daemon#179: wing/room canonicalization is enforced via
    # FastAPI dependency injection rather than handler-body calls, so
    # new query-param endpoints automatically inherit the contract.
    wing: str | None = Depends(rooms.wing_filter_dep),
    room: str | None = Depends(rooms.room_validator_dep),
    # palace-daemon#189: per-request rerank override. ?rerank=false skips
    # the cross-encoder for this request only; absent → PALACE_RERANK_ENABLED.
    rerank: bool | None = None,
    # judged classes: include drawers flagged chatter/stale (left out by default).
    all_classes: bool = False,
    x_api_key: str | None = Header(default=None),
):
    """Semantic search over the main `mempalace_drawers` collection.

    `kind` controls Stop-hook auto-save checkpoint visibility (#194):
    `"content"` (default) excludes them, `"all"` returns the superset,
    `"checkpoint"` returns only checkpoints. Recovery/audit reads can also
    use the `mempalace_session_recovery_read` MCP tool directly.

    `wing` and `room` are optional exact-match filters forwarded to
    ``mempalace_search``. Pre-2026-05-16 this endpoint silently dropped
    those params (FastAPI strips unknown query args, and the signature
    didn't accept them) — callers asking for scoped results got
    palace-wide results back instead.
    """
    import main
    main._check_auth(x_api_key)
    if kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {_VALID_KINDS}; got {kind!r}",
        )
    # `wing` and `room` already canonicalized by the FastAPI dependencies
    # in the signature above (palace-daemon#179). Handler body just uses
    # them as filters.
    args = main._search_args(q, limit)
    if wing:
        args["wing"] = wing
    if room:
        args["room"] = room
    if all_classes:
        args["all_classes"] = True
    result = await main._call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": args},
    })
    # Apply the checkpoint filter before rerank so the cross-encoder ranks
    # only the kind-eligible pool. mempalace no longer carries a read-side
    # kind= filter (companion 7ba28dc retired it), so it's enforced here.
    filtered = _apply_kind_filter(main._unwrap(result), kind)
    return main._rerank.rerank_response(q, filtered, enabled=rerank)


# ── Postgres-native BM25 search ──────────────────────────────────────
#
# Phase 2 of the hybrid-search-taxonomy initiative (familiar.realm.watch
# spec §3.6). The daemon issues postgres tsvector queries directly
# rather than routing through the mempalace_search MCP tool — the MCP
# path is vector-only and lives in chromadb-shaped code.
#
# 503 when backend is chroma. The chroma path has its own BM25
# fallback via _bm25_only_via_sqlite, surfaced through
# candidate_strategy="union" on the existing /search endpoint.


@router.post("/search/hybrid")
async def search_hybrid(
    body: SearchHybridBody,
    x_api_key: str | None = Header(default=None),
):
    """Hybrid search: vector + BM25 + graph in a single ranked result set.

    Phase 4 of the hybrid-search-taxonomy initiative. Routes through
    mempalace's ``search_memories`` with ``candidate_strategy="hybrid"``,
    which:
      1. Runs vector candidate selection (existing)
      2. Unions BM25 candidates from postgres tsvector (Phase 2)
      3. Adds graph-expanded drawers — vector-seeded entity expansion
         AND query-NER entity matching (Phase 3)
      4. Reranks the combined pool with the hybrid scorer

    Body::

        {
          "query":         "pgvector advisory lock race",
          "wing":          "memorypalace",      // optional, exact-match filter
          "room":          "problems",          // optional, canonical only
          "limit":         10,
          "include_trace": false,               // optional, attaches per-source
                                                // counts if true (no latencies)
          "budget_ms":     2000                 // optional (#284): past it, or when
                                                // the daemon is busy, answer from
                                                // BM25 /search/fast, marked
                                                // "degraded": "bm25-fast"
        }

    Every response carries ``timings_ms``: ``hybrid`` (the whole tool call),
    split into ``queue`` (waiting for a read slot and a tool thread) and
    ``compute`` (mempalace's embed, vector, BM25, graph and fusion, as one
    figure), then ``rerank`` and ``total``; ``fast`` replaces ``rerank`` when
    degraded. The same numbers go to the log, so a slow search under load says
    whether it waited or computed. The per-stage split inside mempalace is
    still open on #284 (``include_trace`` carries counts, not latencies).

    Returns the same hit shape as /search; each hit has a `matched_via`
    field naming the source (vector, bm25_postgres, graph_seeded,
    graph_ner) which the trace flag surfaces.

    Requires postgres backend.
    """
    import main
    main._check_auth(x_api_key)
    if main._mp._config.backend != "postgres":
        raise HTTPException(
            status_code=503,
            detail="/search/hybrid requires MEMPALACE_BACKEND=postgres; daemon is on chroma.",
        )
    # palace-daemon#179: body fields (query, wing, room, limit,
    # include_trace, fusion_mode, candidate_strategy, search_endpoint)
    # already validated + canonicalized by SearchHybridBody at parse time.
    args = {
        "query": body.query,
        "limit": body.limit,
        "candidate_strategy": body.candidate_strategy or "hybrid",
    }
    if body.wing:
        args["wing"] = body.wing
    if body.room:
        args["room"] = body.room
    args["include_trace"] = body.include_trace
    if body.fusion_mode is not None:
        args["fusion_mode"] = body.fusion_mode
    if body.all_classes:
        args["all_classes"] = True

    # #284: time the daemon-side stages. _call stamps ``box`` (``entered``
    # before the read semaphore, ``submitted`` after it, ``started`` from the
    # worker thread when mempalace begins), so the log line splits ``queue``
    # (semaphore + executor wait) from ``compute`` (mempalace's embed, vector,
    # BM25, graph and fusion as one figure; the per-stage split inside
    # mempalace is still open on #284, and include_trace carries per-source
    # counts, not latencies). ``rerank`` is the cross-encoder.
    t0 = time.monotonic()
    box: dict = {}
    call = main._call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": args},
    }, box=box)
    degraded_reason = None
    if body.budget_ms is None:
        result = await call
    else:
        # Shielded: past the budget only THIS route stops waiting. The _call
        # task runs on and keeps holding the read semaphore and its in-flight
        # slot until the worker thread finishes, so callers without a budget
        # keep queueing behind it exactly as before. Cancelling _call instead
        # would free the semaphore while the thread ran on, and a wave of
        # budgeted hook calls could then run PALACE_MCP_TOOL_MAX_INFLIGHT
        # hybrid threads at once instead of PALACE_MAX_READ_CONCURRENCY,
        # adding CPU contention in exactly the mine drain this is for.
        task = asyncio.ensure_future(call)
        task.add_done_callback(_retrieve_abandoned)
        try:
            result = await asyncio.wait_for(asyncio.shield(task), body.budget_ms / 1000)
        except asyncio.TimeoutError:
            result = None
            degraded_reason = f"hybrid exceeded budget_ms={body.budget_ms}"
            if "submitted" not in box:
                # Still queued for a read slot: it holds nothing and no thread
                # runs, so cancelling only spares the daemon a full hybrid
                # search nobody will read. _call stamps ``submitted`` with no
                # await between the semaphore and the executor, so a task
                # without it cannot be holding a slot.
                task.cancel()
        else:
            err = result.get("error") if isinstance(result, dict) else None
            if isinstance(err, dict) and err.get("code") in _HYBRID_DEGRADE_CODES:
                degraded_reason = f"hybrid refused ({err.get('code')}): {err.get('message')}"
    t_hybrid = time.monotonic()
    hybrid_ms = _ms(t_hybrid - t0)
    split = _stage_split(box, t_hybrid)

    if degraded_reason is not None:
        return await _hybrid_degraded_to_fast(body, degraded_reason, hybrid_ms, split, t0, x_api_key)

    t1 = time.monotonic()
    response = main._rerank.rerank_response(body.query, main._unwrap(result), enabled=body.rerank)
    rerank_ms = _ms(time.monotonic() - t1)
    total_ms = _ms(time.monotonic() - t0)
    _log.info(
        "/search/hybrid timings: hybrid=%.0fms (queue=%s compute=%s) rerank=%.0fms total=%.0fms",
        hybrid_ms, split.get("queue", "?"), split.get("compute", "?"), rerank_ms, total_ms,
    )
    if isinstance(response, dict):
        response["timings_ms"] = {"hybrid": hybrid_ms, **split, "rerank": rerank_ms, "total": total_ms}
    return response


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 1)


def _stage_split(box: dict, t_end: float) -> dict:
    """``{"queue": ms, "compute": ms}`` from _call's stamps (#284).

    Empty when the call never reached the slow path (a fast intercept, or a
    test fake). A worker that has not started by ``t_end`` has queued the
    whole time and computed nothing; one still running has computed so far.
    """
    entered = box.get("entered")
    if entered is None:
        return {}
    started = box.get("started")
    if started is None:
        return {"queue": _ms(t_end - entered), "compute": 0.0}
    return {"queue": _ms(started - entered), "compute": _ms(t_end - started)}


def _retrieve_abandoned(task: asyncio.Future) -> None:
    """Consume the outcome of a _call task its route stopped waiting for, so
    asyncio never logs "exception was never retrieved" for it."""
    if not task.cancelled():
        task.exception()


# JSON-RPC codes from main._call that mean "the hybrid leg did not run to an
# answer" rather than "the query is bad": -32003 busy (in-flight ceiling),
# -32001 PALACE_MCP_TOOL_TIMEOUT_SECONDS exceeded.
_HYBRID_DEGRADE_CODES = (-32003, -32001)


async def _hybrid_degraded_to_fast(body, reason: str, hybrid_ms: float, split: dict, t0: float, x_api_key):
    """Answer a budgeted /search/hybrid from BM25 /search/fast (#284).

    The fallback has no vector leg, no graph leg and no rerank; /search/fast
    has no room filter, so a room-scoped request over-fetches and filters
    here. The response says all of that: ``degraded`` names the route that
    answered and ``warnings`` carries the reason, so a caller never mistakes
    a keyword answer for a hybrid one.
    """
    import main
    fetch = min(100, body.limit * 3) if body.room else body.limit
    t1 = time.monotonic()
    try:
        hits = await main.search_fast(
            q=body.query, limit=fetch, wing=body.wing,
            all_classes=body.all_classes, x_api_key=x_api_key,
        )
    except HTTPException as e:
        _log.warning("/search/hybrid degraded (%s) and the BM25 fallback failed: %s", reason, e.detail)
        raise HTTPException(
            status_code=504,
            detail=f"{reason}; BM25 /search/fast fallback failed too: {e.detail}",
        )
    fast_ms = round((time.monotonic() - t1) * 1000, 1)
    results = []
    for hit in hits or []:
        if body.room and hit.get("room") != body.room:
            continue
        hit = dict(hit)
        hit.setdefault("text", hit.get("snippet") or "")
        hit["matched_via"] = "bm25_postgres"
        results.append(hit)
        if len(results) >= body.limit:
            break
    total_ms = round((time.monotonic() - t0) * 1000, 1)
    _log.warning(
        "/search/hybrid degraded to bm25-fast: %s (hybrid=%.0fms queue=%s compute=%s fast=%.0fms total=%.0fms)",
        reason, hybrid_ms, split.get("queue", "?"), split.get("compute", "?"), fast_ms, total_ms,
    )
    return {
        "results": results,
        "query": body.query,
        "degraded": "bm25-fast",
        "warnings": [
            f"{reason}; answered from BM25 /search/fast "
            "(no vector or graph leg, no rerank)"
        ],
        "errors": [],
        "timings_ms": {"hybrid": hybrid_ms, **split, "fast": fast_ms, "total": total_ms},
    }


@router.post("/search/keyword")
async def search_keyword(
    body: SearchKeywordBody,
    x_api_key: str | None = Header(default=None),
):
    """BM25 keyword search over mempalace_drawers.doc_tsv.

    Body::

        {
          "query": "pgvector lazy index race",
          "wing":  "memorypalace",          // optional, exact-match filter
          "room":  "problems",              // optional, must be canonical if set
          "limit": 20
        }

    Returns the same result shape as ``/search`` for callers that mix
    the two (each hit has id, document, wing, room, metadata, score).
    Uses ``websearch_to_tsquery`` for user-friendly query parsing
    (phrase syntax, OR, negation).
    """
    import main
    main._check_auth(x_api_key)
    if main._mp._config.backend != "postgres":
        raise HTTPException(
            status_code=503,
            detail="/search/keyword requires MEMPALACE_BACKEND=postgres; daemon is on chroma.",
        )
    # palace-daemon#179: body fields (query, wing, room, limit) already
    # validated + canonicalized by SearchKeywordBody at parse time.
    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
    if not dsn:
        raise HTTPException(status_code=500, detail="MEMPALACE_POSTGRES_DSN not set in daemon environment")

    from mempalace.searcher import _bm25_only_via_postgres
    result = _bm25_only_via_postgres(
        body.query, dsn, wing=body.wing, room=body.room, n_results=body.limit,
    )
    if not body.all_classes and isinstance(result, dict) and result.get("results"):
        # Same request as before when nothing is flagged; only a page that lost flagged
        # hits is fetched again, wider, and filtered before the rerank sees it.
        # The lookups are blocking psycopg2 calls: run them off the event loop.
        hits = result["results"]
        hidden = await asyncio.to_thread(
            _judged_hidden_ids, [h.get("drawer_id") or h.get("id") for h in hits], dsn
        )
        if hidden:
            wider = await asyncio.to_thread(
                _bm25_only_via_postgres,
                body.query, dsn, wing=body.wing, room=body.room,
                n_results=min(body.limit + 2 * len(hidden), 400),
            )
            # Use the wider page only when it has results; a failed or empty refetch keeps
            # (and filters) the page we already had.
            usable = isinstance(wider, dict) and bool(wider.get("results")) and "error" not in wider
            result = await asyncio.to_thread(_drop_judged, wider if usable else result, dsn, body.limit)
    return main._rerank.rerank_response(body.query, result, enabled=body.rerank)


@router.post("/search/age-fused")
async def search_age_fused(
    body: SearchAgeFusedBody,
    x_api_key: str | None = Header(default=None),
):
    """Vector + AGE graph fusion search (Phase 5 of the AGE-integration work).

    Combines mempalace's vector retrieval with AGE entity-overlap on the
    write-through graph populated by kg_writethrough.py + backfill_age.py.
    Returns RRF-merged results so callers that want graph-aware retrieval
    don't have to fuse client-side.

    Body::

        {
          "query":         "pgvector advisory lock race",
          "wing":          "memorypalace",   // optional
          "room":          "problems",       // optional
          "limit":         10,
          "graph_top_k":   50,                // graph candidates to fetch
          "fusion_k":      60,                // RRF k constant
          "include_trace": false              // attach per-source counts
        }

    Returns the same hit shape as /search, plus an optional ``trace``
    field with {n_vector, n_graph, n_after_fusion}. Each hit has an
    extra ``matched_via`` key (``"vector"``, ``"graph"``, or ``"both"``).

    Requires:
      - MEMPALACE_BACKEND=postgres (AGE lives in postgres)
      - The kg_writethrough hook has populated MENTIONS edges (either via
        write-through on writes or via mempalace.backfill_age)

    Empty graph or extractor producing zero entities falls through to
    vector-only — the endpoint never errors on a missing AGE state.
    """
    import main
    main._check_auth(x_api_key)
    if main._mp._config.backend != "postgres":
        raise HTTPException(
            status_code=503,
            detail="/search/age-fused requires MEMPALACE_BACKEND=postgres; daemon is on chroma.",
        )
    # palace-daemon#179: body fields (query, wing, room, limit,
    # graph_top_k, fusion_k, include_trace) already validated +
    # canonicalized by SearchAgeFusedBody at parse time. Pull locals so
    # the rest of the handler reads naturally.
    query = body.query
    wing = body.wing
    room = body.room
    limit = body.limit
    graph_top_k = body.graph_top_k
    fusion_k = body.fusion_k
    include_trace = body.include_trace
    rr = body.rerank  # palace-daemon#189 per-request rerank override

    # Step 1: Vector retrieval via mempalace_search (existing MCP tool).
    vec_result = await main._call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": main._search_args(
            query,
            # Over-fetch so RRF has more candidates to work with.
            max(graph_top_k, limit * 3),
        ) | ({"wing": wing} if wing else {}) | ({"room": room} if room else {})
          | ({"all_classes": True} if body.all_classes else {})},
    })
    vec_hits = (main._unwrap(vec_result) or {}).get("results") or []

    # Step 2: AGE graph entity-overlap.
    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
    if not dsn:
        # No AGE access — fall through to vector-only with a warning trace.
        if include_trace:
            return main._rerank.rerank_response(query, {"results": vec_hits[:limit], "trace": {
                "n_vector": len(vec_hits), "n_graph": 0, "n_after_fusion": min(limit, len(vec_hits)),
                "warning": "MEMPALACE_POSTGRES_DSN not set; age-fused falls back to vector-only",
            }}, enabled=rr)
        return main._rerank.rerank_response(query, {"results": vec_hits[:limit]}, enabled=rr)

    # Initialize *before* the AGE lookup so the trace block can read it
    # even when the lookup raises before extraction happens.
    query_entities: list = []
    graph_hits_by_drawer: dict[str, float] = {}

    def _age_lookup() -> tuple[list, dict[str, float]]:
        """Sync AGE entity-overlap lookup. Called via ``asyncio.to_thread``
        so the daemon's event loop isn't blocked on Postgres I/O.

        #157: AGE's Cypher parser rejected the original ``RETURN d.id AS id,
        r.count AS count`` form with a SyntaxError ("syntax error at or near
        AS") — multi-AS RETURN with a relationship property is unsupported
        in this AGE version. Every call raised, the per-entity try/except
        silently swallowed it, and graph_hits_by_drawer stayed empty
        (n_graph=0 in /search/age-fused's trace).

        Workaround: use ``properties(r) AS edge_props`` which returns the
        full edge property map (verified against AGE 1.5 on familiar). The
        Python code below extracts ``count`` from that map; missing/null
        falls back to 1, matching the previous default."""
        from mempalace.knowledge_graph_age import KnowledgeGraphAGE
        kg = KnowledgeGraphAGE(dsn)
        hits: dict[str, float] = {}
        extractor = main._load_age_extractor()
        qents = extractor(query) if extractor else []
        try:
            for qe in qents:
                try:
                    rows = kg._run_cypher(
                        """
                        MATCH (d:Drawer)-[r:MENTIONS]->(e:Entity {name: $ename})
                        RETURN d.id AS drawer_id, properties(r) AS edge_props
                        """,
                        {"ename": qe.name},
                        fetch=True,
                    )
                except Exception as e:
                    # Don't swallow silently — log so future Cypher-syntax
                    # regressions are visible. The original per-entity try/
                    # except was hiding #157 for weeks.
                    _log.warning(
                        "/search/age-fused: AGE Cypher failed for entity %r: %s",
                        getattr(qe, "name", qe), e,
                    )
                    continue
                for r in rows:
                    drawer_id = kg._unwrap_agtype(r[0])
                    edge_props = kg._unwrap_agtype(r[1]) or {}
                    cnt = (edge_props.get("count") if isinstance(edge_props, dict) else None) or 1
                    if drawer_id:
                        hits[str(drawer_id)] = hits.get(str(drawer_id), 0) + int(cnt)
        finally:
            kg.close()
        return qents, hits

    try:
        query_entities, graph_hits_by_drawer = await asyncio.to_thread(_age_lookup)
    except Exception as e:
        # AGE not available — log + fall through.
        _log.warning("/search/age-fused: AGE lookup failed: %s — falling back to vector-only", e)

    # Step 3: RRF fusion. Vector rank by position; graph rank by overlap count.
    # Vector hits from mempalace_search expose the drawer id as `drawer_id`
    # (not `id`) — pre-#150 the `hit.get("id")` lookup returned None for
    # every hit, collapsing vec_ranks to {None: last_index} and effectively
    # disabling the vector half of the fusion. Falling back to `drawer_id`
    # restores the intended ranking.
    vec_ranks = {(hit.get("id") or hit.get("drawer_id")): i for i, hit in enumerate(vec_hits)}
    graph_ranks = {did: i for i, did in enumerate(sorted(graph_hits_by_drawer, key=lambda d: -graph_hits_by_drawer[d])[:graph_top_k])}

    union = set(vec_ranks) | set(graph_ranks)
    fused_scores: dict[str, float] = {}
    for did in union:
        score = 0.0
        if did in vec_ranks:
            score += 1.0 / (fusion_k + vec_ranks[did])
        if did in graph_ranks:
            score += 1.0 / (fusion_k + graph_ranks[did])
        fused_scores[did] = score

    # Build the merged result list — preserve full hit metadata when
    # vector saw the drawer; hydrate graph-only drawers from postgres so
    # the response shape matches /search (palace-daemon#150). Pre-#150 the
    # graph-only stubs had document=None and no text field, which caused
    # bench consumers (LongMemEval, /context) to see ~5.5× narrower
    # context vs /search default and a corresponding QA-acc regression.
    vec_by_id = {(hit.get("id") or hit.get("drawer_id")): hit for hit in vec_hits}
    fused_order = sorted(fused_scores.items(), key=lambda kv: -kv[1])[:limit]

    # Pre-fetch text + metadata for any graph-only drawers in one query
    # so we don't N+1 the database. Vector-matched drawers already have
    # their full hit dict from mempalace_search and don't need hydration.
    graph_only_ids = [did for did, _ in fused_order if did not in vec_by_id]
    hydrated: dict[str, dict] = {}
    if graph_only_ids:
        def _hydrate_drawers(ids: list[str]) -> dict[str, dict]:
            import psycopg2
            try:
                with psycopg2.connect(dsn, connect_timeout=3) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SET LOCAL statement_timeout = '5s'")
                        cur.execute(
                            "SELECT id, document, wing, room, "
                            "       COALESCE(metadata->>'topic', '') AS topic, "
                            "       COALESCE(metadata->>'source_file', '') AS source_file, "
                            "       metadata->>'created_at' AS created_at, "
                            "       metadata->>'judged_class' AS judged_class "
                            "FROM mempalace_drawers WHERE id = ANY(%s)",
                            (ids,),
                        )
                        return {
                            r[0]: {
                                "text": r[1] or "",
                                "wing": r[2],
                                "room": r[3],
                                "topic": r[4],
                                "source_file": r[5],
                                "created_at": r[6] or None,
                                "judged_class": r[7] if len(r) > 7 else None,
                            }
                            for r in cur.fetchall()
                        }
            except Exception as e:
                _log.warning("/search/age-fused: graph-only hydration failed: %s", e)
                return {}
        hydrated = await asyncio.to_thread(_hydrate_drawers, graph_only_ids)

    judged_excluded = 0
    out_hits: list[dict] = []
    for did, score in fused_order:
        if did in vec_by_id:
            hit = dict(vec_by_id[did])
            hit["matched_via"] = "both" if did in graph_ranks else "vector"
            hit["rrf_score"] = score
        else:
            # Graph-only drawer — emit a hit matching /search's shape so
            # bench consumers see the same context width. If hydration
            # failed (postgres bounced etc.), fall back to the historic
            # minimal-stub shape so the response is still valid.
            row = hydrated.get(did)
            if row and not body.all_classes and row.get("judged_class") in JUDGED_SEARCH_EXCLUDED:
                judged_excluded += 1  # graph-only and flagged: vector hits are filtered by mempalace
                continue
            if row:
                hit = {
                    "drawer_id": did,
                    "text": row["text"],
                    "wing": row["wing"],
                    "room": row["room"],
                    "topic": row["topic"],
                    "source_file": row["source_file"],
                    "created_at": row["created_at"],
                    "matched_via": "graph",
                    "rrf_score": score,
                    "graph_mentions": graph_hits_by_drawer.get(did, 0),
                }
            else:
                hit = {
                    "id": did,
                    "document": None,
                    "matched_via": "graph",
                    "rrf_score": score,
                    "graph_mentions": graph_hits_by_drawer.get(did, 0),
                }
        out_hits.append(hit)

    response = {"results": out_hits}
    vec_payload = main._unwrap(vec_result) if isinstance(vec_result, dict) else {}
    if isinstance(vec_payload, dict):
        if vec_payload.get("judged_excluded"):
            response["judged_excluded"] = vec_payload["judged_excluded"]
        if vec_payload.get("warnings"):
            response["warnings"] = list(vec_payload["warnings"])
    _note_judged(response, judged_excluded)
    if include_trace:
        response["trace"] = {
            "n_vector": len(vec_hits),
            "n_graph": len(graph_hits_by_drawer),
            "n_after_fusion": len(out_hits),
            "query_entities": [e.name for e in query_entities],
        }
    return main._rerank.rerank_response(query, response, enabled=rr)


@router.get("/context")
async def context(
    topic: str,
    limit: int = 5,
    # palace-daemon#194: same kind= semantics as /search.
    kind: str = "content",
    all_classes: bool = False,
    x_api_key: str | None = Header(default=None),
):
    """Alias for /search with a semantically friendlier name for LLM tool
    prompts. `kind` carries the same checkpoint-filter semantics as /search
    (#194): "content" excludes Stop-hook checkpoints, "all" is the superset,
    "checkpoint" returns only checkpoints."""
    import main
    main._check_auth(x_api_key)
    if kind not in _VALID_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {_VALID_KINDS}; got {kind!r}",
        )
    result = await main._call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": main._search_args(topic, limit)
                   | ({"all_classes": True} if all_classes else {})},
    })
    return _apply_kind_filter(main._unwrap(result), kind)
