-- AGE edge-endpoint indexes for the hybrid/age-fused graph-walk paths.
--
-- Cat 7b (perf/7b-hybrid-latency): /search/hybrid p50 was ~3-5x /search and
-- /search/keyword (measured 2026-05-30 on prod familiar: vector 626ms p50,
-- union 429ms p50, hybrid 2064ms p50, p95 5.6s). Profiling traced the entire
-- delta to AGE graph-walk Cypher that the hybrid candidate-merger and the
-- daemon's /search/age-fused `_age_lookup` issue against the MENTIONS / RELATION
-- edge tables.
--
-- Root cause: AGE compiles `MATCH (d:Drawer)-[r:MENTIONS]->(e:Entity {name:X})`
-- and `MATCH (a:Entity)-[r:RELATION]->() WHERE a.name=X` to joins on the edge
-- backing tables' `start_id` / `end_id` graphid columns — but AGE only creates
-- a btree on the label table's own `id`. There is NO index on `start_id` or
-- `end_id`. Every per-entity edge lookup therefore parallel-seq-scans the whole
-- edge table:
--
--   * MENTIONS: 6.69M rows  (Drawer->Entity mention links)
--   * RELATION: 1.92M rows  (Entity->Entity semantic edges)
--
-- EXPLAIN ANALYZE on the live graph (entity_name='MemPalace'):
--   Parallel Seq Scan on "MENTIONS" r  (rows=2230501 loops=3)   <- full table
--   ... 5792ms cold / 278ms warm for a single entity, x N query entities.
--
-- These indexes let the planner walk Entity (idx_entity_name GIN, already
-- present) -> MENTIONS(end_id) / RELATION(start_id) via a bitmap index scan
-- instead of a full seq scan. Validated on a synthetic 3.17M-edge AGE graph
-- (apache/age:release_PG16_1.6.0, mirroring prod's index situation): the
-- MENTIONS seq scan flips to `Bitmap Index Scan on idx_mentions_end_id`
-- (cost 13855 -> 831; cold wall-clock 320ms -> 206ms at half-prod scale, and
-- the seq-scan cost grows linearly with the table while the index scan grows
-- with the result set — so the delta widens at prod's 2x scale).
--
-- Idempotent: every statement is CREATE INDEX IF NOT EXISTS. Safe to re-run.
-- Run via the daemon's `POST /backfill-age/indexes` route (which uses
-- CREATE INDEX CONCURRENTLY so it never takes a table lock against live reads),
-- or apply this file directly during an offline maintenance window:
--
--   psql "$MEMPALACE_POSTGRES_DSN" -f scripts/age_graph_indexes.sql
--
-- NOTE: this plain file uses non-CONCURRENT CREATE INDEX (it must run inside a
-- maintenance window or a fresh graph). The daemon route issues the
-- CONCURRENTLY variant for online use. Both produce identical indexes.

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- MENTIONS (Drawer -> Entity). The /search/age-fused path filters Entity by
-- name then walks `end_id`; the hybrid NER path walks `start_id` (r.source).
CREATE INDEX IF NOT EXISTS idx_mentions_end_id
    ON mempalace_kg."MENTIONS" (end_id);
CREATE INDEX IF NOT EXISTS idx_mentions_start_id
    ON mempalace_kg."MENTIONS" (start_id);

-- RELATION (Entity -> Entity). The hybrid seed-expansion path walks both
-- directions (#291 split the bidirectional match into two directional queries);
-- both endpoints need an index for the planner to avoid the all-vertex
-- nested-loop Append it currently falls back to.
CREATE INDEX IF NOT EXISTS idx_relation_start_id
    ON mempalace_kg."RELATION" (start_id);
CREATE INDEX IF NOT EXISTS idx_relation_end_id
    ON mempalace_kg."RELATION" (end_id);

ANALYZE mempalace_kg."MENTIONS";
ANALYZE mempalace_kg."RELATION";
