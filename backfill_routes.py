"""AGE-backfill route handlers — extracted from main.py per palace-daemon#101.

The two backfill endpoints (``POST /backfill-age`` to trigger a graph
backfill subprocess, ``GET /backfill-age/status`` to poll it) plus the
run-state they share and the ``_backfill_unprocessed_breakdown`` query
helper live here on an ``APIRouter`` that main.py mounts via
``app.include_router``.

Why the handlers reference ``main.X`` rather than importing the symbols
directly: the auth gates (``_check_auth``, ``_check_viz_auth``) and the
mempalace instance (``_mp``) live in main.py, and the test suite patches
them as ``main._check_auth`` / ``main._mp`` (see
tests/test_viz_session_auth.py). Looking them up through ``main`` at
request time keeps those patches effective and the tests unmodified —
the same lazy-``import main`` pattern search_routes.py / daemon_tools.py /
fast_intercept.py use (#101 slices 3/5/6).

``_backfill_state`` and ``_backfill_lock`` are private to these handlers
(no other call site in main.py used them), so they move here outright.
They are shared by *reference* within this module — the handlers and the
background ``_run_backfill`` task mutate the dict's contents and acquire
the lock; the names are never rebound, so the behavior is identical to
when they lived in main.py.

``_backfill_unprocessed_breakdown`` is a pure query helper (takes a live
connection, mutates no module state). main.py re-exports it so
tests/test_backfill_unprocessed.py keeps resolving
``main._backfill_unprocessed_breakdown`` unchanged.

``BackfillAgeBody`` and ``record_db_error`` are imported directly from
search_models / db_errors — neither imports main, so this is safe and
non-circular (search_routes.py imports search_models the same way).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time as _time
from typing import Any

from fastapi import APIRouter, Body, Cookie, Header, HTTPException, Request

from db_errors import record_db_error as _record_db_error
from search_models import BackfillAgeBody

router = APIRouter()

# Shared run-state for the at-most-one-at-a-time backfill subprocess.
_backfill_state: dict[str, Any] = {"in_progress": False}
_backfill_lock = asyncio.Lock()


@router.post("/backfill-age")
async def backfill_age(
    request: Request,
    body: BackfillAgeBody = Body(default_factory=BackfillAgeBody),
    x_api_key: str | None = Header(default=None),
):
    """Trigger AGE graph backfill from existing drawer rows.

    Runs `mempalace-backfill-age` (or `python -m mempalace.backfill_age`)
    as a background subprocess. Returns immediately with status; poll
    /backfill-age/status for progress.

    Body (all optional)::

        {
          "wing":          null,    // restrict to one wing
          "skip_palace":   false,   // skip Wing/Room/Drawer structure
          "skip_entities": false,   // skip per-drawer entity extraction
          "restart":       false    // clear checkpoint, start fresh
        }

    Requires MEMPALACE_BACKEND=postgres.
    """
    import main

    main._check_auth(x_api_key)
    if main._mp._config.backend != "postgres":
        raise HTTPException(status_code=503, detail="backfill-age requires postgres backend")

    async with _backfill_lock:
        if _backfill_state["in_progress"]:
            return {"status": "already_running", "started_at": _backfill_state.get("started_at")}

        dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
        if not dsn:
            cfg = main._mp.MempalaceConfig()
            dsn = cfg.postgres_dsn
        if not dsn:
            raise HTTPException(status_code=500, detail="no postgres DSN available")

        # palace-daemon#179 Option C: body fields (wing, skip_palace,
        # skip_entities, restart) already validated + wing-canonicalized
        # by BackfillAgeBody at parse time.
        cmd = [sys.executable, "-m", "mempalace.backfill_age", "--dsn", dsn]
        if body.wing:
            cmd += ["--wing", body.wing]
        if body.skip_palace:
            cmd.append("--skip-palace")
        if body.skip_entities:
            cmd.append("--skip-entities")
        if body.restart:
            cmd.append("--restart")

        _backfill_state["in_progress"] = True
        _backfill_state["started_at"] = _time.monotonic()
        _backfill_state["output_lines"] = []

    async def _run_backfill():
        proc = None
        active_mines = getattr(request.app.state, "active_mines", None)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            # Track for lifespan shutdown cleanup (#136 problem B). Same set
            # the auto-mine + /mine paths use.
            if active_mines is not None:
                active_mines.add(proc)
            async for line in proc.stdout:
                decoded = line.decode().rstrip()
                _backfill_state.setdefault("output_lines", []).append(decoded)
                if len(_backfill_state["output_lines"]) > 200:
                    _backfill_state["output_lines"] = _backfill_state["output_lines"][-100:]
            await proc.wait()
            _backfill_state["returncode"] = proc.returncode
        except Exception as e:
            _backfill_state["error"] = str(e)
        finally:
            if proc is not None and active_mines is not None:
                active_mines.discard(proc)
            _backfill_state["in_progress"] = False
            _backfill_state["finished_at"] = _time.monotonic()

    asyncio.create_task(_run_backfill())
    return {"status": "started", "command": " ".join(cmd[:4]) + " ..."}


@router.get("/backfill-age/status")
async def backfill_age_status(
    x_api_key: str | None = Header(default=None),
    palace_viz_session: str | None = Cookie(default=None),
):
    """Poll backfill-age progress.

    Detects both daemon-spawned and externally-launched (parallel) workers
    by checking the checkpoint table and OS process list.
    """
    import main

    main._check_viz_auth(x_api_key, palace_viz_session)
    result = {
        "in_progress": _backfill_state["in_progress"],
    }
    if _backfill_state.get("started_at"):
        elapsed = _time.monotonic() - _backfill_state["started_at"]
        result["elapsed_seconds"] = round(elapsed, 1)
    if _backfill_state.get("output_lines"):
        result["recent_output"] = _backfill_state["output_lines"][-10:]
    if _backfill_state.get("returncode") is not None:
        result["returncode"] = _backfill_state["returncode"]
    if _backfill_state.get("error"):
        result["error"] = _backfill_state["error"]

    try:
        import psycopg2, subprocess as _sp
        dsn = os.environ.get("MEMPALACE_POSTGRES_DSN") or getattr(main._mp.MempalaceConfig(), "postgres_dsn", None)
        if dsn:
            # #110: record OperationalError on connect before allowing the outer
            # except to swallow it for graceful degradation.
            try:
                _bf_conn = psycopg2.connect(dsn, connect_timeout=5)
            except psycopg2.OperationalError as e:
                _record_db_error(e)
                raise
            with _bf_conn as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SET LOCAL statement_timeout = '5s'; "
                        "SELECT COUNT(*) FROM mempalace_kg_backfill_state WHERE phase = 'drawer'"
                    )
                    checkpointed = cur.fetchone()[0]
                with conn.cursor() as cur:
                    cur.execute(
                        "SET LOCAL statement_timeout = '5s'; "
                        "SELECT COUNT(*) FROM mempalace_drawers"
                    )
                    total = cur.fetchone()[0]
                unprocessed, reason_codes = _backfill_unprocessed_breakdown(conn)
            result["checkpointed_drawers"] = checkpointed
            result["total_drawers"] = total
            # Drawers that exist in `mempalace_drawers` but have no `drawer`
            # row in `mempalace_kg_backfill_state`. Categorized by metadata
            # `filed_at` vs the run window: drawers ingested during or after
            # the backfill cursor snapshot are the dominant cause on a healthy
            # palace; a next run picks them up. Non-zero `pre_run_unmarked`
            # means rows the run could not mark — investigate daemon logs.
            result["unprocessed_drawers"] = unprocessed
            result["unprocessed_reason_codes"] = reason_codes
            if total > 0:
                result["progress_pct"] = round(100 * checkpointed / total, 1)

            proc = _sp.run(
                ["pgrep", "-fc", "mempalace.backfill_age"],
                capture_output=True, text=True, timeout=3,
            )
            workers = int(proc.stdout.strip()) if proc.returncode == 0 else 0
            if workers > 0:
                result["in_progress"] = True
                result["workers"] = workers
    except Exception as exc:
        logging.getLogger("palace-daemon").warning("backfill-age/status enrichment failed: %s", exc)

    return result


def _backfill_unprocessed_breakdown(conn) -> tuple[int, dict[str, int]]:
    """Bucket drawers missing from the AGE backfill checkpoint by why.

    Buckets keyed off the drawer's `metadata->>'filed_at'` versus the
    backfill run window (min/max `completed_at` for `phase='drawer'`):

    - `added_during_run`: filed inside the run window — the streaming
      cursor snapshot pre-dated them.
    - `added_after_run`: filed after the last checkpoint mark — a fresh
      backfill run will pick them up.
    - `pre_run_unmarked`: filed before the run started yet never marked —
      either errored (rolled back during processing) or a partial run.
    - `no_filed_at`: metadata lacks `filed_at`; can't be bucketed.

    Returns (total_unprocessed, reason_codes). All-zero codes are omitted.
    Empty checkpoint table -> all rows are `pre_run_unmarked`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SET LOCAL statement_timeout = '10s'; "
            "WITH win AS ("
            "  SELECT MIN(completed_at) AS run_start, MAX(completed_at) AS run_end "
            "  FROM mempalace_kg_backfill_state WHERE phase = 'drawer'"
            "), gap AS ("
            "  SELECT (d.metadata->>'filed_at')::timestamptz AS filed_at "
            "  FROM mempalace_drawers d "
            "  LEFT JOIN mempalace_kg_backfill_state s "
            "    ON s.phase = 'drawer' AND s.key = d.id "
            "  WHERE s.key IS NULL"
            ") "
            "SELECT "
            "  COUNT(*) AS total, "
            "  COUNT(*) FILTER (WHERE filed_at IS NULL) AS no_filed_at, "
            "  COUNT(*) FILTER (WHERE filed_at IS NOT NULL AND filed_at < (SELECT run_start FROM win)) AS pre_run_unmarked, "
            "  COUNT(*) FILTER (WHERE filed_at IS NOT NULL "
            "                   AND filed_at >= (SELECT run_start FROM win) "
            "                   AND filed_at <= (SELECT run_end FROM win)) AS added_during_run, "
            "  COUNT(*) FILTER (WHERE filed_at IS NOT NULL AND filed_at > (SELECT run_end FROM win)) AS added_after_run "
            "FROM gap"
        )
        row = cur.fetchone()
    total, no_filed_at, pre_run, during_run, after_run = row
    codes: dict[str, int] = {
        "added_during_run": during_run,
        "added_after_run": after_run,
        "pre_run_unmarked": pre_run,
        "no_filed_at": no_filed_at,
    }
    return total, {k: v for k, v in codes.items() if v}
