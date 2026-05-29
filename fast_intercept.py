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
    stats = main._read_kg_postgres_stats()
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
