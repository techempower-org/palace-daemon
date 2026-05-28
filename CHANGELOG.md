# Changelog

## 1.8.4 — 2026-05-27

### Fixed — *watchdog no longer starves the systemd keepalive during a rebuild*

`_watchdog_loop` is health-gated: it withholds `WATCHDOG=1` whenever the
palace `_get_collection()` probe returns `None` or throws, so a genuinely
wedged daemon gets killed and restarted by systemd. But a `mode=rebuild`
repair holds `_exclusive_palace()` with the client/collection caches
nulled for the entire operation (6-9h on a large palace — see the
`/repair` handler). During that window the probe returns `None`, so the
health-gate would withhold the keepalive and systemd would **SIGABRT the
daemon mid-rebuild** — the most destructive possible moment for a kill.

The loop now detects `in_progress + mode == "rebuild"` and sends
`WATCHDOG=1` **unconditionally**, skipping the probe entirely. Outside a
rebuild (and for non-rebuild repairs like `light`/`scan`/`prune`, which
don't null the caches) the original health-gated behavior is preserved.

This is currently **latent** on our deployment — the `palace-daemon.service`
unit ships without `WatchdogSec=`, so the loop doesn't run — but it is a
footgun: adding a watchdog timer (a natural hardening step for a
`Restart=always` service) would otherwise turn every long rebuild into a
kill. Mirrors the philosophy of upstream `0315d97`, adapted to this fork's
differently-structured (health-gated) loop.

Triage notes for the rest of the upstream watchdog/stats batch:
`c61f2ba` (serialize `/stats` tool calls) targets chroma's HNSW SIGBUS
under concurrent reads and is **N/A** on the postgres backend (serializing
would only add latency); the crash-loop detection from `aa9320d` (#21) is
**already present** here, with auto-recovery that upstream lacks.

## 1.8.3 — 2026-05-27

### Fixed — *`/mine` no longer corrupts the chroma log store (#29)*

`POST /mine` spawns `mempalace mine` as a subprocess, which opens its
**own** ChromaDB `PersistentClient` on the palace path while the daemon
already holds one. ChromaDB 1.x's Rust backend cannot tolerate two
`PersistentClient` instances on the same path — in- *or* cross-process —
and the log store corrupts (`Failed to pull logs from the log store`).
The mine then *appears* to succeed (200 OK, CPU spike) but persists zero
drawers; recovery needs `mempalace repair`.

`/mine` is now **backend-aware**:

- **postgres** (our deployment) — **unchanged**. Postgres handles
  concurrent connections natively, so the dual-client corruption cannot
  occur. The subprocess still runs under `_mine_sem`, exactly as before.
- **chroma** — guarded *lock-and-reopen* choreography so only one
  `PersistentClient` touches the files at any instant:
  1. Enter `_exclusive_palace()` — hold every read/write/mine slot so no
     daemon-mediated work races the mine.
  2. **Deterministically release** the daemon's client via
     `close_palace()` — drops the mcp caches *and* calls the real
     `PersistentClient.close()`, releasing chromadb's Rust-side SQLite
     file lock synchronously. (A bare cache drop would leak the lock
     until GC — see mempalace#262 — leaving a stale client locking the
     path when the subprocess opens it, reproducing the corruption.)
  3. Spawn the subprocess — now the sole client.
  4. Reopen the daemon's client in a `finally`, so it is always restored
     even if the mine fails. If the reopen itself throws, caches stay
     `None` and the next request lazily reopens (self-heal); logged
     `CRITICAL`.

This is a proper upstream fix for chroma users (issue filed by the
upstream maintainer); it does not affect the postgres deployment, which
was never susceptible. The deeper fix — mining in-process through the
daemon's single client — is deferred to mempalace#261 (injectable
backend for `miner.mine()`); this guard is the correct interim fix and
remains valid as a fallback.

- **New env knob `PALACE_CHROMA_FLUSH_SECONDS`** (default `0.0`):
  optional settle margin after the deterministic close, for very large
  palaces. `0` relies on the synchronous `close_palace()`.
- **Refactor**: the client-teardown sequence (previously duplicated in
  shutdown and auto-repair) is now a single `_drop_chroma_client(close)`
  helper. `close=True` releases the Rust lock (the new mine path);
  `close=False` keeps the legacy cache-only drop (shutdown, auto-repair).
- **Stale-comment fix**: the shutdown teardown's "no clean close()
  (chroma#5868)" note was outdated — chroma 1.5.x does expose
  `Client.close()`. Shutdown deliberately keeps the cache-only path
  (the process is exiting); the comment now says why.

## 1.8.2 — 2026-05-25

### Changed — *`/graph` splits RELATION triples and MENTIONS edges*

**Breaking change to `/graph` response shape.** 1.8.0–1.8.1 labelled
the Drawer→Entity `MENTIONS` edges as "triples" — both in the
`kg_triples` list and in `kg_stats.triples`. A triple is an
entity→entity *semantic fact* (the `RELATION` label); a mention is a
*provenance link* from a drawer to an entity it names. They are not
the same thing, and the live corpus makes the conflation obvious — ~1
RELATION row vs. ~5.66M MENTIONS edges. Reporting 5.66M "triples"
overstated the size of the actual knowledge graph by six orders of
magnitude.

- **New response field `kg_mentions`**: drawer→entity rows projected
  from `MATCH (d:Drawer)-[r:MENTIONS]->(e:Entity)`. Shape:
  `{subject: drawer-id, predicate: "MENTIONS", object: entity-id,
  source_file: etype, confidence, valid_from: null, valid_to: null}`.
- **`kg_triples` is now real triples only**: projects
  `MATCH (a:Entity)-[r:RELATION]->(b:Entity)` with
  `predicate = r.relation_type`. Returns the ~1 RELATION row in the
  current corpus rather than the 5.66M mentions stream.
- **`kg_stats` schema change**:
  `{entities, triples, mentions, relationship_types}`. Dropped
  `current_facts` / `expired_facts` (`RELATION`-only concepts that
  hard-zeroed under the MENTIONS-dominated AGE backend anyway).
  `relationship_types` is now derived from non-empty counts —
  `["RELATION", "MENTIONS"]` when both are populated, `["MENTIONS"]`
  in the current corpus.
- **Limit semantics**: `?limit=N` now caps entities (×1), triples
  (×2), and mentions (×2). The mentions sample on `/graph?limit=1`
  is intentionally tiny — the field is for debug previews; bulk
  consumers should hit `POST /cypher` directly.
- **Frontend**: `static/viz.html` `renderKGStats` reads
  `entities/triples/mentions` straight from `kg_stats` (the AGE
  backing-table totals) instead of sizing arrays. The D3 force graph
  concatenates `kg_triples ∪ kg_mentions`; the existing
  `idIndex.has(subject) && idIndex.has(object)` filter naturally
  drops MENTIONS rows (drawer ids aren't in the entity index) so
  RELATION continues to dominate the visualization without a
  special case.
- **Test coverage**: `tests/test_graph_wings_dispatch.py` updated to
  13 tests — `TestReadKgPostgresAGE` now stubs three Cypher queries
  (entities / RELATION / MENTIONS) and asserts the 3-tuple return;
  `TestReadKgStatsAGE` covers the new flat schema and adds a new
  test for the empty-RELATION case (current corpus state) where
  `relationship_types` correctly excludes the empty label.

Consumers (SME's `MemPalaceDaemonAdapter`, the local viz dashboard,
ad-hoc `jq` over `/graph`) need to update field references:
`kg_stats.current_facts/expired_facts` → gone; new `kg_mentions`
list available; `kg_triples` will look ~empty until the RELATION
pipeline is wired up. The fork's `/graph` is the only mempalace
deployment carrying these fields, so the blast radius is limited
to JP's downstream consumers.

## 1.8.1 — 2026-05-25

### Fixed — *`GET /graph` `kg_stats` now reflects live AGE counts*

Cosmetic but misleading follow-up to 1.8.0. `/graph` migrated the
`kg_entities`/`kg_triples` lists to AGE but still pulled `kg_stats` from
the legacy `mempalace_kg_stats` MCP tool, which counts the near-empty
`RELATION` table. Result: a `/graph` response that listed 500 sampled
entities and 1,000 sampled MENTIONS edges but reported
`kg_stats.triples: 1` — the single leftover `RELATION` row.

- **AGE-backed stats under `MEMPALACE_BACKEND=postgres`**:
  `_read_kg_postgres_stats` SELECT-counts the AGE backing label tables
  directly through `kg._conn`:

      SELECT count(*) FROM mempalace_kg."Entity"
      SELECT count(*) FROM mempalace_kg."MENTIONS"

  Cypher (`MATCH ()-[r:MENTIONS]->() RETURN count(r)`) is the obvious
  shape but it materializes the full 5.58M-edge scan through AGE's
  agtype wrapper, which exhausts Postgres shared memory
  (`could not resize shared memory segment to 67108864 bytes`).
  Counting the backing table reaches the same answer at SQL-table-scan
  speed (the visibility map allows an index-only count). AGE preserves
  identifier case, so the quoted-identifier table names are required.

  Projection: `{entities, triples, current_facts, expired_facts: 0,
  relationship_types: ["MENTIONS"]}`. `expired_facts` is hard-zero —
  the daemon doesn't carry temporal expiry on MENTIONS edges (it was a
  `RELATION`-table concept under the chroma KG).

- **Dispatcher**: `_read_kg_stats_direct` returns the AGE payload under
  postgres and `None` under chroma. `None` keeps the legacy MCP path
  authoritative for chroma palaces rather than forcing the AGE branch.

- **`/graph` wiring**: the gather block now fans out a third direct task
  (`kg_stats_direct_task`) alongside wings/rooms and KG entities/triples.
  Final field is `kg_stats_age or _unwrap(kg_stats_resp) or {}` —
  postgres palaces get live AGE counts, chroma palaces get the MCP tool,
  unreachable AGE degrades to the MCP fallback rather than crashing.

- **Test coverage**: 4 new tests in `tests/test_graph_wings_dispatch.py`
  pin the projection, the no-DSN degrade-to-None branch, Cypher-failure
  degrade-to-zero, and the chroma/postgres dispatch split. Stubs
  `KnowledgeGraphAGE` via `sys.modules` injection — no live Postgres
  needed.

Verified live on familiar: `kg_stats` now reports the real entity +
MENTIONS counts; sampled `kg_entities`/`kg_triples` lists stay
consistent with the headline figures.

## 1.8.0 — 2026-05-25

### Changed — *`GET /graph` KG section migrated from sqlite to live Apache AGE*

Resolves roadmap item #3 from the [2026-05-25 backfill
milestone](#milestone--2026-05-25--age-knowledge-graph-backfill-complete-629k-nodes--595m-edges)
("Switch `GET /graph`'s KG section from `knowledge_graph.sqlite3` to a
live AGE `MATCH ... RETURN ...`"). Before this release, `/graph` served
the legacy `~/.mempalace/knowledge_graph.sqlite3` snapshot — a stale
shadow that holds a handful of `RELATION` rows from the pre-backfill
write-through path. The live KG (264k entities, 5.58M `MENTIONS` edges)
has lived in Postgres + Apache AGE since the 2026-05-25 backfill, so
consumers were getting the wrong picture.

- **AGE-backed KG read under `MEMPALACE_BACKEND=postgres`**:
  `_read_kg_postgres` now drives two Cypher queries via
  `KnowledgeGraphAGE._run_cypher` (the same internal path `POST /cypher`
  and `POST /search/age-fused` use):

      MATCH (e:Entity) RETURN e.name AS name LIMIT $n

      MATCH (d:Drawer)-[r:MENTIONS]->(e:Entity)
      RETURN d.id AS subject, e.name AS object,
             r.count AS count, r.etype AS etype,
             r.confidence AS confidence
      LIMIT $n

  The MENTIONS projection lands in the `kg_triples` slot with the
  existing keys preserved: `subject=drawer.id`, `predicate="MENTIONS"`,
  `object=entity.name`, `confidence=r.confidence`, `source_file=r.etype`
  (re-used as the entity-type tag), `valid_from`/`valid_to`=null
  (MENTIONS edges are atemporal).
- **New `?limit=N` query param on `GET /graph`** (default 500, max
  50000): caps the entity-row count and applies 2× this to triples.
  The full 264k-entity / 5.58M-edge graph is too large for a single
  response — callers needing more should query AGE directly via
  `POST /cypher`. The chroma sqlite branch also honors the limit so
  /graph stays bounded under either backend.
- **Response shape unchanged**: all six top-level keys (`wings`,
  `rooms`, `tunnels`, `kg_entities`, `kg_triples`, `kg_stats`) keep
  their existing schema; SME's `MemPalaceDaemonAdapter` and the `/viz`
  D3 force-graph need no client-side change.
- **Tests**: two new cases in `tests/test_graph_wings_dispatch.py`
  (`TestReadKgPostgresAGE`) stub `KnowledgeGraphAGE` to pin the Cypher
  text, the `LIMIT $n` bindings, and the row → response-key projection.
- **Docs**: `docs/graph-endpoint.md` updated — the "2026-05-25 update"
  pending-migration banner is replaced by a "shipped in 1.8.0"
  historical note documenting the new `limit` parameter.

### Added — *`/backfill-age/status` exposes unprocessed-drawer breakdown*

`GET /backfill-age/status` now returns two additional keys, `unprocessed_drawers` (int) and `unprocessed_reason_codes` (dict, nonzero buckets only), exposing drawers present in `mempalace_drawers` but missing from the AGE backfill checkpoint. Buckets — `added_during_run` / `added_after_run` / `pre_run_unmarked` / `no_filed_at` — distinguish "expected gap from a streaming-cursor snapshot pre-dating new ingest" (the dominant cause on a healthy palace) from "rows the run failed to mark" (which warrants log review). Diagnosis on the live palace: 1,676 `added_during_run` + 1 `added_after_run`, all storyvox ingest landing during the backfill window. Backed by a single CTE+anti-join query with `SET LOCAL statement_timeout='10s'`. Tests in `tests/test_backfill_unprocessed.py`.

## [Unreleased]

### Added — 2026-05-28 — *`bench-active.lock` pauses auto-mine during external bench runs (#104)*

External bench runs (SME LongMemEval, candidate-strategy ablation, etc.) drive the daemon hard. The WatcherService-spawned auto-mine running concurrently with the bench contributed to today's morning postgres OOMs (#97, #102) and the daemon SIGTERM cycle root-caused upstream. This lands a file-lock contract so the bench runner can pause auto-mine without restarting the daemon (which would be catastrophic mid-bench).

- **Lock file**: default `<palace_data_dir>/.bench-active.lock`; override via `PALACE_BENCH_LOCK_PATH`.
- **Daemon behavior**: `WatcherService._internal_mine` checks `_bench_lock_active()` at every spawn. If present and fresh, the daemon **skips** spawning a new mine and logs a single INFO line (`watcher: auto_mine_paused (reason=...)`). Doesn't fail; just defers until the next watch event after the bench finishes.
- **Stale-lock auto-cleanup**: a lock older than `PALACE_BENCH_LOCK_MAX_AGE_SECONDS` (default 6 h) is ignored, so a crashed bench can't wedge auto-mine indefinitely.
- **`scripts/bench-lock.sh`** — operator/bench-runner CLI:
  ```
  scripts/bench-lock.sh acquire    # touch the lock
  scripts/bench-lock.sh release    # remove the lock
  scripts/bench-lock.sh status     # present/absent + age
  ```
  Picks the lock path the same way the daemon does (`PALACE_BENCH_LOCK_PATH` env, else `$PALACE_DATA/.bench-active.lock`, else `/srv/mempalace-data/palace/.bench-active.lock`).
- **9 tests** in `tests/test_bench_lock.py` covering path resolution (env override, default, fallback when `_mp._config` is unavailable), lock detection (present/absent), stale-lock auto-ignore, custom max-age override + non-numeric fallback, and graceful degradation on unreadable paths.

Closes [#104](https://github.com/techempower-org/palace-daemon/issues/104). The companion change on the SME side (touch the lock around each bench run) is tracked upstream.

### Added — 2026-05-28 — *`/search/hybrid` accepts `fusion_mode` (#105)*

mempalace#162 (merged as #295) added `fusion_mode="rrf"` as an opt-in alongside the default convex blend in `search_memories`. The internal A/B finding favors convex on a 3K-drawer local palace, but the corpus-level test that matters needs the production 402K-drawer palace. This adds the daemon-side surface so callers can pass `fusion_mode` through `/search/hybrid`:

```json
POST /search/hybrid
{
  "query": "...",
  "fusion_mode": "rrf"   // optional; "convex" default
}
```

Forward-compatible: the daemon accepts and forwards `fusion_mode` via the existing `mempalace_search` MCP envelope. End-to-end effect is gated on **mempalace#302** (companion change adding `fusion_mode` to the MCP input schema + threading through `tool_search` to `search_memories()`). Until that lands, the value is dropped by mempalace's MCP whitelist — the daemon-side accept/validate is in place so when mempalace#302 ships, no further palace-daemon change is needed.

6 tests in `tests/test_search_hybrid_fusion_mode.py` covering: omitted (not forwarded), convex (forwarded), rrf (forwarded), invalid string (400), non-string (400), and explicit null (treated as omitted).

Closes [#105](https://github.com/techempower-org/palace-daemon/issues/105).

### Fixed — 2026-05-28 — *db_errors ring buffer populated from fast-status path + fast-intercept fallback (#108)*

#99 landed the DB-error ring buffer and `_connect_postgres()` records, but three daemon-side paths still touched postgres without recording on failure: `_fast_status_payload` (direct `psycopg2.connect`), the `/mcp` fast-intercept fallback (caught generic `Exception` without classification), and `/status/fast` (calls through `_fast_status_payload`). Net effect: a postgres flap could leave `/health.db_errors.by_pattern` empty while the daemon logged the warning, undercutting the observability promise of #97/#99.

- `_fast_status_payload` now wraps `psycopg2.connect()` with a try/except that calls `_record_db_error(e)` on `OperationalError`, then re-raises so existing callers keep their behaviour.
- The `/mcp` fast-intercept's `except Exception` clause now checks for `psycopg2.OperationalError` and records when matched (mirroring the pattern from `/mcp`'s daemon-native dispatch).
- 5 tests in `tests/test_db_error_integration.py` covering both records-on-error paths and the negative cases (missing DSN, non-DB errors) that should *not* populate the buffer.

No behavioural change beyond the ring buffer being populated more completely; HTTP status codes, fallback paths, and log messages are all preserved.

Closes [#108](https://github.com/techempower-org/palace-daemon/issues/108).

### Added — 2026-05-28 — *DB-error observability + postgres memcg pressure canary (#97)*

Today's morning OOM cluster (postgres killed twice inside its docker memcg at 08:57 + 09:19 PDT, surfaced as 26+ `OperationalError: connection is closed` events) was invisible to `/health` — the daemon process stayed up while in-flight queries returned errors. Same silent-failure-under-healthy-surface shape #92 was filed to close, just for the postgres dependency. Three hooks land here so the next time it happens the operator sees it.

- **DB-error ring buffer + `/health` summary.** Every `psycopg2.OperationalError` caught by `_connect_postgres()` or the `/mcp` daemon-native-tools dispatch is classified by surface message and recorded into a bounded deque (max 1000 entries, ~100 KB ceiling). The `/health` response now carries a `db_errors` block:
  ```json
  "db_errors": {
    "total_last_window": 0,
    "window_seconds": 300,
    "by_pattern": {},
    "newest_ts": null
  }
  ```
  Pattern buckets: `in_recovery` / `connection_closed` / `server_closed` / `connection_lost` / `connect_failed` / `timeout` / `other` — matches what the 2026-05-28 journal grep cataloged. Lock-guarded for thread-safe snapshots under concurrent error recording.
- **Postgres memcg pressure canary at startup.** Lifespan logs the postgres container's docker stats: INFO below threshold, WARNING above. Threshold tunable via `PALACE_POSTGRES_MEMCG_WARN_PERCENT` (default 75 %; today's OOMs happened at 81 % sustained so 75 gives ~5-15 min lead time).
- **Postgres memcg in `/health`.** `/health` adds a `postgres_memcg` block (`{container, usage, limit, percent, probed_at}`) when docker stats succeeds. Probe is bounded at 2 s with defensive guards against null fields and TypeError; failure modes degrade gracefully (field omitted, `/health` stays green).
- **Container name override** via `PALACE_POSTGRES_CONTAINER` (default `mempalace-db`), threaded through canary → probe.
- **33 tests** in `tests/test_observability_hooks.py` covering pattern classification (8 cases including `connect_failed` for psycopg2's "connection refused"), ring-buffer windowing + bounding + truncation, docker-stats happy path + 4 failure modes + null-field guards, canary INFO/WARN/skip flows, env-threading, tz-aware UTC timestamps, lock presence, and the `_connect_postgres()` → `_record_db_error()` integration.

Closes [#97](https://github.com/techempower-org/palace-daemon/issues/97); the postgres-memcg-tuning question (raising the cgroup limit so the OOM doesn't happen in the first place) is a separate concern, not addressed here.

### Added — 2026-05-28 — *deploy-resilience: rsync backup + drift canary + Syncthing keepalive (#92)*

The 2026-05-28 Syncthing outage (clean exit at 07:55 PDT, no auto-restart, ~1.5 h of mempalace work undeployed before anyone noticed) exposed three gaps in the deploy story. This change closes all three.

- **`scripts/rsync-mempalace.sh`** — backup deploy path mirroring `scripts/deploy.sh`'s shape. Pushes the local mempalace tree to the deploy host via rsync (`--delete`, `--exclude __pycache__/`, etc.), restarts the daemon, polls `/health`, optionally runs `scripts/verify-routes.sh`. Same env-var + config-file knobs (`PALACE_HOST`, `PALACE_API_KEY`, `MEMPALACE_LOCAL_DIR`, `MEMPALACE_REMOTE_DIR`, etc.) so the existing `scripts/deploy.conf` works unchanged.
- **Daemon-startup drift canary** — `main.py` now logs the deployed `mempalace/__init__.py`'s mtime + age on every restart:
  - INFO when fresh (`mempalace canary: /path (mtime YYYY-MM-DDTHH:MM:SS, age 5.0h, warn-threshold 24.0h)`)
  - **WARNING** when stale beyond the threshold, with a pointer to `scripts/rsync-mempalace.sh`
  - Threshold tunable via `PALACE_CANARY_WARN_HOURS` (default 24)
  - Defensive: missing `__file__` / `getmtime` failure / non-numeric env → log skip, never crash startup
  - 10 tests in `tests/test_mempalace_canary.py` covering fresh / stale / threshold / fallback / failure modes
- **`scripts/syncthing-keepalive/`** — templated systemd unit + timer that probes `syncthing@<user>.service` every 5 minutes and starts it if clean-exited. `Restart=on-failure` on the Syncthing unit wouldn't have caught today's exit (status=0); the keepalive overlay does without modifying the upstream Syncthing unit. README in the directory walks through installation on familiar.

The three together convert silent staleness into a journal-grep-able signal (canary) backed by a working recovery path (rsync) and proactive watchdog (keepalive). See [#92](https://github.com/techempower-org/palace-daemon/issues/92) for the original gap analysis.

### Added — 2026-05-28 — *daemon-native MCP tools for rooms / wakeup / mined (#93)*

Six new daemon-native tools that close the gap mempalace's CLI hit under daemon-strict mode. The CLI commands `mempalace rooms list/add/rename/remove`, `mempalace wake-up`, and `mempalace mined` all opened a local ChromaDB client and silently broke once the local palace was retired. They now route through `/mcp` to the daemon, which is the single writer to the postgres backend.

| Tool | Args | Returns |
|---|---|---|
| `mempalace_rooms_list` | `{}` | `[{name, description, added_at}]` — empty list when the table doesn't exist (`UndefinedTable`) |
| `mempalace_rooms_add` | `{name, description?}` | `{action: "added"\|"updated", name}` — uses the `xmax=0` system column |
| `mempalace_rooms_rename` | `{old, new}` | `{old, new, affected_drawers}` — cascades via the existing `ON UPDATE CASCADE` FK |
| `mempalace_rooms_remove` | `{name}` | `{name, removed: bool}` — refuses with `error.data.referencing_drawers` if any drawer references the room |
| `mempalace_mined` | `{wing?, limit?}` | grouped `{sources_by_wing, wing_filter, total_wings, total_sources}` |
| `mempalace_wakeup` | `{wing?}` | `{text, tokens, wing}` — delegates to `mempalace.layers.MemoryStack().wake_up(wing=...)` |

**Implementation notes:**
- Hangs off the existing `/mcp` fast-intercept dispatch; the new `_DAEMON_NATIVE_TOOLS` table is additive.
- JSON-RPC error codes: `-32602` (invalid params), `-32004` (custom: backend down), `-32000` (internal). CLI consumer can branch on failure mode.
- Connection pattern matches `_fast_status_payload()`: `psycopg2.connect(dsn, connect_timeout=3)` + `SET LOCAL statement_timeout` + tight `try/finally`.
- All rooms-mutating tools invalidate the in-process `_canonical_rooms_cache` after success.
- 29 tests in `tests/test_daemon_native_tools.py` covering happy/sad paths for every handler.

Companion mempalace work: [mempalace#285](https://github.com/techempower-org/mempalace/issues/285). Once that ships, the CLI commands will pick these up automatically through `_call_daemon_mcp()`.

### Added — 2026-05-28 — *canonical predicate vocabulary mapping for the RELATION edge type*

Collapses the production graph's predicate vocabulary from ~64k freeform
LLM-derived strings to **40 canonicals + ~195 retained code-token raws**.
Until this landed, the graph leg of `mempalace_search?candidate_strategy=hybrid`
was effectively a no-op — most edges' `relation_type` appeared once,
so traversal couldn't find paths.

Migration applied to the production palace on 2026-05-28 covered
**1,761,790 RELATION edges**, mapping **755,034 (42.9%)** to a canonical
and binning **997,519** as `'other'`. The remaining edges already carried
acceptable code-token raws and were left intact.

**The arc, by PR:**

- **#75** — write seam: new `kg_canonical_writepass` module, guarded by
  the default-OFF `MEMPALACE_KG_CANONICAL_WRITETHROUGH` env knob. Every
  new KG triple now goes through the canonicalizer at write time, so the
  vocabulary stops growing freeform. Includes a dry-run migration tool
  (`scripts/canonical_migration.py`) that emits a remap plan from the
  current edges' `relation_type` distribution.
- **#77** — migration `--apply`: replaces the SystemExit stub with a
  real direct-SQL UPDATE on the AGE backing table. Set-based remap with
  a TEMP `predicate_mapping` table joined into a single
  `UPDATE mempalace_kg."RELATION" ... FROM predicate_mapping` —
  **~63k edges/sec** on the production palace. Per-cypher remap (the
  obvious-but-wrong shape) was previously degrading exponentially due to
  MVCC dead-tuple accumulation; we hit AdminShutdown at batch 8 before
  pivoting. Preserves the original `relation_type` under
  `raw_relation_type` so the migration is idempotent and reversible.
- **#85** — batched `CanonicalMapper.map_predicates(list)` for bulk
  callers — the migration tool was the immediate beneficiary, but any
  future code that needs to map many predicates at once now has a
  proper interface rather than a Python loop over single-string maps.

**Embedding-based mapping path** (used during the production migration):
GPU-accelerated MiniLM-L6 ONNX via `onnxruntime-gpu` on a 2080 Ti gave
~65× faster batched embedding than CPU. Threshold 0.45 cosine. A 30/30
A/B test against the CanonicalMapper short-circuit confirmed batched
output matched single-call output exactly.

### Added — 2026-05-28 — *stage-distinguished KG write-through logging*

Closes [#76](https://github.com/techempower-org/palace-daemon/issues/76).
The single `MEMPALACE_KG_WRITETHROUGH` log line conflated two
independent stages — inline `MENTIONS` extraction and the
extraction-queue path. Operators couldn't tell which was on/off from
the startup log.

- **#78** — startup logs each stage distinctly:
  `KG write-through stages: MENTIONS=on (MEMPALACE_KG_WRITETHROUGH); EXTRACTION_QUEUE=off (MEMPALACE_KG_EXTRACTION_QUEUE)`.
  Truthy spellings (`1`/`true`/`yes`/`on` case-insensitive) accepted;
  everything else (including blank/None) reads as `OFF`. 9 tests + 20
  subtests covering both-on, silent-OFF, all truthy variants, blank/None
  safety.
- **#83** — env-value safety: defends against non-string values (`True`,
  `42`) flowing through `os.environ`-shaped overrides by coercing to
  `str(...)` before `.strip()`. Gemini-flagged on #78; fixed without
  changing observable behaviour.

### Changed — 2026-05-28 — *canonical-mapping modules ship from mempalace*

Closes the loop on the PYTHONPATH-strip footgun. Until 2026-05-28, the
KG write-through worker's bare `from kg_canonical_writepass import ...`
silently fell to an identity-fallback because mempalace's
`_strip_leaked_pythonpath_from_sys_path()` (a defensive ABI-hygiene
measure against multi-Python compiled-extension contamination) removed
palace-daemon's source dir from `sys.path` whenever it had been added
via PYTHONPATH. The workaround was a `.pth` file in the venv's
`site-packages` (since `.pth` entries are added through site-init, not
PYTHONPATH).

- **mempalace#290** ports `kg_canonical_writepass`, `kg_canonical_vocab`,
  and `kg_predicate_norm` into the mempalace package itself — they
  resolve via the editable install, not by sys.path discovery, so the
  strip doesn't touch them.
- **#87** reduces the in-repo files to thin re-export shims
  (`from mempalace.kg_canonical_writepass import *`) so historical bare
  imports keep working. New code is expected to import
  package-qualified directly.

### Removed — 2026-05-28 — *`.pth` installer workaround for the PYTHONPATH-strip*

With the canonical-mapping modules now shipping from mempalace
(see above), the `.pth`-into-venv workaround is no longer needed.
**#88** deletes `scripts/install-canonical-pth.sh` and the call from
both `scripts/deploy.sh` and `scripts/auto-repair-if-empty.sh`. Net 201
deletions, 2 insertions. Also fixes a latent
`[ "$RUN_VERIFY" = "1" ] && TOTAL=6` no-op in `deploy.sh` (the .pth
step had silently bumped base `TOTAL` to 6, making the verify-bumps
clause a no-op) by switching to symmetric `+1` arithmetic matching the
existing `PRE_RESTART_HOOK` conditional. Resolves the workaround
side of [#79](https://github.com/techempower-org/palace-daemon/issues/79).

### Removed — 2026-05-28 — *canonical-mapping shim modules (kg_canonical_writepass / kg_canonical_vocab / kg_predicate_norm)*

Closes [#89](https://github.com/techempower-org/palace-daemon/issues/89). The three thin re-export shims from #87 (`from mempalace.kg_canonical_writepass import *`, etc.) are now retired. The bare top-level imports they preserved had no remaining callers in palace-daemon's tree or in mempalace itself (audited 2026-05-28); the shim's "slated for removal once all callers migrate" comment was the last gate.

- Six callers rewritten to package-qualified imports: `tests/test_kg_canonical_vocab.py`, `tests/test_kg_canonical_writepass.py`, `tests/test_kg_predicate_norm.py`, `scripts/canonical_migration.py`, `scripts/canonical_vocab_report.py`, `scripts/predicate_norm_report.py`.
- The tests' `sys.path` hack (`sys.path.insert(0, _ROOT)`) is no longer needed and is removed alongside the imports.
- Shim files deleted: `kg_canonical_writepass.py`, `kg_canonical_vocab.py`, `kg_predicate_norm.py`.
- Behaviour verified by re-running the 56 affected tests against the new imports — pass-rate identical, since the shims were already a no-op `*` re-export.

### Milestone — 2026-05-25 — *AGE knowledge-graph backfill complete (629k nodes / 5.95M edges)*

The `/backfill-age` endpoint (added in commit `b4016c6`) finished its first
full pass on the 273k-drawer production palace. The AGE graph at
`mempalace_kg` is now fully populated and graph-fused retrieval has real
material to traverse.

**Final stats** (from `GET /backfill-age/status` after the run):

- `drawers_seen`: 364,394
- `drawers_skipped_checkpoint`: 352,951 (idempotent resume — most rows were
  already processed in earlier partial runs by parallel workers)
- `entities_added`: 142,315 (this run; cumulative entity total: 263,982)
- `errors`: 0
- `wall_clock_s`: 3,660.9 (~61 min)
- `returncode`: 0
- `progress_pct`: 100.0

**AGE graph shape now visible** (via `POST /cypher`):

| Layer | Nodes | Edges (outgoing) |
|---|---|---|
| Wing | 89 | 197 `CONTAINS` (→ Room), 3,139 `SHARED_VIA` (tunnel edges) |
| Room | 8 (canonical taxonomy) | 365,481 `CONTAINS` (→ Drawer) |
| Drawer | 365,496 | 5,576,602 `MENTIONS` (→ Entity) |
| Entity | 263,982 | — (leaves) |
| **Total** | **629,575** | **5,945,419** |

What this unlocks now that the graph is populated:

- **`POST /search/age-fused`** (Phase 5, shipped 2026-05-17) — the vector ⊕
  AGE entity-overlap RRF fusion path was effectively vector-only against an
  empty graph for the first week. With 5.58M `MENTIONS` edges live, the
  graph-only candidate set is now meaningful. The +5pp R@5 graph-only lift
  on the n=200 git-derived probe spike should reproduce against the full
  palace — re-running the [age-write-through-spike eval](https://github.com/techempower-org/multipass-structural-memory-eval/blob/feat/rlm-adapter/docs/benchmarks/2026-05-17-age-write-through-spike.md)
  is the next planning item.
- **`POST /cypher`** — entity-anchored Cypher queries are now production-
  viable. `MATCH (d:Drawer)-[:MENTIONS]->(e:Entity {name:'familiar'})
  RETURN d` returns real candidate sets, not empty rows.
- **`mempalace_walk_palace`** (MCP tool) — wing/room/entity walks at
  depth 2–3 return non-trivial subgraphs.
- **`GET /graph`** still uses the legacy sqlite KG path; switching it to
  read from AGE is a pending follow-up (sqlite KG is now a stale shadow of
  the AGE state).

**Roadmap follow-ups**:

1. Investigate the 4-drawer discrepancy (`total=364,398` vs `seen=364,394`)
   — likely NULL-content or unicode rows the entity extractor rejected.
   Probably surface as a counter in `/backfill-age/status`.
2. Re-run the age-fused eval against the full palace and capture lift
   numbers in a new benchmark doc under `multipass-structural-memory-eval`.
3. Switch `GET /graph`'s KG section from `knowledge_graph.sqlite3` to a
   live AGE `MATCH (e:Entity) RETURN e LIMIT N` (the daemon already has
   the cypher path internally).
4. Caddy reverse proxy at `palace.jphe.in` returned 502 during this session
   while the daemon was healthy on `localhost:8085` — investigate whether
   upstream timeout / health check is tripping during long-running graph
   queries.

### Added — 2026-05-24 — *Gzip-NCD novelty scoring at drawer write time*

Implements [#45](https://github.com/techempower-org/palace-daemon/issues/45),
derived from the True Memory paper (arXiv:2605.04897, Section 5.3).

- **New module `novelty.py`** — pure-stdlib gzip-NCD scorer. Computes
  Normalized Compression Distance between incoming drawer content and a
  rolling window of recent drawers in the same wing/room. No model deps
  — uses Python's `gzip.compress()` as the scoring function.
- **Wired into `POST /memory`**: novelty scoring runs in parallel with
  the drawer write via `asyncio.gather`, adding zero latency to the
  write path. The response gains a `novelty` block with
  `{enabled, novelty_score, window_size, most_similar_index, status}`.
- **Tag, not a gate**: all drawers are stored regardless of score. The
  `novelty_score` (0=duplicate, 1=novel) is informational metadata for
  downstream retrieval boosting or curation UIs.
- **Toggle**: `PALACE_NOVELTY_ENABLED` env var (default: `"true"`). Read
  live per-request. Window size: `PALACE_NOVELTY_WINDOW` (default: `20`).
- **Graceful fallback**: if the list-drawers call fails, returns
  `novelty_score=1.0` with `status=failed` — the write succeeds either
  way.
- **Tests**: 27 cases in `tests/test_novelty.py` covering NCD math,
  env-var gating, window configuration, edge cases, and async
  integration with mocked `_call`.

### Added — 2026-05-24 — *FlashRank cross-encoder reranking (spike)*

Spike for [techempower-org/familiar.realm.watch#43](https://github.com/techempower-org/familiar.realm.watch/issues/43).
All four `/search*` endpoints now run a neural-rerank pass after
hybrid retrieval and before the response leaves the daemon.

- **New module `rerank.py`** — lazy-loaded singleton FlashRank ranker
  (`ms-marco-TinyBERT-L-2-v2`, ~4 MB ONNX, CPU-friendly). Cached for the
  daemon's lifetime; ~90–100 ms cold load, ~15–40 ms per request for
  n ≤ 20 hits.
- **Endpoints touched**: `/search`, `/search/hybrid`, `/search/keyword`,
  `/search/age-fused`. The reranker pulls `text` (or `document` for
  graph-only stubs) from each hit, scores against the query, and reorders
  in place. Graph-only stubs with no rerankable text sink to the tail.
- **Response contract preserved**: same `results` list, same per-hit
  fields. Each hit gains a `rerank_score` float (numpy scalars coerced
  to JSON-safe Python floats). A new `rerank` block attaches to the
  response with `{enabled, model, n_input, n_reranked, latency_ms, status}`.
- **Toggle**: `PALACE_RERANK_ENABLED` env var (default: `"true"`). Read
  live per-request so operators can flip via systemd `Environment=` +
  restart without a code change. Model override: `PALACE_RERANK_MODEL`.
- **Graceful fallback**: import failure, missing model, or a ranker
  exception during the request all return the original ordering plus
  `status=failed` + reason. The endpoint never hard-errors on rerank
  trouble.
- **Tests**: 15 cases in `tests/test_rerank.py` covering env-var gating,
  empty/None input, graph-stub handling, response-shape preservation,
  load-failure fallback, and a live smoke test against the real ONNX
  model (auto-skipped when `flashrank` isn't installed).
- **Requirements**: `flashrank>=0.2.10` added to `requirements.txt`.

### Added — 2026-05-23 — *Crash-loop detection + `monitor.py`*

Cherry-picked from upstream (`rboarescu/palace-daemon`, implements
[#21](https://github.com/rboarescu/palace-daemon/issues/21)):

- **Crash-loop detection**: tracks restart timestamps in
  `~/.cache/palace-daemon/restart_history.json`. If 3+ restarts occur
  within a 600s rolling window, `/health` returns
  `{"status": "crash_loop", ...}` with HTTP 503.
- **`monitor.py`**: standalone live dashboard that polls `/health`,
  `/stats`, and `/repair/status`. ANSI terminal UI with alerts on
  unreachable, degraded, drawer-count drops, and active repairs.
- Retired `patches/mcp_server_get_collection.patch` — the retry-on-
  cache-failure logic was absorbed into mempalace 3.3.5's
  `_get_collection_chroma` backend.
- **Crash-loop extras** (completes #21 spec): configurable thresholds
  via `PALACE_CRASH_LOOP_THRESHOLD_COUNT`, `_SECONDS`, and
  `_RECOVERY_SECONDS` env vars; auto-exit degraded after 30min clean
  uptime; desktop-notify via `notify-send` on crash-loop detection.
- **Verified backups**: `/backup` now runs `PRAGMA integrity_check` and
  a smoke retrieval on the snapshot; returns `{integrity, smoke_test,
  rows_sampled, status}`.
- **`_READ_TOOLS` sync**: added `mempalace_list_tags`,
  `mempalace_memories_filed_away`, `mempalace_walk_palace`; sorted
  alphabetically; synced against mempalace 3.3.5.
- **Unified routing audit**: all client write paths verified to route
  through daemon HTTP API — no bypasses found.
- **`monitor.py` docs**: added Scripts & tooling table to README.

### Changed — 2026-05-22 — *`/admin/refresh-rooms` response now includes `count`*

Clears the existing `TODO` at `main.py:1488`. The endpoint already
existed (added with the canonical-room validation in Phase 1D,
2026-05-14) but the inline comment still flagged it as outstanding
and the response lacked a count. This change:

- Drops the stale `(TODO)` comment.
- Sharpens the docstring to spell out the cache-clear-then-eager-
  rebuild sequence and reaffirm the single `X-API-Key` auth model
  (no separate admin token — palace-daemon has 27 endpoints, all
  routed through `_check_auth`).
- Adds a `count` field to the JSON response (`{refreshed, rooms,
  count}`) so callers can verify shape without re-counting.
- Adds `tests/test_admin_refresh_rooms.py` — 7 regression tests
  covering cache-clear-before-rebuild ordering, response shape,
  POST-only routing, and `X-API-Key` auth (correct/wrong/missing).

### Added — 2026-05-17 — *`/search/age-fused` endpoint: vector ⊕ AGE graph fusion*

Phase 5 of the multi-project AGE-integration plan (Phases 1-4 + 6 land on
`techempower-org/mempalace:feat/age-kg-parity`). Adds a new POST endpoint
that combines mempalace's vector retrieval with AGE entity-overlap on the
write-through knowledge graph populated by `mempalace.kg_writethrough` +
`mempalace.backfill_age`. Returns RRF-merged results so callers that want
graph-aware retrieval don't have to fuse client-side.

- `POST /search/age-fused` — body: `{query, wing?, room?, limit, graph_top_k,
  fusion_k, include_trace}`. Pipeline:
  1. Vector retrieval via existing `mempalace_search` MCP path (over-fetches
     so RRF has more candidates).
  2. Query entity extraction — tries `sme.extractors.regex.extract` first,
     falls back to `mempalace.kg_writethrough._builtin_regex_extractor`.
  3. AGE lookup — `MATCH (d:Drawer)-[r:MENTIONS]->(e:Entity {name})` for
     each query entity; sum `r.count` per drawer.
  4. RRF fusion — combines vector + graph ranks via `1 / (k + rank)`.
  5. Returns hits with `matched_via ∈ {vector, graph, both}` and `rrf_score`.
- Graceful degradation: missing `MEMPALACE_POSTGRES_DSN` → vector-only with
  warning trace. Empty AGE graph / no extractable entities → vector-only.
  Per-entity Cypher errors → skip that entity, continue.
- `_load_age_extractor()` — cached extractor loader; SME's regex extractor
  preferred for richer two-pass capture, mempalace's builtin as fallback.

Cited from `techempower-org/multipass-structural-memory-eval@28ae3f1`: the
AGE write-through spike on n=200 git-derived probes showed graph-only beats
vector by +5pp R@5 and fusion adds another +4pp on top (file-level vectors).
This endpoint lands that retrieval pattern in production code path, gated
behind the new endpoint so vector-only behavior on the default `/search`
is preserved.

Caveats:
- 503 when `MEMPALACE_BACKEND` is not `postgres`.
- Graph-only hits return a minimal stub (`document=None`); callers fetch
  full drawers via `/memory/{id}` if they need the body.

### Added — 2026-05-15 — *woven warnings/errors pipeline (mempalace#86 daemon side)*

Propagates the new `warnings: list[str]` / `errors: list[str]` fields that
mempalace#86 introduces on drawer-write responses through the daemon's
HTTP surface, and surfaces them inline in the themed `systemMessage` line
that the hook already emits.

- `messages.ensure_warnings_fields(payload)` — shape-normalizer used by
  `/memory` and `/silent-save` to guarantee `warnings` and `errors`
  arrays are present on every write response, even when paired with an
  older mempalace that doesn't emit them. Graceful degradation: no
  crash, just empty lists.
- `messages.save_ok(count, themes, warnings, errors)` — leading glyph now
  reflects the actual outcome: `✦` clean / `⚠` warning / `✕` failed.
  Warning and error texts render on an indented secondary line so they
  read naturally inside the existing chain output.
- `clients/hook.py` — `_theme_save_ok`, `_theme_save_fail`, and
  `_theme_precompact_save` all parse the new fields out of the response
  and render the same `glyph + chain + indented-note` shape. Helper
  functions `_extract_inner` and `_split_outcome` factor the parsing so
  the JSON-RPC envelope and the already-unwrapped `/memory` shape both
  feed the same renderer.
- Tests at [`tests/test_warnings_pipeline.py`](tests/test_warnings_pipeline.py)
  cover the normalizer, the themed `save_ok` body, and the hook renderers
  for each of clean / warn / error.

Visible outcome: when a Stop-hook write hits a non-canonical room (or
HNSW rebuild rejects the write, or any other warning condition mempalace
chooses to surface), the user sees:

```
⚠ Saved with warning — palace → wing:X → room:sessions → drawer:abc@08:48
    room 'diary' is not canonical (canonical: sessions). accepted as-is.
```

instead of the previous silent-failure-then-discover-it-days-later pattern.

### Fixed — 2026-05-15 — *`/graph` wing counts stale after postgres cutover*

`/graph` was reporting wing/room drawer counts from the legacy
`chroma.sqlite3` snapshot even when `MEMPALACE_BACKEND=postgres`. Under
postgres the chroma file is a frozen pre-migration store that no longer
receives writes, so counts ratchet down to whatever was present at
cutover and never refresh — `familiar_realm_watch` reported 25 drawers
in `/graph` against 235 live in postgres (~10× stale).

`_read_wings_rooms_direct` now dispatches by `_mp._config.backend`:
under postgres it runs two cheap `GROUP BY` queries against the indexed
`wing` and `(wing, room)` columns of `mempalace_drawers` (~150 ms each
on the canonical 270K-drawer palace, well under the previous chroma
direct-read budget — small enough to compute live on every call rather
than cache). Under chroma the original sqlite path is preserved.
`_read_kg_direct` also short-circuits to empty under postgres backend
(the live KG is in AGE; the sibling sqlite is the same kind of
pre-migration leftover) so `/graph.kg_entities` no longer surfaces
frozen snapshot data.

Tests at `tests/test_graph_wings_dispatch.py` pin the dispatch and
verify the chroma sqlite path is never opened under postgres.

### Added — 2026-05-13 / 2026-05-14 — *hybrid retrieval endpoints + postgres-direct surface*

After the [techempower-org/mempalace](https://github.com/techempower-org/mempalace) substrate cutover to Postgres + pgvector + Apache AGE landed (2026-05-13/14), the daemon needed to expose the new backend's capabilities over HTTP. Four endpoints added, all postgres-backend-gated (return 503 if `MEMPALACE_BACKEND=chroma`):

- **`POST /search/hybrid`** — vector ∪ BM25 ∪ AGE graph-expanded candidates, hybrid-reranked. Routes through `mempalace.searcher.search_memories(candidate_strategy="hybrid")`. Accepts `query`, optional `wing`, optional `room` (validated against `mempalace_canonical_rooms`), `limit` (1..100), `include_trace` for per-source counts + latencies. Each hit gets a `matched_via` field naming the source (`vector` / `bm25_postgres` / `graph_seeded` / `graph_ner`).
- **`POST /search/keyword`** — postgres-native BM25 only. `tsvector` query via `plainto_tsquery` + ILIKE fallback for underscore identifiers (the `pg_advisory_xact_lock` class of identifier that `to_tsquery` tokenizes wrong). Cheaper than `/search/hybrid` when the caller wants only lexical matches; mirrors the chromadb-era `_bm25_only_via_sqlite` semantics.
- **`POST /cypher`** — direct AGE graph query path. Takes a Cypher string + optional params, returns serialized rows. Behind `PALACE_DAEMON_API_KEY` like every write endpoint. Enables knowledge-graph tooling that needs to traverse the graph without going through MCP.
- **`POST /embed`** — direct embedding endpoint. Wraps `mempalace.embedding.get_embedding_function()` to return vectors for arbitrary text. Used by upstream tools (familiar-side reflection writers, multipass eval adapters) that need stable embeddings without owning an ONNX runtime.

`/memory` now normalizes wing slug (the Phase 1A migration that landed in mempalace) and validates room against the canonical 7-room set at the boundary — returns 400 with the valid set if the caller misuses one.

### Added — 2026-05-14 / 2026-05-15 — *deploy-palace-daemon.sh; broader operational tooling*

- **[`ops/scripts/deploy-palace-daemon.sh`](ops/scripts/deploy-palace-daemon.sh)** — one-shot deployer. Rsyncs to `/var/tmp/palace-daemon-src` (avoiding /tmp tmpfs full conditions that bit us once), then sudo-rsyncs into `/mnt/raid/projects/palace-daemon/`, pip installs requirements into the existing venv at `~/.local/share/palace-daemon/venv`, `systemctl restart palace-daemon`, then polls `/health` until ready. Replaces the previous syncthing-based "edit on katana, syncthing mirrors to disks" deploy flow.
- **Why deploy-script instead of syncthing**: 2026-05-14 03:33 UTC, syncthing produced a `main.sync-conflict-*.py` against disks's older copy, and during conflict resolution the `.git` directory on katana ended up missing `HEAD` + `config` with `objects/` holding unresolvable deltas. Recovered via fresh clone; `~/Projects/.stignore` now globally excludes `.git` to prevent recurrence. Documented in mempalace memory note `reference_pgvector_lazy_index_race.md` adjacent. The takeaway: syncthing is not a deploy primitive for git-tracked work.

### Operational notes — 2026-05-14

- **`PALACE_MAX_WRITE_CONCURRENCY` bumped 1 → 2** (`79a3949`) and hook default `mine_timeout_s` 30 → 60 — concurrent mines from multiple Claude sessions were occasionally timing out under the postgres backend's `CREATE INDEX` window. Raised the cap once the mempalace-side `pg_advisory_xact_lock` fix (mempalace fork commit `4566f8a`) made the lazy-index race deterministic.
- **Remote URL** migrated from `jphein/palace-daemon` to `techempower-org/palace-daemon`. Old jphein URLs redirect but emit a push warning.

### Fixed — 2026-05-11 / 2026-05-12 operational debugging session

A long debugging session against the disks palace surfaced (and fixed)
six structural issues in palace-daemon. Documented here grouped by issue
number for easy navigation.

- **`clients/hook.py` never sent `X-API-Key`** (1a843ca). Daemon was
  configured with `PALACE_API_KEY` but the hook built every `/mcp` and
  `/mine` request with only `Content-Type`. Every hook save 401'd while
  the broad `except Exception` logged "daemon unreachable", actively
  misdirecting diagnosis. Added `_request_headers()` helper that pulls
  from env; split `HTTPError` from `URLError` so 4xx/auth failures
  no longer impersonate transport failures.

- **`#7` `clients/mempalace-mcp.py` had the same swallow pattern**
  (009694b). Caught `urllib.error.URLError` as "Daemon unreachable" —
  HTTPError is a subclass of URLError, so 401s silently surfaced as
  "unreachable" via the same trap. Split into explicit HTTPError handler
  with code+reason in the message.

- **`#8` ChromaDB SIGTERM corruption** (e714c76). Chromadb 1.5.x
  PersistentClient has no clean `close()` (chroma-core/chroma#5868).
  systemd SIGTERM was killing the daemon mid-flush, leaving the HNSW
  segment in partial-flush corruption (non-empty `data_level0.bin`,
  missing index metadata file). Lifespan shutdown now: cancels the
  watchdog task with timeout, drops cached client+collection refs,
  `gc.collect()`, then `await asyncio.sleep(2.0)` to give chromadb
  background flush threads a chance to finish before exit. Stop time
  dropped from 30s SIGKILL to 2.3s clean shutdown.

- **`#9` `/repair?mode=rebuild` deadlocked indefinitely** (053a36c).
  `rebuild_index()` instantiates a fresh `ChromaBackend()` →
  `PersistentClient` against the same palace path, but the daemon's
  cached `PersistentClient` was still holding the sqlite filelock.
  The new client waited forever. Cache is now cleared (with gc + 0.5s
  sleep) BEFORE invoking `rebuild_index` so the new client can acquire
  the filelock cleanly.

- **`#10` Silent degradation when `hnswlib` is absent** (255cace).
  ChromaDB has no error path for missing hnswlib — it falls back to
  brute-force on in-memory batches with no log line, and the persistence
  layer (which needs `hnswlib.Index.save_index`) becomes unreachable so
  no segment files ever get written. We burned ~2 hours diagnosing
  partial-flush symptoms before realizing the venv was missing the dep.
  Added an import-time guard that exits with a clear install instruction
  pointing at `chroma-hnswlib` (chroma's binary fork, easier to install
  than the source-only `hnswlib`).

- **`#11` Recursive `/mcp` self-call** (938dd2f). The daemon hosts
  mempalace's MCP server in-process via `_call()`. When
  `PALACE_DAEMON_URL` was present in the daemon's environment — which
  routinely happens if `EnvironmentFile=` is shared with hook/client
  tools that DO need it — mempalace's `_daemon_strict()` returned True
  and forwarded every `/mcp` envelope back to the daemon. Recursive
  self-call until `_DAEMON_FORWARD_TIMEOUT_DEFAULT=120` fired.
  Pinned `Environment=PALACE_DAEMON_STRICT=0` in the unit so the
  in-process path is taken regardless of what the EnvironmentFile
  contains. `/health` dropped from 30-60s timeout to 280ms.

### Test — 2026-05-12

- **`#6` Regression tests for hook.py auth + error classification**
  (058c268). `tests/test_hook_auth.py` — 9 unit tests covering
  `_request_headers()` (env present/absent/whitespace), `_post_mcp` /
  `_post_mine` outgoing headers, and the HTTPError-vs-URLError log
  message split. Uses `unittest.mock` to intercept `urllib.request.urlopen`
  and inspect captured Request objects. Locks in the post-`1a843ca` behavior
  so the silent-auth-failure pattern can't regress.

### Maintenance
- `patches/mcp_server_get_collection.patch` reduced to just the "log exception + retry once on cache failure" slice. The `hnsw:num_threads=1` enforcement portion landed upstream via `_pin_hnsw_threads()` in `mempalace/mcp_server.py` and is no longer carried locally. Daemon behaviour is unchanged. The remaining slice is filed upstream as [MemPalace/mempalace#1286](https://github.com/MemPalace/mempalace/pull/1286); once that merges the patch retires entirely.

### Docs
- `docs/typescript-port-plan.md` — planning artifact for the prospective TypeScript port (no commitments; sections marked `[OPEN]`/`[LEANING]`/`[DECIDED]`). Triggered by Ben's 2026-04-21 Discord note that the next canonical mempalace is being rewritten in TS, plus the architectural argument in `docs/event-log-frame.md` that the daemon's role (materialized-view coordinator over the event log) is naturally portable.
- `docs/hook-routing-fix.md` — added a `Status: SHIPPED` header pointing at `62425e3` (2026-04-24, when `clients/hook.py` was added) and clarifying that `clients/mempal-fast.py` is the simpler successor for cases that don't need the full approval/mine flow.
- README — four new rows in the **Open upstream PRs** table for PRs [#15](https://github.com/rboarescu/palace-daemon/pull/15) (`/viz`), [#16](https://github.com/rboarescu/palace-daemon/pull/16) (`/list`), [#17](https://github.com/rboarescu/palace-daemon/pull/17) (`DELETE/PATCH /memory`), and [#18](https://github.com/rboarescu/palace-daemon/pull/18) (lifespan auto-migrate), all filed 2026-04-30. PR #13 was also rebased onto `upstream/main` on 2026-04-30 to clear a `CHANGELOG.md` conflict with upstream's `b4aee82` patch sync — branch state went `CONFLICTING` → `MERGEABLE / CLEAN`. **Pending PRs queue** (under "Fork change queue") is now empty: every generalisable change ahead of `upstream/main` is an open PR.

## [1.7.2] - 2026-04-27

### Pulled in from upstream/main (rboarescu's v1.5.1, sync 2026-04-27)
- **`_get_collection` silent failures** — exceptions now logged (palace path + error) instead of silently returning `None`.
- **Stale collection cache self-healing** — `_get_collection` retries once after clearing all caches on failure; the incident that required a manual daemon restart now self-heals on the next tool call.
- **HNSW `num_threads=1` enforced on every open** — `_get_collection` calls `collection.modify()` after every open, merging the metadata in. ChromaDB 1.5.x does not persist HNSW metadata across reopens (issue #1161); without this, every cache clear silently re-enabled parallel inserts and risked SIGSEGV under concurrent writes.
- **`/health` reflects actual palace state** — previously returned HTTP 200 `ok` even when the collection was broken. Now calls `_get_collection()` and returns HTTP 503 `degraded` if the palace is unavailable.
- **Systemd watchdog** — daemon sends `READY=1` on startup and `WATCHDOG=1` every `WatchdogSec/2` seconds via `sd_notify` (stdlib-only). Watchdog pings are gated on a live `_get_collection()` check; if the palace goes dark, the watchdog goes silent and systemd restarts the daemon. `palace-daemon.service` updated: `Type=notify`, `NotifyAccess=main`, `WatchdogSec=120`.
- **Startup warmup opens the collection** — lifespan warmup calls `_get_collection(create=True)` directly instead of `ping`, so `num_threads=1` is applied before `_warn_if_hnsw_threads_unset` runs at startup.
- **`PALACE_MAX_READ_CONCURRENCY` / `PALACE_MAX_WRITE_CONCURRENCY` env vars** — split out from `PALACE_MAX_CONCURRENCY` for finer control. Set `PALACE_MAX_WRITE_CONCURRENCY=1` to serialize writes (mitigates issue #1161).
- **`--force` flag and self-healing startup** — automatically clears stale processes on the target port. Our fork's existing `ExecStartPre=fuser -k` accomplishes the same thing belt-and-suspenders.
- **Toast-injection revert** — `a64244c` reverted MCP-breaking toast injection, kept REST endpoint toasts.

### Fork-side notes
- Naming collision: this fork released its own v1.5.1 (`b4b39fc`, kind= filter + `_canonical_topic` + verify-routes.sh + limit= bug fix) before upstream tagged v1.5.1 with the content above. The two v1.5.1's cover different work; our fork's history kept its v1.5.1 entry below for posterity.
- Note: PR #4 was *closed* on the upstream side rather than merged via the GitHub UI — rboarescu cherry-picked the contents into upstream `main` directly as `ef6ac03` and closed the PR. Our README phrasing ("merged via PR #4") will be tightened in a follow-up.

## [1.7.1] - 2026-04-27

### Removed
- **`kind=` query parameter on `/search` and `/context`**. Companion to mempalace fork's [`7ba28dc`](https://github.com/jphein/mempalace/commit/7ba28dc) retiring the read-side `kind=` filter machinery. After the Phase A–E checkpoint collection split (mempalace) all Stop-hook auto-save checkpoints live in the dedicated `mempalace_session_recovery` collection; verified empirically on the canonical 151K-drawer palace (763 checkpoints in recovery, 0 in `mempalace_drawers`). The filter was filtering nothing.
- `_VALID_KINDS` constant and the kind validation in `_search_args`.
- `kind=`-related probes from `scripts/verify-routes.sh`.
- Recovery checkpoint reads remain available via mempalace's `mempalace_session_recovery_read` MCP tool.

## [1.7.0] - 2026-04-26

### Added
- **`GET /viz`** — self-contained status dashboard. Single HTML page that fetches `/graph`, `/repair/status`, and `/health` in parallel and renders five panels: status strip (version, drawer count, repair pulse, pending writes), D3 force-directed knowledge graph, wing/room hierarchy (Mermaid tree), wings bar chart, tunnels list with click-to-highlight. D3 + Mermaid loaded via CDN, no static-file deps. Optional `?refresh=N` for auto-refresh, `?key=…` for ergonomic auth bookmarking.
- Inspired by upstream MemPalace PRs #1022 (sangeethkc — D3 KG viz), #393 (jravas — Mermaid diagrams), #431 (MiloszPodsiadly — CLI stats), #256 (rusel95 — sync_status MCP), #601 (mvanhorn — brief overview). None cherry-picked; the page consumes the daemon's own `/graph` endpoint so it benefits from the direct-sqlite optimization (sub-second on 151K drawers) and stays decoupled from upstream's evolution.
- Security: all wing/room/entity names from `/graph` enter the DOM via `textContent` / safe `setAttribute`, never `innerHTML`. Mermaid labels pass through a `_/` sanitizer that strips `[`, `]`, `"`, `<`, `>`, `|`, `` ` `` to avoid breaking the parser. CDN-loaded D3 + Mermaid are the only third-party scripts.
- HTML template at `static/viz.html`; cached at module load. New endpoint defined alongside `/graph` in `main.py`.
- Added `GET /viz` probe to `scripts/verify-routes.sh`.

## [1.6.0] - 2026-04-25

### Added
- **`GET /graph`** — single-shot structural snapshot for SME-style consumers. Mirrors `/stats`'s `asyncio.gather` shape but adds a parallel `mempalace_list_rooms` fan-out per wing and a direct read-only sqlite read of `knowledge_graph.sqlite3`. Replaces what an adapter would otherwise compose serially over HTTP — on the 151K-drawer canonical palace `list_wings` alone takes ~30s, so a serial composition costs minutes.
- Response shape: `{ "wings": {...}, "rooms": [{"wing", "rooms"}], "tunnels": [...], "kg_entities": [...], "kg_triples": [...], "kg_stats": {...} }`.
- KG read uses URI-mode `?mode=ro` so the daemon can never accidentally write that file. Schema differences across mempalace versions tolerated via per-query `OperationalError` catch.
- Added `GET /graph` probe to `scripts/verify-routes.sh`.

### Notes
- Spec: `docs/graph-endpoint.md`. Coordinates with `multipass-structural-memory-eval` (SME) — adapter prefers `/graph` once daemon ≥ 1.6.0 and falls back to MCP composition otherwise.
- `_kg_path()` derives KG location from `_mp._config.palace_path` (sibling to `chroma.sqlite3`), so non-default deployments (`PALACE_PATH=/mnt/raid/...`) work unchanged.

## [1.5.1] - 2026-04-25

### Added
- **`/search` and `/context` accept `kind=` query param** mirroring the `mempalace_search` MCP tool's input_schema enum. Three values: `content` (default, excludes Stop-hook auto-save checkpoints), `checkpoint` (only checkpoints, recovery/audit), `all` (no filter, pre-2026-04-25 behavior). Backed by jphein/mempalace commit `8d02835`'s read-side filter. End-to-end validated against the 151K-drawer canonical palace: same query returned 5 CHECKPOINT-shaped diary entries with `kind=all` vs. 5 substantive content drawers with `kind=content`. Invalid values return 400.
- **`_canonical_topic()` helper** in `_silent_save_write`. Rewrites legacy synonyms (currently `"auto-save"` → `"checkpoint"`) at the daemon boundary with a warning log line, so client-side topic drift can't silently leak into palace metadata. Defense-in-depth on top of the per-client canonical-topic constants in `clients/hook.py` and `clients/mempal-fast.py`.
- **`scripts/verify-routes.sh`** — curl-based smoke test that exercises every public route post-deploy. Designed for manual `systemctl --user restart palace-daemon` validation, not CI (depends on a live palace).

### Fixed
- **`/search` and `/context` now actually honor `limit=`.** Earlier versions passed `max_results` to the `mempalace_search` MCP tool, but the tool's input_schema declares `limit` — `mempalace.mcp_server.handle_request` then silently dropped the unknown key via its schema-property whitelist (line 1677), and *every* response was capped at the default 5 regardless of what the user asked for. Confirmed against running v1.5.0. Renamed to `limit` so the user-supplied value actually binds.

### Notes
- The `/search` filter is a daemon-side wrapper around the read-side filter that lives in `mempalace.searcher`. It works because the daemon imports the fork's mempalace at `/mnt/raid/projects/memorypalace`. Upstream MemPalace doesn't have the `kind=` parameter on `mempalace_search` yet — fork PR pending. Until that lands, this daemon needs the fork checked out as its mempalace install. **Update 2026-04-27:** retired in fork v1.7.1; structural fix made it inert.

## [1.5.0] - 2026-04-24

### Added
- **`POST /repair`** — coordinates repairs with daemon-mediated traffic. Four modes:
  - `light` — clears client/collection caches; next open re-runs `quarantine_stale_hnsw()`. Cheap, non-blocking for other callers.
  - `scan` — runs `mempalace.repair.scan_palace` under a read slot, returns the count of corrupt IDs found.
  - `prune` — runs `mempalace.repair.prune_corrupt` under a write slot; the cross-process flock in `ChromaCollection` already serializes this against live writers.
  - `rebuild` — destructive collection swap (`delete_collection` + `create_collection` are *outside* the flock, so a naked rebuild concurrent with any writer silently loses writes). Holds every read/write/mine semaphore slot during the rebuild window to prevent daemon-mediated writes from racing the swap.
- **`POST /silent-save`** — HTTP path for Claude Code Stop-hook silent saves. Normal ops: writes a diary checkpoint via `tool_diary_write` under the write semaphore. During `/repair mode=rebuild`: appends the payload to `<palace_parent>/palace-daemon-pending.jsonl` and returns a themed "held in trust" message. The queue drains automatically once the rebuild completes.
- **`GET /repair/status`** — current repair state + pending-writes queue depth.
- **Themed messages** — `messages.py` centralizes user-facing strings for save, save-queued, repair-begin, repair-complete, and drain-fail paths. Saves use `✦`; palace ops use `◈`.

### Notes
- The rebuild coordination is daemon-scoped: external `mempalace repair rebuild` CLI invocations still race against any other process's writes because `delete_collection` / `create_collection` are backend-level operations that the `ChromaCollection` flock does not protect. For safe concurrent rebuilds, route through the daemon.
- Fork's `mempalace/hooks_cli.py` opt-in: set `PALACE_DAEMON_URL` (and optionally `PALACE_API_KEY`) and silent Stop-hook saves will POST to the daemon, picking up the queue-and-drain behavior and themed messages. Unset or unreachable → falls through to the legacy direct-write path with no behavior change.

## [1.4.5] - 2026-04-25

### Changed
- **`clients/hook.py` — time-based and session-end saves**
  - `TIME_SAVE_INTERVAL` (300 s) was defined but never used; now wired in as a second independent save trigger in `hook_stop`. Saves fire if ≥5 min have elapsed with any unsaved exchanges, regardless of the 15-exchange count gate.
  - New `force_on_stop` setting (default `true`) adds a third trigger: saves whenever `since_last > 0` and at least `force_min_interval` seconds (default 60 s) have passed since the last save. Captures session-end stops that fall below the exchange-count threshold.
  - `force_min_interval` is now configurable via `hook_settings.json` (falls back to hardcoded `FORCE_MIN_INTERVAL = 60`).
  - `hook_session_start` seeds `{session_id}_last_save_ts` at session open so the first Stop of a new session doesn't spuriously fire the time trigger.
  - `hook_session_start` now prunes state files older than 7 days from `~/.mempalace/hook_state/` to prevent unbounded accumulation.
  - Module docstring updated to list all four `hook_settings.json` keys (`force_on_stop`, `force_min_interval` added).
  - Diary auto-save entries now embed the trigger reason (`hook.count`, `hook.time`, `hook.force`).

## [1.4.2] - 2026-04-24

### Fixed
- **Backup connection leak** — `POST /backup` now wraps both SQLite connections (`src`/`dst` and `check`) in `try/finally` blocks so they are always closed even when backup or integrity check fails.
- **World-writable lock file** — daemon lock file moved from `/tmp/palace-daemon-{port}.lock` to `~/.cache/palace-daemon/daemon-{port}.lock` (directory created with mode `0o700`).
- **`/mine` path traversal** — `POST /mine` now validates that `dir` is an absolute path with no `..` components, exists, and is a directory. Rejects invalid input with 400 before spawning a subprocess.
- **HNSW retry on write ops** — auto-repair retry in `_call()` is now restricted to `_READ_TOOLS`; write ops get a diagnostic hint instead of a retry that could produce duplicate drawers.
- **`bootstrap.sh` silent scp failure** — each `scp` call now has an explicit `|| { echo ...; exit 1; }` guard with a descriptive error message.

### Changed
- **`POST /backup` dir permissions** — backup directory created with `mode=0o700` instead of default umask.
- **`/mine` param validation** — `mode` validated against `{convos, projects}`, `extract` against `{exchange, general}`, `limit` coerced to `int` with clear 400 errors.
- **`bootstrap.sh` env overrides** — `ARTEMIS_HOST` and `ARTEMIS_CLIENTS_PATH` are now overridable via environment variables (defaults unchanged).
- **Debug scripts** — `rebuild_v3.py`, `refresh_index.py`, `repair_rebuild_surgical.py`, `stress_test.py`, `purge_wings.py` moved to `scripts/`; `main.py.bak` deleted.
- **`palace-daemon.service`** — `ExecStartPre` stale-lock path updated to match new lock location (`~/.cache/palace-daemon/daemon-8085.lock`).

## [1.4.1] - 2026-04-24

### Added
- **`purge_wings.py`** — offline SQLite-based wing purge utility; bypasses ChromaDB entirely to safely bulk-delete drawers when HNSW is stale or corrupt. Deletes in 500-item batches, backs up before changes, clears HNSW segment dirs so daemon rebuilds a clean index on next start.

### Changed
- `palace-daemon.service` hardened with two `ExecStartPre` guards: `fuser -k 8085/tcp` clears any stale process holding the port; `rm -f /tmp/palace-daemon-8085.lock` removes a stale lock file. Both prefixed with `-` so they're no-ops when nothing is blocking.
- README systemd section updated: system service is now the recommended install for always-on hosts; user service demoted to desktops/dev only. Added `WARNING` callout against installing both (causes crash-loop collision on port 8085).
- Bumped `VERSION` to `1.4.1`.

### Fixed
- Removed 10,828 rogue drawers mined from `~/.` and `~/palace-daemon/` by the old `mempalace hook run` fallback. Palace reduced from 11,632 → 685 real drawers. Vector index rebuilt via `mempalace repair`.
- Resolved dual user+system service collision that caused crash-loop on `systemctl restart palace-daemon`.

## [1.4.0] - 2026-04-24

### Added
- **`clients/hook.py`** — stdlib-only hook runner replacing `mempalace hook run`. Routes all mine operations through `POST /mine` on palace-daemon; never spawns mempalace as a subprocess. If daemon is unreachable, passes through silently with no fallback to direct DB access.
- **`clients/bootstrap.sh`** — one-command client setup script. Copies `mempalace-mcp.py` and `hook.py` from Artemis and wires them into Claude Code, Gemini CLI, VSCode, Cursor, or JetBrains. Clients need no mempalace install — both files are stdlib-only.
- **`docs/hook-routing-fix.md`** — permanent plan document capturing the hook routing fix design, constraints, and verification steps.

### Changed
- `~/.claude/settings.json` and `~/.gemini/settings.json` hook commands updated to use `hook.py` instead of `mempalace hook run`.
- `~/.mempalace/hook_settings.json` `daemon_url` normalised to `http://localhost:8085` (was `10.0.0.5:8085`) on Artemis.
- `README.md` — expanded Clients section: remote client table, `hook.py` usage and behaviour, `hook_settings.json` field reference, per-tool hook configs, `bootstrap.sh` usage.

### Security
- Mine operations now require explicit user approval via block response before executing; no implicit auto-mine on session stop.
- `MEMPAL_DIR` is the only mine trigger; transcript directory fallback removed.

## [1.3.0] - 2026-04-24

### Added
- **Auto-Healing HNSW Index** — daemon now automatically detects "Internal error: Error finding id", quarantines stale index segments via `quarantine_stale_hnsw`, and retries the request seamlessly.
- **Silent Save / Flush** — implemented automatic memory checkpointing on daemon shutdown via `lifespan` and added a manual `POST /flush` endpoint.
- **Port-Specific Locking** — lock files are now dynamic (`/tmp/palace-daemon-{port}.lock`), allowing parallel instances (e.g., production and shadow/test palace) on the same host.
- **Chaos Test Suite** — added `chaos_test.py` for high-concurrency validation and index corruption simulation.

### Changed
- Updated `README.md` with "Shadow Palace" testing workflow and new API endpoints.
- Improved logging for auto-repair events to provide better visibility during recovery.
- Bumped internal version to 1.3.0.

## [1.2.0] - 2026-04-23

### Added
- **Daemon Instance Locking** — implemented fcntl-based file lock (`/tmp/palace-daemon.lock`) to prevent multiple daemon instances from running concurrently
- **Graceful Shutdown** — added SIGINT/SIGTERM handlers to ensure clean exits and reduce risk of stale SQLite locks during restarts
- **Hardened Service** — `palace-daemon.service` now enforces explicit environment paths (`MEMPALACE_PALACE`) to prevent path ambiguity

### Changed
- **"Daemon-Only" Policy** — removed the direct fallback mode in `mempalace-mcp.py`. The client now exits with an error if the daemon is unreachable. This prevents "split-brain" scenarios and potential database corruption from concurrent process access.
- Improved HNSW stale index detection to catch more internal error variations and provide specific recovery commands.

### Security & Stability
- Added high-visibility warnings against accessing the database over network mounts (NFS/Samba) which caused `SQLITE_IOERR` in previous versions.

## [1.1.2] - 2026-04-23

### Added
- `POST /backup` endpoint — performs atomic, verified SQLite backups with integrity checks
- `POST /reload` endpoint — clears internal client cache to refresh the database index
- Self-healing hints — the daemon now detects "Internal error: Error finding id" during searches and provides actionable advice

### Fixed
- `palace-daemon.service` — port conflict handling: added `ExecStartPre=-/usr/bin/fuser -k 8085/tcp` to ensure port 8085 is free before starting
- Improved service reliability by adding `KillMode=mixed` to `palace-daemon.service`
- `main.py` — added `VERSION` constant and exposed it in `/health`

### Changed
- Updated documentation with API references for new endpoints and clearer systemd instructions

## [1.1.1] - 2026-04-22

### Fixed
- `clients/mempalace-mcp.py` — SyntaxError on startup: `--api-key` argument
  used `default=API_KEY` before `global API_KEY` declaration; changed default
  to `None` so the client actually starts

## [1.1.0] - 2026-04-22

### Added
- `PALACE_MAX_CONCURRENCY` env var (default 4) — tunes read concurrency at runtime
- `clients/mempalace-mcp.py` fallback mode — if the daemon is unreachable at
  startup, falls back to importing `mempalace.mcp_server` in-process instead
  of exiting, so Claude Code keeps working when the daemon is down

### Changed
- Replaced `asyncio.Lock()` with three semaphores for concurrent access control:
  - `_read_sem(N)` — up to N concurrent read-only ops (search, query, stats, …)
  - `_write_sem(N//2)` — up to N//2 concurrent write ops (add, kg mutations, …)
  - `_mine_sem(1)` — one mine job at a time, independent of reads/writes
- `/mine` now uses `_mine_sem` only — long import jobs no longer block read or
  write traffic (requires mempalace ≥3.3.2 for internal mine locking)
- `/health` bypasses all semaphores — always responds immediately even under
  full load, safe for load balancers and monitoring
- `/stats` fans out its three sub-calls with `asyncio.gather()` — response time
  cut to roughly one third of the previous sequential implementation

## [1.0.0] - 2026-04-21

### Added
- `POST /mcp` — full MCP JSON-RPC proxy endpoint
- `GET /health` — daemon + palace status
- `GET /search` — semantic search over palace drawers
- `GET /context` — alias for /search, named for LLM tool prompts
- `POST /memory` — store a drawer (wing, room, content)
- `GET /stats` — wing/room counts, KG stats
- `POST /mine` — run `mempalace mine` under the global asyncio.Lock,
  serializing bulk imports against live queries
- Optional API key auth via `PALACE_API_KEY` env var (`X-Api-Key` header)
- Configurable host, port, palace path via CLI args or env vars
- `clients/mempalace-mcp.py` — zero-dependency stdio MCP proxy for remote clients
- systemd service unit (`palace-daemon.service`)
