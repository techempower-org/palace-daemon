# Changelog

## [Unreleased]

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
