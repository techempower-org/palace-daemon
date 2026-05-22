"""
palace-daemon — HTTP/MCP gateway for MemPalace with concurrent access control

Three semaphores govern concurrency (all tunable via PALACE_MAX_CONCURRENCY):
  _read_sem  — up to N concurrent read-only ops (search, query, stats, …)
  _write_sem — up to N//2 concurrent write ops (add, update, kg mutations, …)
  _mine_sem  — one mine job at a time, independent of reads/writes

Roadmap:
  [HIGH] Verified Backups: /backup endpoint with integrity_check + smoke test retrieval.
  [DONE] Stability: Auto-detect "Internal Error" during search and trigger index recovery.
  [DONE] Flush: Ensure memories are checkpointed on shutdown and via /flush.
  [HIGH] Unified Routing: Ensure all clients (including miners/compactors) use the Daemon API.
  [MED]  Maintenance: Automate _READ_TOOLS sync with upstream mempalace.
"""
import argparse
import asyncio
import hmac
import json
import logging
import os
import sqlite3
import sys
import fcntl
import signal
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
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

# ── Config (env vars override CLI defaults) ───────────────────────────────────

VERSION = "1.7.2"
DEFAULT_HOST = os.getenv("PALACE_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("PALACE_PORT", "8085"))
DEFAULT_PALACE = os.getenv("PALACE_PATH", "")
API_KEY = os.getenv("PALACE_API_KEY", "")  # read at startup for argparse default; auth checks re-read from env dynamically
PALACE_MAX_CONCURRENCY = int(os.getenv("PALACE_MAX_CONCURRENCY", "4"))
PALACE_MAX_READ_CONCURRENCY = int(os.getenv("PALACE_MAX_READ_CONCURRENCY", str(PALACE_MAX_CONCURRENCY)))
PALACE_MAX_WRITE_CONCURRENCY = int(os.getenv("PALACE_MAX_WRITE_CONCURRENCY", str(max(1, PALACE_MAX_CONCURRENCY // 2))))

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


# ── Rebuild progress capture (palace-daemon#12) ──────────────────────────────
#
# `mempalace.repair.rebuild_index` prints "Staged N/M" and "Re-filed N/M"
# style progress to stdout. The function doesn't accept a callback (filed
# upstream as MemPalace/mempalace#1485), so we capture stdout via
# contextlib.redirect_stdout and parse the lines to expose progress to
# /repair/status. Once #1485 lands and a new mempalace version is installed,
# this can switch to a direct callback.

import contextlib  # noqa: E402
import io          # noqa: E402
import re          # noqa: E402
import time as _time  # noqa: E402


_REBUILD_RE_STAGED = re.compile(r"Staged\s+(\d+)/(\d+)")
_REBUILD_RE_REFILED = re.compile(r"Re-filed\s+(\d+)/(\d+)")
_REBUILD_RE_FOUND = re.compile(r"Drawers found:\s+(\d+)")


def _make_rebuild_progress_state() -> dict[str, Any]:
    """Initial progress dict, exposed via /repair/status during a rebuild."""
    return {
        "phase": "starting",
        "completed": 0,
        "expected": 0,
        "rate_per_sec": 0.0,
        "eta_seconds": None,
        "elapsed_seconds": 0.0,
        "last_message": "",
        "started_at_monotonic": _time.monotonic(),
    }


class _RebuildProgressBuffer(io.TextIOBase):
    """Stdout sink for rebuild_index that parses progress lines into a dict.

    Lives in the executor thread (rebuild runs synchronously there) but
    writes a small set of int/float/str fields into ``state`` — Python's
    GIL makes individual dict assignments safe enough for the /repair/status
    handler running on the main asyncio thread to read.
    """

    def __init__(self, state: dict[str, Any]):
        super().__init__()
        self._state = state
        self._buf = ""

    def writable(self) -> bool:  # io.TextIOBase contract
        return True

    def write(self, s: str) -> int:
        # Buffer + split on newlines so we don't parse partial lines.
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._handle_line(line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._handle_line(self._buf)
            self._buf = ""

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self._state["last_message"] = line
        m = _REBUILD_RE_STAGED.search(line)
        if m:
            self._update_phase("stage", int(m.group(1)), int(m.group(2)))
            return
        m = _REBUILD_RE_REFILED.search(line)
        if m:
            self._update_phase("refile", int(m.group(1)), int(m.group(2)))
            return
        m = _REBUILD_RE_FOUND.search(line)
        if m:
            self._state["expected"] = int(m.group(1))
            self._state["phase"] = "extracting"
            return
        # Other status messages — let the operator see them via last_message.

    def _update_phase(self, phase: str, completed: int, expected: int) -> None:
        elapsed = _time.monotonic() - self._state["started_at_monotonic"]
        rate = completed / elapsed if elapsed > 0 else 0.0
        remaining = max(0, expected - completed)
        # "Refile" phase costs an additional re-embed of the full set; reflect
        # that in the ETA by doubling the remaining work when we're in stage.
        # (See MemPalace/mempalace#1486 — temp-collection atomicity double-pass.)
        if phase == "stage":
            work_remaining = remaining + expected
        else:  # refile
            work_remaining = remaining
        eta = work_remaining / rate if rate > 0 else None
        self._state["phase"] = phase
        self._state["completed"] = completed
        self._state["expected"] = expected
        self._state["elapsed_seconds"] = round(elapsed, 1)
        self._state["rate_per_sec"] = round(rate, 2)
        self._state["eta_seconds"] = round(eta, 0) if eta is not None else None


@contextlib.contextmanager
def _capture_rebuild_progress(state: dict[str, Any]):
    """Redirect stdout to a parser that updates ``state`` while we're inside.

    Used around the run_in_executor(rebuild_index) call so the executor
    thread's stdout flows through _RebuildProgressBuffer instead of the
    default sys.stdout (which would go to journald, mixed in with other
    daemon output).
    """
    buf = _RebuildProgressBuffer(state)
    with contextlib.redirect_stdout(buf):
        try:
            yield buf
        finally:
            buf.flush()


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
_READ_TOOLS = {
    "mempalace_search",
    "mempalace_kg_query",
    "mempalace_kg_stats",
    "mempalace_kg_timeline",
    "mempalace_graph_stats",
    "mempalace_status",
    "mempalace_list_drawers",
    "mempalace_get_drawer",
    "mempalace_list_rooms",
    "mempalace_list_wings",
    "mempalace_list_tunnels",
    "mempalace_find_tunnels",
    "mempalace_follow_tunnels",
    "mempalace_traverse",
    "mempalace_diary_read",
    "mempalace_check_duplicate",
    "mempalace_get_taxonomy",
    "mempalace_get_aaak_spec",
    "mempalace_hook_settings",
}


def _check_auth(x_api_key: str | None):
    key = os.getenv("PALACE_API_KEY", "")
    if not key:
        return
    # hmac.compare_digest requires both arguments to be the same type and
    # non-None. Treat a missing header as an empty string so we always run
    # the constant-time path — short-circuiting on ``x_api_key is None``
    # would reintroduce a timing distinction between "no header" and
    # "wrong header".
    provided = x_api_key or ""
    if not hmac.compare_digest(provided, key):
        raise HTTPException(status_code=401, detail="Invalid API key")


# Sentinel for "no value passed" — distinguishes _parse_path_map() (read env)
# from _parse_path_map(None) (no mapping). Closes Copilot's test-isolation
# concern on jphein/palace-daemon#1: the previous None default coupled tests
# to whatever PALACE_DAEMON_PATH_MAP happened to be in the test process env.
_PATH_MAP_USE_ENV: object = object()


def _parse_path_map(raw=_PATH_MAP_USE_ENV) -> list[tuple[str, str]]:
    """Parse PALACE_DAEMON_PATH_MAP into ordered (client_prefix, daemon_prefix) pairs.

    Format: comma-separated ``client_prefix=daemon_prefix`` entries. Whitespace
    around each token is stripped. Empty entries and entries missing ``=`` are
    skipped silently. Order is preserved so the operator can put more-specific
    prefixes first.

    Args:
        raw: When omitted, reads from ``PALACE_DAEMON_PATH_MAP``. Pass an
            explicit string (or ``""``/``None``) to bypass env entirely —
            tests use this to stay deterministic regardless of CI / dev env.

    Example::

        PALACE_DAEMON_PATH_MAP="/home/jp/.claude/=/mnt/raid/claude-config/,/home/jp/Projects/=/mnt/raid/projects/"
    """
    if raw is _PATH_MAP_USE_ENV:
        raw = os.environ.get("PALACE_DAEMON_PATH_MAP", "")
    raw = (raw or "").strip()
    if not raw:
        return []
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        client_prefix, daemon_prefix = entry.split("=", 1)
        client_prefix = client_prefix.strip()
        daemon_prefix = daemon_prefix.strip()
        if client_prefix and daemon_prefix:
            pairs.append((client_prefix, daemon_prefix))
    return pairs


def _translate_client_path(path: str) -> str:
    """Translate a client-side absolute path to a daemon-side path.

    Hooks running on a client machine (e.g. katana) speak in their own
    filesystem namespace (``/home/jp/.claude/...``); the daemon may see the
    same files at a different mount (``/mnt/raid/claude-config/...`` via
    Syncthing). ``PALACE_DAEMON_PATH_MAP`` lets the operator declare those
    rewrites without coupling client code to deployment specifics.

    The first matching prefix wins; non-matching paths pass through
    unchanged so daemon-side absolute paths still work.

    Joining is normalized so mismatched trailing/leading slashes between
    the two prefixes can't produce paths like ``/mnt/raid/ccprojects/...``
    (Copilot finding on jphein/palace-daemon#1).
    """
    for client_prefix, daemon_prefix in _parse_path_map():
        if path.startswith(client_prefix):
            suffix = path[len(client_prefix):]
            return daemon_prefix.rstrip("/") + "/" + suffix.lstrip("/")
    return path


def _sem_for(request_dict: dict) -> asyncio.Semaphore:
    method = request_dict.get("method", "")
    if method == "ping":
        return _read_sem
    tool_name = request_dict.get("params", {}).get("name", "")
    return _read_sem if tool_name in _READ_TOOLS else _write_sem


async def _auto_repair():
    """Trigger index recovery and reload the mempalace client."""
    loop = asyncio.get_running_loop()
    palace_path = _mp._config.palace_path
    moved = await loop.run_in_executor(None, quarantine_stale_hnsw, palace_path)
    if moved:
        _log.warning("AUTO-REPAIR: Quarantined %d stale HNSW segments. Reloading client.", len(moved))
        _mp._client_cache = None
        _mp._collection_cache = None
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
        # Same valid-mode/extract sets as the live /mine endpoint —
        # apply them on replay too so a queue entry can't smuggle through
        # a value the live endpoint would reject (Copilot finding on
        # jphein/palace-daemon#4).
        VALID_MODES = {"convos", "projects"}
        VALID_EXTRACTS = {"exchange", "general"}
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
                if mode not in VALID_MODES:
                    _log.warning("drain-mine: skipping %s — invalid mode %r", directory, mode)
                    continue
                extract = payload.get("extract")
                if extract is not None and extract not in VALID_EXTRACTS:
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
                    stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    count += 1
                else:
                    _log.warning(
                        "drain-mine: replay returned %s for %s\n  stderr: %s",
                        proc.returncode,
                        directory,
                        (stderr or b"").decode(errors="replace")[:300],
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
    wing = payload.get("wing", "") or ""
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
        try:
            result = await loop.run_in_executor(None, _mp.handle_request, request_dict)
            
            if result and "error" in result:
                msg = str(result["error"].get("message", ""))
                is_hnsw_error = "Internal error: Error finding id" in msg or "Internal error: id" in msg
                
                tool_name = request_dict.get("params", {}).get("name", "")
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging
    logger = logging.getLogger(__name__)
    
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
            mempalace_bin = os.path.join(os.path.dirname(sys.executable), "mempalace")
            argv = [mempalace_bin, "mine", path, "--mode", "projects", "--wing", wing]
            # Same pattern as /mine endpoint: list-form argv, no shell.
            async with _mine_sem:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                # Surface the actual subprocess output — the rc alone hides
                # 'No mempalace.yaml found' / 'directory not readable' /
                # python tracebacks that operators need to diagnose.
                # Closes Copilot finding on jphein/palace-daemon#3.
                logger.warning(
                    "watcher mine returned %s for %s\n  stderr: %s\n  stdout: %s",
                    proc.returncode,
                    path,
                    (stderr or b"").decode(errors="replace")[:500],
                    (stdout or b"").decode(errors="replace")[-500:],
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
    try:
        watcher = getattr(app.state, "watcher", None)
        if watcher is not None:
            watcher.stop()
            logger.info("WatcherService stopped.")
    except Exception:
        logger.exception("WatcherService stop failed (non-fatal)")
    try:
        # We call mempalace_memories_filed_away which triggers a checkpoint in recent mempalace versions
        await _call({
            "jsonrpc": "2.0", "id": "shutdown",
            "method": "tools/call",
            "params": {"name": "mempalace_memories_filed_away", "arguments": {}}
        }, retry_on_hnsw=False)
        logger.info("Flush complete.")
    except Exception as e:
        logger.error("Error during shutdown flush: %s", e)

    # --- Shutdown: explicit ChromaDB client teardown ---
    # ChromaDB 1.5.x PersistentClient has no clean close() (chroma#5868).
    # The flush above triggers a checkpoint; this block ensures the client
    # is actually torn down — drop refs, force a GC pass, then sleep
    # briefly so chromadb's background flush threads finish writing
    # before the process exits. Without this, SIGTERM at the wrong
    # millisecond leaves the HNSW segment in partial-flush corruption:
    # data_level0.bin written, link_lists.bin not yet, the chromadb
    # metadata file missing. The integrity gate then quarantines on
    # next open and we burn cycles rebuilding the index every restart.
    # See #8.
    try:
        _mp._collection_cache = None
        _mp._client_cache = None
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
    except Exception:
        pass
    status = "ok" if palace_ok else "degraded"
    payload = {"status": status, "daemon": "palace-daemon", "version": VERSION, "palace": result}
    if not palace_ok:
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
    return _unwrap(result)


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
    wing = body.get("wing") or None
    room = body.get("room") or None
    limit = int(body.get("limit") or 10)
    include_trace = bool(body.get("include_trace") or False)
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="'limit' must be 1..100")
    if room is not None and room not in _canonical_rooms():
        raise HTTPException(
            status_code=400,
            detail={"error": f"room {room!r} is not canonical",
                    "valid_rooms": sorted(_canonical_rooms())},
        )

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

    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_search", "arguments": args},
    })
    return _unwrap(result)


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
    wing = body.get("wing") or None
    room = body.get("room") or None
    limit = int(body.get("limit") or 20)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="'limit' must be 1..200")

    # Validate room if provided so callers get fast feedback (vs an
    # empty-result silent surprise from a typo).
    if room is not None and room not in _canonical_rooms():
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"room {room!r} is not in the canonical set",
                "valid_rooms": sorted(_canonical_rooms()),
            },
        )

    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
    if not dsn:
        raise HTTPException(status_code=500, detail="MEMPALACE_POSTGRES_DSN not set in daemon environment")

    from mempalace.searcher import _bm25_only_via_postgres
    result = _bm25_only_via_postgres(query, dsn, wing=wing, room=room, n_results=limit)
    return result


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
    wing = body.get("wing") or None
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
            return {"results": vec_hits[:limit], "trace": {
                "n_vector": len(vec_hits), "n_graph": 0, "n_after_fusion": min(limit, len(vec_hits)),
                "warning": "MEMPALACE_POSTGRES_DSN not set; age-fused falls back to vector-only",
            }}
        return {"results": vec_hits[:limit]}

    # Initialize *before* the AGE lookup so the trace block can read it
    # even when the lookup raises before extraction happens.
    query_entities: list = []
    graph_hits_by_drawer: dict[str, float] = {}

    def _age_lookup() -> tuple[list, dict[str, float]]:
        """Sync AGE entity-overlap lookup. Called via ``asyncio.to_thread``
        so the daemon's event loop isn't blocked on Postgres I/O."""
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
                        RETURN d.id AS id, r.count AS count
                        """,
                        {"ename": qe.name},
                        fetch=True,
                    )
                except Exception:
                    continue
                for r in rows:
                    drawer_id = kg._unwrap_agtype(r[0])
                    cnt = kg._unwrap_agtype(r[1]) or 1
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
    vec_ranks = {hit.get("id"): i for i, hit in enumerate(vec_hits)}
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
    # vector saw the drawer; synth minimal metadata when graph-only.
    vec_by_id = {hit.get("id"): hit for hit in vec_hits}
    fused_order = sorted(fused_scores.items(), key=lambda kv: -kv[1])[:limit]
    out_hits: list[dict] = []
    for did, score in fused_order:
        if did in vec_by_id:
            hit = dict(vec_by_id[did])
            hit["matched_via"] = "both" if did in graph_ranks else "vector"
            hit["rrf_score"] = score
        else:
            # Graph-only drawer — minimal stub. Caller can fetch full
            # drawer via /memory/{id} if needed.
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
    return response


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
    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": "mempalace_update_drawer", "arguments": args},
    })
    return _unwrap(result)


def _normalize_wing_slug(s: str) -> str:
    """Canonical wing-slug form per the 2026-05-14 taxonomy spec §3.2.

    Idempotent: applying twice yields the same result. Used at the
    /memory boundary so writes from any caller (familiar, manual curl,
    test rigs) land with the same slug shape as the miner produces.
    """
    import re as _re
    if not s:
        return "unknown"
    s = s.lower()
    if s.startswith("wing_"):
        s = s[5:]
    s = _re.sub(r"[^a-z0-9_]", "_", s)
    return s or "unknown"


# Cached set of canonical room names. Populated lazily on first /memory
# write; invalidate via POST /admin/refresh-rooms after registering a new
# canonical room (e.g. `mempalace rooms add`). Otherwise cached for the
# daemon's lifetime.
_canonical_rooms_cache: set[str] | None = None


def _canonical_rooms() -> set[str]:
    """Read the configurable room set from mempalace_canonical_rooms.

    Falls back to the spec's default 7 when the lookup table is absent
    or the backend isn't postgres (legacy chroma path doesn't have the
    FK lookup; validate against the spec defaults).
    """
    global _canonical_rooms_cache
    if _canonical_rooms_cache is not None:
        return _canonical_rooms_cache

    DEFAULTS = {"architecture", "decisions", "problems", "planning",
                "sessions", "references", "discoveries"}

    try:
        if _mp._config.backend != "postgres":
            _canonical_rooms_cache = DEFAULTS
            return _canonical_rooms_cache
        import psycopg2
        dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
        if not dsn:
            _canonical_rooms_cache = DEFAULTS
            return _canonical_rooms_cache
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM mempalace_canonical_rooms")
                rows = cur.fetchall()
        if rows:
            _canonical_rooms_cache = {r[0] for r in rows}
        else:
            _canonical_rooms_cache = DEFAULTS
    except Exception:
        _canonical_rooms_cache = DEFAULTS
    return _canonical_rooms_cache


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

    result = await _call({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {
            "name": "mempalace_add_drawer",
            "arguments": {"wing": wing, "room": room, "content": content},
        },
    })
    unwrapped = _unwrap(result)
    if isinstance(unwrapped, dict) and unwrapped.get('success'):
        unwrapped['toast'] = f'Filed to {wing}/{room}'
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
    global _canonical_rooms_cache
    _canonical_rooms_cache = None
    rooms = sorted(_canonical_rooms())
    return {"refreshed": True, "rooms": rooms, "count": len(rooms)}


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

        from mempalace.knowledge_graph_age import KnowledgeGraphAGE

        # Reuse mempalace's AGE helper for RETURN-alias parsing + agtype unwrap.
        # Constructing a fresh KnowledgeGraphAGE bootstraps the graph if absent,
        # which is harmless for a query (the MERGE on absent graphs creates it).
        dsn = _mp._config.postgres_dsn
        if not dsn:
            return None, "MEMPALACE_POSTGRES_DSN not configured"
        try:
            kg = KnowledgeGraphAGE(dsn=dsn)
        except psycopg2.Error as e:
            return None, f"postgres connect failed: {e}"
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
            try:
                rows = kg._run_cypher(cypher, params={}, fetch=True)
            except psycopg2.errors.ReadOnlySqlTransaction as e:
                kg._conn.rollback()
                return None, ("read-only", str(e))
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
        if isinstance(err, tuple) and err and err[0] == "read-only":
            raise HTTPException(
                status_code=403,
                detail=(
                    "/cypher is read-only; write verbs (CREATE/MERGE/SET/DELETE/"
                    "DETACH DELETE/REMOVE) are rejected. Use mempalace_kg_* MCP "
                    f"tools for graph mutations. PostgreSQL: {err[1]}"
                ),
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


def _kg_path() -> str:
    """KG sqlite path. Lives next to chroma.sqlite3 inside the palace dir."""
    return os.path.join(_mp._config.palace_path, "knowledge_graph.sqlite3")


def _chroma_path() -> str:
    """Chroma sqlite path inside the palace dir."""
    return os.path.join(_mp._config.palace_path, "chroma.sqlite3")


def _read_wings_rooms_postgres() -> tuple[dict[str, int], list[dict]]:
    """Read wings + rooms-per-wing directly from the postgres backend.

    Two cheap GROUP BY queries on the indexed `wing` / (`wing`,`room`)
    columns of `mempalace_drawers`. Measured at ~150ms each on the
    canonical 270K-drawer palace, well under the original chroma-sqlite
    direct read budget and small enough to compute live on every /graph
    call instead of caching.

    Returns ({}, []) on any failure so /graph degrades gracefully — the
    SME adapter falls back to MCP composition in that case.
    """
    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN") or getattr(
        _mp._config, "postgres_dsn", None
    )
    if not dsn:
        return {}, []

    wings: dict[str, int] = {}
    rooms_by_wing: dict[str, dict[str, int]] = {}
    try:
        import psycopg2
        # Short timeout — /graph is interactive; we'd rather degrade than
        # block the request behind a stuck planner.
        with psycopg2.connect(dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SET LOCAL statement_timeout = '10s'; "
                    "SELECT wing, COUNT(*) FROM mempalace_drawers GROUP BY wing"
                )
                for name, n in cur.fetchall():
                    if name:
                        wings[name] = n
            with conn.cursor() as cur:
                cur.execute(
                    "SET LOCAL statement_timeout = '10s'; "
                    "SELECT wing, room, COUNT(*) FROM mempalace_drawers "
                    "GROUP BY wing, room"
                )
                for wing, room, n in cur.fetchall():
                    if wing and room:
                        rooms_by_wing.setdefault(wing, {})[room] = n
    except Exception:
        # Schema drift, connection issue, statement timeout, anything —
        # degrade to empty rather than 500 the /graph request.
        return {}, []

    all_wings = set(wings) | set(rooms_by_wing)
    rooms = [{"wing": w, "rooms": rooms_by_wing.get(w, {})} for w in sorted(all_wings)]
    return wings, rooms


def _read_wings_rooms_direct() -> tuple[dict[str, int], list[dict]]:
    """Read wings + rooms directly from the live backend, off-loop.

    Bypasses the MCP fan-out (list_wings + list_rooms × N) which serializes
    through the read semaphore and stalls under load. Computes live on
    every call — both backends are fast enough that caching just creates
    staleness bugs (the chroma sqlite snapshot used to lag the live
    postgres backend by ~10× after the chroma → postgres migration; this
    was the original motivation for routing by backend here).

    - postgres: two GROUP BY queries on `mempalace_drawers` (~150ms each
      on the canonical 270K-drawer palace).
    - chroma:   GROUP BY on `embedding_metadata` in the persistent
      client's `chroma.sqlite3`. ~200× faster than the MCP fan-out on
      151K drawers (~0.4s vs 60-120s under contention).

    Schemas are internal to the respective backends — not part of
    mempalace's public API. Tolerated by catching OperationalError /
    psycopg2 errors; if the schema ever drifts, /graph degrades to empty
    wings/rooms (the SME adapter then falls back to its MCP composition
    path).
    """
    # Route by configured backend. The chroma sqlite path is a stale
    # snapshot under postgres (it was the pre-migration store and
    # receives no further writes), so reading it would return frozen
    # counts — exactly the "10× stale" bug this function exists to avoid.
    backend = getattr(_mp._config, "backend", None)
    if backend == "postgres":
        return _read_wings_rooms_postgres()

    chroma = _chroma_path()
    if not os.path.isfile(chroma):
        return {}, []
    try:
        conn = sqlite3.connect(f"file:{chroma}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError:
        return {}, []

    wings: dict[str, int] = {}
    rooms_by_wing: dict[str, dict[str, int]] = {}
    try:
        try:
            for name, n in conn.execute(
                "SELECT string_value, COUNT(*) FROM embedding_metadata "
                "WHERE key='wing' GROUP BY string_value"
            ):
                if name:
                    wings[name] = n
        except sqlite3.OperationalError:
            pass
        try:
            for wing, room, n in conn.execute(
                "SELECT em_w.string_value, em_r.string_value, COUNT(*) "
                "FROM embedding_metadata em_w "
                "JOIN embedding_metadata em_r ON em_w.id = em_r.id "
                "WHERE em_w.key='wing' AND em_r.key='room' "
                "GROUP BY em_w.string_value, em_r.string_value"
            ):
                if wing and room:
                    rooms_by_wing.setdefault(wing, {})[room] = n
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()

    # Iterate the union of wings + rooms_by_wing keys, not just `wings`,
    # so a partial schema-drift (wings query OperationalError-ed but the
    # rooms-per-wing query succeeded, or vice versa) doesn't silently
    # drop the half that worked.
    all_wings = set(wings) | set(rooms_by_wing)
    rooms = [{"wing": w, "rooms": rooms_by_wing.get(w, {})} for w in sorted(all_wings)]
    return wings, rooms


def _read_kg_postgres() -> tuple[list[dict], list[dict]]:
    """Read entities + triples directly from the live AGE graph.

    Shape matches the chroma sqlite KG schema so the /graph response
    stays stable across backends:
      entity:  {id, name, type, properties}
      triple:  {subject, predicate, object, valid_from, valid_to,
                confidence, source_file}

    AGE entities don't have a separate `id`/`type`/`properties` model —
    they're MERGE'd by name when a triple is added, so we map
    id=name=name, type='entity', properties={}. `source_file` is
    populated from the relation's `source` field if present.

    Limited to 500 triples to bound /graph latency on large palaces;
    callers that need the full graph should query AGE directly via
    /cypher.
    """
    dsn = getattr(_mp._config, "postgres_dsn", None) or os.environ.get(
        "MEMPALACE_POSTGRES_DSN"
    )
    if not dsn:
        return [], []

    try:
        # Construct on-demand. The AGE helper's __init__ is cheap once
        # the extension is loaded; the explicit close() in finally
        # releases the connection we opened here.
        from mempalace.knowledge_graph_age import KnowledgeGraphAGE

        kg = KnowledgeGraphAGE(dsn=dsn)
    except Exception:
        return [], []

    try:
        # query_triples() returns dicts with keys subject, relation_type,
        # object, source, valid_from, valid_to, confidence.
        rows = kg.query_triples()
    except Exception:
        rows = []
    finally:
        try:
            kg.close()
        except Exception:
            pass

    rows = rows[:500]  # cap to bound payload

    triples: list[dict] = []
    entity_names: set[str] = set()
    for r in rows:
        subj = r.get("subject")
        pred = r.get("relation_type")
        obj = r.get("object")
        if not (subj and pred and obj):
            continue
        triples.append({
            "subject": subj,
            "predicate": pred,
            "object": obj,
            "valid_from": r.get("valid_from"),
            "valid_to": r.get("valid_to"),
            "confidence": r.get("confidence"),
            "source_file": r.get("source"),
        })
        entity_names.add(subj)
        entity_names.add(obj)

    entities = [
        {"id": n, "name": n, "type": "entity", "properties": {}}
        for n in sorted(entity_names)
    ]
    return entities, triples


def _read_kg_direct() -> tuple[list[dict], list[dict]]:
    """Read-only snapshot of KG entities + triples.

    Under the chroma backend the KG lives in a sibling SQLite file
    (`knowledge_graph.sqlite3`); a read there does not cross the
    single-writer invariant. Schema differences (older palaces,
    in-progress migrations) are tolerated by catching OperationalError
    on each query.

    Under the postgres backend the KG lives in AGE (the `mempalace_kg`
    graph). We use `KnowledgeGraphAGE` to read the live graph directly —
    one Cypher MATCH for triples, then derive the entity list from the
    union of subjects+objects (AGE schema has no separate entity-row
    concept; entities are MERGE'd by name when triples are inserted).
    Limited to 500 triples to bound `/graph` latency on large palaces;
    UI surfaces this as a sample, not a full export.
    """
    if getattr(_mp._config, "backend", None) == "postgres":
        return _read_kg_postgres()
    kg_path = _kg_path()
    if not os.path.isfile(kg_path):
        return [], []
    try:
        conn = sqlite3.connect(f"file:{kg_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return [], []

    entities: list[dict] = []
    triples: list[dict] = []
    try:
        try:
            for r in conn.execute("SELECT id, name, type, properties FROM entities"):
                try:
                    props = json.loads(r["properties"] or "{}")
                except (TypeError, ValueError):
                    props = {}
                entities.append({
                    "id": r["id"],
                    "name": r["name"],
                    "type": r["type"] or "unknown",
                    "properties": props,
                })
        except sqlite3.OperationalError:
            pass
        try:
            for r in conn.execute(
                "SELECT subject, predicate, object, valid_from, valid_to, "
                "confidence, source_file FROM triples"
            ):
                triples.append({
                    "subject": r["subject"],
                    "predicate": r["predicate"],
                    "object": r["object"],
                    "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"],
                    "confidence": r["confidence"],
                    "source_file": r["source_file"],
                })
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return entities, triples


@app.get("/graph")
async def graph(x_api_key: str | None = Header(default=None)):
    """Single-shot structural snapshot for SME-style consumers.

    Mirrors `/stats`'s asyncio.gather pattern but adds:
    - rooms-per-wing fan-out (parallel)
    - direct sqlite read of the KG (no extra MCP roundtrip)

    Replaces what an SME adapter would otherwise compose by serially
    calling list_wings + list_rooms × N + list_tunnels + kg_stats over
    HTTP. On the 151K-drawer canonical palace, list_wings alone takes
    ~30s; the gather here finishes in well under that.
    """
    _check_auth(x_api_key)

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
    kg_direct_task   = _direct_under_sem(_read_kg_direct)

    graph_stats_resp, kg_stats_resp, (wings, rooms), (kg_entities, kg_triples) = (
        await asyncio.gather(
            graph_stats_task,
            kg_stats_task,
            wings_rooms_task,
            kg_direct_task,
        )
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
        "kg_stats": _unwrap(kg_stats_resp) or {},
    }


# ── /viz status dashboard ───────────────────────────────────────────────────

_VIZ_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "viz.html")
_VIZ_HTML_CACHE: str | None = None


@app.get("/viz", response_class=HTMLResponse)
async def viz(
    key: str | None = None,
    x_api_key: str | None = Header(default=None),
):
    """Self-contained status dashboard at /viz.

    Returns the HTML page from static/viz.html. The page then fetches
    /graph, /repair/status, and /health client-side and renders five panels:
    KG force-graph (D3), wings bar chart, wing/room hierarchy (Mermaid),
    tunnels list, KG stats.

    Auth: same as every other endpoint — ``X-Api-Key`` header. As an
    ergonomic shortcut for browser bookmarking, ``?key=...`` is also
    accepted; the page reads it from the URL and re-supplies it to the
    data endpoints. The ``?key=...`` shape leaks the key into browser
    history, proxy logs, and referer headers — prefer the header for
    anything beyond a personal bookmark.

    The HTML template is read from disk lazily on the first request and
    cached in-process thereafter (one disk read per daemon process).

    Inspired by upstream PRs #1022 (D3 KG viz), #393 (Mermaid diagrams),
    #431 (CLI stats), #256 (sync_status MCP), #601 (brief overview) — none
    cherry-picked, just patterns synthesized over the daemon's /graph.
    """
    # Accept the API key from either the X-Api-Key header (preferred) or
    # the ?key= query parameter (bookmarkable). _check_auth is a no-op
    # when PALACE_API_KEY is unset, so this preserves the
    # zero-config-local-dev experience.
    _check_auth(x_api_key or key)
    global _VIZ_HTML_CACHE
    if _VIZ_HTML_CACHE is None:
        try:
            with open(_VIZ_HTML_PATH, encoding="utf-8") as f:
                _VIZ_HTML_CACHE = f.read()
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"viz template missing: {e}")
    return HTMLResponse(content=_VIZ_HTML_CACHE)


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
    Uses sqlite3 .backup to ensure consistency even under load.
    """
    _check_auth(x_api_key)
    palace_path = _mp._config.palace_path
    db_path = os.path.join(palace_path, "chroma.sqlite3")
    
    backup_dir = os.path.join(os.path.dirname(palace_path), "palace.backup")
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"chroma.sqlite3.{timestamp}.bak")

    # Hold the write semaphore so no daemon-driven writes race the backup start.
    async with _write_sem:
        try:
            src = sqlite3.connect(db_path)
            dst = sqlite3.connect(backup_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

            check = sqlite3.connect(backup_path)
            try:
                cursor = check.cursor()
                cursor.execute("PRAGMA integrity_check;")
                status = cursor.fetchone()[0]
            finally:
                check.close()

            if status != "ok":
                if os.path.exists(backup_path):
                    os.remove(backup_path)
                raise Exception(f"Integrity check failed: {status}")

            return {
                "status": "success",
                "backup_file": backup_path,
                "integrity": status,
                "timestamp": timestamp
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")


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

    _VALID_MODES = {"convos", "projects"}
    _VALID_EXTRACTS = {"exchange", "general"}
    if mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"'mode' must be one of: {', '.join(_VALID_MODES)}")
    if extract is not None and extract not in _VALID_EXTRACTS:
        raise HTTPException(status_code=400, detail=f"'extract' must be one of: {', '.join(_VALID_EXTRACTS)}")
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

    async with _mine_sem:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

    return {
        "returncode": proc.returncode,
        "stdout": stdout.decode(),
        "stderr": stderr.decode(),
    }


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

    uvicorn.run("main:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
