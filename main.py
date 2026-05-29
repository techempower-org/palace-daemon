"""
palace-daemon — HTTP/MCP gateway for MemPalace with concurrent access control

Three semaphores govern concurrency (all tunable via PALACE_MAX_CONCURRENCY):
  _read_sem  — up to N concurrent read-only ops (search, query, stats, …)
  _write_sem — up to N//2 concurrent write ops (add, update, kg mutations, …)
  _mine_sem  — one mine job at a time, independent of reads/writes

Roadmap:
  [DONE] Verified Backups: /backup endpoint with integrity_check + smoke test retrieval.
  [DONE] Stability: Auto-detect "Internal Error" during search and trigger index recovery.
  [DONE] Flush: Ensure memories are checkpointed on shutdown and via /flush.
  [DONE] Unified Routing: Ensure all clients (including miners/compactors) use the Daemon API.
  [DONE] Maintenance: Automate _READ_TOOLS sync with upstream mempalace.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import sys
import fcntl
import signal
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Cookie, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

# Hard fail if hnswlib isn't importable. ChromaDB has no error path for
# this — it silently degrades to brute-force on small in-memory batches,
# and the persistence layer (hnswlib.Index.save_index) is unreachable so
# nothing ever gets written to disk. Symptom is "segments stuck in
# partial-flush shape forever" which is brutal to diagnose. See #10.
try:
    import hnswlib  # noqa: F401
except ImportError as _hnsw_err:
    print(
        "FATAL: hnswlib is not importable in this venv. ChromaDB will silently\n"
        "       degrade to brute-force search with NO HNSW persistence.\n"
        "       Install the chroma-maintained binary fork:\n"
        f"         {sys.executable.rsplit('/', 1)[0]}/pip install --no-deps chroma-hnswlib\n"
        "       (Or with uv: uv pip install --python <venv-python> --no-deps chroma-hnswlib)\n"
        f"       Underlying error: {_hnsw_err}",
        file=sys.stderr,
    )
    sys.exit(1)

import mempalace.mcp_server as _mp
from mempalace import repair as _mp_repair
from mempalace.backends.chroma import quarantine_stale_hnsw

import messages
import rerank as _rerank

# ── Config (env vars override CLI defaults) ───────────────────────────────────

VERSION = "1.9.1"
DEFAULT_HOST = os.getenv("PALACE_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("PALACE_PORT", "8085"))
DEFAULT_PALACE = os.getenv("PALACE_PATH", "")
API_KEY = os.getenv("PALACE_API_KEY", "")  # read at startup for argparse default; auth checks re-read from env dynamically
PALACE_MAX_CONCURRENCY = int(os.getenv("PALACE_MAX_CONCURRENCY", "4"))
PALACE_MAX_READ_CONCURRENCY = int(os.getenv("PALACE_MAX_READ_CONCURRENCY", str(PALACE_MAX_CONCURRENCY)))
PALACE_MAX_WRITE_CONCURRENCY = int(os.getenv("PALACE_MAX_WRITE_CONCURRENCY", str(max(1, PALACE_MAX_CONCURRENCY // 2))))
# Per-tool ceiling for /mcp. Surfaces a JSON-RPC error envelope instead of an
# indefinite TCP-level hang when an MCP tool stalls (issue #49). Mine and any
# tool listed in _MCP_TIMEOUT_EXEMPT are excluded — they're legitimately long.
# 0 disables the timeout entirely.
PALACE_MCP_TOOL_TIMEOUT_SECONDS = float(os.getenv("PALACE_MCP_TOOL_TIMEOUT_SECONDS", "60"))
# When on (default), /mcp intercepts mempalace_status and mempalace_kg_stats and
# returns from the daemon's direct-SQL fast paths (~100× faster than the upstream
# Python-side aggregations at our production scale). Set to 0 to fall through to
# the slow path — useful when you need the full relationship_types list that the
# fast kg_stats can't enumerate cheaply. Issue #49.
PALACE_MCP_FAST_INTERCEPT = os.getenv("PALACE_MCP_FAST_INTERCEPT", "1") not in ("0", "false", "False", "")

# Canonical topic for Stop-hook auto-save checkpoint diary entries.
# Defined here so /silent-save can canonicalize at the daemon boundary
# even when client code drifts. Must match clients/hook.py and
# clients/mempal-fast.py. mempalace's tool_diary_write routes drawers
# with this topic to the dedicated mempalace_session_recovery collection.
CHECKPOINT_TOPIC = "checkpoint"
# Legacy synonyms that older clients (or future buggy ones) might write.
# When /silent-save sees one of these, it rewrites to CHECKPOINT_TOPIC
# and emits a warning log line. The write-side router accepts both.
CHECKPOINT_TOPIC_SYNONYMS = ("auto-save",)

# Read ops: up to PALACE_MAX_READ_CONCURRENCY concurrent.
# Write ops: up to PALACE_MAX_WRITE_CONCURRENCY concurrent.
# Set PALACE_MAX_WRITE_CONCURRENCY=1 to serialise writes (mitigates MemPalace
# issue #1161 — HNSW num_threads not persisted in ChromaDB 1.5.x).
# Mine jobs: exclusive semaphore independent of reads/writes so a long mine
# doesn't starve normal traffic.
_read_sem = asyncio.Semaphore(PALACE_MAX_READ_CONCURRENCY)
_write_sem = asyncio.Semaphore(PALACE_MAX_WRITE_CONCURRENCY)
_mine_sem = asyncio.Semaphore(1)

# Repair state — when in_progress is True, /silent-save queues instead of writing.
# The fast-path check is lock-free (single-assignment dict); _repair_lock serializes
# start/end transitions and prevents overlapping repairs.
_repair_state: dict[str, Any] = {"in_progress": False, "mode": None, "started_at": None}
_repair_lock = asyncio.Lock()

_log = logging.getLogger("palace-daemon")

# ── Crash-loop detection (#101 eighth slice) ───────────────────────────────
# Lives in crash_loop.py now. main.py keeps the `_`-prefixed names alive via
# re-export so the lifespan handler and /health endpoint keep working
# unchanged. The module-level STARTUP_MONOTONIC is captured at crash_loop's
# import time, which happens during main.py's load — so the auto-recovery
# clock still measures daemon process uptime as expected.
from crash_loop import (  # noqa: E402
    CRASH_LOOP_DIR as _CRASH_LOOP_DIR,
    CRASH_LOOP_RECOVERY as _CRASH_LOOP_RECOVERY,
    CRASH_LOOP_THRESHOLD as _CRASH_LOOP_THRESHOLD,
    CRASH_LOOP_WINDOW as _CRASH_LOOP_WINDOW,
    RESTART_HISTORY_PATH as _RESTART_HISTORY_PATH,
    STARTUP_MONOTONIC as _STARTUP_MONOTONIC,
    crash_loop_state as _crash_loop_state,
    record_restart as _record_restart,
)

# Optional settle margin after the deterministic chroma client close in the
# /mine lock-and-reopen choreography. close_palace() releases chromadb's
# Rust-side SQLite file lock synchronously, so 0.0 is correct for normal
# palaces; this is only a safety knob for very large palaces. (#29)
PALACE_CHROMA_FLUSH_SECONDS = float(os.getenv("PALACE_CHROMA_FLUSH_SECONDS", "0.0"))


# ── Rebuild progress capture (#101 tenth slice) ────────────────────────────
# The stdout-capturing buffer + regex parsers live in rebuild_progress.py now.
# main.py re-exports under the original `_`-prefixed names so the /repair
# handler and any external operators reading the public surface keep working.
import contextlib  # noqa: E402,F401  — kept in main's namespace; other paths use it
import time as _time  # noqa: E402,F401  — used elsewhere in main

from rebuild_progress import (  # noqa: E402
    REBUILD_RE_FOUND as _REBUILD_RE_FOUND,
    REBUILD_RE_REFILED as _REBUILD_RE_REFILED,
    REBUILD_RE_STAGED as _REBUILD_RE_STAGED,
    RebuildProgressBuffer as _RebuildProgressBuffer,
    capture_rebuild_progress as _capture_rebuild_progress,
    make_rebuild_progress_state as _make_rebuild_progress_state,
)


# ── Systemd watchdog / sd_notify ─────────────────────────────────────────────

def _sd_notify(msg: str) -> None:
    """Send a message to systemd notify socket without external dependencies."""
    sock_path = os.environ.get("NOTIFY_SOCKET", "")
    if not sock_path:
        return
    try:
        import socket as _sock
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_DGRAM) as s:
            # Abstract namespace sockets use NUL prefix; systemd uses @ prefix.
            addr = chr(0) + sock_path[1:] if sock_path.startswith("@") else sock_path
            s.sendto(msg.encode(), addr)
    except Exception:
        pass


def _watchdog_interval() -> int:
    """Return WatchdogSec in seconds from WATCHDOG_USEC (set by systemd), or 0."""
    try:
        return int(os.environ.get("WATCHDOG_USEC", "0")) // 1_000_000
    except ValueError:
        return 0


async def _watchdog_loop(interval_secs: int) -> None:
    """Ping systemd watchdog at half the watchdog interval, only when palace is healthy.

    Honor CancelledError so the lifespan shutdown can stop us cleanly —
    otherwise uvicorn hangs on "Waiting for background tasks to complete"
    until systemd SIGKILLs at TimeoutStopSec.
    """
    tick = max(10, interval_secs // 2)
    while True:
        try:
            await asyncio.sleep(tick)
        except asyncio.CancelledError:
            return
        # During mode=rebuild, send the keepalive unconditionally and skip the
        # probe. A rebuild holds _exclusive_palace() with the client/collection
        # caches nulled (see the /repair handler), so _get_collection() can
        # return None or block for the whole 6-9h operation. The health-gate
        # below would then withhold WATCHDOG=1 and systemd would SIGABRT the
        # daemon mid-rebuild — exactly when a kill is most destructive. Keep
        # feeding the watchdog; the rebuild is a known long-running operation
        # we want to run to completion.
        if _repair_state.get("in_progress") and _repair_state.get("mode") == "rebuild":
            _sd_notify("WATCHDOG=1\n")
            continue
        try:
            loop = asyncio.get_running_loop()
            col = await loop.run_in_executor(None, _mp._get_collection)
            if col is not None:
                _sd_notify("WATCHDOG=1\n")
            else:
                _log.warning("Watchdog: palace collection unavailable — skipping WATCHDOG=1")
        except Exception as e:
            _log.warning("Watchdog check failed: %s", e)


async def _warn_if_hnsw_threads_unset() -> None:
    """Warn if hnsw:num_threads != 1 after a collection reopen.

    ChromaDB 1.5.x does not persist HNSW metadata across reopens (MemPalace
    issue #1161). After any cache clear the collection silently reverts to
    parallel inserts, risking SIGSEGV under concurrent writes.

    No-op when MEMPALACE_BACKEND != "chroma" — the HNSW thread-pinning warning
    is a chroma-only concern. Without this gate, postgres-backed daemons see
    confusing "MemPalace issue #1161" log noise that doesn't apply. (#14)
    """
    if getattr(_mp._config, "backend", "chroma") != "chroma":
        return
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _mp.handle_request, {
            "jsonrpc": "2.0", "id": "hnsw-check", "method": "ping", "params": {}
        })
        col = _mp._collection_cache
        meta = (col and getattr(col, "_collection", None) and
                getattr(col._collection, "metadata", None)) or {}
        threads = meta.get("hnsw:num_threads")
        if threads != 1:
            _log.warning(
                "HNSW num_threads=%s after collection reopen — parallel inserts active. "
                "Concurrent writes risk SIGSEGV. See MemPalace issue #1161. "
                "Upgrade to mempalace >=3.3.4 when available.",
                threads,
            )
    except Exception:
        pass


# Tools that only read state — everything else is treated as a write.
# Synced against mempalace 3.3.5 (mempalace/mcp_server.py tool_* functions).
_READ_TOOLS = {
    "mempalace_check_duplicate",
    "mempalace_diary_read",
    "mempalace_find_tunnels",
    "mempalace_follow_tunnels",
    "mempalace_get_aaak_spec",
    "mempalace_get_drawer",
    "mempalace_get_taxonomy",
    "mempalace_graph_stats",
    "mempalace_hook_settings",
    "mempalace_kg_query",
    "mempalace_kg_stats",
    "mempalace_kg_timeline",
    "mempalace_list_drawers",
    "mempalace_list_rooms",
    "mempalace_list_tags",
    "mempalace_list_tunnels",
    "mempalace_list_wings",
    "mempalace_memories_filed_away",
    "mempalace_search",
    "mempalace_status",
    "mempalace_traverse",
    "mempalace_walk_palace",
}

# Tools exempt from PALACE_MCP_TOOL_TIMEOUT_SECONDS because they're
# legitimately long-running (a mine can take minutes on a large palace).
# Anything not in this set, including writes, is bounded — a hung MCP call
# producing no response is strictly worse than a JSON-RPC timeout error.
_MCP_TIMEOUT_EXEMPT = {
    "mempalace_mine",  # subprocess that scans an entire repo
}


# Valid /mine request values, shared by the live endpoint and the rebuild
# drain replay. Kept module-level so the two paths can't drift — a previous
# local redefinition let "session" mode diverge, silently dropping queued
# session mines on drain (Copilot finding on jphein/palace-daemon#5).
_MINE_VALID_MODES = {"convos", "projects", "session"}
_MINE_VALID_EXTRACTS = {"exchange", "general"}


# ── Auth helpers (#101 ninth slice) ────────────────────────────────────────
# Strict header check + /viz session cookie helpers live in auth.py now.
# main.py keeps the `_`-prefixed names alive via re-export so the route
# handlers, lifespan, and existing tests keep working unchanged.
#
# The PALACE_VIZ_SESSION_TTL_SECONDS / PALACE_VIZ_COOKIE_SECURE constants
# live in auth.py too — the one test that patches the TTL was updated to
# patch `auth.PALACE_VIZ_SESSION_TTL_SECONDS` rather than `main.*`.
from auth import (  # noqa: E402
    PALACE_VIZ_COOKIE_SECURE,
    PALACE_VIZ_SESSION_TTL_SECONDS,
    VIZ_COOKIE_NAME as _VIZ_COOKIE_NAME,
    check_auth as _check_auth,
    check_viz_auth as _check_viz_auth,
    mint_viz_token as _mint_viz_token,
    valid_viz_token as _valid_viz_token,
)


# ── Path-map translation (#101 eleventh slice) ─────────────────────────────
# Lives in path_map.py now. main.py keeps the `_`-prefixed names alive via
# re-export so the /mine, /silent-save, watcher translator, and existing
# tests in test_path_translation.py / test_mine_*.py keep working unchanged.
from path_map import (  # noqa: E402
    PATH_MAP_USE_ENV as _PATH_MAP_USE_ENV,
    parse_path_map as _parse_path_map,
    translate_client_path as _translate_client_path,
)


def _sem_for(request_dict: dict) -> asyncio.Semaphore:
    method = request_dict.get("method", "")
    if method == "ping":
        return _read_sem
    # JSON-RPC params can be null or a positional list; only the dict form
    # carries a tool name. Treat everything else as a write so we err on the
    # side of holding the write semaphore for malformed requests.
    params = request_dict.get("params")
    tool_name = params.get("name", "") if isinstance(params, dict) else ""
    return _read_sem if tool_name in _READ_TOOLS else _write_sem


def _drop_chroma_client(close: bool = True) -> None:
    """Drop the daemon's chroma client caches; optionally release the Rust lock.

    Both the mcp-local caches (``_client_cache``/``_collection_cache``) and the
    pooled ``ChromaBackend._clients`` handle reference the same PersistentClient,
    so dropping the caches alone does not release chromadb's Rust-side SQLite
    file lock — that lock is held until ``PersistentClient.close()`` runs.

    close=True routes the release through ``close_palace()`` (→ ``_close_client()``
    → ``client.close()``) for a *deterministic* lock release. This is required
    before handing the palace to an external writer (the /mine subprocess): a
    bare cache drop would leave the daemon's stale client locking the path when
    the subprocess opens its own client, reproducing the dual-client log-store
    corruption this guards against (#29). Must not be the leaky
    ``_force_chroma_cache_reset()`` (mempalace#262), which only pops the dict.

    close=False keeps the legacy cache-only drop for shutdown, where the process
    is exiting and the OS reclaims the lock regardless.
    """
    _mp._collection_cache = None
    _mp._client_cache = None
    if close:
        from mempalace.palace import get_backend
        get_backend("chroma").close_palace(_mp._config.palace_path)


async def _auto_repair():
    """Trigger index recovery and reload the mempalace client."""
    loop = asyncio.get_running_loop()
    palace_path = _mp._config.palace_path
    moved = await loop.run_in_executor(None, quarantine_stale_hnsw, palace_path)
    if moved:
        _log.warning("AUTO-REPAIR: Quarantined %d stale HNSW segments. Reloading client.", len(moved))
        # Cache-only drop: we lazily reopen on the next request, and quarantine
        # already moved the bad segments aside — no external writer involved,
        # so no need to force the Rust lock release here.
        _drop_chroma_client(close=False)
        await _warn_if_hnsw_threads_unset()
        return len(moved)
    _log.info("AUTO-REPAIR: No stale segments found during scan.")
    return 0


# ── Exclusive palace context (for rebuild) ───────────────────────────────────

@asynccontextmanager
async def _exclusive_palace():
    """Acquire every semaphore slot — no daemon-mediated work runs until release.

    Used by /repair mode=rebuild, which deletes and recreates the collection
    (backend-level, outside the ChromaCollection flock). Any in-flight write
    would race with the delete/create and be lost. Holding every slot makes
    sure nothing daemon-mediated is mid-flight when the rebuild starts.
    """
    read_slots = PALACE_MAX_CONCURRENCY
    write_slots = max(1, PALACE_MAX_CONCURRENCY // 2)
    r_held = 0
    w_held = 0
    m_held = False
    try:
        for _ in range(read_slots):
            await _read_sem.acquire()
            r_held += 1
        for _ in range(write_slots):
            await _write_sem.acquire()
            w_held += 1
        await _mine_sem.acquire()
        m_held = True
        yield
    finally:
        if m_held:
            _mine_sem.release()
        for _ in range(w_held):
            _write_sem.release()
        for _ in range(r_held):
            _read_sem.release()


# ── Pending-writes queue (held during rebuild) ───────────────────────────────

def _pending_writes_path() -> str:
    """Location of the jsonl queue that holds silent-saves during rebuild."""
    palace_path = _mp._config.palace_path
    parent = os.path.dirname(palace_path.rstrip("/")) or os.path.expanduser("~")
    return os.path.join(parent, "palace-daemon-pending.jsonl")


async def _enqueue_pending_write(payload: dict) -> None:
    """Append a silent-save payload to the pending-writes queue (off-loop)."""
    path = _pending_writes_path()
    line = json.dumps({"payload": payload, "enqueued_at": datetime.now().isoformat()})

    def _append():
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    await asyncio.to_thread(_append)


def _pending_mines_path() -> str:
    """Location of the jsonl queue that holds /mine requests during rebuild.

    Separate from the silent-save queue because mines are fire-and-forget
    subprocess invocations rather than diary writes — replayed via the
    same /mine subprocess pattern, not _do_silent_save_write.
    """
    palace_path = _mp._config.palace_path
    parent = os.path.dirname(palace_path.rstrip("/")) or os.path.expanduser("~")
    return os.path.join(parent, "palace-daemon-pending-mines.jsonl")


async def _enqueue_pending_mine(payload: dict) -> None:
    """Append a /mine request payload to the pending-mines queue (off-loop)."""
    path = _pending_mines_path()
    line = json.dumps({"payload": payload, "enqueued_at": datetime.now().isoformat()})

    def _append():
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    await asyncio.to_thread(_append)


async def _drain_pending_mines() -> int:
    """Replay queued /mine requests after a rebuild completes.

    Same rename-then-read pattern as _drain_pending_writes — concurrent
    /mine POSTs landing during the drain go to a fresh pending file.
    Each entry is replayed by spawning the same subprocess the live
    /mine endpoint would, gated by _mine_sem. Failed entries are
    quarantined to a timestamped .failed-* file.
    """
    path = _pending_mines_path()
    if not os.path.isfile(path):
        return 0
    proc_path = path + ".processing"
    try:
        os.rename(path, proc_path)
    except OSError:
        return 0
    count = 0
    failed_lines: list[str] = []
    try:
        with open(proc_path, encoding="utf-8") as f:
            lines = [ln for ln in f.readlines() if ln.strip()]
        # Dedup queued mines by (dir, wing, mode) — re-mining the same dir
        # is the goal, not running it N times. A storm of hook fires during
        # rebuild may have queued the same target dozens of times; one
        # successful drain replay covers them all.
        seen: set = set()
        unique_entries: list = []
        for line in reversed(lines):  # keep newest of each (dir, wing, mode)
            try:
                entry = json.loads(line)
                payload = entry.get("payload", {})
                key = (payload.get("dir"), payload.get("wing"), payload.get("mode", "convos"))
                if key in seen:
                    continue
                seen.add(key)
                unique_entries.append((line, entry))
            except json.JSONDecodeError:
                failed_lines.append(line)
        # Replay in original order
        unique_entries.reverse()
        # Module-level _MINE_VALID_* sets, shared with the live /mine
        # endpoint — apply them on replay too so a queue entry can't smuggle
        # through a value the live endpoint would reject, and the two paths
        # can't drift (Copilot findings on jphein/palace-daemon#4 and #5).
        for line, entry in unique_entries:
            try:
                payload = entry["payload"]
                raw_dir = payload.get("dir")
                if not isinstance(raw_dir, str) or not raw_dir:
                    _log.warning("drain-mine: skipping entry — invalid 'dir'")
                    continue
                directory = _translate_client_path(raw_dir)
                dir_path = Path(directory)
                # Same path-shape gate as /mine (absolute + no traversal).
                if not dir_path.is_absolute() or ".." in dir_path.parts:
                    _log.warning(
                        "drain-mine: skipping %s — non-absolute or contains '..'", raw_dir
                    )
                    continue
                if not dir_path.is_dir():
                    _log.warning("drain-mine: skipping %s — not a directory", directory)
                    continue
                wing = payload.get("wing", "general")
                mode = payload.get("mode", "convos")
                if mode not in _MINE_VALID_MODES:
                    _log.warning("drain-mine: skipping %s — invalid mode %r", directory, mode)
                    continue
                extract = payload.get("extract")
                if extract is not None and extract not in _MINE_VALID_EXTRACTS:
                    _log.warning(
                        "drain-mine: skipping %s — invalid extract %r", directory, extract
                    )
                    continue
                limit = payload.get("limit")
                if limit is not None:
                    try:
                        limit = int(limit)
                    except (TypeError, ValueError):
                        _log.warning(
                            "drain-mine: skipping %s — invalid limit %r", directory, limit
                        )
                        continue
                mempalace_bin = os.path.join(os.path.dirname(sys.executable), "mempalace")
                cmd = [mempalace_bin, "mine", directory, "--mode", mode, "--wing", wing]
                # Re-apply optional fields the original /mine accepted but
                # the prior drain dropped silently (Copilot finding on #4).
                if extract:
                    cmd += ["--extract", extract]
                if limit:
                    cmd += ["--limit", str(limit)]
                async with _mine_sem:
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    # Track for lifespan shutdown cleanup (#136 problem B).
                    # active_mines may be None during the at-startup drain
                    # call (before lifespan startup completes); the post-
                    # rebuild /repair drain has it populated.
                    active_mines = getattr(app.state, "active_mines", None)
                    if active_mines is not None:
                        active_mines.add(proc)
                    try:
                        stdout, stderr = await proc.communicate()
                    finally:
                        if active_mines is not None:
                            active_mines.discard(proc)
                if proc.returncode == 0:
                    count += 1
                else:
                    _log.warning(
                        "drain-mine: replay returned %s for %s\n  stderr: %s",
                        proc.returncode,
                        directory,
                        (stderr or b"")[:1200].decode(errors="replace")[:300],
                    )
                    failed_lines.append(line)
            except Exception:
                _log.exception("drain-mine: entry replay raised")
                failed_lines.append(line)
        if failed_lines:
            qpath = proc_path + ".failed-" + datetime.now().strftime("%Y%m%d%H%M%S")
            with open(qpath, "w", encoding="utf-8") as f:
                f.writelines(failed_lines)
            _log.warning("drain-mine: %d entries quarantined at %s", len(failed_lines), qpath)
        os.remove(proc_path)
    except Exception:
        _log.exception("drain-mine: read failed; leaving %s in place", proc_path)
    return count


async def _drain_pending_writes() -> int:
    """Replay queued silent-saves after a rebuild completes.

    Rename-then-read so a concurrent /silent-save appending after the rename
    lands in a fresh pending file, not the one we're draining. Each entry is
    replayed under _write_sem to honour _do_silent_save_write's contract.
    Failed entries are quarantined to a timestamped file so the next drain
    pass doesn't replay successful saves.
    """
    path = _pending_writes_path()
    if not os.path.isfile(path):
        return 0
    proc_path = path + ".processing"
    try:
        os.rename(path, proc_path)
    except OSError:
        return 0
    count = 0
    failed_lines: list[str] = []
    try:
        with open(proc_path, encoding="utf-8") as f:
            lines = [ln for ln in f.readlines() if ln.strip()]
        for line in lines:
            try:
                entry = json.loads(line)
                async with _write_sem:
                    result = await _do_silent_save_write(entry["payload"])
                if result.get("success"):
                    count += 1
                else:
                    _log.warning("drain: replay failed: %s", result.get("error"))
                    failed_lines.append(line)
            except Exception:
                _log.exception("drain: entry replay raised")
                failed_lines.append(line)
        if failed_lines:
            qpath = proc_path + ".failed-" + datetime.now().strftime("%Y%m%d%H%M%S")
            with open(qpath, "w", encoding="utf-8") as f:
                f.writelines(failed_lines)
            _log.warning("drain: %d entries quarantined at %s", len(failed_lines), qpath)
        os.remove(proc_path)
    except Exception:
        _log.exception("drain: read failed; leaving %s in place", proc_path)
    return count


def _canonical_topic(topic) -> str:
    """Canonicalize a Stop-hook checkpoint topic at the daemon boundary.

    Synonyms become ``CHECKPOINT_TOPIC`` with a warning log so client-side
    drift is visible. Any other string is left as-is — the caller may
    have legitimately used a non-checkpoint topic name on this diary
    write (e.g. "musings", "decisions") and we shouldn't clobber that.

    Non-string inputs (``None``, numbers, lists from a malformed JSON
    payload) collapse to ``CHECKPOINT_TOPIC`` with a warning, rather
    than leaking through to ``tool_diary_write`` and turning a bad
    client request into an internal type error.
    """
    if not isinstance(topic, str):
        _log.warning(
            "silent-save: non-string topic %r (%s); coercing to %r",
            topic, type(topic).__name__, CHECKPOINT_TOPIC,
        )
        return CHECKPOINT_TOPIC
    if topic in CHECKPOINT_TOPIC_SYNONYMS:
        _log.warning(
            "silent-save: rewriting non-canonical checkpoint topic %r → %r",
            topic, CHECKPOINT_TOPIC,
        )
        return CHECKPOINT_TOPIC
    return topic


async def _do_silent_save_write(payload: dict) -> dict:
    """Write a diary checkpoint via tool_diary_write in an executor.

    Caller is expected to hold _write_sem. Returns mempalace's raw dict
    (typically {"success": True, "entry_id": ...} or {"success": False, "error": ...}).
    """
    raw_wing = payload.get("wing", "") or ""
    # Normalize wing the same way /memory POST does so /silent-save's
    # diary entries are reachable by the same wing filter post-#175.
    # Pre-fix: hook sends wing="Palace_Daemon" → stored as "Palace_Daemon";
    # /search?wing=Palace_Daemon normalizes to "palace_daemon" → misses.
    # Empty wing stays empty — /silent-save's handler warns on empty
    # rather than coercing to "unknown", so preserve that contract here.
    wing = _normalize_wing_slug(raw_wing) if raw_wing else ""
    entry = payload.get("entry", "")
    topic = _canonical_topic(payload.get("topic", CHECKPOINT_TOPIC))
    agent_name = payload.get("agent_name", "session-hook")
    loop = asyncio.get_running_loop()

    def _work():
        from mempalace.mcp_server import tool_diary_write
        return tool_diary_write(
            agent_name=agent_name,
            entry=entry,
            topic=topic,
            wing=wing,
        )

    try:
        return await loop.run_in_executor(None, _work)
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _call(request_dict: dict, retry_on_hnsw: bool = True) -> dict:
    async with _sem_for(request_dict):
        loop = asyncio.get_running_loop()
        # JSON-RPC params can be a dict, an array, or null; only the dict
        # form has a tool name. Be defensive so malformed requests turn
        # into a normal error envelope instead of a 500.
        params = request_dict.get("params")
        tool_name = params.get("name", "") if isinstance(params, dict) else ""
        # Bound every MCP tool except the long-running mine. A handler that
        # never returns produces no ASGI response bytes at all (issue #49) —
        # surfacing as a JSON-RPC timeout error is strictly more debuggable
        # than a silent client-side TCP timeout. asyncio.wait_for cancels the
        # awaitable, but a thread spawned via run_in_executor keeps running;
        # the daemon eats the work, the caller gets the error envelope.
        timeout = (
            None
            if PALACE_MCP_TOOL_TIMEOUT_SECONDS <= 0 or tool_name in _MCP_TIMEOUT_EXEMPT
            else PALACE_MCP_TOOL_TIMEOUT_SECONDS
        )
        try:
            fut = loop.run_in_executor(None, _mp.handle_request, request_dict)
            result = await (asyncio.wait_for(fut, timeout) if timeout else fut)
        except asyncio.TimeoutError:
            return {
                "jsonrpc": "2.0",
                "id": request_dict.get("id"),
                "error": {
                    "code": -32001,
                    "message": (
                        f"MCP tool {tool_name!r} exceeded "
                        f"PALACE_MCP_TOOL_TIMEOUT_SECONDS={PALACE_MCP_TOOL_TIMEOUT_SECONDS}s"
                    ),
                },
            }
        except Exception as e:
            return {"jsonrpc": "2.0", "id": request_dict.get("id"), "error": {"code": -32000, "message": str(e)}}

        try:
            if result and "error" in result:
                msg = str(result["error"].get("message", ""))
                is_hnsw_error = "Internal error: Error finding id" in msg or "Internal error: id" in msg
                if is_hnsw_error and retry_on_hnsw and tool_name in _READ_TOOLS:
                    # Auto-repair and retry ONCE (write ops are excluded: retrying risks duplicate drawers)
                    repaired_count = await _auto_repair()
                    if repaired_count > 0:
                        return await loop.run_in_executor(None, _mp.handle_request, request_dict)

                    result["error"]["message"] += " (Daemon hint: HNSW index stale. Auto-repair attempted but index might still be inconsistent)"
                elif is_hnsw_error and tool_name not in _READ_TOOLS:
                    result["error"]["message"] += " (Daemon hint: HNSW error on write op — manual /reload may be needed)"
            return result or {}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": request_dict.get("id"), "error": {"code": -32000, "message": str(e)}}


_TRUTHY_FLAG = frozenset({"1", "true", "yes", "on"})


# Canary helpers extracted to canaries.py per #101 refactor (continued slice).
# main.py keeps the private _-prefixed names via re-export so existing tests
# that patch `main._log_mempalace_canary` etc. keep working unchanged.
from canaries import newest_mempalace_mtime as _newest_mempalace_mtime  # noqa: E402
from canaries import log_mempalace_canary as _log_mempalace_canary  # noqa: E402
from canaries import postgres_memcg_status as _postgres_memcg_status  # noqa: E402
from canaries import log_postgres_memcg_canary as _log_postgres_memcg_canary  # noqa: E402


def _log_kg_writethrough_stages(env, logger) -> None:
    """Log each KG write-through stage's on/off state by env flag (issue #76).

    mempalace logs one generic "KG write-through attached" line on collection
    init, but the composer reads two independent env flags that compose two
    different stages: MEMPALACE_KG_WRITETHROUGH (cheap entity-MENTIONS path,
    runs on every drawer write) and MEMPALACE_KG_EXTRACTION_QUEUE (enqueue
    each drawer for async LLM triple extraction). One stage being silently
    OFF is invisible in the generic log — and on 2026-05-27 that silent-OFF
    meant ~12,300 drawers were never enqueued for triple extraction.

    Log each stage by its env flag so operators can confirm in journalctl
    which stages actually attached on this boot.
    """
    def _on(name: str) -> str:
        # Defensive over arbitrary dict callers: `os.environ` only ever stores
        # strings, but a hand-crafted mapping (tests, YAML/JSON config) might
        # carry None, bools, or ints. Coerce to string before .strip() so the
        # helper never raises AttributeError on the input type.
        #   None  → '' (OFF), False → '' (OFF), True → 'True' (ON),
        #   0     → '' (OFF), 1     → '1' (ON), '1'/'true'/'yes'/'on' → ON.
        return "on" if str(env.get(name) or "").strip().lower() in _TRUTHY_FLAG else "OFF"

    logger.info(
        "KG write-through stages: MENTIONS=%s (MEMPALACE_KG_WRITETHROUGH); "
        "EXTRACTION_QUEUE=%s (MEMPALACE_KG_EXTRACTION_QUEUE)",
        _on("MEMPALACE_KG_WRITETHROUGH"),
        _on("MEMPALACE_KG_EXTRACTION_QUEUE"),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging
    logger = logging.getLogger(__name__)

    _record_restart()
    cl_state = _crash_loop_state()
    if cl_state["crash_loop"]:
        msg = (
            f"palace-daemon CRASH-LOOP: {cl_state['restart_count']} restarts "
            f"in {cl_state['window_seconds']}s"
        )
        logger.critical(msg)
        # Best-effort desktop notification — don't fail if notify-send is
        # missing (headless server, SSH-only, etc.).
        try:
            import subprocess
            subprocess.Popen(
                ["notify-send", "--urgency=critical", "palace-daemon", msg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass
        except Exception:
            pass

    # Uvicorn installs its own SIGINT/SIGTERM handlers that shut down gracefully;
    # we don't need to override them. Calling sys.exit() from inside an asyncio
    # signal handler tears the event loop down mid-coroutine and skips lifespan
    # shutdown (the flush). Leave signal handling to uvicorn.

    # Stale-HNSW quarantine is a chromadb-segment salvage step. On postgres
    # backends, the indexes live in pgvector and this preflight has nothing
    # to do — running it I/O-walks the legacy chroma sqlite + segment dirs at
    # every startup, and any noise it logs is misleading. Gate behind backend
    # detection. (#14)
    if getattr(_mp._config, "backend", "chroma") == "chroma":
        moved = quarantine_stale_hnsw(_mp._config.palace_path)
        if moved:
            logger.warning(
                "Quarantined %d stale HNSW segment(s) — ChromaDB will rebuild indexes: %s",
                len(moved), moved,
            )

    # Migrate Stop-hook auto-save checkpoints from the main searchable
    # collection into the dedicated mempalace_session_recovery collection
    # so they don't dominate vector top-N. Idempotent — re-runs return 0
    # once the canonical palace has reorganized. Gated behind
    # PALACE_AUTO_MIGRATE_CHECKPOINTS so operators can disable in
    # environments where the one-time migration cost is unwanted. The
    # migration shape is also exposed via `mempalace repair --mode reorganize`
    # for explicit operator-driven runs.
    if os.environ.get("PALACE_AUTO_MIGRATE_CHECKPOINTS", "1") != "0":
        try:
            from mempalace.migrate import migrate_checkpoints_to_recovery

            loop = asyncio.get_running_loop()
            moved_checkpoints = await loop.run_in_executor(
                None, migrate_checkpoints_to_recovery, _mp._config.palace_path
            )
            if moved_checkpoints:
                logger.info(
                    "Migrated %d checkpoint drawer(s) from main → mempalace_session_recovery; "
                    "mempalace_search now queries content-only.",
                    moved_checkpoints,
                )
        except ImportError as e:
            # Distinguish "mempalace.migrate or migrate_checkpoints_to_recovery
            # genuinely unavailable on this mempalace release line" (feature
            # gating, expected) from "import failed for some other reason
            # — e.g. a transitive dep missing inside mempalace.migrate"
            # (real error, surface at warning level).
            if getattr(e, "name", None) == "mempalace.migrate":
                logger.debug(
                    "mempalace.migrate not available on this release; skipping auto-migrate."
                )
            else:
                logger.warning(
                    "Auto-migrate skipped — unexpected ImportError from mempalace.migrate: %s",
                    e,
                )
        except Exception as e:
            logger.warning("Auto-migrate of checkpoints failed (non-fatal): %s", e)

    # Warm the ChromaDB client before accepting traffic. The Rust HNSW binding
    # occasionally segfaults on the very first request if opened cold; opening
    # it here (before yield) ensures the PersistentClient is fully initialized.
    # We open the collection directly (not via ping) so that _get_collection's
    # hnsw:num_threads=1 fix is applied before _warn_if_hnsw_threads_unset runs.
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _mp._get_collection, True)
        logger.info("Palace client warmed up.")
    except Exception as e:
        logger.warning("Warmup collection open failed (non-fatal): %s", e)
    _log_kg_writethrough_stages(os.environ, logger)
    _log_mempalace_canary(logger)
    _log_postgres_memcg_canary(logger)
    await _warn_if_hnsw_threads_unset()

    # Signal systemd that startup is complete (Type=notify in service file).
    _sd_notify("READY=1\n")

    # Start systemd watchdog loop if WatchdogSec is configured.
    # Stash the task on app.state so shutdown can cancel it cleanly —
    # otherwise uvicorn will wait on it until systemd SIGKILLs.
    app.state.watchdog_task = None
    wdog_secs = _watchdog_interval()
    if wdog_secs > 0:
        app.state.watchdog_task = asyncio.create_task(_watchdog_loop(wdog_secs))
        logger.info("Systemd watchdog active (interval=%ds, tick=%ds).", wdog_secs, max(10, wdog_secs // 2))

    # File-watcher service: mines configured directories on file change.
    # Configured via PALACE_WATCH_DIRS (comma-separated path[=wing]).
    # Mirrors the pattern used by the /mine endpoint above.
    app.state.watcher = None
    # Active auto-mine subprocesses (#136 problem B). Tracked here so the
    # lifespan shutdown can terminate them cleanly before systemd has to
    # SIGKILL the cgroup. _internal_mine adds the proc on spawn and removes
    # it on completion via try/finally.
    app.state.active_mines = set()
    try:
        from watcher import WatcherService, make_async_mine_fn, parse_watch_dirs

        # Translate watch paths from the client namespace to the daemon
        # namespace BEFORE the is_dir() check. Without this, an env var
        # written as /home/jp/Projects/... would always be skipped on
        # the daemon (Copilot finding on jphein/palace-daemon#2).
        targets = parse_watch_dirs(translator=_translate_client_path)
        loop = asyncio.get_running_loop()

        async def _internal_mine(path: str, wing: str) -> None:
            # parse_watch_dirs already translated the path; this guard
            # catches edge cases (target deleted after startup, etc.).
            dir_path = Path(path)
            if not dir_path.is_dir():
                logger.warning("watcher: skipping mine for non-dir path %s", path)
                return
            # Bench-active lock (#104) — pause auto-mine while an external
            # bench is driving the daemon hard. The lock file is touched by
            # the bench runner; daemon checks per-tick and skips spawning.
            # Stale locks (default >6h old) are ignored so a crashed bench
            # doesn't wedge auto-mine indefinitely.
            active, reason = _bench_lock_active()
            if active:
                logger.info(
                    "watcher: auto_mine_paused (reason=bench-active.lock present, %s, path=%s, wing=%s)",
                    reason, path, wing,
                )
                return
            mempalace_bin = os.path.join(os.path.dirname(sys.executable), "mempalace")
            argv = [mempalace_bin, "mine", path, "--mode", "projects", "--wing", wing]
            # Same pattern as /mine endpoint: list-form argv, no shell.
            async with _mine_sem:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                # Track the proc so the lifespan shutdown can terminate it
                # cleanly (#136 problem B). discard() in finally is
                # idempotent and tolerant of the proc already being gone.
                app.state.active_mines.add(proc)
                try:
                    stdout, stderr = await proc.communicate()
                finally:
                    app.state.active_mines.discard(proc)
            if proc.returncode != 0:
                # Surface the actual subprocess output — the rc alone hides
                # 'No mempalace.yaml found' / 'directory not readable' /
                # python tracebacks that operators need to diagnose.
                # Closes Copilot finding on jphein/palace-daemon#3.
                logger.warning(
                    "watcher mine returned %s for %s\n  stderr: %s\n  stdout: %s",
                    proc.returncode,
                    path,
                    (stderr or b"")[:2000].decode(errors="replace")[:500],
                    (stdout or b"")[-2000:].decode(errors="replace")[-500:],
                )

        watcher = WatcherService(make_async_mine_fn(loop, _internal_mine))
        watcher.start(targets)
        # Only publish the watcher to app.state when it actually started
        # observing — otherwise GET /watch would report running:true on
        # an idle/disabled service (Copilot finding on jphein/palace-daemon#2).
        if watcher.is_running:
            app.state.watcher = watcher
    except Exception as e:
        logger.warning("File-watcher startup failed (non-fatal): %s", e)

    yield

    # --- Shutdown: cancel background tasks first so uvicorn isn't blocked ---
    logger.info("Lifespan: shutting down, flushing memories...")
    wdog_task = getattr(app.state, "watchdog_task", None)
    if wdog_task is not None and not wdog_task.done():
        wdog_task.cancel()
        try:
            await asyncio.wait_for(wdog_task, timeout=3.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    # Terminate any active auto-mine subprocesses before stopping the watcher
    # (#136 problem B). Without this, systemd has to SIGKILL the cgroup at
    # TimeoutStopSec, leaving the child process to be reaped at the cgroup
    # boundary rather than via the daemon's own teardown.
    #
    # Order: SIGTERM all, then wait briefly for each, then SIGKILL stragglers.
    # The total cleanup budget is configurable via
    # PALACE_MINE_SHUTDOWN_TIMEOUT_S (default 3) — must stay well under
    # systemd's TimeoutStopSec (30s) given everything else in shutdown.
    active_mines = list(getattr(app.state, "active_mines", set()) or ())
    if active_mines:
        mine_shutdown_s = float(os.environ.get("PALACE_MINE_SHUTDOWN_TIMEOUT_S", "3"))
        logger.info(
            "Lifespan: terminating %d active mine subprocess(es) (budget=%.1fs)",
            len(active_mines), mine_shutdown_s,
        )
        for proc in active_mines:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning("mine terminate failed (pid=%s): %s", getattr(proc, "pid", "?"), e)
        for proc in active_mines:
            try:
                await asyncio.wait_for(proc.wait(), timeout=mine_shutdown_s)
            except asyncio.TimeoutError:
                logger.warning(
                    "mine pid=%s did not exit within %.1fs after SIGTERM; sending SIGKILL",
                    getattr(proc, "pid", "?"), mine_shutdown_s,
                )
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                except Exception as e:
                    logger.warning("mine kill failed (pid=%s): %s", getattr(proc, "pid", "?"), e)
    try:
        watcher = getattr(app.state, "watcher", None)
        if watcher is not None:
            watcher.stop()
            logger.info("WatcherService stopped.")
    except Exception:
        logger.exception("WatcherService stop failed (non-fatal)")
    # Shutdown flush — bounded so it can't exceed systemd's TimeoutStopSec
    # and force the SIGKILL escalation path (#136). The MCP `_call` wrapper
    # has its own PALACE_MCP_TOOL_TIMEOUT_SECONDS guard (default 60s), which
    # is longer than the 30s systemd budget — so we wrap the call with an
    # outer wait_for that's safely below TimeoutStopSec. If the flush
    # exceeds the deadline we log and continue teardown rather than letting
    # systemd hammer the daemon with SIGKILL mid-checkpoint.
    SHUTDOWN_FLUSH_TIMEOUT_S = float(os.environ.get("PALACE_SHUTDOWN_FLUSH_TIMEOUT_S", "10"))
    try:
        await asyncio.wait_for(
            _call(
                {
                    "jsonrpc": "2.0", "id": "shutdown",
                    "method": "tools/call",
                    "params": {"name": "mempalace_memories_filed_away", "arguments": {}},
                },
                retry_on_hnsw=False,
            ),
            timeout=SHUTDOWN_FLUSH_TIMEOUT_S,
        )
        logger.info("Flush complete.")
    except asyncio.TimeoutError:
        logger.warning(
            "Shutdown flush exceeded %.1fs (PALACE_SHUTDOWN_FLUSH_TIMEOUT_S); "
            "continuing teardown to stay under systemd TimeoutStopSec.",
            SHUTDOWN_FLUSH_TIMEOUT_S,
        )
    except Exception as e:
        logger.error("Error during shutdown flush: %s", e)

    # --- Shutdown: explicit ChromaDB client teardown ---
    # We keep the cache-only drop (close=False) here rather than the
    # deterministic close() that the /mine path uses: the process is exiting,
    # so the OS reclaims the file lock regardless, and this gc+sleep dance is
    # the battle-tested path for letting chromadb's background flush threads
    # finish before exit. The flush above triggers a checkpoint; this block
    # ensures the client is torn down — drop refs, force a GC pass, then sleep
    # briefly so chromadb's background flush threads finish writing
    # before the process exits. Without this, SIGTERM at the wrong
    # millisecond leaves the HNSW segment in partial-flush corruption:
    # data_level0.bin written, link_lists.bin not yet, the chromadb
    # metadata file missing. The integrity gate then quarantines on
    # next open and we burn cycles rebuilding the index every restart.
    # See #8.
    try:
        _drop_chroma_client(close=False)
        import gc
        gc.collect()
        # Two seconds is empirically enough on this palace; chromadb's
        # internal flush thread completes its work in ~hundreds of ms
        # after the refs are dropped. If your palace is much larger,
        # consider raising this — but the daemon will be SIGKILLed at
        # TimeoutStopSec (30s by default), so don't exceed that.
        # asyncio.sleep, not time.sleep — we're still inside the event loop.
        await asyncio.sleep(2.0)
        logger.info("ChromaDB client torn down cleanly.")
    except Exception as e:
        logger.error("Error during chromadb teardown (non-fatal): %s", e)


app = FastAPI(title="palace-daemon", lifespan=lifespan)


# ── MCP proxy ─────────────────────────────────────────────────────────────────

@app.post("/mcp")
async def mcp_proxy(request: Request, x_api_key: str | None = Header(default=None)) -> JSONResponse:
    _check_auth(x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    # Fast-intercept the two tools that scan the full corpus upstream
    # (mempalace_status sweeps chroma metadata; mempalace_kg_stats runs
    # three full Cypher graph walks). Both have direct-SQL equivalents
    # that return the same envelope shape in sub-millisecond time.
    # The dispatch table is built per-request rather than at module load
    # so the helpers can live next to their /status/fast / /graph cousins
    # below — they aren't defined yet at this point in the source file.
    # Issue #49.
    params = body.get("params") if isinstance(body, dict) else None
    tool = params.get("name") if isinstance(params, dict) else None
    arguments = params.get("arguments") if isinstance(params, dict) else None
    if not isinstance(arguments, dict):
        arguments = {}

    # Fast-intercepts that have an upstream MCP equivalent — failures fall
    # through to the slow path so behaviour matches the upstream MCP server.
    fast_fn = None
    if PALACE_MCP_FAST_INTERCEPT and tool in ("mempalace_status", "mempalace_kg_stats"):
        fast_fn = {
            "mempalace_status": _fast_mcp_status_payload,
            "mempalace_kg_stats": _fast_mcp_kg_stats_payload,
        }[tool]
    if fast_fn is not None:
        loop = asyncio.get_running_loop()
        try:
            payload = await loop.run_in_executor(None, fast_fn)
            envelope = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}]
                },
            }
            return JSONResponse(content=envelope)
        except Exception as e:
            # #108: if the fast-path failed because postgres bounced, record
            # it on the observability ring buffer so /health.db_errors
            # reflects the failure. The fall-through to the slow path
            # below is unchanged — behavioural parity with upstream is
            # preserved.
            try:
                import psycopg2 as _ps2
                if isinstance(e, _ps2.OperationalError):
                    _record_db_error(e)
            except Exception:
                pass
            _log.warning("fast-intercept %s failed (%s); falling back to /mcp slow path", tool, e)

    # Daemon-native tools (#93): rooms CRUD + wakeup + mined. These have no
    # upstream MCP equivalent — the CLI commands they replace open a local
    # ChromaDB client which breaks under daemon-strict mode. JSON-RPC error
    # codes (-32602 invalid params, -32004 backend down, -32000 internal)
    # let the CLI consumer branch on failure mode rather than guessing.
    native_fn = _DAEMON_NATIVE_TOOLS.get(tool)
    if native_fn is not None:
        loop = asyncio.get_running_loop()
        try:
            payload = await loop.run_in_executor(None, native_fn, arguments)
            envelope = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "content": [{"type": "text", "text": json.dumps(payload, default=str)}]
                },
            }
            return JSONResponse(content=envelope)
        except _DaemonToolError as e:
            envelope = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": e.code, "message": str(e), "data": e.data},
            }
            return JSONResponse(content=envelope)
        except Exception as e:
            _log.exception("daemon-native %s failed", tool)
            # If the underlying cause is a DB OperationalError (postgres
            # bouncing mid-query, not at connect time — _connect_postgres
            # already records connect-time errors), capture it for the
            # /health observability hook so the silent-failure pattern
            # from #97 stays visible.
            try:
                import psycopg2 as _ps2
                if isinstance(e, _ps2.OperationalError):
                    _record_db_error(e)
            except Exception:
                pass
            envelope = {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": -32000, "message": f"internal error: {e}"},
            }
            return JSONResponse(content=envelope)

    # tools/list augmentation (#140). The upstream mempalace MCP server
    # doesn't know about the 6 daemon-native tools registered in
    # daemon_tools.DAEMON_NATIVE_TOOLS, so its tools/list response misses
    # them and MCP clients (Claude Code, Claude Desktop, anyone using the
    # standard discovery handshake) can't find them. We forward to
    # upstream, then merge in the daemon-native descriptors before
    # returning. tools/call already routes through the dispatch above
    # — this just closes the discovery gap.
    method = body.get("method") if isinstance(body, dict) else None
    if method == "tools/list":
        response = await _call(body)
        try:
            from daemon_tools import DAEMON_NATIVE_TOOL_DESCRIPTORS
            upstream_tools = response.get("result", {}).get("tools", []) if isinstance(response, dict) else []
            existing_names = {t.get("name") for t in upstream_tools if isinstance(t, dict)}
            # Skip any descriptor whose name already appears upstream so
            # we never produce a duplicate. (Defensive — the daemon-native
            # names were chosen to not collide, but a future mempalace
            # release adding the same names would otherwise silently
            # produce duplicates.)
            additions = [
                d for d in DAEMON_NATIVE_TOOL_DESCRIPTORS
                if d["name"] not in existing_names
            ]
            if additions and isinstance(response, dict) and isinstance(response.get("result"), dict):
                response["result"]["tools"] = list(upstream_tools) + additions
        except Exception:
            _log.exception("tools/list augmentation failed (non-fatal)")
        return JSONResponse(content=response)

    response = await _call(body)
    return JSONResponse(content=response)


# ── REST convenience endpoints ────────────────────────────────────────────────

@app.get("/health")
async def health():
    # Bypass semaphores — health must respond even when all slots are busy.
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _mp.handle_request, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}) or {}
    # Test actual collection access so /health reflects true palace state.
    palace_ok = False
    try:
        col = await loop.run_in_executor(None, _mp._get_collection)
        palace_ok = col is not None
    except Exception as e:
        # /health degrades to "degraded" (503) when the collection can't
        # open. Log the specific cause so operators don't have to guess
        # whether it's postgres down, AGE init failure, or a mempalace bug.
        # Lesson from #157: silent except: pass hides bugs for weeks.
        _log.warning("/health: collection open failed → status=degraded: %s", e)
    cl = _crash_loop_state()
    # #143: crash_loop is informational, NOT a service-down signal.
    # Returning 503 for crash_loop caused monitoring tools to interpret
    # "had a few deploys recently" as "service is failing" — exactly
    # backwards. status now reflects whether the palace can actually
    # serve traffic; the crash_loop fields stay in the body as
    # observability data but don't drive the HTTP code.
    status = "ok" if palace_ok else "degraded"
    # #97 observability hooks: db_errors counter + postgres memcg pressure.
    # Both are cheap (deque scan + one docker-stats fork bounded at 2s).
    # If the memcg probe fails (docker down, container missing) we omit
    # the field rather than degrading /health's status.
    db_errors = _db_errors_summary(window_s=300.0)
    memcg = await loop.run_in_executor(None, _postgres_memcg_status)
    payload = {
        "status": status, "daemon": "palace-daemon", "version": VERSION,
        "palace": result, **cl,
        "db_errors": db_errors,
    }
    if memcg is not None:
        payload["postgres_memcg"] = memcg
    # 503 only when palace_ok=False (the daemon truly can't serve). A
    # crash_loop=True reading with palace_ok=True returns 200 with the
    # informational fields populated, so monitoring tools can read
    # restart_count without auto-restarting the service.
    if status != "ok":
        return JSONResponse(content=payload, status_code=503)
    return payload


def _search_args(query: str, limit: int) -> dict:
    """Build the mempalace_search MCP tool arguments dict.

    Param-name fidelity matters: the MCP tool's input_schema declares
    ``limit`` and unknown keys are silently dropped by the
    schema-whitelist filter in ``mempalace.mcp_server.handle_request``.
    Earlier daemon versions passed ``max_results`` here, which never
    bound and quietly capped every /search response at the default 5.
    """
    return {"query": query, "limit": limit}


@app.get("/search")
async def search(
    q: str,
    limit: int = 5,
    wing: str | None = None,
    room: str | None = None,
    x_api_key: str | None = Header(default=None),
):
    """Semantic search over the main `mempalace_drawers` collection.
    Stop-hook auto-save checkpoints live in the dedicated
    `mempalace_session_recovery` collection and are not surfaced here —
    use the `mempalace_session_recovery_read` MCP tool for those.

    `wing` and `room` are optional exact-match filters forwarded to
    ``mempalace_search``. Pre-2026-05-16 this endpoint silently dropped
    those params (FastAPI strips unknown query args, and the signature
    didn't accept them) — callers asking for scoped results got
    palace-wide results back instead.
    """
    _check_auth(x_api_key)
    # Validate room so a typo gets a fast 400 (vs an empty-result surprise
    # from a non-matching filter). Same contract as /search/hybrid,
    # /search/keyword, /search/age-fused (all routed through this helper).
    _rooms.validate_room_or_raise(room)
    # Normalize wing so a caller's "Palace_Daemon" matches the stored
    # "palace_daemon" written by POST /memory's normalization. Pre-fix
    # asymmetric — writes normalized, reads didn't.
    wing = _rooms.normalize_wing_filter(wing)
    args = _search_args(q, limit)
    if wing:
        args["wing"] = wing
    if room:
        args["room"] = room
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": args},
    })
    return _rerank.rerank_response(q, _unwrap(result))


# ── Postgres-native BM25 search ──────────────────────────────────────
#
# Phase 2 of the hybrid-search-taxonomy initiative (familiar.realm.watch
# spec §3.6). The daemon issues postgres tsvector queries directly
# rather than routing through the mempalace_search MCP tool — the MCP
# path is vector-only and lives in chromadb-shaped code.
#
# 503 when backend is chroma. The chroma path has its own BM25
# fallback via _bm25_only_via_sqlite, surfaced through
# candidate_strategy="union" on the existing /search endpoint.


@app.post("/search/hybrid")
async def search_hybrid(request: Request, x_api_key: str | None = Header(default=None)):
    """Hybrid search: vector + BM25 + graph in a single ranked result set.

    Phase 4 of the hybrid-search-taxonomy initiative. Routes through
    mempalace's ``search_memories`` with ``candidate_strategy="hybrid"``,
    which:
      1. Runs vector candidate selection (existing)
      2. Unions BM25 candidates from postgres tsvector (Phase 2)
      3. Adds graph-expanded drawers — vector-seeded entity expansion
         AND query-NER entity matching (Phase 3)
      4. Reranks the combined pool with the hybrid scorer

    Body::

        {
          "query":         "pgvector advisory lock race",
          "wing":          "memorypalace",      // optional, exact-match filter
          "room":          "problems",          // optional, canonical only
          "limit":         10,
          "include_trace": false                // optional, attaches per-source
                                                // counts + latencies if true
        }

    Returns the same hit shape as /search; each hit has a `matched_via`
    field naming the source (vector, bm25_postgres, graph_seeded,
    graph_ner) which the trace flag surfaces.

    Requires postgres backend.
    """
    _check_auth(x_api_key)
    if _mp._config.backend != "postgres":
        raise HTTPException(
            status_code=503,
            detail="/search/hybrid requires MEMPALACE_BACKEND=postgres; daemon is on chroma.",
        )

    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="'query' is required and must be non-empty")
    wing = _rooms.normalize_wing_filter(body.get("wing"))
    room = body.get("room") or None
    limit = int(body.get("limit") or 10)
    include_trace = bool(body.get("include_trace") or False)
    # fusion_mode (#105): pass-through to mempalace's search_memories so
    # callers can A/B convex vs RRF at production scale. Mempalace's MCP
    # schema-whitelist currently drops unknown keys, so this needs the
    # companion mempalace#298 to land before it has end-to-end effect.
    # We accept + validate the value here so the daemon's input surface
    # is forward-compatible.
    fusion_mode = body.get("fusion_mode")
    if fusion_mode is not None:
        if not isinstance(fusion_mode, str) or fusion_mode not in ("convex", "rrf"):
            raise HTTPException(
                status_code=400,
                detail="'fusion_mode' must be 'convex' or 'rrf'",
            )
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="'limit' must be 1..100")
    _rooms.validate_room_or_raise(room)

    args = {
        "query": query,
        "limit": limit,
        "candidate_strategy": "hybrid",
    }
    if wing:
        args["wing"] = wing
    if room:
        args["room"] = room
    args["include_trace"] = include_trace
    if fusion_mode is not None:
        args["fusion_mode"] = fusion_mode

    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": args},
    })
    return _rerank.rerank_response(query, _unwrap(result))


@app.post("/search/keyword")
async def search_keyword(request: Request, x_api_key: str | None = Header(default=None)):
    """BM25 keyword search over mempalace_drawers.doc_tsv.

    Body::

        {
          "query": "pgvector lazy index race",
          "wing":  "memorypalace",          // optional, exact-match filter
          "room":  "problems",              // optional, must be canonical if set
          "limit": 20
        }

    Returns the same result shape as ``/search`` for callers that mix
    the two (each hit has id, document, wing, room, metadata, score).
    Uses ``websearch_to_tsquery`` for user-friendly query parsing
    (phrase syntax, OR, negation).
    """
    _check_auth(x_api_key)
    if _mp._config.backend != "postgres":
        raise HTTPException(
            status_code=503,
            detail="/search/keyword requires MEMPALACE_BACKEND=postgres; daemon is on chroma.",
        )

    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="'query' is required and must be non-empty")
    wing = _rooms.normalize_wing_filter(body.get("wing"))
    room = body.get("room") or None
    limit = int(body.get("limit") or 20)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="'limit' must be 1..200")

    # Validate room if provided so callers get fast feedback (vs an
    # empty-result silent surprise from a typo).
    _rooms.validate_room_or_raise(room)

    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
    if not dsn:
        raise HTTPException(status_code=500, detail="MEMPALACE_POSTGRES_DSN not set in daemon environment")

    from mempalace.searcher import _bm25_only_via_postgres
    result = _bm25_only_via_postgres(query, dsn, wing=wing, room=room, n_results=limit)
    return _rerank.rerank_response(query, result)


@app.post("/search/age-fused")
async def search_age_fused(request: Request, x_api_key: str | None = Header(default=None)):
    """Vector + AGE graph fusion search (Phase 5 of the AGE-integration work).

    Combines mempalace's vector retrieval with AGE entity-overlap on the
    write-through graph populated by kg_writethrough.py + backfill_age.py.
    Returns RRF-merged results so callers that want graph-aware retrieval
    don't have to fuse client-side.

    Body::

        {
          "query":         "pgvector advisory lock race",
          "wing":          "memorypalace",   // optional
          "room":          "problems",       // optional
          "limit":         10,
          "graph_top_k":   50,                // graph candidates to fetch
          "fusion_k":      60,                // RRF k constant
          "include_trace": false              // attach per-source counts
        }

    Returns the same hit shape as /search, plus an optional ``trace``
    field with {n_vector, n_graph, n_after_fusion}. Each hit has an
    extra ``matched_via`` key (``"vector"``, ``"graph"``, or ``"both"``).

    Requires:
      - MEMPALACE_BACKEND=postgres (AGE lives in postgres)
      - The kg_writethrough hook has populated MENTIONS edges (either via
        write-through on writes or via mempalace.backfill_age)

    Empty graph or extractor producing zero entities falls through to
    vector-only — the endpoint never errors on a missing AGE state.
    """
    _check_auth(x_api_key)
    if _mp._config.backend != "postgres":
        raise HTTPException(
            status_code=503,
            detail="/search/age-fused requires MEMPALACE_BACKEND=postgres; daemon is on chroma.",
        )

    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="'query' is required and must be non-empty")
    wing = _rooms.normalize_wing_filter(body.get("wing"))
    room = body.get("room") or None
    limit = int(body.get("limit") or 10)
    graph_top_k = int(body.get("graph_top_k") or 50)
    fusion_k = int(body.get("fusion_k") or 60)
    include_trace = bool(body.get("include_trace") or False)

    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="'limit' must be 1..200")
    if graph_top_k < 1 or graph_top_k > 1000:
        raise HTTPException(status_code=400, detail="'graph_top_k' must be 1..1000")
    if fusion_k < 1 or fusion_k > 1000:
        raise HTTPException(status_code=400, detail="'fusion_k' must be 1..1000")

    # Validate room against the canonical set so a typo gets a fast 400
    # (not an empty-result surprise from a non-matching filter). Same
    # contract as /search/hybrid and /search/keyword. Pre-fix this
    # endpoint accepted any room string and silently produced empty
    # vector results when it didn't match — surfaced today during PR #172
    # live validation.
    _rooms.validate_room_or_raise(room)

    # Step 1: Vector retrieval via mempalace_search (existing MCP tool).
    vec_result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": _search_args(
            query,
            # Over-fetch so RRF has more candidates to work with.
            max(graph_top_k, limit * 3),
        ) | ({"wing": wing} if wing else {}) | ({"room": room} if room else {})},
    })
    vec_hits = (_unwrap(vec_result) or {}).get("results") or []

    # Step 2: AGE graph entity-overlap.
    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
    if not dsn:
        # No AGE access — fall through to vector-only with a warning trace.
        if include_trace:
            return _rerank.rerank_response(query, {"results": vec_hits[:limit], "trace": {
                "n_vector": len(vec_hits), "n_graph": 0, "n_after_fusion": min(limit, len(vec_hits)),
                "warning": "MEMPALACE_POSTGRES_DSN not set; age-fused falls back to vector-only",
            }})
        return _rerank.rerank_response(query, {"results": vec_hits[:limit]})

    # Initialize *before* the AGE lookup so the trace block can read it
    # even when the lookup raises before extraction happens.
    query_entities: list = []
    graph_hits_by_drawer: dict[str, float] = {}

    def _age_lookup() -> tuple[list, dict[str, float]]:
        """Sync AGE entity-overlap lookup. Called via ``asyncio.to_thread``
        so the daemon's event loop isn't blocked on Postgres I/O.

        #157: AGE's Cypher parser rejected the original ``RETURN d.id AS id,
        r.count AS count`` form with a SyntaxError ("syntax error at or near
        AS") — multi-AS RETURN with a relationship property is unsupported
        in this AGE version. Every call raised, the per-entity try/except
        silently swallowed it, and graph_hits_by_drawer stayed empty
        (n_graph=0 in /search/age-fused's trace).

        Workaround: use ``properties(r) AS edge_props`` which returns the
        full edge property map (verified against AGE 1.5 on familiar). The
        Python code below extracts ``count`` from that map; missing/null
        falls back to 1, matching the previous default."""
        from mempalace.knowledge_graph_age import KnowledgeGraphAGE
        kg = KnowledgeGraphAGE(dsn)
        hits: dict[str, float] = {}
        extractor = _load_age_extractor()
        qents = extractor(query) if extractor else []
        try:
            for qe in qents:
                try:
                    rows = kg._run_cypher(
                        """
                        MATCH (d:Drawer)-[r:MENTIONS]->(e:Entity {name: $ename})
                        RETURN d.id AS drawer_id, properties(r) AS edge_props
                        """,
                        {"ename": qe.name},
                        fetch=True,
                    )
                except Exception as e:
                    # Don't swallow silently — log so future Cypher-syntax
                    # regressions are visible. The original per-entity try/
                    # except was hiding #157 for weeks.
                    logging.warning(
                        "/search/age-fused: AGE Cypher failed for entity %r: %s",
                        getattr(qe, "name", qe), e,
                    )
                    continue
                for r in rows:
                    drawer_id = kg._unwrap_agtype(r[0])
                    edge_props = kg._unwrap_agtype(r[1]) or {}
                    cnt = (edge_props.get("count") if isinstance(edge_props, dict) else None) or 1
                    if drawer_id:
                        hits[str(drawer_id)] = hits.get(str(drawer_id), 0) + int(cnt)
        finally:
            kg.close()
        return qents, hits

    try:
        query_entities, graph_hits_by_drawer = await asyncio.to_thread(_age_lookup)
    except Exception as e:
        # AGE not available — log + fall through.
        logging.warning("/search/age-fused: AGE lookup failed: %s — falling back to vector-only", e)

    # Step 3: RRF fusion. Vector rank by position; graph rank by overlap count.
    # Vector hits from mempalace_search expose the drawer id as `drawer_id`
    # (not `id`) — pre-#150 the `hit.get("id")` lookup returned None for
    # every hit, collapsing vec_ranks to {None: last_index} and effectively
    # disabling the vector half of the fusion. Falling back to `drawer_id`
    # restores the intended ranking.
    vec_ranks = {(hit.get("id") or hit.get("drawer_id")): i for i, hit in enumerate(vec_hits)}
    graph_ranks = {did: i for i, did in enumerate(sorted(graph_hits_by_drawer, key=lambda d: -graph_hits_by_drawer[d])[:graph_top_k])}

    union = set(vec_ranks) | set(graph_ranks)
    fused_scores: dict[str, float] = {}
    for did in union:
        score = 0.0
        if did in vec_ranks:
            score += 1.0 / (fusion_k + vec_ranks[did])
        if did in graph_ranks:
            score += 1.0 / (fusion_k + graph_ranks[did])
        fused_scores[did] = score

    # Build the merged result list — preserve full hit metadata when
    # vector saw the drawer; hydrate graph-only drawers from postgres so
    # the response shape matches /search (palace-daemon#150). Pre-#150 the
    # graph-only stubs had document=None and no text field, which caused
    # bench consumers (LongMemEval, /context) to see ~5.5× narrower
    # context vs /search default and a corresponding QA-acc regression.
    vec_by_id = {(hit.get("id") or hit.get("drawer_id")): hit for hit in vec_hits}
    fused_order = sorted(fused_scores.items(), key=lambda kv: -kv[1])[:limit]

    # Pre-fetch text + metadata for any graph-only drawers in one query
    # so we don't N+1 the database. Vector-matched drawers already have
    # their full hit dict from mempalace_search and don't need hydration.
    graph_only_ids = [did for did, _ in fused_order if did not in vec_by_id]
    hydrated: dict[str, dict] = {}
    if graph_only_ids:
        def _hydrate_drawers(ids: list[str]) -> dict[str, dict]:
            import psycopg2
            try:
                with psycopg2.connect(dsn, connect_timeout=3) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SET LOCAL statement_timeout = '5s'")
                        cur.execute(
                            "SELECT id, content, wing, room, "
                            "       COALESCE(metadata->>'topic', '') AS topic, "
                            "       COALESCE(metadata->>'source_file', '') AS source_file, "
                            "       created_at "
                            "FROM mempalace_drawers WHERE id = ANY(%s)",
                            (ids,),
                        )
                        return {
                            r[0]: {
                                "text": r[1] or "",
                                "wing": r[2],
                                "room": r[3],
                                "topic": r[4],
                                "source_file": r[5],
                                "created_at": r[6].isoformat() if r[6] else None,
                            }
                            for r in cur.fetchall()
                        }
            except Exception as e:
                logging.warning("/search/age-fused: graph-only hydration failed: %s", e)
                return {}
        hydrated = await asyncio.to_thread(_hydrate_drawers, graph_only_ids)

    out_hits: list[dict] = []
    for did, score in fused_order:
        if did in vec_by_id:
            hit = dict(vec_by_id[did])
            hit["matched_via"] = "both" if did in graph_ranks else "vector"
            hit["rrf_score"] = score
        else:
            # Graph-only drawer — emit a hit matching /search's shape so
            # bench consumers see the same context width. If hydration
            # failed (postgres bounced etc.), fall back to the historic
            # minimal-stub shape so the response is still valid.
            row = hydrated.get(did)
            if row:
                hit = {
                    "drawer_id": did,
                    "text": row["text"],
                    "wing": row["wing"],
                    "room": row["room"],
                    "topic": row["topic"],
                    "source_file": row["source_file"],
                    "created_at": row["created_at"],
                    "matched_via": "graph",
                    "rrf_score": score,
                    "graph_mentions": graph_hits_by_drawer.get(did, 0),
                }
            else:
                hit = {
                    "id": did,
                    "document": None,
                    "matched_via": "graph",
                    "rrf_score": score,
                    "graph_mentions": graph_hits_by_drawer.get(did, 0),
                }
        out_hits.append(hit)

    response = {"results": out_hits}
    if include_trace:
        response["trace"] = {
            "n_vector": len(vec_hits),
            "n_graph": len(graph_hits_by_drawer),
            "n_after_fusion": len(out_hits),
            "query_entities": [e.name for e in query_entities],
        }
    return _rerank.rerank_response(query, response)


def _load_age_extractor():
    """Lazy load the entity extractor for /search/age-fused.

    Tries SME's regex extractor first; falls back to mempalace's builtin.
    Cached on first call.
    """
    global _AGE_EXTRACTOR_CACHE
    if "_AGE_EXTRACTOR_CACHE" in globals():
        return _AGE_EXTRACTOR_CACHE
    try:
        from sme.extractors.regex import extract as ex  # type: ignore
    except ImportError:
        try:
            from mempalace.kg_writethrough import _builtin_regex_extractor as ex  # type: ignore
        except ImportError:
            ex = None
    _AGE_EXTRACTOR_CACHE = ex
    return ex


@app.get("/context")
async def context(
    topic: str,
    limit: int = 5,
    x_api_key: str | None = Header(default=None),
):
    """Alias for /search with a semantically friendlier name for LLM tool
    prompts."""
    _check_auth(x_api_key)
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": _search_args(topic, limit)},
    })
    return _unwrap(result)


@app.get("/list")
async def list_drawers(
    wing: str | None = None,
    room: str | None = None,
    limit: int = 20,
    offset: int = 0,
    x_api_key: str | None = Header(default=None),
):
    """List drawers by metadata (wing/room) — no search query required.

    Wraps mempalace's ``mempalace_list_drawers`` MCP tool. Unlike /search,
    this is an unranked listing pulled directly from sqlite metadata, so
    it's the right path for browsing a wing without an embeddable query
    (e.g. a "show me everything in wing=reflect" panel that just wants
    a flat list, not a vector top-N).

    Ordering is whatever ``mempalace_list_drawers`` returns — currently
    the natural sqlite metadata-table order, which approximates insertion
    order but is not guaranteed to be strictly chronological. Pass
    ``limit`` / ``offset`` for pagination.

    Either ``wing`` or ``room`` (or both) can be supplied; with neither,
    returns the first ``limit`` drawers across the whole palace.
    """
    _check_auth(x_api_key)
    # Validate room so a typo gets a fast 400 — same contract as the
    # /search* endpoints.
    _rooms.validate_room_or_raise(room)
    # Normalize wing so callers get symmetric read/write behavior.
    wing = _rooms.normalize_wing_filter(wing)
    args: dict = {"limit": int(limit), "offset": int(offset)}
    if wing is not None:
        args["wing"] = wing
    if room is not None:
        args["room"] = room
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_list_drawers", "arguments": args},
    })
    return _unwrap(result)


@app.delete("/memory/{drawer_id}")
async def delete_memory(drawer_id: str, x_api_key: str | None = Header(default=None)):
    """Delete a drawer by id. Wraps mempalace_delete_drawer."""
    _check_auth(x_api_key)
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_delete_drawer", "arguments": {"drawer_id": drawer_id}},
    })
    return _unwrap(result)


@app.patch("/memory/{drawer_id}")
async def update_memory(drawer_id: str, request: Request, x_api_key: str | None = Header(default=None)):
    """Update a drawer's content/wing/room. Wraps mempalace_update_drawer.

    Body keys (all optional, but at least one is required): ``content``,
    ``wing``, ``room``. Only supplied keys are forwarded to the underlying
    tool. An empty body returns 400 — that's an ambiguous no-op rather
    than something we should silently let through to mempalace.
    """
    _check_auth(x_api_key)
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    args: dict = {"drawer_id": drawer_id}
    if "content" in body: args["content"] = body["content"]
    if "wing" in body: args["wing"] = body["wing"]
    if "room" in body: args["room"] = body["room"]
    if len(args) == 1:
        raise HTTPException(
            status_code=400,
            detail="PATCH /memory/{id} requires at least one of: content, wing, room.",
        )
    # Validate room before forwarding to mempalace — without this a PATCH
    # could let non-canonical room values into the DB even though POST
    # /memory rejects them. Same contract as the other room-accepting
    # endpoints.
    if "room" in args:
        _rooms.validate_room_or_raise(args["room"])
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_update_drawer", "arguments": args},
    })
    return _unwrap(result)


# ── Wing-slug + canonical-rooms validation (#101 twelfth slice) ────────────
# Lives in rooms.py now. main.py keeps the `_`-prefixed names alive via
# re-export so the /memory and /search route handlers and the /admin/
# refresh-rooms handler keep working unchanged.
#
# Tests that mutated `main._canonical_rooms_cache` directly were updated
# to mutate `rooms._canonical_rooms_cache` — module-level attribute
# writes don't propagate through re-exports, so the test needs to touch
# the source-of-truth binding in rooms.py.
import rooms as _rooms  # noqa: E402
from rooms import (  # noqa: E402
    canonical_rooms as _canonical_rooms,
    normalize_wing_slug as _normalize_wing_slug,
)


@app.post("/memory")
async def store_memory(request: Request, x_api_key: str | None = Header(default=None)):
    _check_auth(x_api_key)
    body = await request.json()
    content = body.get("content", "")

    # Taxonomy enforcement at the write boundary. Wing slug is normalized
    # (idempotent) so the same project is always the same slug regardless
    # of how the caller spelled it. Room is validated against the
    # configurable canonical set; non-canonical writes are rejected with
    # the valid set in the error payload so callers can adapt.
    wing = _normalize_wing_slug(body.get("wing") or "unknown")
    room = body.get("room") or "discoveries"  # spec's catch-all default
    valid_rooms = _canonical_rooms()
    if room not in valid_rooms:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"room {room!r} is not in the canonical set",
                "valid_rooms": sorted(valid_rooms),
                "hint": "Use one of the canonical rooms, or `mempalace rooms add` to register a new one.",
            },
        )

    # Novelty scoring runs in parallel with the write — the score is
    # informational metadata, not a gate.  Fire both concurrently so NCD
    # doesn't add latency to the write path.
    from novelty import compute_novelty_for_write

    write_coro = _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {
            "name": "mempalace_add_drawer",
            "arguments": {"wing": wing, "room": room, "content": content},
        },
    })
    novelty_coro = compute_novelty_for_write(content, wing, room, _call)
    result, novelty_info = await asyncio.gather(write_coro, novelty_coro)

    unwrapped = _unwrap(result)
    if isinstance(unwrapped, dict) and unwrapped.get('success'):
        unwrapped['toast'] = f'Filed to {wing}/{room}'
    if isinstance(unwrapped, dict):
        unwrapped['novelty'] = novelty_info
    # mempalace#86: bubble warnings/errors up to the client. Default to
    # empty lists when paired with a mempalace that doesn't emit them.
    return _ensure_warnings_fields(unwrapped)


@app.post("/admin/refresh-rooms")
async def refresh_rooms(x_api_key: str | None = Header(default=None)):
    """Clear the canonical-rooms cache and rebuild it from the database.

    The /memory write boundary validates ``room`` against a cached set
    pulled from ``mempalace_canonical_rooms`` (postgres). The cache lives
    for the daemon's lifetime, so a freshly registered room (e.g. via
    ``mempalace rooms add``) is invisible until the daemon restarts —
    unless this endpoint is called.

    Behavior:
        * Drops ``_canonical_rooms_cache``.
        * Eagerly repopulates it by calling ``_canonical_rooms()``, which
          re-reads the postgres lookup table (or falls back to the spec's
          7 defaults when the table is absent or the backend isn't
          postgres).
        * Returns the new room list plus its count.

    Auth: standard ``X-API-Key`` (same ``PALACE_API_KEY`` as every other
    endpoint — palace-daemon has a single API-key model rather than a
    separate admin token).
    """
    _check_auth(x_api_key)
    # #101 twelfth slice: the cache lives in rooms.py now. Mutate it
    # through the module so the live binding (not a re-exported alias)
    # is cleared.
    _rooms._canonical_rooms_cache = None
    rooms_list = sorted(_canonical_rooms())
    return {"refreshed": True, "rooms": rooms_list, "count": len(rooms_list)}


@app.get("/stats")
async def stats(x_api_key: str | None = Header(default=None)):
    _check_auth(x_api_key)
    tools = ["mempalace_kg_stats", "mempalace_graph_stats", "mempalace_status"]
    responses = await asyncio.gather(*[
        _call({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": t, "arguments": {}}})
        for i, t in enumerate(tools, 1)
    ])
    kg, graph, status = [_unwrap(r) for r in responses]
    return {"kg": kg, "graph": graph, "status": status}


# ── Fast direct-SQL endpoints ───────────────────────────────────────
#
# Bypass the MCP dispatch + semaphore + executor pipeline entirely.
# Pure SQL against postgres with a short statement_timeout — never
# blocked by AGE graph locks, backfill workers, or collection-level
# operations. Designed for CLI tools that need sub-second responses.


# ── Bench-active lock (#104) ──────────────────────────────────────────────────
# Helpers extracted to bench_lock.py per #101 refactor. main.py re-exports
# the names as `_bench_lock_path` / `_bench_lock_active` so existing tests
# that mock `main._bench_lock_*` keep working without churn.

from bench_lock import bench_lock_path as _bench_lock_path  # noqa: E402
from bench_lock import bench_lock_active as _bench_lock_active  # noqa: E402


# Postgres helpers (#93/#96/#108) — extracted to postgres.py per #101 (fourth slice).
# main.py re-exports the `_`-prefixed names so existing call sites keep working.
# Tests must patch `postgres.postgres_dsn` (not `main._postgres_dsn`) because
# intra-module callers in postgres.py bypass main's namespace.
from postgres import _DaemonToolError, _RPC_INVALID_PARAMS, _RPC_BACKEND_DOWN, _RPC_INTERNAL  # noqa: E402,F401
from postgres import postgres_dsn as _postgres_dsn  # noqa: E402
from postgres import require_postgres as _require_postgres  # noqa: E402,F401
from postgres import connect_postgres as _connect_postgres  # noqa: E402,F401


# #101 thirteenth slice: _fast_status_payload moved to fast_intercept.py.
# Re-exported under the old name so existing call sites (/status/fast
# route, fast_intercept.fast_mcp_status_payload's lazy lookup) and tests
# (test_db_error_integration's direct call + test_mcp_fast_intercept's
# patch.object) keep working unchanged.
from fast_intercept import fast_status_payload as _fast_status_payload  # noqa: E402,F401


# ── Daemon-native MCP tools (#93) ─────────────────────────────────────────────
# Six tools the CLI needs when daemon-strict mode is on. The mempalace CLI's
# `cmd_rooms` / `cmd_wakeup` / `cmd_mined` open a local ChromaDB client today
# and break against the retired local palace; routing them through the daemon
# closes that gap. Companion to mempalace#285. _DaemonToolError + _RPC_*
# constants live in postgres.py — see re-exports above.


# DB-error observability (#97) — extracted to db_errors.py per #101 refactor.
# main.py re-exports the `_`-prefixed names so existing call sites + tests
# that patch `main._record_db_error` / mutate `main._DB_ERROR_LOG` keep
# working unchanged. The deque + lock are module-level objects shared by
# reference across both namespaces; mutations through either reach the
# same underlying state.
from db_errors import DB_ERROR_LOG as _DB_ERROR_LOG  # noqa: E402
from db_errors import DB_ERROR_LOG_LOCK as _DB_ERROR_LOG_LOCK  # noqa: E402
from db_errors import classify_db_error as _classify_db_error  # noqa: E402
from db_errors import record_db_error as _record_db_error  # noqa: E402
from db_errors import db_errors_summary as _db_errors_summary  # noqa: E402


# --- #101 fifth slice ----------------------------------------------------
# The daemon-native MCP tools live in `daemon_tools.py` now. We re-export
# them under the original `_`-prefixed names so the /mcp dispatcher and
# every test that calls `main._fast_mcp_*` keeps working unchanged.
from daemon_tools import (  # noqa: E402
    DAEMON_NATIVE_TOOLS as _DAEMON_NATIVE_TOOLS,
    fast_mcp_mined as _fast_mcp_mined,
    fast_mcp_rooms_add as _fast_mcp_rooms_add,
    fast_mcp_rooms_list as _fast_mcp_rooms_list,
    fast_mcp_rooms_remove as _fast_mcp_rooms_remove,
    fast_mcp_rooms_rename as _fast_mcp_rooms_rename,
    fast_mcp_wakeup as _fast_mcp_wakeup,
    invalidate_rooms_cache as _invalidate_rooms_cache,
    normalize_room_name as _normalize_room_name,
)


@app.get("/status/fast")
async def status_fast(x_api_key: str | None = Header(default=None)):
    """Fast palace status via direct SQL — no MCP, no AGE, no locks."""
    _check_auth(x_api_key)
    if not _postgres_dsn():
        raise HTTPException(status_code=503, detail="postgres backend not configured")
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _fast_status_payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── /mcp fast-intercept payloads (issue #49) ──────────────────────────────────
# The two payload wrappers live in `fast_intercept.py` now (#101 sixth slice).
# They reach back here for `_fast_status_payload` / `_read_kg_postgres_stats`
# via lazy `import main`, which preserves `patch.object(main, ...)` in the
# unit tests (test_mcp_fast_intercept.py) without test edits.
from fast_intercept import (  # noqa: E402
    fast_mcp_kg_stats_payload as _fast_mcp_kg_stats_payload,
    fast_mcp_status_payload as _fast_mcp_status_payload,
)


@app.get("/search/fast")
async def search_fast(
    q: str,
    limit: int = 5,
    wing: str | None = None,
    x_api_key: str | None = Header(default=None),
):
    """Fast BM25 text search via direct SQL — no vector, no AGE locks."""
    _check_auth(x_api_key)
    # Normalize wing so callers get symmetric read/write behavior.
    wing = _rooms.normalize_wing_filter(wing)
    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN") or getattr(
        _mp._config, "postgres_dsn", None
    )
    if not dsn:
        raise HTTPException(status_code=503, detail="postgres backend not configured")

    loop = asyncio.get_running_loop()
    def _query():
        import psycopg2
        # #110: record OperationalError on connect for /health.db_errors visibility
        try:
            conn = psycopg2.connect(dsn, connect_timeout=3)
        except psycopg2.OperationalError as e:
            _record_db_error(e)
            raise
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '5s'")
                sql = """
                    SELECT id, wing, room, metadata,
                           ts_rank_cd(doc_tsv, plainto_tsquery('english', %s)) AS rank,
                           left(document, 500) AS snippet
                    FROM mempalace_drawers
                    WHERE doc_tsv @@ plainto_tsquery('english', %s)
                """
                params = [q, q]
                if wing:
                    sql += " AND wing = %s"
                    params.append(wing)
                sql += " ORDER BY rank DESC LIMIT %s"
                params.append(limit)
                cur.execute(sql, params)
                results = []
                for row in cur.fetchall():
                    drawer_id, w, r, meta_raw, rank, snippet = row
                    meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
                    results.append({
                        "id": drawer_id,
                        "wing": w,
                        "room": r,
                        "rank": rank,
                        "snippet": snippet,
                        "source_file": meta.get("source_file"),
                        "tags": meta.get("tags"),
                    })
                return results

    try:
        return await loop.run_in_executor(None, _query)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Postgres-native helper endpoints ────────────────────────────────
#
# These two endpoints expose Postgres-backend value-adds that don't have
# an obvious shape under chromadb. Both return 503 when the daemon is
# running against the chromadb backend; both require auth.


@app.post("/cypher")
async def cypher_query(request: Request, x_api_key: str | None = Header(default=None)):
    """Run a Cypher query against the AGE knowledge-graph and return rows.

    Body::

        {
          "cypher": "MATCH (s:Entity)-[r:RELATION]->(o) RETURN s.name, r.relation_type, o.name",
          "graph": "mempalace_kg"         // optional, defaults to mempalace_kg
        }

    Spares clients from learning AGE's ``SELECT * FROM cypher('g', $$...$$) AS (col agtype, ...)``
    SQL wrapper. The daemon parses RETURN aliases out of the Cypher
    source (just like ``KnowledgeGraphAGE._run_cypher`` does) so callers
    don't have to declare column types.

    Enforced read-only: the underlying psycopg2 transaction is marked
    ``READ ONLY`` before the Cypher executes, so AGE write verbs
    (``CREATE``, ``MERGE``, ``SET``, ``DELETE``, ``DETACH DELETE``,
    ``REMOVE``) fail at the PostgreSQL layer with SQLSTATE 25006 and the
    endpoint returns 403. Callers that need to mutate the graph must go
    through the ``mempalace_kg_*`` MCP tools. Returns 503 when the daemon
    isn't on postgres backend.
    """
    _check_auth(x_api_key)
    if _mp._config.backend != "postgres":
        raise HTTPException(
            status_code=503,
            detail="/cypher requires MEMPALACE_BACKEND=postgres; daemon is on chroma.",
        )

    body = await request.json()
    cypher = body.get("cypher")
    if not isinstance(cypher, str) or not cypher.strip():
        raise HTTPException(status_code=400, detail="'cypher' body field is required")
    graph = body.get("graph", "mempalace_kg")
    if not isinstance(graph, str) or not graph.strip():
        raise HTTPException(status_code=400, detail="'graph' must be a non-empty string")

    def _run():
        import psycopg2
        import psycopg2.errors
        # mempalace.knowledge_graph_age uses psycopg (v3) for the actual
        # Cypher execution, so SyntaxError / OutOfMemory / etc. surface
        # from psycopg.errors — NOT psycopg2.errors. We need both.
        try:
            import psycopg
            import psycopg.errors as _pg_errors
        except ImportError:
            psycopg = None
            _pg_errors = None

        from mempalace.knowledge_graph_age import KnowledgeGraphAGE

        # Reuse mempalace's AGE helper for RETURN-alias parsing + agtype unwrap.
        # Constructing a fresh KnowledgeGraphAGE bootstraps the graph if absent,
        # which is harmless for a query (the MERGE on absent graphs creates it).
        dsn = _mp._config.postgres_dsn
        if not dsn:
            return None, "MEMPALACE_POSTGRES_DSN not configured"
        # KnowledgeGraphAGE uses psycopg v3 internally, so connect-time
        # failures surface as psycopg.Error not psycopg2.Error. Catch both
        # so the constructor failure doesn't escape as a generic 500.
        _construct_excs = (psycopg2.Error,)
        if psycopg is not None:
            _construct_excs = _construct_excs + (psycopg.Error,)
        try:
            kg = KnowledgeGraphAGE(dsn=dsn)
        except _construct_excs as e:
            return None, ("postgres-error", f"postgres connect failed: {e}")
        try:
            # Enforce read-only at the transaction layer. AGE writes go
            # through ``SELECT * FROM cypher(...)`` so the daemon can't
            # tell from the SQL surface whether the embedded Cypher
            # mutates — the read-only transaction is what makes the
            # guarantee real. ``SET TRANSACTION READ ONLY`` must run
            # before any other statement in the current transaction;
            # KnowledgeGraphAGE.__init__ commits its bootstrap transaction,
            # leaving a clean state. Rollback any stale transaction as a
            # safety belt before starting the read-only one.
            kg._conn.rollback()
            with kg._conn.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            # Build per-error tuples that include both psycopg2 (legacy)
            # and psycopg (v3) exception classes — mempalace.knowledge_
            # graph_age uses psycopg v3 internally so SyntaxError /
            # OutOfMemory / etc. surface as psycopg.errors.*, not
            # psycopg2.errors.*.
            _read_only_excs = (psycopg2.errors.ReadOnlySqlTransaction,)
            _oom_excs = (psycopg2.errors.OutOfMemory,)
            _timeout_excs = (psycopg2.errors.QueryCanceled,)
            _bad_query_excs = (
                psycopg2.errors.SyntaxError,
                psycopg2.errors.UndefinedColumn,
                psycopg2.errors.UndefinedTable,
                psycopg2.errors.UndefinedFunction,
            )
            _generic_excs = (psycopg2.Error,)
            if _pg_errors is not None:
                _read_only_excs = _read_only_excs + (_pg_errors.ReadOnlySqlTransaction,)
                _oom_excs = _oom_excs + (_pg_errors.OutOfMemory,)
                _timeout_excs = _timeout_excs + (_pg_errors.QueryCanceled,)
                _bad_query_excs = _bad_query_excs + (
                    _pg_errors.SyntaxError,
                    _pg_errors.UndefinedColumn,
                    _pg_errors.UndefinedTable,
                    _pg_errors.UndefinedFunction,
                )
                _generic_excs = _generic_excs + (psycopg.Error,)

            try:
                rows = kg._run_cypher(cypher, params={}, fetch=True)
            except _read_only_excs as e:
                kg._conn.rollback()
                return None, ("read-only", str(e))
            except _oom_excs as e:
                # Postgres shared memory exhausted — typically from an
                # unbounded MATCH that materializes too much agtype before
                # LIMIT applies (see #160 for the structural fix path).
                # Roll back so the connection stays healthy if the caller
                # retries with a tighter query.
                try:
                    kg._conn.rollback()
                except Exception:
                    pass
                return None, ("shared-memory-exhausted", str(e))
            except _timeout_excs as e:
                # statement_timeout hit — Cypher took longer than allowed.
                try:
                    kg._conn.rollback()
                except Exception:
                    pass
                return None, ("timeout", str(e))
            except _bad_query_excs as e:
                # Caller-supplied Cypher has a syntax / schema mismatch.
                # 400 is more honest than 500 for these.
                try:
                    kg._conn.rollback()
                except Exception:
                    pass
                return None, ("bad-query", str(e))
            except _generic_excs as e:
                # Any other postgres-side failure (connection dropped,
                # transaction aborted from an earlier error, etc.).
                try:
                    kg._conn.rollback()
                except Exception:
                    pass
                return None, ("postgres-error", str(e))
            aliases = kg._extract_return_aliases(cypher)
            unwrap = kg._unwrap_agtype
            shaped = []
            for row in rows:
                if aliases:
                    shaped.append({alias: unwrap(val) for alias, val in zip(aliases, row)})
                else:
                    shaped.append([unwrap(val) for val in row])
            return shaped, None
        finally:
            kg.close()

    loop = asyncio.get_running_loop()
    rows, err = await loop.run_in_executor(None, _run)
    if err is not None:
        if isinstance(err, tuple) and err:
            kind, msg = err[0], err[1]
            if kind == "read-only":
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "/cypher is read-only; write verbs (CREATE/MERGE/SET/DELETE/"
                        "DETACH DELETE/REMOVE) are rejected. Use mempalace_kg_* MCP "
                        f"tools for graph mutations. PostgreSQL: {msg}"
                    ),
                )
            if kind == "bad-query":
                # Caller's Cypher is malformed — 400 with the postgres
                # error so they can fix the query.
                raise HTTPException(
                    status_code=400,
                    detail={"error": "bad-query", "postgres": msg},
                )
            if kind == "timeout":
                # 504 (gateway timeout) is the standard HTTP code for
                # operations that exceeded a backend deadline.
                raise HTTPException(
                    status_code=504,
                    detail={"error": "timeout", "postgres": msg,
                            "hint": "Tighten the query (add LIMIT, narrow the MATCH) and retry."},
                )
            if kind == "shared-memory-exhausted":
                # 507 (insufficient storage) is the closest standard HTTP
                # code for a postgres-side resource limit.
                raise HTTPException(
                    status_code=507,
                    detail={"error": "shared-memory-exhausted", "postgres": msg,
                            "hint": "Bound the MATCH with a CTE LIMIT before joining "
                                    "to Entity/Drawer for property lookup. See #160."},
                )
            if kind == "postgres-error":
                raise HTTPException(
                    status_code=502,
                    detail={"error": "postgres-error", "postgres": msg},
                )
        raise HTTPException(status_code=500, detail=err)
    return {"graph": graph, "rows": rows, "count": len(rows)}


@app.post("/embed")
async def embed_text(request: Request, x_api_key: str | None = Header(default=None)):
    """Embed a list of texts via the daemon's configured embedding function.

    Body::

        {"texts": ["hello world", "foo bar"]}

    Returns::

        {"embeddings": [[0.1, ...], [0.2, ...]], "dim": 384, "model": "default"}

    Designed for clients that want vectors but don't have onnxruntime +
    a 90 MB model locally (e.g. hook scripts on laptops). The daemon
    already holds the embedding function in memory for its own use; this
    just exposes it.

    Works under either backend (chroma or postgres) since the embedding
    function is backend-independent.
    """
    _check_auth(x_api_key)
    body = await request.json()
    texts = body.get("texts")
    if not isinstance(texts, list) or not texts:
        raise HTTPException(status_code=400, detail="'texts' must be a non-empty list")
    if not all(isinstance(t, str) for t in texts):
        raise HTTPException(status_code=400, detail="'texts' entries must all be strings")
    if len(texts) > 256:
        raise HTTPException(status_code=400, detail="batch limit is 256 texts per request")

    def _embed():
        from mempalace.backends.chroma import ChromaBackend

        # mempalace's embedding function is the same function chromadb and
        # PostgresBackend both use; ChromaBackend._resolve_embedding_function
        # is the canonical accessor.
        ef = ChromaBackend._resolve_embedding_function()
        if ef is None:
            return None, "no embedding function resolved (mempalace config issue)"
        try:
            vectors = ef(texts)
        except Exception as e:
            return None, f"embedding failed: {e}"
        # Normalize: ef may return numpy arrays; downstream HTTP consumers
        # are more reliable with plain Python lists.
        out = []
        for v in vectors:
            try:
                out.append([float(x) for x in v])
            except Exception:
                out.append(list(v) if v is not None else None)
        return out, None

    loop = asyncio.get_running_loop()
    embeddings, err = await loop.run_in_executor(None, _embed)
    if err is not None:
        raise HTTPException(status_code=500, detail=err)
    dim = len(embeddings[0]) if embeddings and embeddings[0] is not None else 0
    return {"embeddings": embeddings, "dim": dim, "count": len(embeddings)}


# Direct-SQL KG / wings readers (#49 / 1.8.2 split) — extracted to kg_reader.py
# per #101 refactor (seventh slice). main.py re-exports the _-prefixed names so
# existing call sites + tests that patch `main._read_kg_*` / `main._kg_path` /
# `main._chroma_path` keep working. Tests that patch the intra-module
# dispatchers (`_read_wings_rooms_direct` → `_read_wings_rooms_postgres`,
# `_read_kg_direct` → `_read_kg_postgres`, `_read_kg_stats_direct` →
# `_read_kg_postgres_stats`) must patch `kg_reader.read_*` instead — the
# dispatchers call their helpers via this module's namespace, bypassing
# main's re-exports.
from kg_reader import kg_path as _kg_path  # noqa: E402,F401
from kg_reader import chroma_path as _chroma_path  # noqa: E402,F401
from kg_reader import read_wings_rooms_postgres as _read_wings_rooms_postgres  # noqa: E402,F401
from kg_reader import read_wings_rooms_direct as _read_wings_rooms_direct  # noqa: E402,F401
from kg_reader import read_kg_postgres as _read_kg_postgres  # noqa: E402,F401
from kg_reader import read_kg_postgres_stats as _read_kg_postgres_stats  # noqa: E402,F401
from kg_reader import read_kg_stats_direct as _read_kg_stats_direct  # noqa: E402,F401
from kg_reader import read_kg_direct as _read_kg_direct  # noqa: E402,F401


@app.get("/graph")
async def graph(
    x_api_key: str | None = Header(default=None),
    palace_viz_session: str | None = Cookie(default=None),
    limit: int = Query(
        500,
        ge=1,
        le=50000,
        description=(
            "Cap on KG entity count returned (and 2× this on MENTIONS "
            "triples). Default 500 keeps /graph fast on the full 264k-"
            "entity / 5.58M-edge palace. Callers needing more should "
            "query AGE directly via POST /cypher."
        ),
    ),
):
    """Single-shot structural snapshot for SME-style consumers.

    Mirrors `/stats`'s asyncio.gather pattern but adds:
    - rooms-per-wing fan-out (parallel)
    - direct read of the KG from the active backend (sqlite under
      chroma; AGE Cypher under postgres) — no extra MCP roundtrip

    Replaces what an SME adapter would otherwise compose by serially
    calling list_wings + list_rooms × N + list_tunnels + kg_stats over
    HTTP. On the 151K-drawer canonical palace, list_wings alone takes
    ~30s; the gather here finishes in well under that.

    Under the postgres backend the KG section reads live from Apache
    AGE, returning three separate edge views (1.8.2 split):

    - ``kg_triples`` — RELATION edges, the entity→entity semantic facts
      consumers expect when they hear "triple." Currently ~1 placeholder
      row; the corpus has not been triple-extracted.
    - ``kg_mentions`` — MENTIONS edges (Drawer→Entity), the dominant
      relationship in the live graph (5.66M+). Atemporal mention links,
      not semantic facts. Pre-1.8.2 these lived in ``kg_triples`` and
      were mislabeled.
    - ``kg_stats`` reports entity, triples, and mentions counts
      separately.

    Under the chroma backend ``kg_triples`` is sourced from the legacy
    sqlite ``triples`` table (real semantic facts) and ``kg_mentions``
    is always ``[]`` (no MENTIONS concept in the chroma KG).
    """
    _check_viz_auth(x_api_key, palace_viz_session)
    entity_limit = int(limit)
    triple_limit = int(limit) * 2
    mention_limit = int(limit) * 2

    def _mcp(tool: str, args: dict, rid: int) -> dict:
        return {
            "jsonrpc": "2.0", "id": rid,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }

    # Phase 1: parallel reads.
    #
    # MCP path (cheap tools — graph_stats is computed in mempalace, not
    # walked, and kg_stats is a single sqlite count): graph_stats gives us
    # tunnels via top_tunnels (mempalace 3.3.4's mempalace_list_tunnels
    # returns [] on palaces where graph_stats reports tunnels — bug
    # tracked in docs/graph-endpoint.md Part 2). kg_stats gives the
    # entities/triples summary the SME adapter already consumes.
    #
    # Direct sqlite path (no semaphore, ~0.4s on a 151K-drawer palace):
    # wings, rooms-per-wing, KG entities + triples. These are the
    # expensive parts when fanned out via MCP (list_wings is ~30s,
    # list_rooms × N wings serializes through the 4-slot read semaphore
    # and starves under load). Reading the underlying sqlite directly
    # bypasses the fan-out entirely.
    graph_stats_task = _call(_mcp("mempalace_graph_stats", {}, 1))
    kg_stats_task    = _call(_mcp("mempalace_kg_stats",    {}, 2))

    # Gate direct-sqlite reads on _read_sem so /graph yields to
    # /repair mode=rebuild's _exclusive_palace() and respects the
    # read-concurrency budget (rather than spawning unbounded threads
    # under load — 2 threads/request × N concurrent /graph requests).
    async def _direct_under_sem(work):
        async with _read_sem:
            return await asyncio.to_thread(work)

    wings_rooms_task = _direct_under_sem(_read_wings_rooms_direct)
    kg_direct_task   = _direct_under_sem(
        lambda: _read_kg_direct(
            entity_limit=entity_limit,
            triple_limit=triple_limit,
            mention_limit=mention_limit,
        )
    )
    kg_stats_direct_task = _direct_under_sem(_read_kg_stats_direct)

    (
        graph_stats_resp,
        kg_stats_resp,
        (wings, rooms),
        (kg_entities, kg_triples, kg_mentions),
        kg_stats_age,
    ) = await asyncio.gather(
        graph_stats_task,
        kg_stats_task,
        wings_rooms_task,
        kg_direct_task,
        kg_stats_direct_task,
    )

    graph_payload = _unwrap(graph_stats_resp) or {}
    tunnels = [
        {"room": t.get("room"), "wings": t.get("wings", [])}
        for t in (graph_payload.get("top_tunnels") or [])
    ]

    return {
        "wings": wings,
        "rooms": rooms,
        "tunnels": tunnels,
        "kg_entities": kg_entities,
        "kg_triples": kg_triples,
        "kg_mentions": kg_mentions,
        "kg_stats": kg_stats_age or _unwrap(kg_stats_resp) or {},
    }


# ── /viz status dashboard ───────────────────────────────────────────────────

_VIZ_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "viz.html")
_VIZ_HTML_CACHE: str | None = None


@app.get("/viz", response_class=HTMLResponse)
async def viz(
    x_api_key: str | None = Header(default=None),
    palace_viz_session: str | None = Cookie(default=None),
):
    """Self-contained status dashboard at /viz.

    Returns the HTML page from static/viz.html. The page then fetches
    /graph, /repair/status, and /health client-side and renders five panels:
    KG force-graph (D3), wings bar chart, wing/room hierarchy (Mermaid),
    tunnels list, KG stats.

    Auth: the ``X-Api-Key`` header, or a viz session cookie minted by
    ``POST /viz/session``. The key is never accepted in the URL — a
    ``?key=`` query string would leak into browser history, proxy logs,
    and referer headers. ``_check_viz_auth`` is a no-op when
    PALACE_API_KEY is unset, preserving zero-config local dev.

    The HTML template is read from disk lazily on the first request and
    cached in-process thereafter (one disk read per daemon process).

    Inspired by upstream PRs #1022 (D3 KG viz), #393 (Mermaid diagrams),
    #431 (CLI stats), #256 (sync_status MCP), #601 (brief overview) — none
    cherry-picked, just patterns synthesized over the daemon's /graph.
    """
    _check_viz_auth(x_api_key, palace_viz_session)
    global _VIZ_HTML_CACHE
    if _VIZ_HTML_CACHE is None:
        try:
            with open(_VIZ_HTML_PATH, encoding="utf-8") as f:
                _VIZ_HTML_CACHE = f.read()
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"viz template missing: {e}")
    return HTMLResponse(content=_VIZ_HTML_CACHE)


@app.post("/viz/session")
async def viz_session(x_api_key: str | None = Header(default=None)):
    """Exchange a valid ``X-Api-Key`` header for a short-lived HttpOnly viz
    session cookie, so /viz can be bookmarked without the key in the URL.

    401s if PALACE_API_KEY is set and the header is wrong/missing. When
    PALACE_API_KEY is unset, auth is a no-op and no cookie is set (the
    dashboard already works unauthenticated)."""
    _check_auth(x_api_key)
    key = os.getenv("PALACE_API_KEY", "")
    resp = JSONResponse({"authenticated": bool(key),
                         "ttl": PALACE_VIZ_SESSION_TTL_SECONDS if key else 0})
    if key:
        resp.set_cookie(
            _VIZ_COOKIE_NAME,
            _mint_viz_token(),
            max_age=PALACE_VIZ_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=PALACE_VIZ_COOKIE_SECURE,
            path="/",
        )
    return resp


@app.post("/flush")
async def flush_palace(x_api_key: str | None = Header(default=None)):
    """Manually trigger a checkpoint/flush of memories to disk."""
    _check_auth(x_api_key)
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_memories_filed_away", "arguments": {}},
    })
    return _unwrap(result)


@app.post("/reload")
async def reload_palace(x_api_key: str | None = Header(default=None)):
    """Force the daemon to reconnect to the database and refresh its index."""
    _check_auth(x_api_key)
    # _mp._get_client uses a cache; we clear it to force a fresh PersistentClient
    _mp._client_cache = None; _mp._collection_cache = None
    return {"status": "reloaded", "message": "Palace client cache cleared"}


@app.post("/backup")
async def create_backup(x_api_key: str | None = Header(default=None)):
    """
    Perform a verified atomic backup of the palace database.

    Uses sqlite3 .backup to snapshot the live DB, then verifies the
    backup by running ``PRAGMA integrity_check`` and a smoke retrieval
    (reading rows from ``embedding_metadata``) on the backup file — never
    on the live DB.  All sync I/O runs in an executor to avoid blocking
    the event loop.

    The backup is kept even when verification fails so operators can
    inspect it; the response flags the failure via ``integrity`` /
    ``smoke_test`` fields.
    """
    _check_auth(x_api_key)
    palace_path = _mp._config.palace_path
    db_path = os.path.join(palace_path, "chroma.sqlite3")

    backup_dir = os.path.join(os.path.dirname(palace_path), "palace.backup")
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"chroma.sqlite3.{timestamp}.bak")

    def _do_backup_and_verify() -> dict:
        """Sync: snapshot + integrity check + smoke retrieval."""
        # ── 1. Snapshot via sqlite3.backup ──────────────────────────
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

        result: dict = {
            "backup_path": backup_path,
            "timestamp": timestamp,
            "integrity": "ok",
            "smoke_test": "ok",
            "rows_sampled": 0,
        }

        # ── 2. PRAGMA integrity_check on the backup ────────────────
        try:
            conn = sqlite3.connect(backup_path)
            try:
                cur = conn.cursor()
                cur.execute("PRAGMA integrity_check;")
                status = cur.fetchone()[0]
            finally:
                conn.close()
            if status != "ok":
                result["integrity"] = f"FAILED: {status}"
        except Exception as exc:
            result["integrity"] = f"FAILED: {exc}"

        # ── 3. Smoke retrieval — read rows from the backup ─────────
        try:
            conn = sqlite3.connect(
                f"file:{backup_path}?mode=ro", uri=True, timeout=5,
            )
            try:
                cur = conn.cursor()
                # embedding_metadata is ChromaDB's main data table.
                # A successful read of >=1 row proves the backup is
                # structurally sound and contains real data.
                cur.execute(
                    "SELECT id, key, string_value FROM embedding_metadata LIMIT 5"
                )
                rows = cur.fetchall()
                result["rows_sampled"] = len(rows)
                if not rows:
                    result["smoke_test"] = "WARN: table exists but returned 0 rows"
            except sqlite3.OperationalError as exc:
                # Table might not exist (empty / fresh palace). That's
                # not corruption — just an empty database.
                if "no such table" in str(exc).lower():
                    result["smoke_test"] = "WARN: embedding_metadata table missing (empty palace?)"
                    result["rows_sampled"] = 0
                else:
                    result["smoke_test"] = f"FAILED: {exc}"
            finally:
                conn.close()
        except Exception as exc:
            result["smoke_test"] = f"FAILED: {exc}"

        return result

    # Hold the write semaphore so no daemon-driven writes race the
    # backup start, then run all sync I/O in an executor.
    async with _write_sem:
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, _do_backup_and_verify)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")

    # Derive overall status from verification results.
    ok = (
        result["integrity"] == "ok"
        and result["smoke_test"] == "ok"
    )
    result["status"] = "ok" if ok else "degraded"
    return result


# ── Mine endpoint (serialized bulk import) ────────────────────────────────────

@app.post("/mine")
async def mine(request: Request, x_api_key: str | None = Header(default=None)):
    """
    Run mempalace mine under _mine_sem (one job at a time). Normal read/write
    traffic continues unblocked during the job; mempalace ≥3.3.2 enforces
    its own mine lock at the library level.

    Body: { "dir": "/path/to/files", "wing": "general", "mode": "convos",
            "extract": "exchange", "limit": 100 }
    """
    _check_auth(x_api_key)
    body = await request.json()
    directory = body.get("dir")
    if not directory:
        raise HTTPException(status_code=400, detail="'dir' is required")
    if not isinstance(directory, str):
        # Closes Copilot finding on jphein/palace-daemon#1: a JSON number /
        # object / list would crash _translate_client_path().startswith and
        # surface as 500 rather than a clean 400.
        raise HTTPException(status_code=400, detail="'dir' must be a string")

    # Hook clients send paths in their own filesystem namespace. Translate
    # to the daemon's view via PALACE_DAEMON_PATH_MAP before validation.
    directory = _translate_client_path(directory)

    dir_path = Path(directory)
    if not dir_path.is_absolute() or ".." in dir_path.parts:
        raise HTTPException(status_code=400, detail="'dir' must be an absolute path with no traversal")
    if not dir_path.exists():
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {directory}")
    if not dir_path.is_dir():
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {directory}")

    wing = body.get("wing", "general")
    mode = body.get("mode", "convos")
    extract = body.get("extract")
    limit = body.get("limit")

    if mode not in _MINE_VALID_MODES:
        raise HTTPException(status_code=400, detail=f"'mode' must be one of: {', '.join(sorted(_MINE_VALID_MODES))}")
    if extract is not None and extract not in _MINE_VALID_EXTRACTS:
        raise HTTPException(status_code=400, detail=f"'extract' must be one of: {', '.join(sorted(_MINE_VALID_EXTRACTS))}")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="'limit' must be an integer")

    # During /repair mode=rebuild, queue the mine instead of executing it.
    # Mirrors the /silent-save queue pattern — the rebuild replaces the
    # collection mid-flight, so any concurrent mine subprocess would race
    # the swap. After repair completes, _drain_pending_mines() replays
    # queued mines through the same code path. Pass-through fields preserve
    # extract/limit on replay.
    if (
        _repair_state["in_progress"]
        and _repair_state.get("mode") == "rebuild"
    ):
        await _enqueue_pending_mine({
            "dir": body.get("dir"),  # original (untranslated) path so replay translates fresh
            "wing": wing,
            "mode": mode,
            "extract": extract,
            "limit": limit,
        })
        return {
            "queued": True,
            "reason": "repair-in-progress",
            "systemMessage": (
                "Mine queued — palace is rebuilding. Will replay automatically "
                "when repair completes."
            ),
        }

    mempalace_bin = os.path.join(os.path.dirname(sys.executable), "mempalace")
    cmd = [mempalace_bin, "mine", directory, "--mode", mode, "--wing", wing]
    if extract:
        cmd += ["--extract", extract]
    if limit:
        cmd += ["--limit", str(limit)]

    async def _run_mine_subprocess():
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Track the proc so lifespan shutdown can terminate it cleanly
        # (#136 problem B). The watcher's auto-mine path tracks via the
        # same set in app.state.active_mines.
        active_mines = getattr(request.app.state, "active_mines", None)
        if active_mines is not None:
            active_mines.add(proc)
        try:
            stdout, stderr = await proc.communicate()
        finally:
            if active_mines is not None:
                active_mines.discard(proc)
        return proc, stdout, stderr

    backend = getattr(_mp._config, "backend", "chroma")
    if backend == "chroma":
        # Two PersistentClient instances on one chroma path corrupt the Rust
        # log store, in- or cross-process (#29). The mine subprocess opens its
        # own client, so we make it the *sole* client for the job's duration:
        # hold every slot so no daemon-mediated work races the mine, then
        # deterministically close our client to release the Rust file lock.
        # The reopen lives in finally so the daemon's client is always restored
        # before another request can acquire a slot — even if the mine fails.
        async with _exclusive_palace():
            loop = asyncio.get_running_loop()
            # Teardown lives *inside* the try so the finally always reopens,
            # even if _drop_chroma_client raises or the flush sleep is
            # cancelled — otherwise the client could be left closed.
            try:
                _drop_chroma_client(close=True)
                if PALACE_CHROMA_FLUSH_SECONDS > 0:
                    await asyncio.sleep(PALACE_CHROMA_FLUSH_SECONDS)
                proc, stdout, stderr = await _run_mine_subprocess()
            finally:
                # Reopen before the exclusive lock releases. shield + drain
                # so a cancellation landing on this await can't drop the lock
                # while the reopen thread is still running and let another
                # request race a half-initialized client (#29).
                reopen = loop.run_in_executor(None, _mp._get_collection, True)
                try:
                    await asyncio.shield(reopen)
                except asyncio.CancelledError:
                    with contextlib.suppress(Exception):
                        await reopen
                    raise
                except Exception as e:
                    # Caches stay None → next request lazily reopens (self-heal).
                    _log.critical(
                        "POST /mine: failed to reopen palace client after mine "
                        "(dir=%s) — next request will lazily reopen: %s",
                        directory, e,
                    )
    else:
        # postgres handles concurrent connections natively — the dual-client
        # corruption cannot occur, so keep the original lightweight path.
        async with _mine_sem:
            proc, stdout, stderr = await _run_mine_subprocess()

    out = stdout.decode()
    err = stderr.decode()
    result = {
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
    }
    # mempalace's mcp_server.py redirects stdout → stderr at import time
    # (protects MCP JSON-RPC transport). When mine_sessions imports
    # _get_collection from mcp_server, all print() output lands on stderr.
    # Check both streams before declaring "no output".
    combined = (out.strip() or "") + (err.strip() or "")
    if proc.returncode == 0 and not combined:
        import logging
        logging.warning(
            "POST /mine produced no output for dir=%s wing=%s mode=%s — "
            "directory may be empty or contain no mineable files",
            directory, wing, mode,
        )
        result["warning"] = f"mine produced no output — {directory} may be empty or inaccessible"
    return result


_backfill_state: dict[str, Any] = {"in_progress": False}
_backfill_lock = asyncio.Lock()

@app.post("/backfill-age")
async def backfill_age(request: Request, x_api_key: str | None = Header(default=None)):
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
    _check_auth(x_api_key)
    if _mp._config.backend != "postgres":
        raise HTTPException(status_code=503, detail="backfill-age requires postgres backend")

    async with _backfill_lock:
        if _backfill_state["in_progress"]:
            return {"status": "already_running", "started_at": _backfill_state.get("started_at")}

        dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
        if not dsn:
            cfg = _mp.MempalaceConfig()
            dsn = cfg.postgres_dsn
        if not dsn:
            raise HTTPException(status_code=500, detail="no postgres DSN available")

        body = await request.json() if request.headers.get("content-type") == "application/json" else {}
        cmd = [sys.executable, "-m", "mempalace.backfill_age", "--dsn", dsn]
        if body.get("wing"):
            cmd += ["--wing", body["wing"]]
        if body.get("skip_palace"):
            cmd.append("--skip-palace")
        if body.get("skip_entities"):
            cmd.append("--skip-entities")
        if body.get("restart"):
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


@app.get("/backfill-age/status")
async def backfill_age_status(
    x_api_key: str | None = Header(default=None),
    palace_viz_session: str | None = Cookie(default=None),
):
    """Poll backfill-age progress.

    Detects both daemon-spawned and externally-launched (parallel) workers
    by checking the checkpoint table and OS process list.
    """
    _check_viz_auth(x_api_key, palace_viz_session)
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
        dsn = os.environ.get("MEMPALACE_POSTGRES_DSN") or getattr(_mp.MempalaceConfig(), "postgres_dsn", None)
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
        import logging
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


@app.get("/watch")
async def watch_list(x_api_key: str | None = Header(default=None)):
    """List the directories the file-watcher is currently monitoring.

    Configured at startup via PALACE_WATCH_DIRS env var; runtime add /
    remove requires a daemon restart. Returns an empty list when the
    watcher isn't running (env unset, watchdog package missing, or
    startup failed).
    """
    _check_auth(x_api_key)
    watcher = getattr(app.state, "watcher", None)
    # Belt + suspenders: lifespan only publishes app.state.watcher when
    # is_running, but check it again here so a thread crash that flips
    # is_running to False between startup and now is reflected in the
    # endpoint's running= field. Closes Copilot finding on
    # jphein/palace-daemon#3.
    if watcher is None or not getattr(watcher, "is_running", False):
        return {"running": False, "targets": []}
    return {"running": True, "targets": watcher.list_targets()}


# ── Repair + silent-save ─────────────────────────────────────────────────────

@app.post("/silent-save")
async def silent_save(request: Request, x_api_key: str | None = Header(default=None)):
    """
    Silent Stop-hook save path. Writes a diary checkpoint during normal ops;
    during /repair mode=rebuild, queues the payload to a jsonl file and
    returns a themed "held in trust" message. The queue drains automatically
    when the rebuild completes.

    Body: {
      session_id, wing, entry, topic?, agent_name?,
      themes?: [...],       # for the returned systemMessage tag
      message_count?: int,  # count the hook wants displayed (often len(messages))
    }
    """
    _check_auth(x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    if not body.get("entry"):
        raise HTTPException(status_code=400, detail="'entry' is required")

    # mempalace#86 surfaces wing/room validation as warnings on the write
    # response, but a missing wing reaches tool_diary_write as "" and may
    # not generate a warning at all depending on mempalace version. Detect
    # it here so the systemMessage always flags the broken default.
    # Don't reject — existing callers may rely on the empty-default — just
    # warn so it shows up in the themed chain.
    daemon_warnings: list[str] = []
    raw_wing = body.get("wing")
    if not raw_wing or (isinstance(raw_wing, str) and not raw_wing.strip()):
        daemon_warnings.append(
            "wing is empty — diary entry will have no wing association"
        )

    themes = body.get("themes") or []
    raw_msg_count = body.get("message_count")
    if raw_msg_count is None:
        msg_count = 1
    else:
        try:
            msg_count = int(raw_msg_count)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="'message_count' must be an integer",
            )
        if msg_count <= 0:
            msg_count = 1

    # Acquire write slot, check rebuild flag under lock, then write or queue.
    # Queue only when /repair is doing a rebuild — other modes (light/scan/
    # prune) don't replace the collection out from under in-flight writes.
    async with _write_sem:
        if (
            _repair_state["in_progress"]
            and _repair_state.get("mode") == "rebuild"
        ):
            await _enqueue_pending_write(body)
            return _ensure_warnings_fields({
                "count": msg_count,
                "themes": themes,
                "queued": True,
                "warnings": daemon_warnings,
                "systemMessage": messages.save_queued(msg_count, themes),
            })
        result = await _do_silent_save_write(body)

    # mempalace#86: tool_diary_write may return warnings/errors lists.
    # Forward them unchanged so clients/hook.py can surface them in the
    # themed systemMessage. Older mempalace returns no such fields → [].
    warnings = result.get("warnings") if isinstance(result, dict) else None
    if not isinstance(warnings, list):
        warnings = []
    # Prepend daemon-side warnings (empty wing, etc.) so they surface
    # even when mempalace itself is silent about the issue.
    warnings = list(daemon_warnings) + list(warnings)
    errors = result.get("errors") if isinstance(result, dict) else None
    if not isinstance(errors, list):
        errors = []

    if result.get("success"):
        return _ensure_warnings_fields({
            "count": msg_count,
            "themes": themes,
            "queued": False,
            "entry_id": result.get("entry_id"),
            "warnings": warnings,
            "errors": errors,
            "toast": f"Palace updated: {msg_count} msgs saved ({themes[0] if themes else "checkpoint"})",
            "systemMessage": messages.save_ok(
                msg_count, themes, warnings=warnings, errors=errors,
            ),
        })
    raise HTTPException(
        status_code=500,
        detail=f"silent save failed: {result.get('error', 'unknown')}",
    )


@app.post("/repair")
async def repair(request: Request, x_api_key: str | None = Header(default=None)):
    """
    Coordinate a repair with daemon-mediated traffic.

    Body: { "mode": "light" | "scan" | "prune" | "rebuild" }
      light   — clear caches; next client open re-runs quarantine_stale_hnsw(). Cheap.
      scan    — find corrupt IDs, write corrupt_ids.txt. Read-only.
      prune   — delete corrupt IDs via the flock-safe col.delete path.
      rebuild — destructive: delete + recreate the collection. Holds every
                semaphore slot; silent-save queues during this window and
                drains automatically on completion.

    Only one repair at a time. Second call while one is in-flight → 409.
    """
    _check_auth(x_api_key)
    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    mode = (body.get("mode") or "light").lower()
    if mode not in ("light", "scan", "prune", "rebuild"):
        raise HTTPException(
            status_code=400,
            detail="mode must be one of: light, scan, prune, rebuild",
        )

    # Start transition — guarded so two /repair callers can't both begin.
    async with _repair_lock:
        if _repair_state["in_progress"]:
            raise HTTPException(
                status_code=409,
                detail=f"repair already in progress (mode={_repair_state['mode']})",
            )
        _repair_state["in_progress"] = True
        _repair_state["mode"] = mode
        _repair_state["started_at"] = datetime.now().isoformat()

    _log.info(messages.repair_begin(mode))
    start = datetime.now()
    result: dict[str, Any] = {}
    drained = 0

    try:
        if mode == "light":
            # Clear cached client + collection. Next touch will re-open and
            # re-run quarantine_stale_hnsw() via make_client().
            async with _write_sem:
                _mp._client_cache = None
                _mp._collection_cache = None
                result = {"caches_cleared": True}
            await _warn_if_hnsw_threads_unset()

        elif mode == "scan":
            # Read-only: cap at read slot.
            async with _read_sem:
                loop = asyncio.get_running_loop()
                palace_path = _mp._config.palace_path
                await loop.run_in_executor(None, _mp_repair.scan_palace, palace_path)
                corrupt_file = os.path.join(palace_path, "corrupt_ids.txt")
                count = 0
                if os.path.isfile(corrupt_file):
                    with open(corrupt_file, encoding="utf-8") as f:
                        count = sum(1 for ln in f if ln.strip())
                result = {"corrupt_ids_found": count, "corrupt_file": corrupt_file}

        elif mode == "prune":
            # Takes flock internally (col.delete). Hold a single write slot so
            # we don't inflate daemon throughput while repair is running.
            async with _write_sem:
                loop = asyncio.get_running_loop()
                palace_path = _mp._config.palace_path
                await loop.run_in_executor(
                    None,
                    lambda: _mp_repair.prune_corrupt(palace_path=palace_path, confirm=True),
                )
                result = {"pruned": True}
            _mp._client_cache = None
            _mp._collection_cache = None
            await _warn_if_hnsw_threads_unset()

        elif mode == "rebuild":
            # Destructive: deletes + recreates the collection. Hold every
            # semaphore slot so no daemon-mediated write races the swap.
            async with _exclusive_palace():
                loop = asyncio.get_running_loop()
                palace_path = _mp._config.palace_path
                # Drop the cached PersistentClient + collection BEFORE
                # rebuild_index opens its own. rebuild_index instantiates
                # a fresh ChromaBackend() → new PersistentClient against
                # the same palace_path. If our cache still holds the
                # previous PersistentClient, the new one deadlocks
                # waiting for the SQLite filelock the cached one is
                # still holding. No timeout. See #9.
                _mp._client_cache = None
                _mp._collection_cache = None
                import gc
                gc.collect()
                # Give chromadb background threads a beat to release
                # their grip before we open a fresh client.
                await asyncio.sleep(0.5)

                # Capture mempalace.repair's progress prints into
                # _repair_state so /repair/status can surface them to
                # the operator. Without this, /repair/status returns
                # just `in_progress: true` for the 6-9h rebuild duration.
                # See #12.
                #
                # We parse rather than threading a callback because
                # mempalace's rebuild_index() hardcodes `progress=print`
                # internally. Once MemPalace/mempalace#1485 lands and a
                # new version is installed, this can switch to a direct
                # callback for cleaner integration.
                _repair_state["progress"] = _make_rebuild_progress_state()
                with _capture_rebuild_progress(_repair_state["progress"]):
                    await loop.run_in_executor(
                        None, _mp_repair.rebuild_index, palace_path
                    )
                # Mark done — keeps the final counts visible in /repair/status
                # for a moment after rebuild ends, until the outer handler
                # clears _repair_state.
                _repair_state["progress"]["phase"] = "done"
                result = {"rebuilt": True}
            await _warn_if_hnsw_threads_unset()

    except Exception as e:
        _log.exception("repair (%s) failed", mode)
        async with _repair_lock:
            _repair_state["in_progress"] = False
            _repair_state["mode"] = None
            _repair_state["started_at"] = None
        raise HTTPException(status_code=500, detail=f"repair failed: {e}")

    # Clear the flag BEFORE draining so replayed silent-saves go direct,
    # not back into the queue.
    async with _repair_lock:
        _repair_state["in_progress"] = False
        _repair_state["mode"] = None
        _repair_state["started_at"] = None
        # Don't strand stale rebuild progress in /repair/status after
        # the operation has completed. (palace-daemon#12)
        _repair_state.pop("progress", None)

    drained_mines = 0
    if mode == "rebuild":
        drained = await _drain_pending_writes()
        # Also replay any /mine requests queued during the rebuild. Mirrors
        # _drain_pending_writes — same rename-then-read, dedup-by-target,
        # subprocess re-execution.
        drained_mines = await _drain_pending_mines()

    duration = (datetime.now() - start).total_seconds()
    _log.info(messages.repair_complete(mode, drained, duration))
    return {
        "mode": mode,
        "result": result,
        "drained": drained,
        "drained_mines": drained_mines,
        "duration_s": round(duration, 3),
        "systemMessage": messages.repair_complete(mode, drained, duration),
    }


@app.get("/repair/status")
async def repair_status():
    """Current repair state + pending-writes + pending-mines queue depths."""
    def _count_lines(path: str) -> int:
        if not os.path.isfile(path):
            return 0
        try:
            with open(path, encoding="utf-8") as f:
                return sum(1 for ln in f if ln.strip())
        except OSError:
            return -1

    writes_path = _pending_writes_path()
    mines_path = _pending_mines_path()
    response = {
        "in_progress": _repair_state["in_progress"],
        "mode": _repair_state["mode"],
        "started_at": _repair_state["started_at"],
        "pending_writes": _count_lines(writes_path),
        "pending_writes_path": writes_path,
        "pending_mines": _count_lines(mines_path),
        "pending_mines_path": mines_path,
    }
    # Surface rebuild progress when present (palace-daemon#12). The
    # `progress` dict is set up in the rebuild handler and lives across
    # the executor-thread callback updates. Strip the internal monotonic
    # field — operators only care about the user-facing numbers.
    progress = _repair_state.get("progress")
    if progress is not None:
        response["progress"] = {
            k: v for k, v in progress.items() if k != "started_at_monotonic"
        }
    # Surface quarantined segment dirs so operators don't have to grep
    # journald to know a chromadb integrity gate fired (see
    # docs/recovery/chromadb-metadata-dict-patch.md). Cheap — one glob,
    # no expensive walk into the segment contents.
    palace_path = _mp._config.palace_path
    quarantined = []
    try:
        import glob
        for pattern in ("*.corrupt-*", "*.drift-*", "*.quarantined-*"):
            for path in glob.glob(os.path.join(palace_path, pattern)):
                if os.path.isdir(path):
                    quarantined.append(os.path.basename(path))
    except Exception:
        # Don't let a glob failure take down the status endpoint.
        pass
    response["quarantined_segments"] = sorted(quarantined)
    response["quarantined_count"] = len(quarantined)
    return response


# ── Helpers ───────────────────────────────────────────────────────────────────

def _unwrap(mcp_response: dict) -> Any:
    try:
        text = mcp_response["result"]["content"][0]["text"]
        return json.loads(text)
    except (KeyError, TypeError, json.JSONDecodeError):
        return mcp_response


# mempalace#86 — bubble warnings/errors through to clients with a stable
# response shape, regardless of whether mempalace emits the new fields.
_ensure_warnings_fields = messages.ensure_warnings_fields


# ── Entry point ───────────────────────────────────────────────────────────────

# Global to prevent GC from closing the file and releasing the lock
_lock_file = None


def _clear_port(port: int):
    """Attempt to kill any process currently holding the target port."""
    import subprocess
    try:
        # Use fuser to kill the process on the port.
        subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
    except Exception:
        pass


def main():
    global _lock_file
    parser = argparse.ArgumentParser(description="palace-daemon — MemPalace HTTP/MCP gateway")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (default: 8085)")
    parser.add_argument("--palace", default=DEFAULT_PALACE, help="Palace path (overrides mempalace config)")
    parser.add_argument("--api-key", default=API_KEY, help="API key for auth (optional)")
    parser.add_argument("--force", action="store_true", help="Force clear port before starting (used by systemd)")
    parser.add_argument("--manual", action="store_true", help="Allow manual start outside of systemd")
    args = parser.parse_args()

    # Prevent accidental manual starts by agents
    if not os.getenv("INVOCATION_ID") and not args.manual:
        print("ERROR: Manual startup detected. Use 'sudo systemctl start palace-daemon' instead.")
        print("If you MUST run manually for debugging, use the --manual flag.")
        sys.exit(1)

    if args.force:
        _clear_port(args.port)

    # Simple file lock to prevent multiple daemon instances on the same port.
    # ~/.cache/palace-daemon/ (mode 0o700) avoids world-writable /tmp exposure.
    lock_dir = Path.home() / ".cache" / "palace-daemon"
    lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file_path = str(lock_dir / f"daemon-{args.port}.lock")
    _lock_file = open(lock_file_path, "w")
    try:
        fcntl.lockf(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print(f"ERROR: Another instance of palace-daemon is already running on port {args.port}.", file=sys.stderr)
        sys.exit(1)

    if args.palace:
        os.environ["MEMPALACE_PALACE"] = args.palace
    if args.api_key:
        os.environ["PALACE_API_KEY"] = args.api_key

    # timeout_graceful_shutdown bounds uvicorn's pre-lifespan wait for
    # in-flight connections + background asyncio tasks (watchdog, /backfill-age,
    # etc.). Without this it defaults to None (wait indefinitely), which on
    # 2026-05-28 produced a 20s wait between SIGTERM and the lifespan
    # shutdown handler firing — eating most of systemd's TimeoutStopSec=30s
    # budget even though the lifespan shutdown itself completes in <5s.
    #
    # 15s is the per-step budget; together with the ~5s lifespan teardown
    # this keeps total shutdown safely under TimeoutStopSec. Configurable
    # via PALACE_UVICORN_SHUTDOWN_TIMEOUT_S.
    uvicorn_shutdown_s = int(os.environ.get("PALACE_UVICORN_SHUTDOWN_TIMEOUT_S", "15"))
    uvicorn.run(
        "main:app",
        host=args.host,
        port=args.port,
        log_level="info",
        timeout_graceful_shutdown=uvicorn_shutdown_s,
    )


if __name__ == "__main__":
    main()
