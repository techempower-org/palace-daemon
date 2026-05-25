# palace-daemon TypeScript port — planning

Status: **PLANNING** — no TS code committed yet. This document is the
working artifact for the port, not the port itself. Edits welcome;
sections are explicitly marked with their decision state (`[OPEN]`,
`[LEANING]`, `[DECIDED]`).

## Why now

Two upstream signals make this the right moment to plan:

1. Ben announced on Discord (2026-04-21) that the next canonical
   implementation of mempalace is being rewritten in TypeScript and
   that Python will track the TS spec. As of 2026-04-26 the TS reference
   is not yet public — only `MemPalace/mempalace-triage` (TS) and
   `MemPalace/mempalace` (Python) exist on the org.
2. The architectural frame in [`event-log-frame.md`](event-log-frame.md)
   argues palace-daemon's value is its **role** (materialized-view
   coordinator over the event log), not its implementation details.
   The role is backend-agnostic and language-agnostic.

If we wait until the TS reference lands to start thinking, we'll be
playing catch-up. If we port the daemon's role first against the
current Python interface as the spec, we can swap the storage adapter
later once the TS reference is concrete. That's the correct shape under
the event-log frame: backends are interchangeable views; the
orchestrator is the durable contract.

This document does not commit us to building. It commits us to
articulating the decision space cleanly enough that the build (or
not-build) decision can be made on technical merit when the TS
reference lands.

## Goals

1. **Functional parity** with the Python daemon's HTTP/MCP surface, as of
   v1.7.0: `/health`, `/search`, `/context`, `/stats`, `/graph`, `/viz`,
   `/repair`, `/repair/status`, `/silent-save`, `/mine`, `/flush`,
   `/reload`, `/backup`, `/mcp`, plus the auto-repair `ExecStartPost`
   integration.
2. **Single static binary** via `bun build --compile` — install story is
   a download + chmod, no Python venv management.
3. **Cold-start under 200ms** — current Python daemon takes ~3-5s
   between systemd start and `/health` 200. Bun + minimal imports
   should drop this to sub-second.
4. **Substitutable storage backend** — the same orchestration code
   works against Python mempalace today (subprocess bridge) and TS
   mempalace tomorrow (native import) without changes to the HTTP
   layer.
5. **Coexistence with the Python daemon during migration** — both run
   on different ports, traffic moves over gradually, parity is
   measurable end-to-end before the Python daemon is retired.

## Non-goals

1. **Parity with mempalace's CLI / hooks_cli.** Those stay in Python (or
   move to TS upstream when Ben's reference lands). The daemon talks
   to mempalace; it isn't mempalace.
2. **Re-implementing chromadb / pgvector / sqlite from scratch.** The
   storage layer is whatever mempalace exposes. The daemon mediates
   access; it doesn't own the data.
3. **Ahead-of-spec TS porting.** No speculative implementations of
   API shapes the TS reference hasn't published. When in doubt, mirror
   what the Python daemon does today.
4. **Replacing the Python daemon overnight.** The Python daemon stays
   in production until the TS one has parity-verified across at least
   one full week of canonical-palace traffic.

## Stack choice [LEANING]

| Choice | Lean | Rationale |
|---|---|---|
| Runtime | **Bun** | Single binary via `bun build --compile`, native TypeScript without a build step, fast cold start, FFI for SQLite if needed. Trade-off: smaller ecosystem than Node, but the daemon's deps are minimal. |
| HTTP framework | **Hono** | Smallest viable, runs on Bun natively, no decorator magic, good streaming support. Alternative: Bun's built-in `Bun.serve()` with manual routing — even smaller, but Hono gives middleware ergonomics. |
| MCP SDK | `@modelcontextprotocol/sdk` (TS) | Anthropic's official; matches what Claude Code itself uses. Daemon's `/mcp` endpoint can re-export tools from this SDK. |
| Validation | **Zod** | Universal in TS land; request body validation for `/repair`, `/silent-save`, `/mine` etc. |
| Logging | **pino** | Structured JSON, fast. Matches systemd journal expectations. |
| Test runner | **bun test** | Built-in, no extra config. |
| File watcher (dev) | **Bun --watch** | Built-in. |

`[OPEN]` — whether to use a SQLite client library (better-sqlite3 via
Bun.sqlite) for the direct-sqlite reads (`/graph` wing/room aggregation,
KG entities/triples), or shell out to `sqlite3 file:...?mode=ro` for
parity with the read-only invariant. Bun's built-in `bun:sqlite` has
WAL support and a clean API; would prefer over a shell-out.

## Component-by-component port mapping

Each Python module/file → its TS counterpart, with port complexity tagged.

| Python | TS | Complexity | Notes |
|---|---|---|---|
| `main.py` (FastAPI app) | `src/server.ts` (Hono app) | M | Direct route mapping. Auth middleware, semaphores, request handlers all become Hono middleware/routes. |
| `_read_sem`/`_write_sem`/`_mine_sem` | `src/concurrency.ts` (custom) | S | TS doesn't have asyncio.Semaphore. Implement as a simple counting semaphore with a promise-queue. ~30 lines. |
| `_exclusive_palace()` (rebuild lock) | `src/concurrency.ts` (same) | S | Acquire all slots; same shape. |
| `_repair_state` + `_repair_lock` | module-level vars + custom mutex | S | TS state is just module vars. Mutex pattern via promise-chain. |
| `_enqueue_pending_write` / `_drain_pending_writes` | `src/queue.ts` | M | JSONL append off the event loop is just `fs.promises.appendFile`. The drain logic (rename + per-entry write-sem + quarantine-on-failure) ports cleanly. |
| `_call()` / `_unwrap()` (MCP forwarding) | `src/mcp.ts` | M | Subprocess bridge to Python mempalace OR native TS import once available — this is the swappable seam. |
| `_read_kg_direct()` | `src/sqlite.ts` (kg) | S | `bun:sqlite` open `?mode=ro`, same SELECTs. |
| `_read_wings_rooms_direct()` | `src/sqlite.ts` (chroma) | S | Same as KG, different SELECT. ChromaDB schema-drift `try/except` becomes try/catch around the prepared statement. |
| `quarantine_stale_hnsw()` integration | TS doesn't import it; calls subprocess `python -m mempalace repair scan` OR native TS once available | M | This is mempalace-side machinery; daemon just invokes it. Subprocess is fine for v1. |
| `messages.py` | `src/messages.ts` | S | Pure constants + format strings. |
| `clients/mempalace-mcp.py` (proxy) | `src/clients/mempalace-mcp.ts` | M | stdio MCP proxy → daemon. Port stays useful until Claude Code's MCP plugin system speaks daemon-HTTP natively. |
| `clients/mempal-fast.py` (Stop hook) | `src/clients/mempal-fast.ts` | S | stdlib-only counterpart already exists in spirit; TS version is JSON parse → POST. |
| `clients/palace-mode` (shell) | stays bash | — | Already shell. No port needed. |
| `clients/palace-mcp-dispatch.sh` | stays bash | — | Same. |
| `static/viz.html` | unchanged | — | Already a static HTML; the TS daemon serves it the same way. |
| `scripts/deploy.sh` | stays bash | — | The deploy host runs systemd; nothing TS-specific. |
| `scripts/verify-routes.sh` | stays bash | — | curl-based smoke test is language-agnostic; works against either daemon. |
| `scripts/auto-repair-if-empty.sh` | stays bash | — | ExecStartPost script, language-agnostic. |
| `palace-daemon.service` | unchanged shape | — | New `ExecStart` line points at the Bun binary, otherwise identical. |

S = small (30-100 lines), M = medium (100-300), L = large (300+).

## Backend adapter strategy

The daemon imports `mempalace.mcp_server` as `_mp` and calls
`_mp.handle_request({...})` for every MCP-mediated operation. That's the
seam.

**Phase 1 (TS reference not yet public):** subprocess bridge.

```
TS daemon
  └─ src/mcp.ts:_call()
        └─ spawn('python3', ['-m', 'mempalace.mcp_server.stdio'])
              ├─ stdin: JSON-RPC request
              └─ stdout: JSON-RPC response
```

Each `_call` is a one-shot subprocess (no persistent Python process) for
v0. Latency ~100-200ms per call. Acceptable for early validation, not
for production.

**Phase 1.5: persistent Python adapter.** Spawn one Python helper at
startup that holds a long-lived chromadb client and serves over a unix
socket or stdio with newline-delimited JSON-RPC. Latency drops to
~5-15ms per call (similar to native function call overhead via FFI).

**Phase 2 (TS reference public):** native TS import.

```
TS daemon
  └─ src/mcp.ts:_call()
        └─ import { handle_request } from '@mempalace/core'
              └─ direct function call, no IPC
```

Same `_call` interface; only the implementation changes. Hono routes,
semaphores, queue, viz, /graph SQL queries — all unchanged.

`[OPEN]` — does the TS reference ship a programmatic API, or only a
binary + protocol? Affects whether Phase 2 is a function-call swap or a
local-server-with-IPC swap. We design Phase 1.5 to support either by
making `_call` always go through a typed interface, even when the
implementation is in-process.

## Phased rollout

Each phase ships a usable artifact; we don't promote a phase until the
previous one is parity-verified against the canonical 150K-drawer
palace.

**Phase 0: Skeleton (1-2 evenings).** Hono server, `/health`, `/version`,
auth middleware, semaphore primitives, no mempalace integration. Goal:
prove the toolchain (Bun + Hono + bun:sqlite) compiles to a static
binary and binds a port.

**Phase 1: Read-only daemon (1 weekend).** Add `/graph`, `/stats`,
`/search`, `/context` via subprocess bridge to Python mempalace. Direct
sqlite reads for the wing/room/KG aggregation parts — those don't go
through subprocess, just bun:sqlite. Smoke-test against
`http://familiar.jphe.in:8085`'s palace via parity diffs (TS daemon points
at the same palace path; outputs should match the Python daemon byte-
for-byte modulo timing fields).

**Phase 2: Write-coordinated daemon (1 weekend).** Add `/silent-save`,
`/repair` (all four modes), `/repair/status`, `/flush`, `/reload`,
queue-and-drain for rebuild. Subprocess bridge handles writes too. The
hard part is testing — need a synthetic palace fixture that's small
enough for fast tests but exercises the queue-and-drain path.

**Phase 3: Persistent adapter + perf parity (1-2 evenings).** Replace
one-shot subprocess with persistent Python helper over unix socket.
Validate `/silent-save` and `/search` p50/p95 latency match or beat the
Python daemon.

**Phase 4: TS-native backend swap (when TS reference lands).** Replace
the persistent Python helper with a native TS import of
`@mempalace/core` (or whatever it ends up being called). Daemon
internals unchanged. Cold start drops further (no Python helper
startup).

**Phase 5: Production cutover (1 week of traffic).** Run the TS daemon
on port 8086 alongside the Python daemon on 8085. Familiar adapter
hits TS first, falls back to Python on error. After a clean week,
swap the systemd unit and retire the Python daemon to a fork branch
for reference.

## Open questions

1. **What does the TS reference ship?** (Programmatic API, daemon-only
   binary, or both?) — affects Phase 4 design materially. Awaiting Ben.
2. **Does upstream want the daemon role at all under TS?** Discussion
   #5 was the right place to ask, but we framed it postgres-side.
   Worth a follow-up once the TS reference lands — does Ben see the
   coordination layer as still load-bearing, or does the new architecture
   collapse it?
3. **MCP SDK version skew.** Claude Code's plugin MCP is server-side
   stdio. `@modelcontextprotocol/sdk` versions move; we should pin
   carefully and test against whatever Claude Code currently expects.
4. **bun:sqlite WAL behavior under concurrent writers.** We promise
   single-writer at the architectural level, but the SAME palace might
   be opened by Python mempalace (writes) and our bun:sqlite reads
   (read-only). bun:sqlite's WAL semantics need a stress test before
   we trust it.
5. **Static binary distribution.** Where does the TS daemon binary
   live? GitHub releases? A Docker image? An apt PPA? We have
   deploy.sh today; the equivalent for "drop a Bun binary on familiar"
   needs a story.
6. **Familiar's adapter.** Familiar talks to palace-daemon over HTTP.
   It shouldn't care which daemon answers — but if we add new endpoints
   in the TS daemon, Familiar needs a way to discover them. Versioning
   on `/health` is one option; `/openapi.json` is another. (Python
   daemon doesn't currently expose either.)

## Decision log

`[2026-04-26]` — Drafted this document. No code committed. Subprocess
bridge identified as the Phase 1 backend strategy; TS reference timing
unknown.

(Future entries go here as decisions land.)

## What this isn't

This is not a roadmap with deadlines. It's a planning artifact for a
project that may never start, may start partially, or may start and
get superseded by Ben's TS reference solving the problem differently.
The act of articulating the decision space is the value, even if the
codebase that emerges doesn't look exactly like the plan.

The closest precedent in this repo is
[`docs/event-log-frame.md`](event-log-frame.md) — written as a frame
for ongoing thinking, not a roadmap. Same disposition.
