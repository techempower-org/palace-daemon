# mempalace_drawers judged-parent indexes: (metadata->>'parent_drawer_id') and (metadata->>'parent_entry_id')

Two schema changes on the production palace, built 2026-11-21 17:15 and 17:16. The fleet lead approved them at
13:58 that day.

## Rule: every expression index is followed by ANALYZE, and the plan check runs after it

An index on an expression (`((metadata->>'key'))`) has **no statistics until ANALYZE runs on its table**. Until
then, the planner guesses its selectivity from a default. Here it estimated 15,239 matching rows for two ids, so it
kept walking the primary key and the new index went unused.

The order is: `CREATE INDEX CONCURRENTLY`, then `ANALYZE <table>` (lift the role's `statement_timeout` for that
session; see [manual-vacuum.md](manual-vacuum.md)), then a plain `EXPLAIN` of the query the index is for. The index
is only done when that plan uses it. A plain column index such as (wing, id) needs no ANALYZE, because the
columns already have statistics.

## Why

The search path hides results whose logical parent is judged out (mempalace `searcher/query.py`). For ids not
found directly, it asks once per key in `_PARENT_ID_KEYS = (parent_drawer_id, parent_entry_id)`:

    SELECT id, NULL::text, wing, room, metadata FROM "mempalace_drawers"
    WHERE (metadata->>$1 IN ($2, …)) ORDER BY id LIMIT 256

Neither key had an index; only `source_file` did. The ids are missing by construction, so the primary-key walk never
found its 256 rows early. It read all 1.5 M rows until the role's 30 s `statement_timeout` cancelled it.

- **Timeouts:** 104 metadata-lookup cancellations in the 24 h before the build, the largest timeout kind.
- **Search latency:** a search that hit both keys waited for both cancels. Three searches in a row took 64,684 ms,
  364 ms and 370 ms. The search latency was this cancel wait, not the drain.

## Change

    CREATE INDEX CONCURRENTLY mempalace_drawers_meta_parent_drawer_id_idx ON public.mempalace_drawers ((metadata->>'parent_drawer_id'));
    CREATE INDEX CONCURRENTLY mempalace_drawers_meta_parent_entry_id_idx ON public.mempalace_drawers ((metadata->>'parent_entry_id'));
    ANALYZE public.mempalace_drawers;

- The two builds ran one after the other, with the same guard, give-up time (00:55) and failure handling as the
  (wing, id) index ([its run-book](mempalace-drawers-wing-id-index.md)).
- Rehearsal on a throwaway postgres covered both keys: an INVALID leftover dropped and rebuilt, a rerun, the
  rollback, and an unknown key refused. It failed with the INVALID-leftover drop skipped.
- Each build: 1 min, 10 MB (about 98.6 % of rows have no such key), valid.
- The build script's own plan check then reported that the plan did **not** use the new index. ANALYZE took 5.7 s,
  and afterwards both plans used it.

## Rollback

    DROP INDEX CONCURRENTLY IF EXISTS public.mempalace_drawers_meta_parent_drawer_id_idx;
    DROP INDEX CONCURRENTLY IF EXISTS public.mempalace_drawers_meta_parent_entry_id_idx;

## Measured

| | before | after the build, before ANALYZE | after ANALYZE |
|---|---|---|---|
| plan, parent_drawer_id (2 ids) | pkey walk + filter, cost 26,289 | the same | Index Scan on its index, cost 329 |
| plan, parent_entry_id (1 id) | pkey walk + filter, cost 52,574 | the same | Index Scan on its index, cost 208 |
| three searches in a row | 64,684 / 364 / 370 ms | | 2,283 / 338 / 348 ms |

## Not verified

- The timeout count after the build: a 24 h count against the 104 before, on Flow row f256ea3a.
- A fresh palace: the mempalace backend creates these two indexes (and runs ANALYZE) in its own PR.
