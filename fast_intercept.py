"""/mcp fast-intercept payloads (#49) — extracted from main.py per #101 (sixth slice).

When ``/mcp`` proxies ``mempalace_status`` or ``mempalace_kg_stats``, the
upstream implementations sweep the full chroma metadata and run three
Cypher scans — 29s and 9s respectively at our production size, which
exceeds client timeouts. These helpers produce the same envelope shape
from direct-SQL fast paths so the response stays sub-second under load.

**Why the lazy ``import main`` in the wrappers**

The fast-intercept wrappers call helpers that test code patches via
``patch.object(main, ...)``. If the wrappers captured a direct
module-level reference, the patch would not intercept — the wrapper
would resolve in its own module's namespace.

The function-local ``import main`` resolves the helper *at call time*
via main's namespace, so patches work without test edits. Same
pattern as ``daemon_tools.invalidate_rooms_cache`` (#131).

This pattern enabled #101 thirteenth slice (this commit) to move
``fast_status_payload`` here without touching tests: main.py re-exports
the function under its old ``_fast_status_payload`` name, and the
wrapper's lazy ``main._fast_status_payload()`` lookup sees both the
patched value (when tests patch it) and the live function (when not).

``_read_kg_postgres_stats`` still lives in ``kg_reader.py`` (extracted
in JP's #134); the same lazy-import pattern applies to its wrapper.
helpers stay where the tests expect them.
"""
from __future__ import annotations

import os
import threading
import time


def fast_status_payload() -> dict:
    """Per-wing / per-room counts via direct SQL — no MCP, no AGE, no locks.

    Shared between ``GET /status/fast`` and the ``/mcp`` fast-intercept
    path (issue #49); the latter wraps this into the ``tool_status``
    envelope shape, the former returns it as-is.

    Extracted from main.py per #101 (thirteenth slice). main.py
    re-exports under ``_fast_status_payload`` so existing test patches
    (``patch.object(main, "_fast_status_payload", ...)``) and direct
    callers (``main._fast_status_payload()`` in test_db_error_integration)
    keep working.
    """
    from postgres import postgres_dsn
    from db_errors import record_db_error

    dsn = postgres_dsn()
    if not dsn:
        raise RuntimeError("postgres backend not configured")
    import psycopg2
    # psycopg2's connection context manager commits/rolls-back the
    # transaction but does NOT close the connection — leaving the close
    # to garbage collection leaks file descriptors under load. Wrap in
    # try/finally so the connection is always released on exit.
    # #108: record OperationalError on connect so the /health observability
    # ring buffer is populated even on the fast-status path (which doesn't
    # go through _connect_postgres). Re-raise so existing callers (the
    # fast-intercept fallback and /status/fast) keep their behaviour.
    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
    except psycopg2.OperationalError as e:
        record_db_error(e)
        raise
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '3s'")
                cur.execute("SELECT count(*) FROM mempalace_drawers")
                total = cur.fetchone()[0]
                cur.execute(
                    "SELECT wing, count(*) FROM mempalace_drawers GROUP BY wing ORDER BY count(*) DESC"
                )
                wings = {r[0]: r[1] for r in cur.fetchall()}
                cur.execute(
                    "SELECT room, count(*) FROM mempalace_drawers WHERE room IS NOT NULL GROUP BY room ORDER BY count(*) DESC"
                )
                rooms = {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()
    return {"total_drawers": total, "wings": wings, "rooms": rooms}


def fast_mcp_list_wings_payload() -> dict:
    """``tool_list_wings`` shape via direct SQL (#239).

    The MCP tool sweeps the whole drawer set to build per-wing counts, which
    at 700K+ drawers exceeds the /mcp tool timeout. This is the ``wings`` half
    of the counts fast_status_payload already computes.
    """
    import main  # lazy — mirrors fast_mcp_status_payload's re-export contract

    return {"wings": main._fast_status_payload().get("wings", {})}


def fast_mcp_get_taxonomy_payload() -> dict:
    """``tool_get_taxonomy`` shape (per-wing × per-room counts) via direct SQL (#239)."""
    from db_errors import record_db_error
    from postgres import postgres_dsn

    dsn = postgres_dsn()
    if not dsn:
        raise RuntimeError("postgres backend not configured")
    import psycopg2

    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
    except psycopg2.OperationalError as e:
        record_db_error(e)
        raise
    taxonomy: dict = {}
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '5s'")
                cur.execute(
                    "SELECT wing, room, count(*) FROM mempalace_drawers "
                    "WHERE wing IS NOT NULL GROUP BY wing, room ORDER BY wing, count(*) DESC"
                )
                for wing, room, n in cur.fetchall():
                    taxonomy.setdefault(wing, {})[room if room is not None else ""] = n
    finally:
        conn.close()
    return {"taxonomy": taxonomy}


def fast_mcp_status_payload() -> dict:
    """``tool_status`` shape via the direct-SQL fast path.

    Adds ``protocol`` and ``aaak_dialect`` (imported lazily because they live
    in the mempalace.mcp_server module the daemon proxies into) so the
    response is byte-compatible with the slow tool. Falling back to the empty
    strings on import failure keeps the intercept usable even if mempalace
    ever drops those constants.
    """
    import main  # lazy — preserves `patch.object(main, "_fast_status_payload")`
    payload = main._fast_status_payload()
    try:
        from mempalace.mcp_server import PALACE_PROTOCOL, AAAK_SPEC

        payload["protocol"] = PALACE_PROTOCOL
        payload["aaak_dialect"] = AAAK_SPEC
    except Exception:
        payload.setdefault("protocol", "")
        payload.setdefault("aaak_dialect", "")
    return payload


def fast_mcp_kg_stats_payload() -> dict:
    """``tool_kg_stats`` shape from the AGE backing-table fast path.

    The upstream tool runs three Cypher scans — ``MATCH (n:Entity)``,
    ``MATCH ()-[r:RELATION]->()`` (with a CASE for current/expired), and
    ``DISTINCT r.relation_type``. Each is a full graph walk through agtype
    and exhausts shared memory under the production-scale palace, which is
    exactly what blocks /mcp (#49).

    The fast path uses ``_read_kg_postgres_stats`` which counts the AGE
    backing label tables directly — sub-millisecond. Trade-off: it can't
    cheaply split current vs expired (needs property access on edges) and
    can't enumerate distinct ``r.relation_type`` values (same), so:

      * ``current_facts`` defaults to ``triples`` (we have no semantic
        triples yet; once extraction lands, set
        ``PALACE_MCP_FAST_INTERCEPT=0`` to get the precise split).
      * ``relationship_types`` is the AGE edge labels present
        (``["RELATION", "MENTIONS"]``-style, filtered to non-empty), not
        the ``r.relation_type`` predicate values the slow path returns.

    Raises if AGE isn't reachable — the caller falls back to the slow path.
    """
    import main  # lazy — preserves `patch.object(main, "_read_kg_postgres_stats")`
    stats = main._read_kg_postgres_stats(exact_mentions=False)
    if not stats:
        raise RuntimeError("AGE knowledge graph unreachable")
    triples = int(stats.get("triples", 0))
    return {
        "entities": int(stats.get("entities", 0)),
        "triples": triples,
        "current_facts": triples,
        "expired_facts": 0,
        "relationship_types": list(stats.get("relationship_types", [])),
    }


# ── kg_stats cache + single flight (#290) ────────────────────────────────────
#
# Even at ~290 ms the payload is not the "sub-millisecond" this module promises,
# and N concurrent callers each pay it. The cache makes a repeat free and the
# lock makes N concurrent cold callers cost ONE query rather than N.
#
# EXACTNESS IS PRESERVED — this caches the real counts, it does not estimate
# them. `pg_class.reltuples` was the alternative in #290 and is not used: the
# cold query was made cheap instead (EXISTS for MENTIONS), so there is nothing
# to trade accuracy for.
#
# TTL default 120 s: kg_stats is an operator-facing summary of a graph that
# changes by mining, and a two-minute-old entity count answers every question
# anyone asks of it. `PALACE_KG_STATS_TTL=0` disables the cache entirely.
_kg_stats_lock = threading.Lock()
_kg_stats_cached: dict | None = None
_kg_stats_at: float = 0.0
_kg_stats_queries = 0          # test seam: counts real computations, not calls


def _kg_stats_ttl() -> float:
    try:
        return float(os.environ.get("PALACE_KG_STATS_TTL", "120"))
    except (TypeError, ValueError):
        return 120.0


def kg_stats_cache_clear() -> None:
    """Drop the cached payload (tests, and any future explicit invalidation)."""
    global _kg_stats_cached, _kg_stats_at
    with _kg_stats_lock:
        _kg_stats_cached = None
        _kg_stats_at = 0.0


def fast_mcp_kg_stats_cached() -> tuple[dict, bool]:
    """``(payload, was_cached)`` — the payload above, memoised with single flight.

    Returns ``was_cached`` so the caller can log "intercepted, cached" apart
    from "intercepted, cold"; #290's complaint was that the journal showed only
    ``/mcp slow path:`` and so read as "not intercepted at all".
    """
    global _kg_stats_cached, _kg_stats_at, _kg_stats_queries
    ttl = _kg_stats_ttl()
    now = time.monotonic()
    if ttl > 0 and _kg_stats_cached is not None and (now - _kg_stats_at) < ttl:
        return dict(_kg_stats_cached), True
    # Single flight: the second caller blocks here, then the re-check below
    # sees the first caller's result and returns it without querying.
    with _kg_stats_lock:
        now = time.monotonic()
        if ttl > 0 and _kg_stats_cached is not None and (now - _kg_stats_at) < ttl:
            return dict(_kg_stats_cached), True
        # Through main's namespace, NOT the local symbol — the same reason
        # fast_mcp_kg_stats_payload reaches for `main._read_kg_postgres_stats`:
        # `patch.object(main, "_fast_mcp_kg_stats_payload")` is the seam three
        # existing suites use to control what /mcp returns (#286's regression
        # coverage among them). Calling the local function would silently
        # bypass every one of those patches.
        import main
        payload = main._fast_mcp_kg_stats_payload()
        _kg_stats_queries += 1
        if ttl > 0:
            _kg_stats_cached = dict(payload)
            _kg_stats_at = time.monotonic()
        return dict(payload), False


# ── /list fast path (#231) ───────────────────────────────────────────────────
#
# ``mempalace_list_drawers`` fetches EVERY row (documents included) into
# Python, collapses chunk groups, then slices the page — ~50s for limit=5
# at 490K drawers when no wing filter narrows the fetch. The fast path
# pages in SQL over "anchor" rows (``metadata->>'parent_drawer_id' IS
# NULL`` — singles and legacy logical parents), which covers all but the
# few chunk groups written without a legacy parent row (49 groups / 146
# chunk rows at production size). Those orphan groups are listed AFTER
# the anchors in a stable order — the upstream tool's ordering contract
# is only "approximates insertion order, not guaranteed", so a stable
# alternative order is compliant.
#
# Chunk-group side data (parents, chunk ids, first-chunk preview) is tiny
# and changes rarely, so it is swept once per ``_CHUNK_GROUP_TTL`` and
# cached; the page query itself is ~2ms unfiltered / ~140ms wing-filtered.

_CHUNK_GROUP_TTL = 300.0
_chunk_group_cache: dict = {"at": 0.0, "groups": None}

_LIST_MAX_RESULTS = 100  # mirrors mempalace mcp_server._MAX_RESULTS


def _tags_from_meta(meta: dict) -> list:
    """Tolerant read of the tags metadata key (list, JSON string, or CSV)."""
    raw = meta.get("tags") if isinstance(meta, dict) else None
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str) and t]
    if isinstance(raw, str) and raw:
        import json as _json

        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                return [t for t in parsed if isinstance(t, str) and t]
        except ValueError:
            pass
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []


def _list_entry(drawer_id, document, wing, room, meta) -> dict:
    """One logical-drawer envelope entry, matching the slow tool's shape."""
    from pathlib import Path

    meta = dict(meta or {})
    meta["wing"] = wing or ""
    meta["room"] = room or ""
    if meta.get("source_file"):
        meta["source_file"] = Path(str(meta["source_file"])).name
    doc = document or ""
    preview = doc[:200] + "..." if len(doc) > 200 else doc
    return {
        "drawer_id": drawer_id,
        "wing": wing or "",
        "room": room or "",
        "content_preview": preview,
        "metadata": meta,
        "tags": _tags_from_meta(meta),
    }


def _load_chunk_groups(cur) -> dict:
    """Sweep the chunk side: parent -> group info. Cheap (146 rows today)."""
    cur.execute(
        "SELECT metadata->>'parent_drawer_id' AS parent, id, document, wing, room, metadata "
        "FROM mempalace_drawers WHERE metadata->>'parent_drawer_id' IS NOT NULL "
        "ORDER BY 1, NULLIF(metadata->>'chunk_index', '')::int NULLS LAST, id"
    )
    groups: dict = {}
    for parent, cid, doc, wing, room, meta in cur.fetchall():
        g = groups.setdefault(
            parent,
            {"chunk_ids": [], "wing": wing, "room": room, "document": doc, "metadata": meta},
        )
        g["chunk_ids"].append(cid)
    if groups:
        cur.execute(
            "SELECT id FROM mempalace_drawers WHERE id = ANY(%s)", (list(groups),)
        )
        legacy = {r[0] for r in cur.fetchall()}
        for parent, g in groups.items():
            g["has_legacy_row"] = parent in legacy
    return groups


def _chunk_groups_cached(cur) -> dict:
    import time as _t

    if (
        _chunk_group_cache["groups"] is None
        or _t.monotonic() - _chunk_group_cache["at"] >= _CHUNK_GROUP_TTL
    ):
        _chunk_group_cache["groups"] = _load_chunk_groups(cur)
        _chunk_group_cache["at"] = _t.monotonic()
    return _chunk_group_cache["groups"]


def fast_list_payload(wing=None, room=None, limit=20, offset=0) -> dict:
    """``mempalace_list_drawers`` envelope via direct SQL (#231).

    Serves the wing/room/limit/offset argument subset only — ``since`` /
    ``before`` / ``tags`` filters must fall through to the slow tool.
    Raises on any DB problem; callers fall back to the slow path (#49
    pattern), recording the error for /health observability (#108).
    """
    from postgres import postgres_dsn
    from db_errors import record_db_error

    dsn = postgres_dsn()
    if not dsn:
        raise RuntimeError("postgres backend not configured")

    limit = max(1, min(int(limit), _LIST_MAX_RESULTS))
    offset = max(0, int(offset))

    import psycopg2

    try:
        conn = psycopg2.connect(dsn, connect_timeout=3)
    except psycopg2.OperationalError as e:
        record_db_error(e)
        raise
    try:
        with conn:
            with conn.cursor() as cur:
                # NOTE: where_sql below is assembled ONLY from the fixed
                # literals in this function; user values always bind via
                # placeholders. (Static-analysis S608 hits are false
                # positives on the composed fragment.)
                cur.execute("SET LOCAL statement_timeout = '10s'")
                where = ["metadata->>'parent_drawer_id' IS NULL"]
                params: list = []
                if wing:
                    where.append("wing = %s")
                    params.append(wing)
                if room:
                    where.append("room = %s")
                    params.append(room)
                where_sql = " AND ".join(where)

                cur.execute(
                    f"SELECT count(*) FROM mempalace_drawers WHERE {where_sql}", params
                )
                anchor_total = cur.fetchone()[0]

                groups = _chunk_groups_cached(cur)
                orphans = [
                    (parent, g)
                    for parent, g in sorted(groups.items())
                    if not g.get("has_legacy_row")
                    and (not wing or g.get("wing") == wing)
                    and (not room or g.get("room") == room)
                ]
                total = anchor_total + len(orphans)

                page: list = []
                if offset < anchor_total:
                    cur.execute(
                        f"SELECT id, document, wing, room, metadata FROM mempalace_drawers "
                        f"WHERE {where_sql} ORDER BY id LIMIT %s OFFSET %s",
                        [*params, limit, offset],
                    )
                    for did, doc, w, r, meta in cur.fetchall():
                        entry = _list_entry(did, doc, w, r, meta)
                        g = groups.get(did)
                        if g:  # legacy logical row for a chunk group
                            entry["metadata"]["chunks"] = len(g["chunk_ids"])
                            entry["metadata"]["chunk_ids"] = list(g["chunk_ids"])
                        page.append(entry)

                # Orphan chunk groups occupy positions [anchor_total, total).
                if len(page) < limit:
                    o_start = max(0, offset - anchor_total)
                    o_take = limit - len(page)
                    for parent, g in orphans[o_start : o_start + o_take]:
                        entry = _list_entry(
                            parent, g.get("document"), g.get("wing"), g.get("room"), g.get("metadata")
                        )
                        entry["metadata"]["chunks"] = len(g["chunk_ids"])
                        entry["metadata"]["chunk_ids"] = list(g["chunk_ids"])
                        page.append(entry)
    except psycopg2.Error as e:
        # #108: query-time failures (statement timeout, dropped connection,
        # bad cast in the chunk sweep) must reach /health.db_errors too —
        # not just connect() errors — before the caller falls back.
        record_db_error(e)
        raise
    finally:
        conn.close()

    return {
        "drawers": page,
        "total": total,
        "count": len(page),
        "offset": offset,
        "limit": limit,
    }
