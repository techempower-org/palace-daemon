# AGENTS.md — palace-daemon

Project context for AI code-review agents (Gemini Code Assist, Claude PR
Review, Copilot, etc.) reviewing changes to this repository.

## What this is

palace-daemon is the HTTP/MCP gateway in front of a mempalace. It serializes
ChromaDB (or postgres+pgvector+Apache AGE on this fork) access across
multiple concurrent clients — MCP servers, hook scripts, the Claude Code
session, and any HTTP-API consumer — so writes don't fight each other and
reads return consistent state. The fork at `techempower-org/palace-daemon`
extends upstream `rboarescu/palace-daemon` with postgres backend
support, hybrid retrieval endpoints (`/search/hybrid`, `/search/keyword`,
`/search/age-fused`), woven warnings/errors pipeline, and palace-graph
endpoints that hit the postgres aggregate path.

## Design principles

These guide review priorities and PR decisions.

- **Single-writer safety is the core invariant.** ChromaDB has no
  multi-writer concurrency model — the daemon's reason to exist is
  serializing writes across hooks, MCP clients, and direct API callers
  on one lock-or-queue surface. Any change that broadens write
  concurrency without explicit serialization is a correctness regression
  before it's a performance optimization.
- **Backend-agnostic at the HTTP surface.** Endpoints take the same body
  shape and return the same response shape whether the underlying
  mempalace is on ChromaDB or postgres+pgvector. The fork's postgres
  cutover preserved every public endpoint's contract — that's load-
  bearing. New endpoints should default to working on both backends or
  raise 503 explicitly when backend-specific (e.g., `/search/hybrid` is
  postgres-only and returns 503 on ChromaDB).
- **Hook-path latency is sacred.** Stop/PreCompact/SessionStart hooks
  run inside the user's editor session — they MUST return under 500ms
  in the happy path. The daemon's hook client at `clients/hook.py`
  enforces this via timeout + degraded-write fallback. Anything that
  makes the hook path slower than the upstream `mempalace mine`
  alternative defeats the daemon's purpose.
- **Errors are diagnostic, not silent.** Every write response returns
  `warnings: list[str]` + `errors: list[str]` (per upstream
  `mempalace#86`). The daemon normalizes these arrays even when paired
  with older mempalace versions and surfaces them in the themed
  `systemMessage` line that the hook emits. Don't suppress these into
  generic success messages.
- **Auth on every endpoint that touches data.** Every endpoint takes
  `X-API-Key` and validates against `PALACE_API_KEY` from env or
  `~/.config/palace-daemon/env`. `/health` is the only exception. New
  endpoints that bypass `_check_auth(x_api_key)` are bugs.

## Style + structure

- **HTTP endpoint convention**: `GET /search?q=` for the simple case;
  `POST /search/<variant>` with JSON body for parameterized variants.
  All bodies use snake_case keys (`include_trace`, not `includeTrace`).
- **Error responses** use `raise HTTPException(status_code=N, detail=...)`
  with specific status codes — `400` for bad input, `401` for auth,
  `503` for backend-required-but-absent, `500` reserved for unexpected
  exceptions. Don't return error envelopes in 200 responses.
- **Tests** live in `tests/` and run via the daemon's own venv (`venv/`)
  with `pytest`. Don't add tests that require a running daemon —
  use the in-process FastAPI TestClient (`from fastapi.testclient import
  TestClient`).

## What's special on this fork

The `techempower-org` fork carries substantial extensions not yet in
upstream `rboarescu/palace-daemon`. Recently landed:

- **`/search/age-fused`** endpoint (Phase 5 of the multi-project AGE
  integration; see `CHANGELOG.md` 2026-05-17) — vector + AGE graph
  RRF fusion. Requires `MEMPALACE_BACKEND=postgres` + the AGE knowledge
  graph populated via `mempalace.kg_writethrough` or
  `mempalace.backfill_age` (on the `techempower-org/mempalace:feat/age-kg-parity`
  branch).
- **Woven warnings/errors pipeline** (2026-05-15) — `warnings: list[str]`
  + `errors: list[str]` on drawer-write responses with shape-normalization
  in `messages.ensure_warnings_fields`.
- **Hybrid/keyword search endpoints** (postgres-only) — `/search/hybrid`
  routes through `mempalace.searcher.search_memories` with
  `candidate_strategy="hybrid"`; `/search/keyword` uses postgres-native
  tsvector via `_bm25_only_via_postgres`.
- **Postgres-aware `/graph`** — palace structural graph (Wing/Room/Tunnel)
  served from postgres aggregate query, not the chroma walk-and-accumulate
  pattern. OOM-safe on the 271k-drawer production palace.

## Review priorities (high → low)

1. Auth bypass in any endpoint that touches data
2. Concurrency hazards (especially writes outside the single-writer lock)
3. Backend-mismatch silent failures (route falls through when backend
   should 503)
4. Hook-path latency regressions (>500ms in the happy path)
5. Public HTTP contract changes (body shape, response shape, status codes)
6. Anything that violates the warnings/errors pipeline by suppressing
   diagnostics into success responses
7. Test coverage gaps on critical paths

## Out of scope for review

- Style nits in CHANGELOG.md, README.md, AGENTS.md, or other docs
- Coverage on the small `messages.py` formatter helpers (already 100%)
- Performance optimization in `clients/hook.py` paths that are
  already under their budget (Stop hook <100ms, PreCompact <500ms)
