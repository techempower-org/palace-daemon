# Hybrid / age-fused retrieval latency — AGE edge-walk seq scans

**Date:** 2026-05-30
**Branch:** `perf/7b-hybrid-latency`
**Context:** SME Cat 7b — "hybrid retrieval is slow." Reported p50 vector 626ms /
union 429ms / **hybrid 2064ms** (p95 5.6s), hybrid ~3-5× the others.
**Measured against:** prod `familiar:8085` (postgres backend, AGE KG), read-only.

## TL;DR

The entire hybrid-vs-union latency delta is **AGE graph-walk Cypher**, not rerank,
not candidate-pool size, not the vector/BM25 fusion. Each per-entity edge lookup
the hybrid candidate-merger and `/search/age-fused` issue does a **full parallel
seq scan of the edge backing table** — MENTIONS (6.69M rows) or RELATION (1.92M
rows) — because AGE never indexes the `start_id` / `end_id` graphid columns the
walks join on. Fix: add the four missing edge-endpoint btree indexes.

## Profile (prod familiar, n=36 per arm, 12 queries × 3 reps)

| Path | p50 | p95 | max | notes |
|---|---:|---:|---:|---|
| `/search` (vector) | 2025ms | 4900ms | 6105ms | rerank ~14ms; cost is the vector candidate path |
| `/search/hybrid?strategy=union` | 344ms | 545ms | 546ms | vector ∪ BM25 — **fast and stable** |
| `/search/hybrid` (default) | 1624ms | 4834ms | 4853ms | vector ∪ BM25 ∪ **graph** |
| `/search/age-fused` | 1792ms | 5995ms | 5995ms | vector + AGE MENTIONS RRF |

Key observations that isolate the cause:

1. **union (vector ∪ BM25) is ~13× faster than hybrid** at the *same retrieval* —
   both return identical `sources={drawer:10}` with `n_input=10` into rerank. The
   only difference is hybrid's graph candidate-merger.
2. **Rerank is not the cost.** Hybrid with `rerank=false` is still ~4.6s; rerank
   itself logs 10-225ms (`n_input=10`). Turning it off changes nothing material.
3. **The graph frequently surfaces zero useful candidates** yet still pays the
   full cost — the expansion runs, scans the table, and contributes nothing to
   the result set on most queries.

## Root cause — `EXPLAIN ANALYZE` on the live graph

Direct timing of the graph-expansion functions (daemon venv, prod DSN):

```
connect + LOAD age + search_path            :    9 ms
one MENTIONS per-entity Cypher (cold)       : 5792 ms   <- the pain
one RELATION per-entity Cypher              : 4134 ms
_graph_expand_from_seeds (2+N cyphers)      : 8355 ms
```

`EXPLAIN ANALYZE` of the daemon's `_age_lookup` MENTIONS Cypher
(`MATCH (d:Drawer)-[r:MENTIONS]->(e:Entity {name:'MemPalace'})`):

```
Parallel Hash Join  (Hash Cond: d.id = r.start_id)
  -> Parallel Seq Scan on "MENTIONS" r  (rows=2230501 loops=3)   <- full 6.69M-row scan
  -> Hash Join (Hash Cond: r.end_id = e.id)
       -> Bitmap Index Scan on idx_entity_name  (rows=1)         <- Entity side IS indexed
```

The Entity filter is fast (GIN `idx_entity_name`, ~12ms). The killer is the edge
table: AGE only creates a btree on each label table's own `id`
(`_ag_label_edge_pkey`). There is **no index on `MENTIONS.start_id` / `end_id`**
(nor on `RELATION`). Every per-entity walk seq-scans the whole edge table.

The hybrid RELATION walk is worse: `MATCH (a:Entity)-[r:RELATION]->()` with an
**anonymous target `()`** forces AGE to build an `Append` over *every* vertex
label (Drawer+Entity+Room+Wing ≈ 1.58M rows), `Materialize` it, then `Nested
Loop` to bind the target — "Rows Removed by Join Filter: 29,968,252", spilling to
the container's 64MB `/dev/shm` (the `could not resize shared memory segment`
errors operators have seen).

Edge table sizes on prod:

```
"Entity":   1,156,190
"RELATION": 1,921,600
"MENTIONS": 6,691,502
"Drawer":     424,957
```

## What does NOT help (negative results, recorded so they aren't re-tried)

- **Parallelizing the per-entity loop.** `_age_lookup` runs one Cypher per query
  entity serially. Running 4 concurrently (thread pool, 4 connections) was
  *slower* (750ms) than serial (544ms): the queries are parallel-seq-scans that
  already saturate Postgres workers + I/O, so concurrency adds contention (one
  query jumped 197ms→738ms under parallel load). Do not parallelize without the
  index in place — it makes p95 worse under concurrency.
- **Rewriting the Cypher to direct SQL on the label tables.** AGE compiles to the
  same plan; the seq scan stays. Only an index changes the plan.

## Fix — the four missing edge-endpoint indexes

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mentions_end_id   ON mempalace_kg."MENTIONS" (end_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_mentions_start_id ON mempalace_kg."MENTIONS" (start_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_relation_start_id ON mempalace_kg."RELATION" (start_id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_relation_end_id   ON mempalace_kg."RELATION" (end_id);
```

These are what `mempalace.backfill_age` *should* create alongside the Drawer.id
unique index (`knowledge_graph_age._ensure_drawer_unique_index`) but does not.

### Validation on a synthetic AGE graph

Built a throwaway `apache/age:release_PG16_1.6.0` graph mirroring prod's index
situation (GIN on `Entity.properties`, no MENTIONS index), scaled to **3.17M
MENTIONS / 115K Entities / 42K Drawers** (~half prod scale).

**Plan flip** (the decisive evidence — the seq scan disappears):

```
BEFORE:  Parallel Seq Scan on "MENTIONS" r   cost=13855  (full table)
AFTER:   Bitmap Index Scan on idx_mentions_end_id  cost=831  (rows matched only)
```

**Cold-cache wall clock** (container restarted between runs to force disk reads):

```
COLD before index (hot entity): 320 ms
COLD after  index (hot entity): 206 ms     (1.6× at half-prod scale)
```

The seq-scan cost grows **linearly with the edge table size**; the index scan
grows with the **result set**. So the delta widens at prod's 2× scale, and widens
further under the cold/contended conditions that produced the observed p95 5.6s
(warm a single MENTIONS Cypher is ~278ms; cold/contended it was 5792ms).

## How to apply (operator action — not auto-applied)

The daemon does **not** create these on startup (no silent DDL on prod). Apply via
the new operator-triggered route, which uses `CREATE INDEX CONCURRENTLY` so the
build never takes a table lock against live `/search/hybrid` reads:

```
curl -X POST http://familiar:8085/backfill-age/indexes -H "X-API-Key: $PALACE_API_KEY"
# -> {"status":"ok","created":[...],"already_present":[...],"errors":{}}
```

Idempotent (skips indexes already present). Or apply `scripts/age_graph_indexes.sql`
directly during a maintenance window.

## Follow-up (mempalace, separate repo)

1. **Index creation belongs in `mempalace.backfill_age`** — add an
   `_ensure_edge_endpoint_indexes()` alongside `_ensure_drawer_unique_index()` so a
   fresh backfill ships the indexes. This daemon route is the bridge until that lands.
2. **The anonymous-target `-[r:RELATION]->()` pattern** in
   `searcher._graph_expand_from_seeds` / `_graph_expand_from_entities` should bind
   or drop the target so AGE stops materializing all vertices. Even with the
   `start_id`/`end_id` indexes, the unbound `()` invites the all-vertex Append.
3. **Container `/dev/shm` is the default 64MB** on `mempalace-db`; raise
   `--shm-size` so any residual parallel hash join doesn't spill-fail. Infra change,
   coordinate separately.
