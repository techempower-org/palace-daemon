"""Direct-SQL readers for wings/rooms and the AGE knowledge graph — extracted
from main.py per #101 refactor (fifth slice).

Owns the read-only helpers the ``/graph`` route uses to bypass the MCP
fan-out (which serialises through the read semaphore and stalls under
load). All paths degrade gracefully — schema drift, missing tables,
connection failures return empty results so the route falls back to its
MCP composition path rather than 500ing.

Module layout:

- ``kg_path()`` / ``chroma_path()`` — sibling sqlite file paths inside
  the chroma-backend palace dir.
- ``read_wings_rooms_postgres()`` — two GROUP BY queries on
  ``mempalace_drawers`` for the postgres backend.
- ``read_wings_rooms_direct()`` — backend dispatcher (postgres → above,
  else GROUP BY on chroma sqlite).
- ``read_kg_postgres()`` — entities + RELATION triples + MENTIONS edges
  from AGE via ``KnowledgeGraphAGE._run_cypher``.
- ``read_kg_postgres_stats()`` — AGE backing-label-table counts
  (entities, triples, mentions) without running a Cypher graph walk.
- ``read_kg_stats_direct()`` — backend dispatcher for stats (postgres →
  above, chroma → None so the MCP path stays authoritative).
- ``read_kg_direct()`` — backend dispatcher for the entity/triple/
  mention lists (postgres → ``read_kg_postgres``, chroma → sqlite read).

main.py re-exports the names under their original ``_``-prefixed form so
existing call sites + tests that ``main._read_kg_*`` / ``main._kg_path``
keep working. Tests that ``patch.object(main, "_read_kg_*", …)`` need to
patch ``kg_reader.read_kg_*`` instead — same pattern as the postgres
slice. The intra-module dispatchers (``read_wings_rooms_direct``,
``read_kg_direct``, ``read_kg_stats_direct``) call their helpers via
this module's namespace, bypassing main's re-exports, so patches on the
``main._`` aliases would miss them.
"""
from __future__ import annotations

import json
import os
import sqlite3


def _config():
    """Lazy mempalace config accessor — avoids importing mempalace at module
    load time (matches the postgres.py / db_errors.py pattern)."""
    import mempalace.mcp_server as _mp
    return _mp._config


def kg_path() -> str:
    """KG sqlite path. Lives next to chroma.sqlite3 inside the palace dir."""
    return os.path.join(_config().palace_path, "knowledge_graph.sqlite3")


def chroma_path() -> str:
    """Chroma sqlite path inside the palace dir."""
    return os.path.join(_config().palace_path, "chroma.sqlite3")


def read_wings_rooms_postgres() -> tuple[dict[str, int], list[dict]]:
    """Read wings + rooms-per-wing directly from the postgres backend.

    Two cheap GROUP BY queries on the indexed `wing` / (`wing`,`room`)
    columns of `mempalace_drawers`. Measured at ~150ms each on the
    canonical 270K-drawer palace, well under the original chroma-sqlite
    direct read budget and small enough to compute live on every /graph
    call instead of caching.

    Returns ({}, []) on any failure so /graph degrades gracefully — the
    SME adapter falls back to MCP composition in that case.
    """
    cfg = _config()
    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN") or getattr(
        cfg, "postgres_dsn", None
    )
    if not dsn:
        return {}, []

    wings: dict[str, int] = {}
    rooms_by_wing: dict[str, dict[str, int]] = {}
    try:
        import psycopg2
        # Short timeout — /graph is interactive; we'd rather degrade than
        # block the request behind a stuck planner.
        # #110: record OperationalError on connect before allowing the outer
        # except to swallow it for graceful degradation.
        try:
            conn = psycopg2.connect(dsn, connect_timeout=5)
        except psycopg2.OperationalError as e:
            import db_errors
            db_errors.record_db_error(e)
            raise
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SET LOCAL statement_timeout = '10s'; "
                    "SELECT wing, COUNT(*) FROM mempalace_drawers GROUP BY wing"
                )
                for name, n in cur.fetchall():
                    if name:
                        wings[name] = n
            with conn.cursor() as cur:
                cur.execute(
                    "SET LOCAL statement_timeout = '10s'; "
                    "SELECT wing, room, COUNT(*) FROM mempalace_drawers "
                    "GROUP BY wing, room"
                )
                for wing, room, n in cur.fetchall():
                    if wing and room:
                        rooms_by_wing.setdefault(wing, {})[room] = n
    except Exception:
        # Schema drift, connection issue, statement timeout, anything —
        # degrade to empty rather than 500 the /graph request.
        return {}, []

    all_wings = set(wings) | set(rooms_by_wing)
    rooms = [{"wing": w, "rooms": rooms_by_wing.get(w, {})} for w in sorted(all_wings)]
    return wings, rooms


def read_wings_rooms_direct() -> tuple[dict[str, int], list[dict]]:
    """Read wings + rooms directly from the live backend, off-loop.

    Bypasses the MCP fan-out (list_wings + list_rooms × N) which serializes
    through the read semaphore and stalls under load. Computes live on
    every call — both backends are fast enough that caching just creates
    staleness bugs (the chroma sqlite snapshot used to lag the live
    postgres backend by ~10× after the chroma → postgres migration; this
    was the original motivation for routing by backend here).

    - postgres: two GROUP BY queries on `mempalace_drawers` (~150ms each
      on the canonical 270K-drawer palace).
    - chroma:   GROUP BY on `embedding_metadata` in the persistent
      client's `chroma.sqlite3`. ~200× faster than the MCP fan-out on
      151K drawers (~0.4s vs 60-120s under contention).

    Schemas are internal to the respective backends — not part of
    mempalace's public API. Tolerated by catching OperationalError /
    psycopg2 errors; if the schema ever drifts, /graph degrades to empty
    wings/rooms (the SME adapter then falls back to its MCP composition
    path).
    """
    # Route by configured backend. The chroma sqlite path is a stale
    # snapshot under postgres (it was the pre-migration store and
    # receives no further writes), so reading it would return frozen
    # counts — exactly the "10× stale" bug this function exists to avoid.
    backend = getattr(_config(), "backend", None)
    if backend == "postgres":
        return read_wings_rooms_postgres()

    chroma = chroma_path()
    if not os.path.isfile(chroma):
        return {}, []
    try:
        conn = sqlite3.connect(f"file:{chroma}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return {}, []

    wings: dict[str, int] = {}
    rooms_by_wing: dict[str, dict[str, int]] = {}
    try:
        try:
            for name, n in conn.execute(
                "SELECT string_value, COUNT(*) FROM embedding_metadata "
                "WHERE key='wing' GROUP BY string_value"
            ):
                if name:
                    wings[name] = n
        except sqlite3.OperationalError:
            pass
        try:
            for wing, room, n in conn.execute(
                "SELECT em_w.string_value, em_r.string_value, COUNT(*) "
                "FROM embedding_metadata em_w "
                "JOIN embedding_metadata em_r ON em_w.id = em_r.id "
                "WHERE em_w.key='wing' AND em_r.key='room' "
                "GROUP BY em_w.string_value, em_r.string_value"
            ):
                if wing and room:
                    rooms_by_wing.setdefault(wing, {})[room] = n
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()

    # Iterate the union of wings + rooms_by_wing keys, not just `wings`,
    # so a partial schema-drift (wings query OperationalError-ed but the
    # rooms-per-wing query succeeded, or vice versa) doesn't silently
    # drop the half that worked.
    all_wings = set(wings) | set(rooms_by_wing)
    rooms = [{"wing": w, "rooms": rooms_by_wing.get(w, {})} for w in sorted(all_wings)]
    return wings, rooms


def read_kg_postgres(
    entity_limit: int = 500,
    triple_limit: int = 1000,
    mention_limit: int = 1000,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Read entities, RELATION triples, and MENTIONS edges from AGE.

    Returns three lists because the live graph holds two semantically
    distinct edge types under the same `/graph` umbrella:

      * **RELATION** — classical semantic triples (entity→entity facts
        like "Alice — works_at — Anthropic"). The graph currently holds
        ~1 of these (a placeholder); the corpus has not yet been
        triple-extracted. Projected into ``kg_triples`` so consumers
        that ask "do we have any semantic facts?" get an honest answer.

      * **MENTIONS** — Drawer→Entity mention edges (5.66M as of
        2026-05-25). These are *not* semantic triples — they're an
        inverted-index artifact of the backfill saying "this drawer
        text mentioned this entity." Projected into ``kg_mentions`` so
        they stop being mislabeled as triples (pre-1.8.2 they lived in
        ``kg_triples``, which conflated the two).

    All three queries run via ``KnowledgeGraphAGE._run_cypher`` (the
    same path ``POST /cypher`` uses).

    Response shape:
      entity:   {id, name, type, properties}
      triple:   {subject, predicate, object, valid_from, valid_to,
                 confidence, source_file}  — RELATION, subject/object
                 are entity names, predicate is r.relation_type
      mention:  {subject, predicate, object, valid_from, valid_to,
                 confidence, source_file}  — MENTIONS, subject is
                 drawer id, object is entity name, predicate is
                 hard-coded "MENTIONS", source_file carries the etype
                 tag (PROPER_NOUN, TECH_IDENT, ...)

    Caps default to 500/1000/1000. Tight to bound /graph latency on the
    full palace; callers raise via the ``limit`` query parameter.
    """
    cfg = _config()
    dsn = getattr(cfg, "postgres_dsn", None) or os.environ.get(
        "MEMPALACE_POSTGRES_DSN"
    )
    if not dsn:
        return [], [], []

    try:
        from mempalace.knowledge_graph_age import KnowledgeGraphAGE

        kg = KnowledgeGraphAGE(dsn=dsn)
    except Exception:
        return [], [], []

    entities: list[dict] = []
    triples: list[dict] = []
    mentions: list[dict] = []
    try:
        try:
            ent_rows = kg._run_cypher(
                "MATCH (e:Entity) RETURN e.name AS name LIMIT $n",
                {"n": int(entity_limit)},
                fetch=True,
            )
        except Exception:
            ent_rows = []
        for r in ent_rows:
            name = kg._unwrap_agtype(r[0])
            if name:
                entities.append({
                    "id": name,
                    "name": name,
                    "type": "entity",
                    "properties": {},
                })

        try:
            rel_rows = kg._run_cypher(
                """
                MATCH (a:Entity)-[r:RELATION]->(b:Entity)
                RETURN a.name AS subject, r.relation_type AS predicate,
                       b.name AS object, r.confidence AS confidence
                LIMIT $n
                """,
                {"n": int(triple_limit)},
                fetch=True,
            )
        except Exception:
            rel_rows = []
        for r in rel_rows:
            subj = kg._unwrap_agtype(r[0])
            obj = kg._unwrap_agtype(r[2])
            if not (subj and obj):
                continue
            triples.append({
                "subject": subj,
                "predicate": kg._unwrap_agtype(r[1]) or "RELATION",
                "object": obj,
                "valid_from": None,
                "valid_to": None,
                "confidence": kg._unwrap_agtype(r[3]),
                "source_file": None,
            })

        try:
            men_rows = kg._run_cypher(
                """
                MATCH (d:Drawer)-[r:MENTIONS]->(e:Entity)
                RETURN d.id AS subject, e.name AS object,
                       r.etype AS etype, r.confidence AS confidence
                LIMIT $n
                """,
                {"n": int(mention_limit)},
                fetch=True,
            )
        except Exception:
            men_rows = []
        for r in men_rows:
            subj = kg._unwrap_agtype(r[0])
            obj = kg._unwrap_agtype(r[1])
            if not (subj and obj):
                continue
            mentions.append({
                "subject": subj,
                "predicate": "MENTIONS",
                "object": obj,
                "valid_from": None,
                "valid_to": None,
                "confidence": kg._unwrap_agtype(r[3]),
                "source_file": kg._unwrap_agtype(r[2]),
            })
    finally:
        try:
            kg.close()
        except Exception:
            pass

    return entities, triples, mentions


def read_kg_postgres_stats() -> dict | None:
    """Live KG stats from Apache AGE — entity, RELATION, MENTIONS counts.

    Three counts straight off the AGE backing label tables (avoiding
    Cypher because ``MATCH ()-[r:MENTIONS]->() RETURN count(r)`` runs the
    full 5.66M-row scan through agtype and exhausts Postgres shared
    memory):

      * ``entities`` — vertex count from ``mempalace_kg."Entity"``
      * ``triples`` — RELATION edge count (real entity→entity semantic
        facts). Currently ~1 row; the corpus has not been triple-
        extracted yet.
      * ``mentions`` — MENTIONS edge count (Drawer→Entity mention
        links). 5.66M+ rows on the live palace.

    Pre-1.8.2 this field was named ``triples`` but counted MENTIONS,
    masking the fact that we have entities but ~zero semantic facts.

    Returns ``None`` when AGE is unreachable so the caller falls back to
    the MCP-derived payload (which is still correct under the chroma
    backend, where the sqlite KG holds RELATION-style triples in its
    ``triples`` table and has no MENTIONS concept).
    """
    cfg = _config()
    dsn = getattr(cfg, "postgres_dsn", None) or os.environ.get(
        "MEMPALACE_POSTGRES_DSN"
    )
    if not dsn:
        return None
    try:
        from mempalace.knowledge_graph_age import KnowledgeGraphAGE

        kg = KnowledgeGraphAGE(dsn=dsn)
    except Exception:
        return None
    try:
        graph = getattr(kg, "GRAPH_NAME", "mempalace_kg")
        entities = 0
        triples = 0
        mentions = 0
        try:
            with kg._conn.cursor() as cur:
                cur.execute(f'SELECT count(*) FROM {graph}."Entity"')
                row = cur.fetchone()
                entities = int(row[0]) if row else 0
                cur.execute(f'SELECT count(*) FROM {graph}."RELATION"')
                row = cur.fetchone()
                triples = int(row[0]) if row else 0
                cur.execute(f'SELECT count(*) FROM {graph}."MENTIONS"')
                row = cur.fetchone()
                mentions = int(row[0]) if row else 0
        except Exception:
            try:
                kg._conn.rollback()
            except Exception:
                pass
    finally:
        try:
            kg.close()
        except Exception:
            pass
    # relationship_types reports only edge labels with nonzero rows so
    # consumers can branch on what's actually populated. RELATION is
    # excluded until/unless triple extraction lands.
    rel_types = [name for name, n in (("RELATION", triples), ("MENTIONS", mentions)) if n]
    return {
        "entities": entities,
        "triples": triples,
        "mentions": mentions,
        "relationship_types": rel_types,
    }


def read_kg_stats_direct() -> dict | None:
    """Dispatcher mirroring `read_kg_direct`: AGE under postgres, else
    None so the `/graph` handler falls back to the MCP `kg_stats` call.

    Returning ``None`` for the chroma branch (rather than reading sqlite
    here) keeps the legacy MCP-tool path authoritative for chroma palaces
    — `read_kg_postgres_stats` is the only place the daemon needs to
    bypass the legacy RELATION counts that broke under postgres.
    """
    if getattr(_config(), "backend", None) == "postgres":
        return read_kg_postgres_stats()
    return None


def read_kg_direct(
    entity_limit: int = 500,
    triple_limit: int = 1000,
    mention_limit: int = 1000,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Read-only snapshot of KG entities, semantic triples, and mention edges.

    Returns ``(entities, triples, mentions)``. The third slot is the
    1.8.2 split — pre-1.8.2 the postgres branch packed MENTIONS edges
    into the ``triples`` slot, conflating "Drawer mentions Entity"
    (5.66M atemporal mention links) with "Entity—predicate—Entity"
    (real semantic facts, currently ~1 placeholder).

    Under the chroma backend the KG lives in a sibling SQLite file
    (`knowledge_graph.sqlite3`); a read there does not cross the
    single-writer invariant. Schema differences (older palaces,
    in-progress migrations) are tolerated by catching OperationalError
    on each query. There is no MENTIONS concept in the chroma KG, so
    the third tuple element is always an empty list under chroma.

    Under the postgres backend the KG lives in AGE (the `mempalace_kg`
    graph). We dispatch to ``read_kg_postgres`` which runs three
    Cypher queries: entities via ``MATCH (e:Entity)``, semantic triples
    via ``MATCH (a:Entity)-[r:RELATION]->(b:Entity)``, mentions via
    ``MATCH (d:Drawer)-[r:MENTIONS]->(e:Entity)``. Limits bound /graph
    latency on the full palace; UI surfaces these as samples, not full
    exports.
    """
    if getattr(_config(), "backend", None) == "postgres":
        return read_kg_postgres(
            entity_limit=entity_limit,
            triple_limit=triple_limit,
            mention_limit=mention_limit,
        )
    path = kg_path()
    if not os.path.isfile(path):
        return [], [], []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return [], [], []

    entities: list[dict] = []
    triples: list[dict] = []
    try:
        try:
            for r in conn.execute(
                "SELECT id, name, type, properties FROM entities LIMIT ?",
                (int(entity_limit),),
            ):
                try:
                    props = json.loads(r["properties"] or "{}")
                except (TypeError, ValueError):
                    props = {}
                entities.append({
                    "id": r["id"],
                    "name": r["name"],
                    "type": r["type"] or "unknown",
                    "properties": props,
                })
        except sqlite3.OperationalError:
            pass
        try:
            for r in conn.execute(
                "SELECT subject, predicate, object, valid_from, valid_to, "
                "confidence, source_file FROM triples LIMIT ?",
                (int(triple_limit),),
            ):
                triples.append({
                    "subject": r["subject"],
                    "predicate": r["predicate"],
                    "object": r["object"],
                    "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"],
                    "confidence": r["confidence"],
                    "source_file": r["source_file"],
                })
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    # chroma KG has no mention-edge concept — third slot is always []
    return entities, triples, []
