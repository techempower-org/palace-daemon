"""/mcp fast-intercept payloads (#49) — extracted from main.py per #101 (sixth slice).

When ``/mcp`` proxies ``mempalace_status`` or ``mempalace_kg_stats``, the
upstream implementations sweep the full chroma metadata and run three
Cypher scans — 29s and 9s respectively at our production size, which
exceeds client timeouts. These helpers produce the same envelope shape
from direct-SQL fast paths so the response stays sub-second under load.

**Why the lazy ``import main``**

Both wrappers internally call helpers that still live in main.py:

- ``fast_mcp_status_payload`` calls ``main._fast_status_payload``
- ``fast_mcp_kg_stats_payload`` calls ``main._read_kg_postgres_stats``

The unit tests in ``tests/test_mcp_fast_intercept.py`` patch those
helpers via ``patch.object(main, "_fast_status_payload", ...)`` and
``patch.object(main, "_read_kg_postgres_stats", ...)`` and then call
the wrappers through ``main._fast_mcp_*_payload``. If the wrappers
captured a direct module-level reference, the patch would not
intercept (the wrapper would resolve in its own module's namespace).

The function-local ``import main`` resolves the helper *at call time*
via main's namespace, so patches work without test edits. Same
pattern as ``daemon_tools.invalidate_rooms_cache`` (#131).

A future slice could pull ``_fast_status_payload`` and
``_read_kg_postgres_stats`` here too, but each move requires either
updating the tests to patch this module, or adding another layer of
indirection back to main. For now the wrappers live here and the
helpers stay where the tests expect them.
"""
from __future__ import annotations


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
