# palace-daemon (techempower-org fork)

**TechEmpower's production fork of [rboarescu/palace-daemon](https://github.com/rboarescu/palace-daemon)** (transferred from `jphein/palace-daemon` in May 2026)

[![version-shield](https://img.shields.io/badge/version-1.7.2-4dc9f6?style=flat-square&labelColor=0a0e14)](https://github.com/techempower-org/palace-daemon/releases) [![upstream-shield](https://img.shields.io/badge/upstream-1.5.1-7dd8f8?style=flat-square&labelColor=0a0e14)](https://github.com/rboarescu/palace-daemon/releases)
[![python-shield](https://img.shields.io/badge/python-3.12+-7dd8f8?style=flat-square&labelColor=0a0e14&logo=python&logoColor=7dd8f8)](https://www.python.org/)
[![license-shield](https://img.shields.io/badge/license-MIT-b0e8ff?style=flat-square&labelColor=0a0e14)](LICENSE)

---

Fork of [rboarescu/palace-daemon](https://github.com/rboarescu/palace-daemon), tracking `upstream/main` through the 2026-04-27 sync (upstream is at [v1.5.1](https://github.com/rboarescu/palace-daemon/commit/d0aabb9); this fork is at v1.7.2-with-unreleased-fork-work — the `/graph` endpoint, `/viz` status dashboard, auto-repair-on-startup, and post-merge deployment tooling that 1.7.2 captured, **plus** the substantial 2026-05-11 → 2026-05-15 reliability + hybrid-retrieval push captured under `[Unreleased]` in the CHANGELOG). Running in production since 2026-04-24, currently fronting the [techempower-org/mempalace](https://github.com/techempower-org/mempalace) **273k-drawer Postgres + pgvector + Apache AGE** palace on [`disks.jphe.in:8085`](https://palace.jphe.in/health). The bulk of the v1.5.0 daemon work (cold-start warmup, `/repair`, `/silent-save`, themed messages, `--palace` flag, MCP timeout) was contributed back to upstream as [PR #4](https://github.com/rboarescu/palace-daemon/pull/4); rboarescu cherry-picked the contents into upstream `main` directly as [`ef6ac03`](https://github.com/rboarescu/palace-daemon/commit/ef6ac03) on 2026-04-25 and closed the PR.

**2026-05-11 → 2026-05-17 fork-side push (unreleased, on `main`):** hybrid-retrieval endpoints (`POST /search/hybrid` + `POST /search/keyword` — vector ∪ tsvector-BM25 ∪ AGE-graph candidates, hybrid-reranked), `POST /cypher` + `POST /embed` for direct AGE / pgvector access, the `Stop`/`PreCompact` hook detach fix (fork + setsid + `dup2` all three FDs so claude's harness pipes can close), canonical-room boundary validation in `/memory`, and [`ops/scripts/deploy-palace-daemon.sh`](ops/scripts/deploy-palace-daemon.sh) — a one-shot deployer that replaces the previous syncthing-based mirror path. **New 2026-05-17**: `POST /search/age-fused` endpoint — Phase 5 of the multi-project AGE-integration plan (see [`techempower-org/palace-daemon#25`](https://github.com/techempower-org/palace-daemon/pull/25) and [companion mempalace PR #101](https://github.com/techempower-org/mempalace/pull/101)). Combines vector retrieval with AGE entity-overlap via RRF fusion — graph_only beats vector by +5pp R@5 on a 2026-05-17 [n=200 git-derived probe spike](https://github.com/techempower-org/multipass-structural-memory-eval/blob/feat/rlm-adapter/docs/benchmarks/2026-05-17-age-write-through-spike.md); fusion adds another +4pp on top. Full day-by-day in [CHANGELOG](CHANGELOG.md).

What this fork adds that you won't get from upstream yet: a **`GET /viz` status dashboard** (self-contained HTML page that fetches `/graph`, `/repair/status`, and `/health` in parallel and renders five panels — status strip with repair pulse, D3 force-directed knowledge graph, Mermaid wing/room hierarchy, tunnels list, wings bar chart — D3 + Mermaid via CDN, no static-file deps); a **`GET /graph` endpoint** (single-shot structural snapshot for SME-style consumers, ~0.4s on the 151K-drawer palace via direct read-only sqlite reads of `embedding_metadata` and `knowledge_graph.sqlite3` — vs. ~60-120s for the equivalent serial MCP composition under load); **`GET /list`** for query-free metadata browse by wing/room (wraps `mempalace_list_drawers`, the right path when `/search` would fall back to BM25 and ignore the wing filter); **`DELETE /memory/{id}` + `PATCH /memory/{id}`** REST CRUD over `mempalace_delete_drawer` / `mempalace_update_drawer` so curation UIs don't have to talk MCP just to fix a typo; **lifespan auto-migrate** of pre-3.3.4 Stop-hook checkpoints into `mempalace_session_recovery` on first restart post-upgrade (idempotent, ImportError-gated, env-overridable via `PALACE_AUTO_MIGRATE_CHECKPOINTS=0`); **auto-repair-on-startup** that detects degraded HNSW recall after restart and fires `/repair {mode:rebuild}` non-blocking in the background (workaround that bought time for the mempalace fork's `645ba20` integrity gate fix to land); the **`limit=` parameter actually being honored** (earlier versions silently capped at 5 due to a max_results→limit name mismatch the MCP tool's whitelist dropped); a **`scripts/deploy.sh`** that bundles `git push → wait for sync → systemctl restart → /health poll → verify-routes smoke test` into one command; **`scripts/verify-routes.sh`** as a curl-based smoke test for every public route; **`clients/palace-mode`** CLI for one-command local↔remote palace switching; **`clients/palace-mcp-dispatch.sh`** that picks daemon vs. in-process MCP based on `PALACE_DAEMON_URL`; and **`clients/mempal-fast.py`** — a stdlib-only Stop/PreCompact hook handler that POSTs to `/silent-save` without importing mempalace (so cold hook fires can't trigger ChromaDB's HNSW SIGSEGV class). Full list below.

[v1.7.2 release notes](CHANGELOG.md) · [PR #4 — upstream contribution](https://github.com/rboarescu/palace-daemon/pull/4) · [Discussion #5 — Postgres backend](https://github.com/rboarescu/palace-daemon/discussions/5) · [Discussion #6 — TS rewrite heads-up](https://github.com/rboarescu/palace-daemon/discussions/6) · [`docs/event-log-frame.md`](docs/event-log-frame.md) — daemon-as-view-coordinator architectural frame · [`docs/typescript-port-plan.md`](docs/typescript-port-plan.md) — TS rewrite planning artifact (no commitments, sections marked `[OPEN]`/`[LEANING]`/`[DECIDED]`)

## Open upstream PRs

Per [PR #4 issue comment](https://github.com/rboarescu/palace-daemon/pull/4#issuecomment-4321234194), rboarescu welcomed post-1.5.0 work as small separate PRs at whatever cadence works.

| PR | Status | Description |
|---|---|---|
| [#7](https://github.com/rboarescu/palace-daemon/pull/7) | OPEN, awaiting review | `fix: honor limit= on /search and /context` — two-line rename `max_results` → `limit` so the user-supplied value actually binds (the MCP tool's input_schema declares `limit`, so `max_results` was being silently dropped). |
| [#8](https://github.com/rboarescu/palace-daemon/pull/8) | OPEN, awaiting review | `feat: canonicalize Stop-hook topic at daemon boundary with warning log` — `_canonical_topic()` rewrites legacy synonyms (`"auto-save"` → `"checkpoint"`) on the `/silent-save` path and emits a warning so client-side drift is observable. Composes with upstream's `0060190` CHECKPOINT_TOPIC constant. |
| [#9](https://github.com/rboarescu/palace-daemon/pull/9) | OPEN, awaiting review | `chore(scripts): add verify-routes.sh smoke test` — curl-based smoke test for every public read-only route. Universal, no fork-mempalace dependencies. |
| [#10](https://github.com/rboarescu/palace-daemon/pull/10) | OPEN, awaiting review | `fix(clients): resolve mempalace-mcp.py via readlink, not absolute path` — bug fix: dispatcher in `clients/palace-mcp-dispatch.sh` as shipped in upstream `main` has a hardcoded `/home/jp/Projects/...` path (accidentally embedded during PR #4's extraction) and fails on every machine except mine. `+6/-1` `readlink -f` sibling resolution. |
| [#11](https://github.com/rboarescu/palace-daemon/pull/11) | OPEN, awaiting review | `docs: event-log frame — palace-daemon as materialized-view coordinator` — architectural reference doc (191 lines) articulating mempalace as Kleppmann-shaped (log + materialized views), the daemon as the view coordinator. Useful frame ahead of the multi-backend transition. |
| [#12](https://github.com/rboarescu/palace-daemon/pull/12) | OPEN, awaiting review | `fix(clients): remove embedded API key + URL defaults from palace-mode` — `clients/palace-mode` shipped with a `DEFAULT_URL` pointing at JP's homelab and a real hex `DEFAULT_KEY` (rotated, but still in upstream's source). Reads both from env, fails fast in `remote` mode if either is unset. |
| [#13](https://github.com/rboarescu/palace-daemon/pull/13) | OPEN, awaiting review | `feat: GET /graph — single-shot structural snapshot for SME-style consumers` — single endpoint returns wings + rooms-per-wing + tunnels + KG entities + triples + KG stats in ~0.4s on the canonical 151K palace; replaces the SME-style 60-120s serial MCP composition. Folds in the `/graph.tunnels` derive-from-`graph_stats.top_tunnels` fix so the response always agrees with `/stats.graph.tunnel_rooms`. Includes `docs/graph-endpoint.md`. `+495/-0`. |
| [#14](https://github.com/rboarescu/palace-daemon/pull/14) | OPEN, awaiting review | `chore(clients): add CHECKPOINT_TOPIC constant to mempal-fast.py` — mirrors the constant already in `clients/hook.py`. Symmetry refactor; both client paths now source the canonical topic value from a per-file constant rather than mixing inline + constant. `+8/-1`. |
| [#15](https://github.com/rboarescu/palace-daemon/pull/15) | OPEN, awaiting review | `feat: GET /viz — self-contained status dashboard` — single HTML page that fetches `/graph`, `/repair/status`, and `/health` and renders five panels (status strip, D3 KG, Mermaid wing/room tree, tunnels, wings bar). D3 + Mermaid via CDN, no new static-file plumbing. **Stacks on #13** because the page consumes `/graph`. |
| [#16](https://github.com/rboarescu/palace-daemon/pull/16) | OPEN, awaiting review | `feat: GET /list — query-free metadata browse by wing/room` — wraps `mempalace_list_drawers` so consumers can enumerate drawers in a wing without inventing an embeddable query (`/search` falls back to BM25 and ignores the wing filter when the query is non-embeddable). 34 lines of `main.py`. |
| [#17](https://github.com/rboarescu/palace-daemon/pull/17) | OPEN, awaiting review | `feat: DELETE /memory/{id} + PATCH /memory/{id}` — REST CRUD over `mempalace_delete_drawer` / `mempalace_update_drawer`. Both tools have been in mempalace since 3.x; this just exposes them over HTTP for curation UIs. 29 lines of `main.py`. |
| [#18](https://github.com/rboarescu/palace-daemon/pull/18) | OPEN, awaiting review | `feat(lifespan): auto-migrate Stop-hook checkpoints to recovery collection on startup` — calls `mempalace.migrate.migrate_checkpoints_to_recovery()` during lifespan startup so operators don't have to run the manual `mempalace repair --mode reorganize` after upgrading. ImportError-gated, env-overridable via `PALACE_AUTO_MIGRATE_CHECKPOINTS=0`. |

**Note:** PRs #8, #9, #10, #11, #12, #13 were each amended once on 2026-04-27 to address Copilot review feedback (force-pushed). Caught real bugs in several cases: PR #9's `/health` 503-hiding (curl `-sS` body grep masked HTTP status), PR #10's GNU-only `readlink -f` (failed on macOS), PR #13's rooms-from-wings-only logic bug (silent data loss on partial schema-drift) and `_read_sem`-bypass concurrency concern. Fixes also backported to fork main + deployed to disks (`152e428`). PR #13 was rebased on 2026-04-30 to clear a `CHANGELOG.md` conflict with upstream's `b4aee82` patch sync; PRs #15–#18 followed the same day after the rebase cleared the way.

### Recently landed in upstream

- **[PR #4](https://github.com/rboarescu/palace-daemon/pull/4)** (cherry-picked into upstream `main` as [`ef6ac03`](https://github.com/rboarescu/palace-daemon/commit/ef6ac03), 2026-04-25, then closed): cold-start warmup, `/repair`, `/silent-save`, themed messages, `--palace` flag, MCP timeout. The bulk of the v1.5.0 daemon work originated here.

### Cross-repo coordination

The daemon depends on a tiny mempalace patch that's also in flight upstream:

- **[MemPalace/mempalace#1286](https://github.com/MemPalace/mempalace/pull/1286)** — `fix(mcp_server): log exception + retry once on _get_collection failure` (filed 2026-04-30, against `develop`). Currently applied locally as `patches/mcp_server_get_collection.patch` via [`scripts/apply_patches.sh`](scripts/apply_patches.sh) on every `pipx upgrade mempalace`. Once #1286 merges, the patch retires entirely (delete the file, drop the apply step from the upgrade workflow).
- **[MemPalace/mempalace#1142](https://github.com/MemPalace/mempalace/pull/1142)** — `docs: add RELEASING.md with mempalace-mcp pre-release check` (filed 2026-04-23, against `develop`). Process doc, no daemon dependency.

## Fork change queue

Everything the fork has ahead of upstream that hasn't been filed as a PR yet. Ranked from most PR-ready to least.

### Pending PRs — ready to file

_As of 2026-04-30, the queue is empty — every generalisable change ahead of `upstream/main` is now an open PR (#7 through #18). The remaining fork-only work is captured below under **Needs generalization before PR**._

### Needs generalization before PR

These have working fork-side implementations but bake in JP-specific assumptions (paths, hostnames, install layouts, fork-mempalace symbols) that would fail or surprise other operators. They're held until they can be split into a universally-applicable shape vs. a fork-private layer.

| Area | Change | What needs generalizing | Files |
|---|---|---|---|
| **Tooling** | `scripts/deploy.sh` — one-command `git push → wait for sync → systemctl restart → /health poll → verify-routes` deploy. | Defaults to `PALACE_HOST=disks`; reads `PALACE_API_KEY` from `~/.claude/settings.local.json`; assumes a Syncthing-mirrored source tree on the deploy host; ssh user paths hardcoded; the post-restart verify hook imports fork-mempalace-only symbols (`_segment_appears_healthy`, `_quarantined_paths`, `_SESSION_RECOVERY_COLLECTION`, `migrate_checkpoints_to_recovery`) that would fail on upstream-mempalace installs. Likely splits into "universal three-step deploy" + "private verify hook." | `scripts/deploy.sh` |
| **Clients** | `clients/palace-mode` — `install`/`verify` subcommands that re-apply plugin-cache customizations after a Claude Code plugin update. The base mode-switching part shipped via PR #12. | The `install` subcommand assumes the Claude Code plugin cache layout under `~/.claude/plugins/cache/mempalace/...`. Needs to be parameterized or removed for the upstream version. | `clients/palace-mode` |
| **Ops** | `scripts/auto-repair-if-empty.sh` — `ExecStartPost` script that probes `/search` after the daemon binds, detects the "vector ranked 0" warning, and fires `/repair {mode:rebuild}` non-blocking in the background. **Now safety-net-only** since mempalace `645ba20` (integrity gate) shipped — a healthy 151K palace no longer triggers it. | Assumes a `systemctl --user` unit + a specific service unit shape with `ExecStartPost`. The probe-and-repair logic itself is generic; the systemd integration is what's JP-shaped. The ~4:48 HNSW-segment-load timeout (`PALACE_AUTO_REPAIR_WAIT_SECS=240`) is calibrated to the 151K canonical palace; smaller palaces can use the 30s default. | `scripts/auto-repair-if-empty.sh`, `palace-daemon.service` |

## What this looks like in practice

The fork's `/graph` endpoint replaces what an SME-style adapter would otherwise compose by serially calling `list_wings` + `list_rooms × N` + `list_tunnels` + `kg_stats` over MCP:

```bash
$ time curl -sS -H "X-Api-Key: $KEY" https://palace.jphe.in/graph | jq '{
    wings: (.wings | length),
    pairs: ([.rooms[] | .rooms | length] | add),
    tunnels: (.tunnels | length),
    kg: {entities: (.kg_entities | length), triples: (.kg_triples | length)}
  }'
{
  "wings": 36,
  "pairs": 165,
  "tunnels": 9,
  "kg": { "entities": 6, "triples": 3 }
}

real    0m0.876s
```

Deploy is a single command that catches sync-lag footguns (Syncthing-mirrored deployment between dev and prod hosts):

```bash
$ scripts/deploy.sh
▸ 1/5  push to origin           ✓ pushed 00ec6be → origin/main
▸ 2/5  wait for sync to disks   ✓ remote at 00ec6be
▸ 3/5  restart palace-daemon    ✓ restart issued
▸ 4/5  wait for daemon health   ✓ healthy on v1.7.0 (after 3s)
▸ 5/5  smoke-test routes        ✓ all 12 routes verified

✦ deploy complete: 00ec6be on http://disks.jphe.in:8085
```

Local↔remote palace switching is one command:

```bash
$ palace-mode status
Mode: remote (http://disks.jphe.in:8085)

$ palace-mode local
→ local mode

$ palace-mode remote http://staging:8085
→ remote mode (PALACE_DAEMON_URL=http://staging:8085)
```

A Stop hook fires from any Claude Code session and routes through the daemon without ever loading mempalace locally:

```
[06:29:17] Daemon silent-save: queued=False count=14 (fast-path)
[06:29:17] Skipping auto-ingest: PALACE_DAEMON_URL set, daemon owns writes
```

The `/viz` dashboard is a single bookmark for live state — drawer count, repair pulse, KG, wing/room tree, tunnels:

```
https://palace.jphe.in/viz?key=$KEY&refresh=15
```

Auto-repair self-heals after a daemon restart that leaves HNSW empty (the false-positive quarantine cascade — pre-fix shape):

```
06:56:42  systemd: Starting palace-daemon...
06:56:45  Quarantined 3 stale HNSW segment(s) — ChromaDB will rebuild indexes
06:57:19  [auto-repair] daemon up after 15s
06:57:20  [auto-repair] DETECTED degraded HNSW recall: vector ranked 0
06:57:20  [auto-repair] kicking off /repair {mode:"rebuild"} in background — daemon stays available
```

After the mempalace-fork integrity-gate fix (`645ba20`) deployed alongside, the same restart now logs the post-fix shape and the auto-repair script exits no-op:

```
HNSW mtime gap 11165s on .../f360e835-... exceeds threshold but segment metadata file is intact — flush-lag, not corruption. Leaving in place.
HNSW mtime gap 11165s on .../02660268-... — Leaving in place.
HNSW mtime gap 11166s on .../4697d280-... — Leaving in place.
[auto-repair] HNSW recall looks healthy (no 'vector ranked 0' warning)
```

## Why this fork exists

The upstream daemon focused on **stability** — semaphore-coordinated reads/writes, mine isolation, MCP-safe API key auth. JP's fork extended that into **production deployment patterns**:

1. **Single-source-of-truth daemon for distributed Claude Code sessions.** Multiple Claude Code instances (different projects, different terminals, different machines) all routing through one daemon prevents the kind of concurrent-writer SQLite corruption that took down the canonical palace on 2026-04-24. The fork's daemon-strict mode (in [techempower-org/mempalace](https://github.com/techempower-org/mempalace)) plus this daemon's queue-and-drain plus `mempal-fast.py`'s no-import path together make that single-writer guarantee enforceable.

2. **Structural snapshots for evaluation frameworks.** When SME ([multipass-structural-memory-eval](https://github.com/M0nkeyFl0wer/multipass-structural-memory-eval)) needed a structural view of the palace for diagnostics, composing it serially over MCP timed out at 60-120s. The fork added `GET /graph` so an evaluator can pull wings, rooms, tunnels, KG entities, and KG triples in one HTTP roundtrip — sub-second on a 151K-drawer palace.

3. **Operational ergonomics.** `palace-mode` for switching local/remote, `deploy.sh` for the one-command release, `verify-routes.sh` for post-restart smoke testing — these are quality-of-life pieces for a daemon that's actually used day-to-day rather than just installed.

The architectural argument for why those pieces survive backend swaps (chroma → pgvector, etc.) is in [`docs/event-log-frame.md`](docs/event-log-frame.md).

## Architectural principles

1. **Single-writer enforced by design.** SQLite + Syncthing replication + multiple writers = corruption. The daemon is the only process that writes to the palace; clients route through it via HTTP/MCP. The fork's `mempal-fast.py` and `palace-mcp-dispatch.sh` make that property hold even for hooks and MCP servers.

2. **Direct sqlite reads for structural data.** `embedding_metadata` and `knowledge_graph.sqlite3` are read-only via `?mode=ro` URI for `/graph`. Bypasses the MCP read semaphore entirely, ~200× faster than the equivalent fan-out under load. Same pattern, different table, for the KG.

3. **Themed messages for save/repair lifecycle.** `messages.py` returns user-facing strings in `systemMessage` so a Claude Code Stop hook surfaces `✦ N memories woven into the palace` without the client knowing the internal save/queue state.

4. **Coordinated rebuild with queue-and-drain.** `/repair mode=rebuild` holds every read/write/mine semaphore slot during the destructive collection swap; `/silent-save` queues to `<palace>/palace-daemon-pending.jsonl` and replays automatically post-rebuild. No saves lost during a rebuild window.

5. **Deploy and verify are the same command.** `deploy.sh` exits non-zero on sync lag, restart failure, or any verify-routes regression. The default cadence for shipping a daemon change is push + restart + verify; if any step fails the deploy aborts, leaving the previous version running.

## Setup

### Requirements
- Python 3.12+
- mempalace ≥ 3.3.2 — the [fork](https://github.com/techempower-org/mempalace) is recommended if you want daemon-strict hook mode (single-writer enforcement) and the warnings/sqlite-fallback search path that aren't yet on `MemPalace/mempalace develop`. Stock mempalace works for everything else; the fork-only `migrate_checkpoints_to_recovery` lifespan call is `ImportError`-gated and degrades cleanly.
- For the local mempalace patch (`patches/mcp_server_get_collection.patch` — log + retry on `_get_collection` failure, in flight upstream as [#1286](https://github.com/MemPalace/mempalace/pull/1286)): re-apply with `scripts/apply_patches.sh` after each `pipx upgrade mempalace` until #1286 merges.

### Install

Uses [uv](https://github.com/astral-sh/uv) for venv + dependency install — faster than `pip` and doesn't require the `python3-venv` apt package (which isn't installed by default on Ubuntu 24.04).

```bash
git clone https://github.com/techempower-org/palace-daemon.git
cd palace-daemon
uv venv venv
uv pip install --python venv/bin/python -r requirements.txt
```

<details>
<summary>pip fallback (if uv unavailable)</summary>

```bash
python3 -m venv venv          # requires apt install python3.12-venv on Ubuntu
source venv/bin/activate
pip install -r requirements.txt
```

uv is preferred; the stdlib `venv + pip` path is legacy and noted only for hosts that don't have uv installed.
</details>

### Run (manual)

```bash
# Default: port 8085, palace at $PALACE_PATH or ~/.mempalace/palace
python main.py

# Custom palace path + auth
PALACE_API_KEY=$(openssl rand -hex 32) python main.py --palace /mnt/raid/projects/mempalace-data/palace
```

### Run (systemd system service — preferred)

```bash
sudo cp palace-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now palace-daemon
```

Edit the unit file to set `User=`, `Group=`, the `ExecStart=` venv path, `PALACE_API_KEY`, `MEMPALACE_PALACE`, and any custom args before installing.

System unit is the only supported configuration. Per `CLAUDE.md` "Service Management" rule: `sudo systemctl [start|stop|restart] palace-daemon` is the canonical control path.

> [!CAUTION]
> **Never install BOTH the system unit AND a `~/.config/systemd/user/palace-daemon.service`.** Both run with `ExecStartPre=/usr/bin/fuser -k 8085/tcp` and will kill each other's listener in a cascade — the second instance's startup hook kills the first's listener, then the first restarts and kills the second. Restart counters in the hundreds within minutes is the symptom. If you have both, delete the user unit at `~/.config/systemd/user/palace-daemon.service` (along with any `.bak` siblings) and run `systemctl --user daemon-reload`.

> [!CAUTION]
> **Don't expose port 8085 without setting `PALACE_API_KEY`.** The `/mine` endpoint accepts arbitrary filesystem paths.

### Plugin client setup

Use `palace-mode install` to wire the [mempalace plugin](https://github.com/MemPalace/mempalace) cache to talk to this daemon (after pointing `PALACE_DAEMON_URL` at it):

```bash
export PALACE_DAEMON_URL=http://your-host:8085
export PALACE_API_KEY=...
./clients/palace-mode install
./clients/palace-mode verify
```

This installs `mempal-fast.py` as the Stop/PreCompact hook handler and `palace-mcp-dispatch.sh` as the MCP server command in the plugin cache. Idempotent — safe to re-run after plugin updates.

## API

| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | Liveness + version + crash-loop state; returns 503 when `degraded` or `crash_loop` |
| `/search` | GET | Semantic search over `mempalace_drawers`; `limit=N`. (Stop-hook checkpoints live in `mempalace_session_recovery` — read via the `mempalace_session_recovery_read` MCP tool.) |
| `/search/hybrid` | POST | Hybrid search — vector + BM25 + graph in one ranked set (`candidate_strategy="hybrid"`) |
| `/search/keyword` | POST | BM25 keyword search over `mempalace_drawers.doc_tsv` with optional `wing`/`room` filters |
| `/search/age-fused` | POST | Vector + AGE graph fusion search with RRF merging |
| `/context` | GET | Same as `/search`, formatted for LLM prompts |
| `/list` | GET | Query-free metadata browse — wraps `mempalace_list_drawers`. `wing=…&room=…&limit=N&offset=N`, all optional |
| `/stats` | GET | Aggregate KG + graph + status counts |
| `/graph` | GET | Single-shot structural snapshot (wings, rooms, tunnels, KG) — see [`docs/graph-endpoint.md`](docs/graph-endpoint.md) |
| `/viz` | GET | Self-contained HTML status dashboard (D3 + Mermaid). Optional `?refresh=N`, `?key=…` |
| `/cypher` | POST | Run a Cypher query against the AGE knowledge-graph; returns aliased rows (no SQL wrapper needed) |
| `/embed` | POST | Embed a list of texts via the daemon's configured embedding function; returns vectors + dim + model |
| `/repair` | POST | Coordinate repair (`mode=light\|scan\|prune\|rebuild`) |
| `/repair/status` | GET | Current repair state + pending-writes queue depth |
| `/silent-save` | POST | Stop-hook save path with queue-and-drain during rebuild |
| `/memory` | POST | Store a drawer with taxonomy enforcement (wing normalization + canonical room validation) |
| `/memory/{id}` | DELETE | Drop a drawer — wraps `mempalace_delete_drawer` |
| `/memory/{id}` | PATCH | Update drawer `content` / `wing` / `room` (all optional in body) — wraps `mempalace_update_drawer` |
| `/admin/refresh-rooms` | POST | Clear + eagerly rebuild the canonical rooms cache (after `mempalace rooms add`); returns `{refreshed, rooms, count}` |
| `/mine` | POST | Bulk import a directory (validated absolute path only) |
| `/watch` | GET | List directories the file-watcher is currently monitoring (configured via `PALACE_WATCH_DIRS`) |
| `/flush` | POST | Force checkpoint of pending writes |
| `/reload` | POST | Invalidate cached client + collection |
| `/backup` | POST | SQLite snapshot to a sibling file |
| `/mcp` | POST | MCP-protocol passthrough |

All endpoints honor `X-Api-Key` when `PALACE_API_KEY` is set.

## Development

```bash
# Smoke-test the running daemon
PALACE_DAEMON_URL=http://localhost:8085 PALACE_API_KEY=... scripts/verify-routes.sh

# One-command deploy (push + sync-wait + restart + verify)
scripts/deploy.sh

# Switch local Claude Code sessions between modes
palace-mode {status,local,remote [URL],install,verify}
```

## Sources

- [rboarescu/palace-daemon](https://github.com/rboarescu/palace-daemon) — upstream
- [MemPalace/mempalace](https://github.com/MemPalace/mempalace) — the underlying memory system this daemon fronts
- [techempower-org/mempalace](https://github.com/techempower-org/mempalace) — the production fork of mempalace this daemon is paired with
- [multipass-structural-memory-eval](https://github.com/M0nkeyFl0wer/multipass-structural-memory-eval) — the SME framework whose palace-daemon adapter consumes `/graph`
- [Apache AGE](https://age.apache.org/) — graph extension for postgres, candidate KG view technology if mempalace's KG ever justifies it (currently doesn't)
- [pgvector](https://github.com/pgvector/pgvector) — vector extension for postgres, candidate semantic-search view technology under upstream MemPalace [#665](https://github.com/MemPalace/mempalace/pull/665)
- [D3.js](https://d3js.org/) + [Mermaid](https://mermaid.js.org/) — `/viz` dashboard rendering, both via CDN, no bundler / no static-asset deps
- Upstream PRs that informed `/viz`: [#1022](https://github.com/MemPalace/mempalace/pull/1022) (D3 KG viz, sangeethkc), [#393](https://github.com/MemPalace/mempalace/pull/393) (Mermaid in docs, jravas), [#431](https://github.com/MemPalace/mempalace/pull/431) (CLI stats, MiloszPodsiadly), [#256](https://github.com/MemPalace/mempalace/pull/256) (sync_status MCP, rusel95), [#601](https://github.com/MemPalace/mempalace/pull/601) (brief overview, mvanhorn) — synthesized, not cherry-picked
- Cross-repo PRs that retire local code paths if/when they merge: [MemPalace/mempalace#1286](https://github.com/MemPalace/mempalace/pull/1286) — log + retry on `_get_collection` failure (would retire `patches/mcp_server_get_collection.patch`)

## License

MIT — same as upstream.
