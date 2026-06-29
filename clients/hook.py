#!/usr/bin/env python3
"""
palace-daemon hook runner — stdlib-only replacement for `mempalace hook run`.

Routes all mine operations through palace-daemon (POST /mine).
Never spawns mempalace as a subprocess or accesses the database directly.
If the daemon is unreachable, passes through silently — no fallback to direct access.

Mine operations require explicit user approval via a block response before firing.
MEMPAL_DIR env var controls what directory to mine; if unset, no mine is triggered.

Usage:
    python3 hook.py --hook stop        --harness claude-code
    python3 hook.py --hook precompact  --harness claude-code
    python3 hook.py --hook session-start --harness codex

Settings: ~/.mempalace/hook_settings.json
    daemon_url        (default: http://localhost:8085)
    silent_save       (default: true)  — pass through after diary save; false = block for manual save
    desktop_toast     (default: false) — fire notify-send on save triggers
    force_on_stop     (default: true)  — save at session end even with few exchanges (≥FORCE_MIN_INTERVAL s between saves)
    force_min_interval (default: 60)   — minimum seconds between force_on_stop saves
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SAVE_INTERVAL = 15           # count-based: save every N exchanges
TIME_SAVE_INTERVAL = 300     # time-based: save if this many seconds elapsed with unsaved exchanges
FORCE_MIN_INTERVAL = 60      # force_on_stop: minimum seconds between saves (prevents per-response spam)
CHECKPOINT_TOPIC = "checkpoint"  # keep in sync with main.py and mempal-fast.py — used by kind= search filter
STATE_DIR = Path.home() / ".mempalace" / "hook_state"
HOOK_SETTINGS_PATH = Path.home() / ".mempalace" / "hook_settings.json"
# auto_wake config is read from the SAME file the mempalace CLI reads, so a
# single `auto_wake` entry arms both the interactive CLI path and this hook
# path. hook.py re-implements the reader in stdlib (no mempalace import).
MEMPALACE_CONFIG_PATH = Path.home() / ".mempalace" / "config.json"
# Where failed transcript ingests are journaled for replay on next session
# start. Kept under hook_state (not the CLI's ~/.mempalace/pending) so the
# two replay paths never contend for the same files.
PENDING_DIR = STATE_DIR / "pending"
# Per-host wake lock: when a sleeping host wakes a burst of stop/precompact
# hooks at once, only the first fires the (possibly slow, possibly costly)
# wake command; the rest see the lock and only wait briefly on /health. The
# lock is an fcntl.flock held for the entire wake+poll (see
# _try_claim_wake_lock) — the kernel releases it when the holder's fd closes
# or the process dies, so there is no TTL to tune and no stale-reclaim race.
WAKE_LOCK_PATH = STATE_DIR / ".wake_inflight"
WAKE_COMMAND_TIMEOUT = 15    # seconds the wake command itself may take
# When a hook skips the wake command because another hook holds the lock,
# it still waits this long on /health in case that other wake succeeds.
WAKE_FOLLOWER_WAIT = 20      # seconds a lock-follower polls /health

# Canonical topic name for Stop-hook auto-save checkpoint diary entries.
# Defined as a constant so all hook code paths agree on the string value
# used downstream by mempalace.searcher.build_where_filter for kind=
# filtering (jphein/mempalace fork-ahead row 21, 2026-04-25). Keep in
# sync with mempal-fast.py and mempalace.hooks_cli.
CHECKPOINT_TOPIC = "checkpoint"

SUPPORTED_HARNESSES = {"claude-code", "codex", "gemini-cli"}

STOP_BLOCK_REASON = (
    "AUTO-SAVE checkpoint (MemPalace). Save this session's key content:\n"
    "1. mempalace_diary_write — AAAK-compressed session summary\n"
    "2. mempalace_add_drawer — verbatim quotes, decisions, code snippets\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "Do NOT write to Claude Code's native auto-memory (.md files). "
    "Continue conversation after saving."
)

PRECOMPACT_BLOCK_REASON = (
    "COMPACTION IMMINENT (MemPalace). Save ALL session content before context is lost:\n"
    "1. mempalace_diary_write — thorough AAAK-compressed session summary\n"
    "2. mempalace_add_drawer — ALL verbatim quotes, decisions, code, context\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "Be thorough — after compaction, detailed context will be lost. "
    "Do NOT write to Claude Code's native auto-memory (.md files). "
    "Save everything to MemPalace, then allow compaction to proceed."
)


def _mine_approval_reason(mine_dir: str, daemon_url: str) -> str:
    return (
        f"AUTO-INGEST requested (MemPalace).\n"
        f"Target directory: {mine_dir}\n\n"
        f"Show the user this directory and ask them to approve or deny mining it into the palace.\n"
        f"  Approve → POST {{\"dir\": \"{mine_dir}\", \"mode\": \"convos\"}} to {daemon_url}/mine\n"
        f"  Deny    → inform user, continue."
    )


def _sanitize_session_id(session_id: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    return sanitized or "unknown"


def _validate_transcript_path(transcript_path: str) -> Path:
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser().resolve()
    if path.suffix not in (".jsonl", ".json"):
        return None
    if ".." in Path(transcript_path).parts:
        return None
    return path


_state_dir_initialized = False
_LOG_MAX_BYTES = 10 * 1024 * 1024  # rotate at 10 MB
_LOG_KEEP = 3                       # hook.log.1 .. hook.log.3, then drop


def _rotate_log_if_needed(log_path: Path):
    """Size-gated rotation, run before each append.

    When ``log_path`` exceeds _LOG_MAX_BYTES, shift .1→.2, .2→.3, drop the
    oldest, rename current → .1, and let the next write start fresh.
    Cheap to call every time: usually just a single ``stat`` syscall.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size < _LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        oldest = log_path.with_name(f"{log_path.name}.{_LOG_KEEP}")
        if oldest.exists():
            try:
                oldest.unlink()
            except OSError:
                pass
        for i in range(_LOG_KEEP - 1, 0, -1):
            src = log_path.with_name(f"{log_path.name}.{i}")
            dst = log_path.with_name(f"{log_path.name}.{i + 1}")
            if src.exists():
                try:
                    src.rename(dst)
                except OSError:
                    pass
        try:
            log_path.rename(log_path.with_name(f"{log_path.name}.1"))
        except OSError:
            pass
    except OSError:
        pass


def _log(message: str):
    global _state_dir_initialized
    try:
        if not _state_dir_initialized:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                STATE_DIR.chmod(0o700)
            except (OSError, NotImplementedError):
                pass
            _state_dir_initialized = True
        log_path = STATE_DIR / "hook.log"
        _rotate_log_if_needed(log_path)
        is_new = not log_path.exists()
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
        if is_new:
            try:
                log_path.chmod(0o600)
            except (OSError, NotImplementedError):
                pass
    except OSError:
        pass


def _output(data: dict):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _detach_for_async_work() -> bool:
    """Fork + setsid + redirect FDs to /dev/null. Returns ``True`` if
    we are the (detached) child and should continue with the slow
    work; ``False`` if we are the parent and should ``return``.

    Used by ``hook_stop`` / ``hook_precompact`` *after* they've emitted
    their user-visible ``systemMessage`` via ``_output`` — claude waits
    for the hook's stdout/stderr pipes to close before clearing the
    event, so the parent's ``return`` lets the harness unblock while
    the child finishes the slow daemon round-trip. Emitting the themed
    message *before* the fork is the whole point of this helper: the
    detached child's stdout is ``/dev/null``, so any ``_output`` call
    after detach is invisible.

    Failures are conservative — if ``os.fork()`` raises, we return
    ``True`` so the caller runs inline (synchronous fallback). Set
    ``PALACE_HOOK_NO_DETACH=1`` to skip the detach entirely (testing).
    """
    if os.environ.get("PALACE_HOOK_NO_DETACH") == "1":
        return True
    try:
        pid = os.fork()
    except OSError:
        return True  # fork failed; run inline as a fallback
    if pid > 0:
        return False  # parent: caller should immediately return
    # We are the child. Detach so claude's harness pipe-close logic
    # doesn't block on us.
    try:
        os.setsid()
        devnull_in = os.open(os.devnull, os.O_RDONLY)
        devnull_out = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_in, 0)
        os.dup2(devnull_out, 1)
        os.dup2(devnull_out, 2)
        os.close(devnull_in)
        os.close(devnull_out)
    except OSError:
        pass
    return True


def _read_last_save_ts(session_id: str) -> float:
    ts_file = STATE_DIR / f"{session_id}_last_save_ts"
    try:
        return float(ts_file.read_text().strip())
    except Exception:
        return 0.0


def _write_last_save_ts(session_id: str):
    ts_file = STATE_DIR / f"{session_id}_last_save_ts"
    try:
        ts_file.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _load_hook_settings() -> dict:
    try:
        return json.loads(HOOK_SETTINGS_PATH.read_text())
    except Exception:
        return {}


def _load_auto_wake() -> dict:
    """Read the opt-in wake-on-demand config from ~/.mempalace/config.json.

    Mirrors ``mempalace.config.MempalaceConfig.auto_wake`` so a single
    ``auto_wake`` entry arms both the CLI and this hook path — but it is
    re-implemented in stdlib because hook.py must not import mempalace.

    Accepts the same two shapes the CLI accepts:

        {"auto_wake": "wakeonlan aa:bb:cc:dd:ee:ff"}
        {"auto_wake": {"command": "...", "timeout_seconds": 45,
                       "poll_interval_seconds": 2}}

    Returns a normalized dict (``command``, ``timeout_seconds``,
    ``poll_interval_seconds``) or ``None`` when disabled. ``PALACE_AUTO_WAKE``
    set to ``0``/``false``/``no`` force-disables. A missing/empty/garbage
    command disables (fail-open to "off": a typo must never run an
    unexpected shell command). Timeouts are clamped to sane bounds.
    """
    env_val = os.environ.get("PALACE_AUTO_WAKE")
    if env_val is not None and env_val.strip().lower() in ("0", "false", "no"):
        return None
    try:
        text = MEMPALACE_CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        # No config file is the normal "auto_wake not set up" state — stay
        # silent so we don't spam the log on every hook fire.
        return None
    except OSError as e:
        _log(f"auto_wake: could not read {MEMPALACE_CONFIG_PATH} ({e}) — auto_wake disabled")
        return None
    try:
        raw = json.loads(text).get("auto_wake")
    except (json.JSONDecodeError, AttributeError) as e:
        # The config file EXISTS but is malformed — surface it. A silent
        # disable here once cost hours of "why isn't auto_wake working?".
        _log(f"auto_wake: malformed {MEMPALACE_CONFIG_PATH} ({e}) — auto_wake disabled")
        return None
    if isinstance(raw, str):
        raw = {"command": raw}
    if not isinstance(raw, dict):
        return None
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        return None

    def _bounded(key, default, lo, hi):
        try:
            val = float(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(val, lo), hi)

    return {
        "command": command.strip(),
        "timeout_seconds": _bounded("timeout_seconds", 45.0, 5.0, 300.0),
        "poll_interval_seconds": _bounded("poll_interval_seconds", 2.0, 0.5, 30.0),
    }


def _is_wake_eligible_error(error: str) -> bool:
    """True when a daemon-call failure string is connection-level.

    ``_post_mcp`` / ``_post_mine`` already classify failures into
    ``"network/transport: ..."`` (a connection-level failure a host wake
    could fix) vs ``"HTTP <code> ..."`` (the daemon answered — waking can't
    help). We key off that prefix instead of re-catching exceptions so the
    classification stays in one place. Unknown/empty strings are treated as
    NOT eligible: only a clearly connection-level failure should trigger a
    wake command.
    """
    if not error:
        return False
    low = error.lower()
    if low.startswith("http"):
        return False
    return (
        "network/transport" in low
        or "no route to host" in low
        or "connection refused" in low
        or "timed out" in low
        or "timeout" in low
        or "name or service not known" in low
        or "temporary failure in name resolution" in low
    )


def _daemon_healthy(daemon_url: str, timeout: float = 3.0) -> bool:
    """True when the daemon answers ``/health`` with 200. Never raises."""
    try:
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/health",
            headers=_request_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        # Any failure means "not healthy yet" — including ValueError from a
        # malformed URL or an HTTPException mid-resume. The poll must never
        # crash the hook.
        return False


def _run_wake_command(command: str) -> bool:
    """Run the wake command through the shell. Returns True on exit 0.

    The command comes from the user's own config file (same trust level as
    their shell startup), never from palace content. It is deliberately NOT
    echoed anywhere — it may embed credentials (IPMI passwords etc).
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=WAKE_COMMAND_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _try_claim_wake_lock():
    """Try to claim the per-host wake lock via ``fcntl.flock``.

    Returns the open file handle when we are the leader — the caller MUST
    keep it open for the whole wake+poll and close it in a ``finally`` to
    release. Returns ``None`` when another live hook already holds the lock
    (we are a follower: skip the wake command, just poll /health briefly).

    Mirrors :func:`_try_claim_mine_slot`. ``LOCK_EX | LOCK_NB`` is fully
    atomic — there is no check-then-create window, so no O_EXCL race — and
    the kernel drops the lock the instant the holder's fd closes or the
    process dies, so there is no TTL to tune and no stale-reclaim path
    (this is what closed the 120s-TTL-vs-300s-timeout split-brain: the lock
    can never expire while the leader is still polling).

    Best-effort: if the file can't even be opened we fail open by returning
    a handle anyway, so a wake is still attempted rather than silently
    skipped.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        fh = open(WAKE_LOCK_PATH, "w")
    except OSError:
        # Can't open the lock file at all — fail open. A sentinel object
        # that close()s cleanly lets the caller treat us as the leader.
        return _NULL_LOCK
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.close()
        return None
    try:
        fh.write(f"pid={os.getpid()} ts={time.time()}\n")
        fh.flush()
    except OSError:
        pass
    return fh


class _NullLock:
    """No-op stand-in for a lock handle when the lock file can't be opened.

    Lets ``_try_claim_wake_lock`` fail open (treat us as leader) without the
    caller special-casing ``None`` vs a real handle: ``close()`` is a no-op.
    """

    def close(self):
        return None


_NULL_LOCK = _NullLock()


def _attempt_wake(daemon_url: str, settings: dict) -> bool:
    """Wake the palace host (if we hold the lock) and poll /health.

    Returns True once the daemon answers /health. The wake command is run
    only by the hook that wins the per-host ``fcntl.flock``; a burst of
    sleeping-host hooks therefore fires exactly one wake command. The lock
    is held for the entire wake+poll and released in the ``finally`` (or by
    the kernel if the process dies). Followers (couldn't get the flock) skip
    the command but still poll /health for a bounded window, so they can
    also retry once the leader's wake lands.

    Never echoes the command (it may carry secrets). At most one full
    attempt per process.
    """
    timeout_s = float(settings.get("timeout_seconds", 45.0))
    poll_s = float(settings.get("poll_interval_seconds", 2.0))
    command = settings.get("command", "")

    lock = _try_claim_wake_lock()
    is_leader = lock is not None
    try:
        if is_leader:
            _log(f"auto_wake: waking palace host (up to {timeout_s:.0f}s)")
            if not _run_wake_command(command):
                _log("auto_wake: wake command failed")
                return False
            deadline = time.monotonic() + timeout_s
        else:
            # Another hook holds the flock and is firing the wake command.
            # Wait a bounded window on /health rather than fire a redundant
            # command.
            _log("auto_wake: wake already in flight (lock held) — waiting on /health")
            deadline = time.monotonic() + min(timeout_s, WAKE_FOLLOWER_WAIT)

        started = time.monotonic()
        while time.monotonic() < deadline:
            if _daemon_healthy(daemon_url):
                _log(f"auto_wake: daemon healthy after {time.monotonic() - started:.0f}s — retrying")
                return True
            time.sleep(poll_s)
        _log(f"auto_wake: daemon still unreachable after {time.monotonic() - started:.0f}s")
        return False
    finally:
        # Closing the leader's handle drops the flock immediately. Followers
        # have lock=None, so there is nothing to close.
        if is_leader:
            try:
                lock.close()
            except OSError:
                pass


def _journal_failed_ingest(transcript_path: str, wing: str, session_id: str) -> None:
    """Append a failed transcript ingest to today's replay journal.

    The payload is tiny — the transcript file itself survives the outage on
    disk and is the durable source; we only record where to find it. Drained
    on the next session start (see ``_drain_pending_journal``). Best-effort:
    any failure is swallowed (the optimistic systemMessage already shipped).
    """
    try:
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        line_obj = {
            "transcript_path": str(transcript_path),
            "wing": wing or "",
            "session_id": session_id or "",
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        day = datetime.now().strftime("%Y-%m-%d")
        path = PENDING_DIR / f"{day}.jsonl"
        line = json.dumps(line_obj, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        _log(f"Journaled failed ingest for replay: {Path(str(transcript_path)).name} wing={wing}")
    except OSError as e:
        _log(f"Journal write failed (non-fatal): {e}")


def _drain_pending_journal(daemon_url: str, max_entries: int = 50) -> None:
    """Replay journaled ingests when the daemon is reachable.

    Called best-effort from ``hook_session_start``. Quick /health gate first
    so an asleep host doesn't slow session start. Dedups by transcript_path
    (keeping the newest entry), caps work at ``max_entries``, replays each via
    ``_ingest_transcript_via_daemon``, and rewrites each journal file
    atomically (tempfile + os.replace) keeping only the entries that still
    failed. Never blocks or crashes session start.

    Entries whose transcript file is gone or no longer valid (deleted,
    truncated, renamed) are DISCARDED rather than re-queued: they are
    unrecoverable, and re-appending them would clog the journal forever and
    burn a replay attempt on every session start. Only genuinely-retryable
    failures (daemon reachable but the mine didn't take) stay in ``remaining``.
    """
    try:
        if not PENDING_DIR.is_dir():
            return
        files = sorted(PENDING_DIR.glob("*.jsonl"))
        if not files:
            return
        if not _daemon_healthy(daemon_url, timeout=2.0):
            _log("Journal drain skipped (daemon not reachable)")
            return
    except OSError:
        return

    processed = 0
    for path in files:
        if processed >= max_entries:
            break
        try:
            raw_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            continue

        # Dedup by transcript_path, keeping the newest ts. During a long
        # outage the same transcript is journaled once per Stop fire.
        by_path: dict = {}
        order: list = []
        for ln in raw_lines:
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            key = obj.get("transcript_path", "")
            prev = by_path.get(key)
            if prev is None:
                order.append(key)
            if prev is None or obj.get("ts", "") >= prev.get("ts", ""):
                by_path[key] = obj

        remaining: list = []
        for key in order:
            obj = by_path[key]
            if processed >= max_entries:
                remaining.append(obj)
                continue
            processed += 1
            tp = obj.get("transcript_path", "")
            wing = obj.get("wing", "")

            # Drop entries whose transcript can no longer be mined. The file
            # on disk is the durable source; if it's gone/invalid the entry is
            # unrecoverable, so discarding it (not re-queueing) keeps the
            # journal from clogging and retrying a dead file every session.
            vpath = _validate_transcript_path(tp)
            if vpath is None or not vpath.is_file():
                _log(f"Journal entry discarded (transcript gone/invalid): {tp or '?'}")
                continue
            try:
                if vpath.stat().st_size < 100:
                    _log(f"Journal entry discarded (transcript too small): {vpath.name}")
                    continue
            except OSError:
                _log(f"Journal entry discarded (transcript unstat-able): {tp or '?'}")
                continue

            ok = False
            try:
                ok = _ingest_transcript_via_daemon(daemon_url, tp, wing)
            except Exception as e:
                _log(f"Journal replay raised (kept for retry): {e}")
                ok = False
            if ok:
                _log(f"Journal replay OK: {vpath.name} wing={wing}")
            else:
                remaining.append(obj)

        _rewrite_journal_file(path, remaining)


def _rewrite_journal_file(path: Path, remaining: list) -> None:
    """Atomically replace a journal file with only the still-failed entries.

    Empty ``remaining`` removes the file. Uses tempfile + os.replace so a
    crash mid-drain can't truncate the journal. Best-effort.

    The temp file is unlinked in a ``finally`` keyed on whether the
    ``os.replace`` consumed it — so a non-OSError raised after ``mkstemp``
    (e.g. a ``TypeError`` from a non-serializable journal object) can't leak
    the temp file into the pending dir.
    """
    try:
        if not remaining:
            try:
                path.unlink()
            except OSError:
                pass
            return
        fd, tmp = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
        replaced = False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for obj in remaining:
                    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, str(path))
            replaced = True
        finally:
            # os.replace consumes tmp on success; on ANY failure (OSError or
            # otherwise) tmp still exists and must be cleaned up.
            if not replaced and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    except OSError:
        pass


def _count_human_messages(transcript_path: str) -> int:
    """Count real user turns in a transcript, excluding tool-result roundtrips.

    Claude Code's messages API frames tool results as ``role: "user"``
    messages whose content is a list of ``{type: "tool_result", ...}``
    blocks — conceptually "the user delivering the tool's output back to
    the model." These aren't human exchanges; counting them inflates
    every save interval by 5–10×.

    Mirrors upstream issue MemPalace/mempalace#549 — same bug in upstream's
    ``hooks_cli._count_human_messages``. Fixed locally first per JP's
    review-before-upstream policy.

    Rules:
      - ``role == "user"`` with string content → count (unless ``<command-message>``)
      - ``role == "user"`` with list content → count only if it has at
        least one ``type: "text"`` block (and that block isn't ``<command-message>``)
      - Codex ``event_msg`` user_message branch unchanged (uses an
        explicit user_message type, so it never has the tool-result
        ambiguity)
    """
    path = _validate_transcript_path(transcript_path)
    if path is None:
        return 0
    if not path.is_file():
        return 0
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            if not content.strip():
                                continue
                            if "<command-message>" in content:
                                continue
                            count += 1
                        elif isinstance(content, list):
                            # A real human turn has at least one text block.
                            # All-tool_result content is a tool roundtrip,
                            # not a human exchange — skip.
                            text_blocks = [
                                b for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            ]
                            if not text_blocks:
                                continue
                            joined = " ".join(b.get("text", "") for b in text_blocks)
                            if not joined.strip():
                                continue
                            if "<command-message>" in joined:
                                continue
                            count += 1
                    elif entry.get("type") == "event_msg":
                        payload = entry.get("payload", {})
                        if isinstance(payload, dict) and payload.get("type") == "user_message":
                            msg_text = payload.get("message", "")
                            if isinstance(msg_text, str) and "<command-message>" not in msg_text:
                                count += 1
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        return 0
    return count


def _get_mine_dir() -> str:
    """Return mine directory from MEMPAL_DIR only. No transcript path fallback."""
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir and os.path.isdir(mempal_dir):
        return mempal_dir
    return ""


def _slugify_project(name: str) -> str:
    """Lower snake_case slug. Dashes, dots, spaces collapse to underscores.
    Anything else non-alnum is dropped. Per the room taxonomy spec
    (familiar.realm.watch docs/superpowers/specs/2026-05-13-palace-room-taxonomy.md)
    wing slugs are project-derived and use lower snake_case.
    """
    s = name.strip().lower()
    s = re.sub(r"[-.\s]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s.strip("_")


def _decode_project_id(project_id: str) -> str:
    """Best-effort decode of Claude Code's ~/.claude/projects/<id> directory name.

    Claude Code encodes the project directory by replacing `/` and `.` with
    `-`, which is destructive — we can't perfectly reconstruct the path.
    But the *last* segment is what we want as the wing slug, and the
    encoding is consistent: take everything after the last `Projects-`
    marker. That sidesteps the path-vs-dotted-name ambiguity for anything
    living under ~/Projects/.

    Examples:
      -home-jp-Projects-familiar-realm-watch → familiar-realm-watch
      -home-jp-Projects-palace-daemon         → palace-daemon
      -home-jp-Projects-memorypalace          → memorypalace
    Fallback: return the raw segment after the leading dash.
    """
    if not project_id:
        return ""
    s = project_id.lstrip("-")
    marker = "Projects-"
    idx = s.find(marker)
    if idx >= 0:
        return s[idx + len(marker):]
    return s


def _project_wing(data: dict, transcript_path: str) -> str:
    """Resolve the wing slug for this session's writes.

    Per the room taxonomy (Project-Topic-Drawer model): wing = project,
    not agent. The hook discovers the project by walking these sources
    in order:

      1. data["cwd"] — Claude Code 2.1+ passes the session cwd on stdin.
      2. transcript_path filename — Claude Code encodes the project root
         in the parent directory name under ~/.claude/projects/.
      3. os.getcwd() — last resort, may be wherever the hook was spawned.

    Returns the bare project slug (e.g. ``familiar_realm_watch``), NO
    ``wing_`` prefix. The prefix is a docstring convention in mempalace
    (mcp_server.py:727) that is *not* enforced by code, and adding it
    here was perpetuating a mixed namespace — JP's palace already has
    both ``wing_X`` and ``X`` flavors of the same projects. The spec
    explicitly rejects the prefix:
    ``familiar.realm.watch/docs/superpowers/specs/2026-05-13-palace-room-taxonomy.md``.

    Fallback: ``personal`` if no project can be detected.
    """
    cwd = (data or {}).get("cwd", "")
    if cwd:
        try:
            p = Path(cwd).expanduser().resolve()
            home_projects = (Path.home() / "Projects").resolve()
            if home_projects == p or home_projects in p.parents:
                rel = p.relative_to(home_projects)
                if rel.parts:
                    return _slugify_project(rel.parts[0])
            # Outside ~/Projects/ — use last segment
            slug = _slugify_project(p.name)
            if slug:
                return slug
        except (OSError, ValueError):
            pass

    # Fallback: decode the transcript_path parent directory
    if transcript_path:
        try:
            parent = Path(transcript_path).parent.name  # e.g. -home-jp-Projects-X
            slug = _slugify_project(_decode_project_id(parent))
            if slug:
                return slug
        except (OSError, ValueError):
            pass

    # Last resort: cwd from the process. If it's $HOME itself, call it
    # personal — the basename of $HOME is the username, not a project.
    try:
        here = Path(os.getcwd()).resolve()
        home = Path.home().resolve()
        if here == home:
            return "personal"
        slug = _slugify_project(here.name)
        if slug:
            return slug
    except OSError:
        pass

    return "personal"


def _request_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _post_mcp(daemon_url: str, tool_name: str, params: dict):
    """POST a JSON-RPC tool call to /mcp.

    Returns ``(ok, response_dict_or_failure_reason)`` so callers can render
    themed feedback based on what actually happened — not just yes/no.

    - On success: ``(True, <parsed JSON-RPC response>)``
    - On HTTP error: ``(False, {"error": "HTTP <code>: <reason>"})``
    - On network/transport error: ``(False, {"error": "<exception>"})``
    """
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool_name, "arguments": params},
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/mcp",
            data=data,
            headers=_request_headers(),
            method="POST",
        )
        # 30s timeout (was 10s before 2026-05-13): the daemon serializes
        # writes via PALACE_MAX_WRITE_CONCURRENCY=1 to avoid SIGSEGV from
        # concurrent chromadb writers. Bursts of 3+ Stop hooks within ~10s
        # would stair-step into queue waits and time out spuriously at 10s.
        # 30s lets a queue of ~5-6 saves complete before we surface a
        # "timed out" message. Real daemon hangs still surface; the wait
        # is only displaced, not hidden.
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return False, {"error": f"HTTP {resp.status}"}
            try:
                body = json.loads(resp.read().decode("utf-8"))
            except Exception:
                body = None
            return True, body
    except urllib.error.HTTPError as e:
        _log(f"mcp via daemon rejected (HTTP {e.code}): {e.reason}")
        return False, {"error": f"HTTP {e.code} {e.reason}"}
    except Exception as e:
        _log(f"mcp via daemon failed (network/transport): {e}")
        return False, {"error": f"network/transport: {e}"}


def _post_mine(daemon_url: str, mine_dir: str, timeout: int = 60,
               mode: str = "convos", wing: str = ""):
    """POST /mine to daemon. Returns (ok, response_or_failure_reason).

    Mode defaults to ``convos`` (the only sensible default for transcript
    ingest — and matches the daemon's accepted set ``{convos, projects}``;
    the older ``"auto"`` literal was never valid and silently 400'd).
    Wing is forwarded when truthy so transcript drawers land in the right
    project wing rather than the daemon's default ``"general"``.
    """
    body = {"dir": mine_dir, "mode": mode}
    if wing:
        body["wing"] = wing
    payload = json.dumps(body).encode()
    try:
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/mine",
            data=payload,
            headers=_request_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False, {"error": f"HTTP {resp.status}"}
            try:
                body = json.loads(resp.read().decode("utf-8"))
            except Exception:
                body = None
            return True, body
    except urllib.error.HTTPError as e:
        _log(f"mine via daemon rejected (HTTP {e.code} {e.reason}) — check PALACE_API_KEY")
        return False, {"error": f"HTTP {e.code} {e.reason}"}
    except Exception as e:
        _log(f"mine via daemon failed (network/transport): {e}")
        return False, {"error": f"network/transport: {e}"}


def _get_palace_stats(daemon_url: str) -> dict:
    """Quick-query the daemon's /stats endpoint to enrich themed messages.

    Returns the parsed stats dict (with drawer_count etc) or an empty dict
    on any failure — never raises. Used as a *garnish* on save/mine feedback;
    the hook proceeds even if the stats call fails.

    Short timeout (2s) because /stats touches the chromadb collection and
    can be slow under load. Themed feedback is nice-to-have; we'd rather
    skip the drawer count than make the user wait on the hook.
    """
    try:
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/stats",
            headers=_request_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status != 200:
                return {}
            return json.loads(resp.read().decode("utf-8")) or {}
    except Exception:
        return {}


def _format_palace_count(stats: dict) -> str:
    """Render the palace drawer count for inline messaging.

    Tolerates different shapes of /stats since the daemon's response may evolve.
    Returns an empty string when no useful count is available.
    """
    if not stats:
        return ""
    # Try several plausible keys before giving up
    for key in ("drawer_count", "drawers", "total", "count", "elements"):
        if key in stats and isinstance(stats[key], int):
            return f"{stats[key]:,} drawers"
    # /stats might nest counts under a per-collection breakdown
    by_col = stats.get("collections") or {}
    if isinstance(by_col, dict):
        drawers = by_col.get("mempalace_drawers")
        if isinstance(drawers, dict) and isinstance(drawers.get("count"), int):
            return f"{drawers['count']:,} drawers"
    return ""


def _display_wing(wing: str) -> str:
    """Drop the ``wing_`` prefix for human-readable rendering.

    Per the palace room taxonomy spec, wing slugs are project-derived
    (e.g. ``familiar_realm_watch``). mempalace's chromadb era prepends
    ``wing_`` for namespacing; humans read the slug bare.
    """
    return wing[5:] if isinstance(wing, str) and wing.startswith("wing_") else wing or "?"


def _drawer_label(topic: str, timestamp: str) -> str:
    """Build a human-readable slug-style drawer label from topic + timestamp.

    Drawer IDs themselves are opaque (content-hashed for idempotency and
    collision-free generation) — see ``diary_write`` / ``add_drawer`` in
    mempalace. They're storage handles, not navigation handles. The
    themed message wants something a human can recognize at a glance,
    so we synthesize ``<topic>@<HH:MM>`` from the metadata mempalace
    already returns. The full drawer_id stays available via search /
    list_drawers when the hash matters.

    Examples:
      _drawer_label("checkpoint", "2026-05-13T08:48:16.427801") → "checkpoint@08:48"
      _drawer_label("precompact", "")                            → "precompact"
      _drawer_label("", "")                                       → "?"
    """
    topic = (topic or "").strip()
    if isinstance(timestamp, str) and "T" in timestamp:
        # ISO-8601 ``YYYY-MM-DDTHH:MM:SS.fff``; we want ``HH:MM``.
        try:
            hhmm = timestamp.split("T", 1)[1][:5]
            if topic:
                return f"{topic}@{hhmm}"
            return f"@{hhmm}"
        except (IndexError, AttributeError):
            pass
    return topic or "?"


def _extract_inner(response: dict) -> dict:
    """Unwrap a daemon /mcp or /memory response into the inner result dict.

    Handles both the JSON-RPC envelope shape (``result.content[0].text``
    holds a JSON-encoded inner dict, as returned by /mcp) and the
    already-unwrapped dict shape (as returned by /memory and /silent-save).

    Returns ``{}`` on any parse failure — themed rendering should degrade
    cleanly when the response shape isn't what we expected.
    """
    if not isinstance(response, dict):
        return {}
    # Already unwrapped (e.g. /memory, /silent-save direct returns).
    if "result" not in response and ("warnings" in response or "errors" in response or "success" in response or "entry_id" in response):
        return response
    try:
        content = response.get("result", {}).get("content", [])
        if content and isinstance(content[0], dict):
            return json.loads(content[0].get("text", "{}")) or {}
    except Exception:
        pass
    return {}


def _split_outcome(inner: dict) -> tuple[list[str], list[str]]:
    """Pull warnings/errors lists out of a write-path response.

    mempalace#86: drawer-write responses carry ``warnings: list[str]`` and
    ``errors: list[str]``. Older mempalace versions don't emit them — we
    default to empty lists so the themed renderer can treat 'no fields' and
    'fields present but empty' identically.
    """
    if not isinstance(inner, dict):
        return [], []
    warnings = inner.get("warnings")
    errors = inner.get("errors")
    if not isinstance(warnings, list):
        warnings = []
    if not isinstance(errors, list):
        errors = []
    # Coerce to str so a misbehaving server can't crash the renderer.
    return [str(w) for w in warnings], [str(e) for e in errors]


def _format_outcome_notes(items: list[str]) -> str:
    """Indented secondary line for warnings/errors. Empty input → empty string."""
    cleaned = [s.strip() for s in items if str(s).strip()]
    if not cleaned:
        return ""
    return "\n    " + "\n    ".join(cleaned)


def _theme_save_ok(exchange_count: int, trigger: str, response: dict, palace_count: str, wing: str = "") -> str:
    """Build the themed-chain message for a Stop-hook save.

    Renders the full chain a human-readable walk takes through the palace
    to reach the drawer that was just filed:

        ◆ Saved — palace → wing:<project> → room:sessions → drawer:…<short-id>

    Per the room taxonomy spec, the wing is the *project*, not the agent.
    The agent identity lives in drawer metadata.

    mempalace#86: when the response carries warnings (non-canonical room,
    deprecated topic, …) or errors (HNSW rebuild rejected the write, …),
    the leading glyph + verb reflect the actual outcome and an indented
    secondary line surfaces the message text. Older mempalace versions
    don't emit those fields → falls back to the original "memory woven"
    phrasing.

    Topic surfaces as a tag after the chain — present but not part of
    the path. Closets (the index layer) are auto-built by
    ``mempalace mine`` and aren't addressable from a diary write.
    """
    inner = _extract_inner(response)
    warnings, errors = _split_outcome(inner)

    topic = inner.get("topic", "") or ""
    timestamp = inner.get("timestamp", "") or ""
    drawer_label = _drawer_label(topic, timestamp)

    display = _display_wing(wing) if wing else _display_wing(inner.get("agent", ""))
    chain = (
        f"palace → wing:{display} → room:sessions → drawer:{drawer_label}"
        if display and display != "?"
        else ""
    )

    if errors:
        glyph_verb = "✕ Save FAILED"
        head = f"{glyph_verb} — {chain}" if chain else f"{glyph_verb}"
        notes = _format_outcome_notes(errors)
    elif warnings:
        glyph_verb = (
            "⚠ Saved with warning" if len(warnings) == 1 else "⚠ Saved with warnings"
        )
        head = f"{glyph_verb} — {chain}" if chain else f"{glyph_verb}"
        notes = _format_outcome_notes(warnings)
    else:
        # Legacy phrasing for the clean path — preserves the existing
        # voice operators are used to.
        head = f"✦ {chain}" if chain else "✦ Memory woven into the palace"
        notes = ""

    tail_bits = [f"exchange {exchange_count}", f"trigger={trigger}"]
    if palace_count:
        tail_bits.append(f"palace now holds {palace_count}")
    return f"{head}  —  " + ", ".join(tail_bits) + notes


def _theme_save_fail(exchange_count: int, trigger: str, failure: dict) -> str:
    """Build the failure themed message for a Stop-hook save.

    mempalace#86: when the response carries an ``errors`` list (e.g.
    {"errors": ["HNSW rebuilding, write rejected"]}), surface the messages
    on an indented secondary line. Falls back to the historical ``error``
    string for transport-level failures (HTTP 401, network/transport, …).
    """
    failure = failure or {}
    errors = failure.get("errors")
    if not isinstance(errors, list):
        errors = []
    head = (
        f"✕ Memory save failed at exchange {exchange_count} (trigger={trigger})"
    )
    if errors:
        return head + _format_outcome_notes([str(e) for e in errors])
    err = failure.get("error", "unknown error")
    return f"{head} — {err}"


def _theme_mine(mine_dir: str, ok: bool, failure: dict, palace_count: str) -> str:
    """Build a themed message for a Pre-compact mine event.

    Mining produces drawers across whatever wings the miner decides —
    we don't get to claim a target wing here. The message names the
    *source* directory (what we mined) rather than inventing a wing.
    """
    source = os.path.basename(mine_dir.rstrip("/")) or mine_dir
    if ok:
        msg = f"◈ Mined into the palace — source: {source}"
        if palace_count:
            msg += f", {palace_count}"
        return msg
    err = (failure or {}).get("error", "unknown error")
    return f"✘ Pre-compact mine failed — source: {source} — {err}"


def _theme_session_start(wing: str, response: dict) -> str:
    """One-line palace greeting at session start.

    Surfaces what mempalace already knows about the project this session
    is operating on. Calls ``tool_list_drawers(wing, room=sessions, limit=1)``
    to get a wing-scoped count regardless of which agent wrote each entry.
    (Was ``room=diary`` historically; renamed to match the canonical 7-room
    set per the Phase 1D FK migration on 2026-05-14.)

    Examples:
      ✦ palace ready — wing:familiar_realm_watch holds 47 diary entries
      ✦ palace ready — wing:new_project is a fresh wing
    """
    inner = {}
    try:
        content = response.get("result", {}).get("content", []) if isinstance(response, dict) else []
        if content and isinstance(content[0], dict):
            inner = json.loads(content[0].get("text", "{}"))
    except Exception:
        inner = {}

    display = _display_wing(wing)
    total = inner.get("total", 0) or 0

    if total == 0:
        return f"✦ palace ready — wing:{display} is a fresh wing"

    plural = "entry" if total == 1 else "entries"
    return f"✦ palace ready — wing:{display} holds {total:,} diary {plural}"


def _extract_diary_context(response: dict, max_chars: int = 4000) -> str:
    """Extract recent diary entries from a diary_read MCP response.

    Returns a formatted context block for injection into the session
    greeting, or empty string if no entries found. Truncates to
    max_chars (~1500 tokens) to stay within the disruption budget.
    """
    try:
        content = response.get("result", {}).get("content", []) if isinstance(response, dict) else []
        if not content or not isinstance(content[0], dict):
            return ""
        inner = json.loads(content[0].get("text", "{}"))
    except Exception:
        return ""

    entries = inner.get("entries", [])
    if not entries:
        return ""

    lines = ["Recent session context:"]
    total_len = len(lines[0])
    for entry in entries:
        text = entry.get("entry", "") or entry.get("content", "") or ""
        topic = entry.get("topic", "") or ""
        ts = entry.get("timestamp", "") or entry.get("created_at", "") or ""
        if not text:
            continue
        preview = text[:400]
        if len(text) > 400:
            preview += "..."
        header = f"  [{topic}]" if topic else ""
        if ts:
            header += f" ({ts[:10]})"
        line = f"{header}\n  {preview}" if header else f"  {preview}"
        if total_len + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total_len += len(line) + 1

    return "\n".join(lines) if len(lines) > 1 else ""


def _theme_precompact_save(wing: str, response: dict, palace_count: str) -> str:
    """Themed message for the pre-compact diary save (context boundary marker).

    Distinct from _theme_save_ok so the operator sees this is a boundary
    save (not a periodic checkpoint). Same chain shape, different sigil.

    mempalace#86: warnings / errors from the underlying write surface on an
    indented second line, identical to _theme_save_ok's treatment.
    """
    inner = _extract_inner(response)
    warnings, errors = _split_outcome(inner)
    topic = inner.get("topic", "precompact") or "precompact"
    timestamp = inner.get("timestamp", "") or ""
    drawer_label = _drawer_label(topic, timestamp)
    display = _display_wing(wing)
    chain = f"palace → wing:{display} → room:sessions → drawer:{drawer_label}"

    if errors:
        head = f"✕ Pre-compact save FAILED — {chain}"
        notes = _format_outcome_notes(errors)
    elif warnings:
        head = f"⚠ Pre-compact boundary save (with warning) — {chain}"
        notes = _format_outcome_notes(warnings)
    else:
        head = f"◆ Pre-compact boundary save — {chain}"
        notes = ""

    if palace_count:
        head += f", palace now holds {palace_count}"
    return head + notes


def _desktop_notify(title: str, body: str) -> None:
    try:
        subprocess.Popen(
            ["notify-send", "--expire-time=4000", "--icon=dialog-information", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError):
        pass


def _parse_harness_input(data: dict, harness: str) -> dict:
    if harness not in SUPPORTED_HARNESSES:
        print(f"Unknown harness: {harness}", file=sys.stderr)
        sys.exit(1)
    return {
        "session_id": _sanitize_session_id(str(data.get("session_id", "unknown"))),
        "stop_hook_active": data.get("stop_hook_active", False),
        "transcript_path": str(data.get("transcript_path", "")),
    }


def _prune_state_files(max_age_days: int = 7):
    cutoff = time.time() - max_age_days * 86400
    try:
        for f in STATE_DIR.iterdir():
            if f.name in ("hook.log",):
                continue
            if f.suffix in ("", ) and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def _mine_target_key(directory: str, mode: str, wing: str) -> str:
    """Stable hash for a (dir, mode, wing) mine target.

    Used as the filename of the per-target lock file so different targets
    get independent slots while the same (dir, mode, wing) triple collapses
    to a single slot — exactly the dedup semantics upstream's
    ``hooks_cli._pid_file_for_cmd`` provides.
    """
    raw = f"{directory}|{mode}|{wing}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _try_claim_mine_slot(directory: str, mode: str, wing: str):
    """Try to acquire an exclusive lock on the (dir, mode, wing) target.

    Returns the open file handle on success — the caller must keep it
    alive (i.e. unclosed) for the duration of the ``/mine`` POST so the
    lock stays held. Returns ``None`` if another hook process is already
    mining the same target.

    Uses ``fcntl.flock`` with ``LOCK_EX | LOCK_NB`` — fully atomic, no
    races between check and claim. Auto-releases when the holding process
    exits (even on crash), so stale locks from killed hooks never block.

    The dedup window is bounded by how long the holding hook holds /mine
    open: typically the request timeout (30s by default). After that the
    hook process exits, the lock releases, and the next save can fire
    another /mine. The daemon-side mine may still be running past that
    point — mempalace's miner dedupes by file via ``prefetch_mined_set``,
    so a duplicate fire just produces a "scan and skip" pass rather than
    a redundant mining of the same source files.
    """
    slot_dir = STATE_DIR / "mine_slots"
    try:
        slot_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    key = _mine_target_key(directory, mode, wing)
    slot_path = slot_dir / f"{key}.lock"
    try:
        fh = open(slot_path, "w")
    except OSError:
        return None
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.close()
        return None
    # Stamp the slot for debugging — never read for logic.
    try:
        fh.write(f"pid={os.getpid()} dir={directory} mode={mode} wing={wing} ts={time.time()}\n")
        fh.flush()
    except OSError:
        pass
    return fh


def _ingest_transcript_via_daemon(daemon_url: str, transcript_path: str, wing: str,
                                  failure_out: dict = None):
    """Restore the transcript-ingest step that mempalace's upstream
    ``hooks_cli._ingest_transcript`` does on every save and precompact.

    Was dropped when palace-daemon's hook became a stdlib-only replacement
    for ``mempalace hook run`` (post-2026-05-11). Without this, Stop/PreCompact
    write only a marker diary entry — the verbatim conversation content
    that used to land via ``mempalace mine --mode convos`` was missing.

    Re-routed through the daemon's ``/mine`` endpoint so we don't take a
    Python import dependency on mempalace itself. Best-effort: any failure
    is logged and the save proceeds — never blocks the hook.

    ``failure_out`` (optional): when a dict is passed, on failure it is
    populated with ``{"error": <str>, "eligible": <bool>}`` so the wake
    wrapper can decide whether a host wake could fix the failure. Default
    ``None`` keeps the original ``bool``-only contract for existing callers
    and the taxonomy test that mocks this function.
    """
    path = _validate_transcript_path(transcript_path)
    if path is None or not path.is_file() or path.stat().st_size < 100:
        return False
    settings = _load_hook_settings()
    if not settings.get("ingest_transcripts", True):
        return False

    mine_dir = str(path.parent)

    # Per-target dedup: if another hook process is currently inside /mine
    # for the same (dir, mode, wing), skip rather than queue a redundant
    # fire. The lock auto-releases when the holding hook exits.
    slot = _try_claim_mine_slot(mine_dir, "convos", wing)
    if slot is None:
        _log(f"Transcript ingest skipped (lock held): {path.name} wing={wing}")
        # Treat lock-held as success: another hook is already doing the
        # work; don't show a failure in themed output.
        return True

    ok = False
    try:
        ok, response = _post_mine(daemon_url, mine_dir,
                                  timeout=settings.get("mine_timeout_s", 60),
                                  mode="convos", wing=wing)
        if ok:
            warning = (response or {}).get("warning", "")
            if warning:
                _log(f"Transcript ingest WARNING: {warning}")
            _log(f"Transcript ingest queued: {path.name} → wing={wing}")
        else:
            err = (response or {}).get("error", "unknown")
            _log(f"Transcript ingest failed: {err}")
            if failure_out is not None:
                failure_out["error"] = err
                failure_out["eligible"] = _is_wake_eligible_error(err)
    except Exception as e:
        _log(f"Transcript ingest exception (non-fatal): {e}")
        if failure_out is not None:
            failure_out["error"] = str(e)
            failure_out["eligible"] = _is_wake_eligible_error(str(e))
    finally:
        try:
            slot.close()
        except OSError:
            pass

    # Session manifest: one addressable drawer per session file with
    # structured metadata (timestamps, exchange count, first/last message).
    # Complements the chunked convos drawers with a navigable anchor that
    # answers "what did session X cover?" queries. Fast (no LLM), idempotent.
    try:
        _post_mine(daemon_url, mine_dir,
                   timeout=settings.get("mine_timeout_s", 60),
                   mode="session", wing=wing)
    except Exception:
        pass  # best-effort; convos mine is the primary

    return ok


def _ingest_with_wake_and_journal(daemon_url: str, transcript_path: str, wing: str,
                                  session_id: str) -> bool:
    """Ingest a transcript, waking a sleeping host and replaying on failure.

    Runs ONLY inside the detached child (after the parent has emitted its
    optimistic systemMessage), so all of this latency is invisible to the
    user. Flow:

      1. Ingest via the daemon.
      2. On a CONNECTION-LEVEL failure (host asleep / no route) AND with
         ``auto_wake`` configured: fire the wake command (once per host
         via the wake lock), wait for /health, then RETRY the ingest once.
         An HTTP/tool-level failure is NOT wake-eligible — the daemon
         answered, so waking can't help; we skip straight to journaling.
      3. If the ingest still failed (wake disabled, wake failed, or a
         non-connection failure), journal the transcript for replay on the
         next session start.

    Returns the final ingest result (True only when the daemon accepted it).
    """
    failure = {}
    ok = _ingest_transcript_via_daemon(daemon_url, transcript_path, wing,
                                       failure_out=failure)
    if ok:
        return True

    if failure.get("eligible"):
        wake_settings = _load_auto_wake()
        if wake_settings:
            if _attempt_wake(daemon_url, wake_settings):
                _log("auto_wake: retrying transcript ingest after wake")
                ok = _ingest_transcript_via_daemon(daemon_url, transcript_path, wing)
                if ok:
                    return True
        else:
            _log("auto_wake: connection-level failure but auto_wake not configured")

    # Still failed after the wake+retry attempt (or it was never eligible /
    # never configured). Journal for replay on next session start.
    _journal_failed_ingest(transcript_path, wing, session_id)
    return False


# ── SessionStart(source="compact") recovery ─────────────────────────
#
# After a compaction the model wakes with only a lossy 9-section summary.
# These helpers pull recent verbatim state from the palace and inject it as
# additionalContext so the model can recover what the summary dropped.
#
# This path is SYNCHRONOUS (no _detach_for_async_work): additionalContext
# only reaches the harness via this process's stdout, and detaching redirects
# stdout to /dev/null. Everything here is therefore bounded to stay well
# inside the SessionStart timeout (5s, from hooks.json) — a fast /health
# gate, then two BM25 /search/fast calls (~280ms each). No hybrid search
# (1100ms, would risk timeout) and no blocking wake+poll cycle.

COMPACT_RECOVERY_OPEN = "[mempalace:compact-recovery]"
COMPACT_RECOVERY_CLOSE = "[/mempalace:compact-recovery]"


def _compact_fallback_context() -> str:
    """Minimal nudge used when the daemon is unreachable or returns nothing."""
    return (
        f"{COMPACT_RECOVERY_OPEN}\n"
        "Context was just compacted. Your verbatim history is in the "
        "palace (410K+ drawers). Use mempalace_search to retrieve what you "
        "need — don't guess from the summary alone.\n"
        f"{COMPACT_RECOVERY_CLOSE}"
    )


def _compact_output(context: str) -> dict:
    """Wrap a context string in the SessionStart additionalContext envelope.

    additionalContext enters the model's context (unlike systemMessage, which
    is user-visible only) — this is the whole point of the compact branch.
    """
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def _search_fast(daemon_url: str, query: str, limit: int = 3,
                 timeout: float = 2.0) -> list:
    """GET /search/fast (BM25, no vector / no AGE locks).

    Returns a list of result dicts (possibly empty), or ``None`` on a
    transport/parse failure. The endpoint returns a bare JSON array of
    ``{id, wing, room, rank, snippet, source_file, tags}`` rows and requires
    auth (carried by ``_request_headers`` → ``X-API-Key``).
    """
    try:
        qs = urllib.parse.urlencode({"q": query, "limit": limit})
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/search/fast?" + qs,
            headers=_request_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8"))
        return body if isinstance(body, list) else None
    except Exception as e:
        _log(f"compact-resume: /search/fast failed for {query!r}: {e}")
        return None


def _kick_wake_nonblocking() -> None:
    """Fire the configured auto_wake command without waiting (best-effort).

    The compact-resume handler must return additionalContext synchronously
    inside the SessionStart timeout, so — unlike the MCP search path — it
    cannot block on a wake+poll cycle. It kicks the wake command and returns
    immediately; the host is then waking by the time the model issues its
    first mempalace_search (which has its own wake+retry). All FDs go to
    /dev/null with start_new_session so the wake child can never write to
    this hook's stdout (which carries the JSON additionalContext) nor hold it
    open. ``shlex.split`` (not ``shell=True``) per the no-shell-injection rule.
    """
    cfg = _load_auto_wake()
    if not cfg:
        return
    command = (cfg.get("command") or "").strip()
    if not command:
        return
    try:
        subprocess.Popen(
            shlex.split(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log("compact-resume: fired non-blocking auto_wake")
    except (OSError, ValueError) as e:
        # ValueError: unbalanced quotes in the command; OSError: binary
        # missing. The wake is best-effort — never crash the hook.
        _log(f"compact-resume: auto_wake kick failed (non-fatal): {e}")


def _format_compact_packet(wing: str, checkpoint_hits: list,
                           session_hits: list) -> str:
    """Render the 500-1000 token recovery packet from /search/fast hits.

    Snippets are clipped to keep the injection within the disruption budget;
    the model uses mempalace_search to pull anything fuller.
    """
    display = _display_wing(wing)
    lines = [
        COMPACT_RECOVERY_OPEN,
        "Context was just compacted. Here is your recent session state "
        "from the palace:",
        "",
        f"Wing: {display}",
    ]

    if checkpoint_hits:
        cp = (checkpoint_hits[0].get("snippet") or "").strip().replace("\n", " ")
        if cp:
            lines.append(f"Last checkpoint: {cp[:220]}")

    if session_hits:
        lines.append("")
        lines.append(f"Recent context (top {len(session_hits)} matches):")
        for i, hit in enumerate(session_hits, 1):
            snippet = (hit.get("snippet") or "").strip().replace("\n", " ")
            room = hit.get("room") or "?"
            hit_wing = hit.get("wing") or display
            lines.append(f"{i}. {snippet[:220]} (wing:{hit_wing}/room:{room})")

    lines.append("")
    lines.append("Use mempalace_search for anything else you need.")
    lines.append(COMPACT_RECOVERY_CLOSE)
    return "\n".join(lines)


def _handle_compact_resume(data: dict, parsed: dict, harness: str) -> None:
    """SessionStart(source="compact") branch: inject recent palace state as
    additionalContext so the model recovers what the lossy summary dropped.

    Synchronous by contract — see the module comment above. Falls back to a
    minimal nudge whenever the daemon is unreachable or returns nothing.
    """
    settings = _load_hook_settings()
    daemon_url = settings.get("daemon_url", "http://localhost:8085")
    session_id = parsed["session_id"]
    wing = _project_wing(data, parsed["transcript_path"])

    # Fast /health gate (1.5s): if the daemon isn't answering quickly, don't
    # spend the SessionStart budget on search timeouts. Kick a non-blocking
    # wake (host is up by the model's first search) and return the nudge.
    if not _daemon_healthy(daemon_url, timeout=1.5):
        _kick_wake_nonblocking()
        _log(f"compact-resume: daemon unreachable (wing={wing}) — minimal nudge")
        _output(_compact_output(_compact_fallback_context()))
        return

    # Daemon is up: two BM25 fast searches, well inside the 5s budget. The
    # wing name / session_id ride in the query TEXT; the endpoint's only
    # structured filter is `wing`, which we deliberately leave unset so a
    # canonicalization mismatch can't zero out the results (spec §3).
    session_hits = _search_fast(daemon_url, f"session state {wing}", limit=3) or []
    checkpoint_hits = _search_fast(daemon_url, f"checkpoint {session_id}", limit=2) or []

    if not session_hits and not checkpoint_hits:
        _log(f"compact-resume: no palace hits (wing={wing}) — minimal nudge")
        _output(_compact_output(_compact_fallback_context()))
        return

    packet = _format_compact_packet(wing, checkpoint_hits, session_hits)
    _log(f"compact-resume: injecting {len(packet)} chars of palace context (wing={wing})")
    _output(_compact_output(packet))


# ── SessionStart search nudge (every non-compact session) ───────────
#
# The model has the mempalace_search tool (search-only MCP mode), but a tool
# in the schema isn't a habit — nothing tells it to reach for the palace
# before guessing. This short additionalContext nudge fires on EVERY normal
# SessionStart so "search your memory first" is in-context from message one.
# (compact sessions get the richer compact-recovery packet instead — see
# _handle_compact_resume.) Kept to ~50 tokens: it is read every session.

SESSION_CONTEXT_OPEN = "[mempalace:session-context]"
SESSION_CONTEXT_CLOSE = "[/mempalace:session-context]"


def _session_nudge(wing: str) -> str:
    """The ~50-token search nudge injected via additionalContext."""
    return (
        f"{SESSION_CONTEXT_OPEN}\n"
        "You have mempalace_search — a memory tool with 410K+ drawers of "
        "verbatim conversation history across 100+ wings. When unsure about "
        "prior decisions, past context, or \"why was this done this way\", "
        "SEARCH FIRST instead of guessing. Short keyword queries work best. "
        f"Wing: {_display_wing(wing)}\n"
        f"{SESSION_CONTEXT_CLOSE}"
    )


def hook_session_start(data: dict, harness: str):
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    transcript_path = parsed["transcript_path"]

    # Claude Code (v2.1.187+) fires SessionStart with a `source` field:
    # "startup" | "resume" | "clear" | "compact". A "compact" restart means
    # the model just lost its working context to a lossy summary, so branch
    # to a synchronous palace-context injection (see _handle_compact_resume).
    # `source` is a top-level payload field, absent on older / non-claude
    # harnesses → default "startup" (the normal greeting path).
    source = data.get("source") or "startup"
    if source == "compact":
        _log(f"SESSION START (compact resume) for session {session_id}")
        _handle_compact_resume(data, parsed, harness)
        return

    _log(f"SESSION START for session {session_id}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _write_last_save_ts(session_id)   # seed so first-stop time_trigger doesn't fire immediately
    _prune_state_files()

    # Greet by surfacing palace state for the project this session is in.
    # Read is non-fatal: if the daemon is unreachable, fail silent rather
    # than block session startup with a transient error.
    settings = _load_hook_settings()
    daemon_url = settings.get("daemon_url", "http://localhost:8085")
    wing = _project_wing(data, transcript_path)

    # Best-effort: replay any transcript ingests that were journaled while
    # the daemon was unreachable (sleeping host, outage). Gated on a quick
    # /health so an asleep host doesn't slow session start; never blocks or
    # crashes startup on drain errors.
    try:
        _drain_pending_journal(daemon_url)
    except Exception as e:
        _log(f"Journal drain skipped (non-fatal): {e}")

    ok, response = _post_mcp(daemon_url, "mempalace_list_drawers", {
        "wing": wing,
        "room": "sessions",
        "limit": 1,
    })
    if not ok:
        # Daemon down — no live palace stats to greet with, but the search
        # nudge is static (needs only the wing), so still inject it. The
        # nudge matters most exactly when the greeting/diary context is
        # missing: the model has no other palace signal this session.
        _log(f"SESSION GREETING skipped (daemon unreachable for wing={wing}) — nudge only")
        _output({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _session_nudge(wing),
            }
        })
        return

    sys_msg = _theme_session_start(wing, response)

    diary_ok, diary_resp = _post_mcp(daemon_url, "mempalace_diary_read", {
        "agent_name": "claude-code",
        "wing": wing,
        "last_n": 2,
    })
    if diary_ok:
        diary_context = _extract_diary_context(diary_resp)
        if diary_context:
            sys_msg += "\n" + diary_context

    _log(f"SESSION GREETING: {sys_msg}")
    # ONE _output object carries both channels: the user-visible greeting
    # (systemMessage) AND the model-facing search nudge (additionalContext).
    # A hook may print only a single JSON object to stdout, so these must
    # not be two separate _output() calls.
    _output({
        "systemMessage": sys_msg,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _session_nudge(wing),
        },
    })


def hook_stop(data: dict, harness: str):
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    stop_hook_active = parsed["stop_hook_active"]
    transcript_path = parsed["transcript_path"]

    if str(stop_hook_active).lower() in ("true", "1", "yes"):
        # Log before bailing: without this line, "harness never invoked the
        # hook" and "harness invoked it with stop_hook_active" are
        # indistinguishable in hook.log (2026-06-10 silent-window incident).
        _log(f"Session {session_id}: stop suppressed (stop_hook_active set by harness)")
        _output({})
        return

    exchange_count = _count_human_messages(transcript_path)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    last_save_file = STATE_DIR / f"{session_id}_last_save"
    last_save = 0
    if last_save_file.is_file():
        try:
            last_save = int(last_save_file.read_text().strip())
        except (ValueError, OSError):
            last_save = 0

    # Self-heal when the counter goes backward. Happens when the rules
    # for "what counts as a human message" tighten — e.g. the 2026-05-13
    # fix that stopped counting tool_result roundtrips dropped this
    # session's count from ~955 to ~87. Without this rebase, since_last
    # is negative, all three save triggers (count/time/force require
    # since_last > 0) wedge, and saves silently stop on running sessions.
    # Rebase the saved checkpoint to the current count: the next real
    # exchange resumes normal trigger behavior; one save cycle skipped.
    if last_save > exchange_count:
        _log(
            f"Session {session_id}: rebased stale last_save "
            f"({last_save} → {exchange_count}) — counter went backward, "
            f"likely after a count-rule change"
        )
        last_save = exchange_count
        try:
            last_save_file.write_text(str(exchange_count), encoding="utf-8")
        except OSError:
            pass

    since_last = exchange_count - last_save
    last_save_ts = _read_last_save_ts(session_id)
    time_since_last = time.time() - last_save_ts

    _log(f"Session {session_id}: {exchange_count} exchanges, {since_last} since last save, {time_since_last:.0f}s elapsed")

    settings = _load_hook_settings()

    # Three independent triggers — any one fires a save:
    #   count   — every SAVE_INTERVAL exchanges (existing behaviour)
    #   time    — every TIME_SAVE_INTERVAL seconds with unsaved exchanges (new)
    #   force   — force_on_stop=true: save at session end even with few exchanges,
    #             subject to FORCE_MIN_INTERVAL to prevent per-response spam
    count_trigger = since_last >= SAVE_INTERVAL and exchange_count > 0
    time_trigger  = time_since_last >= TIME_SAVE_INTERVAL and since_last > 0
    force_trigger = (
        settings.get("force_on_stop", True)
        and since_last > 0
        and time_since_last >= settings.get("force_min_interval", FORCE_MIN_INTERVAL)
    )

    if not (count_trigger or time_trigger or force_trigger):
        _output({})
        return

    trigger = "count" if count_trigger else ("time" if time_trigger else "force")
    _log(f"TRIGGERING SAVE at exchange {exchange_count} (trigger={trigger})")

    try:
        last_save_file.write_text(str(exchange_count), encoding="utf-8")
    except OSError:
        pass
    _write_last_save_ts(session_id)

    daemon_url = settings.get("daemon_url", "http://localhost:8085")
    silent = settings.get("silent_save", True)
    toast = settings.get("desktop_toast", False)

    mine_dir = _get_mine_dir()
    if mine_dir:
        _log(f"Mine approval requested for {mine_dir}")
        if toast:
            _desktop_notify("MemPalace", f"Mine approval needed: {mine_dir}")
        _output({"decision": "block", "reason": _mine_approval_reason(mine_dir, daemon_url)})
        return

    if silent:
        # Diary checkpoints restored (JP, 2026-06-11). The 2026-05-14
        # refactor made the miner the single write path (mine-only); that
        # kept the verbatim drawers but left the diary empty, so the
        # session-start greeting's diary_read context went stale. The hook
        # now writes a checkpoint diary entry AND triggers the transcript
        # mine: the diary entry is the marker the greeting reads; the mine
        # produces the content-rich verbatim drawers.
        wing = _project_wing(data, transcript_path)

        # Emit the themed systemMessage in the parent BEFORE detaching.
        # The detached child has stdout redirected to /dev/null (so
        # claude's harness can close its pipe and clear the event), which
        # means any _output call after detach is invisible to the user.
        # palace_count is the pre-save count — slightly stale but the
        # delta from one save is negligible at 273k drawers, and the
        # _get_palace_stats round-trip is ~50ms on a warm daemon.
        # Save success is optimistic: if the ingest later fails, the
        # failure path lands in hook.log only; the user sees the
        # success-themed line. The original (pre-detach) code emitted
        # only after the ingest returned, so the failure mode showed up
        # in the UI. We accept the slight loss of fidelity to recover
        # the in-session user-visible save confirmation that the detach
        # broke.
        pre_palace_count = _format_palace_count(_get_palace_stats(daemon_url))
        sys_msg = _theme_save_ok(exchange_count, trigger, {}, pre_palace_count, wing)
        _output({"systemMessage": sys_msg})

        if not _detach_for_async_work():
            return

        # We are the (detached) child. Do the slow work. Anything
        # we print from here goes to /dev/null; logging via _log()
        # to ~/.mempalace/hook_state/hook.log is the durable channel.
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"AUTO-SAVE:{session_id}|{exchange_count}.msgs|{ts}|hook.{trigger}"
        # session_id rides in the entry text only — the daemon's diary
        # executor whitelists agent_name/entry/topic/wing and drops the rest.
        rpc_ok, diary_resp = _post_mcp(daemon_url, "mempalace_diary_write", {
            "agent_name": harness,
            "entry": entry,
            "topic": CHECKPOINT_TOPIC,
            "wing": wing,
        })
        # _post_mcp only fails on transport errors; tool-level failure is a
        # success=False inside the JSON-RPC envelope — unwrap and check both.
        inner = _extract_inner(diary_resp) if rpc_ok else {}
        diary_ok = bool(rpc_ok and inner.get("success"))
        if diary_ok:
            _log(f"Diary checkpoint saved at exchange {exchange_count} → {wing}")
        else:
            detail = inner.get("error") or (
                diary_resp.get("error") if isinstance(diary_resp, dict) else str(diary_resp)
            ) or "no success flag in response"
            _log(f"Diary checkpoint FAILED at exchange {exchange_count} → {wing}: {detail}")
        # Wake-on-demand + replay journal live entirely in this detached
        # child: a connection-level failure (host asleep) triggers the
        # configured wake command + a single retry; a still-failed ingest
        # is journaled for replay on the next session start. All of this
        # latency is invisible — the parent already emitted the optimistic
        # "memories woven" systemMessage above.
        ok = _ingest_with_wake_and_journal(daemon_url, transcript_path, wing, session_id)
        _log(f"Silent save (diary+mine) {'OK' if ok else 'FAILED (journaled for replay)'} at exchange {exchange_count} → {wing}")
        if not ok:
            failure_themed = _theme_save_fail(exchange_count, trigger, {"error": "mine via daemon failed"})
            _log(f"FAILURE themed (would-have-emitted): {failure_themed}")
        if toast:
            _desktop_notify("MemPalace", f"Auto-saved at {exchange_count} msgs ({trigger})")
    else:
        if toast:
            _desktop_notify("MemPalace checkpoint", "Save requested — check Claude")
        _output({"decision": "block", "reason": STOP_BLOCK_REASON})


PRECOMPACT_TOPIC = "precompact"


def hook_precompact(data: dict, harness: str):
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    transcript_path = parsed["transcript_path"]
    _log(f"PRE-COMPACT triggered for session {session_id}")

    settings = _load_hook_settings()
    daemon_url = settings.get("daemon_url", "http://localhost:8085")
    toast = settings.get("desktop_toast", False)

    # Phase 1D refactor: precompact is also miner-only now. The transcript
    # ingest captures the current conversation state as drawers via the
    # miner; the previous "boundary marker" diary_write is dropped (it
    # was a stub anyway — the actual conversation content is what matters
    # at compaction, and the miner captures that).
    wing = _project_wing(data, transcript_path)

    # Emit themed systemMessage in the parent BEFORE detaching. See
    # hook_stop for the rationale. palace_count is pre-ingest; ingest
    # outcome is optimistic and falls back to hook.log on failure.
    # The MEMPAL_DIR mine's themed message (which depends on the mine
    # result) cannot be emitted in this single-fork architecture —
    # that path's outcome lands in hook.log only.
    pre_palace_count = _format_palace_count(_get_palace_stats(daemon_url))
    sys_msgs = [_theme_precompact_save(wing, {}, pre_palace_count)]
    mine_dir = _get_mine_dir()
    if mine_dir:
        sys_msgs.append(f"⟳ Precompact MEMPAL_DIR mine queued: {mine_dir}")
    _output({"systemMessage": "\n".join(sys_msgs)})

    if not _detach_for_async_work():
        return

    # We are the (detached) child. Do the slow ingest + optional mine.
    # Both outcomes land in hook.log only; the user already saw the
    # optimistic themed line above. Wake-on-demand + replay journal run
    # here too: a sleeping host is woken and the ingest retried once; a
    # still-failed ingest is journaled for replay on next session start.
    ingest_ok = _ingest_with_wake_and_journal(daemon_url, transcript_path, wing, session_id)
    _log(f"Pre-compact mine {'OK' if ingest_ok else 'FAILED (journaled for replay)'} → {wing}")
    if not ingest_ok:
        _log("FAILURE themed (would-have-emitted): ✘ Pre-compact transcript ingest failed — daemon unreachable")

    if mine_dir:
        _log(f"Precompact mine via daemon: {mine_dir}")
        ok, mine_response = _post_mine(daemon_url, mine_dir,
                                       timeout=60, mode="convos", wing=wing)
        _log(f"Precompact MEMPAL_DIR mine {'OK' if ok else 'skipped (daemon unreachable)'}")
        if ok:
            post_palace_count = _format_palace_count(_get_palace_stats(daemon_url))
            _log(f"MINE-RESULT themed (would-have-emitted): {_theme_mine(mine_dir, ok, mine_response, post_palace_count)}")
        else:
            _log(f"MINE-RESULT themed (would-have-emitted): {_theme_mine(mine_dir, ok, mine_response, '')}")
    else:
        _log("Precompact MEMPAL_DIR mine skipped: MEMPAL_DIR not set")

    if toast:
        _desktop_notify("MemPalace", "Pre-compaction checkpoint triggered")


COMPACTION_TOPIC = "compaction"


def _theme_postcompact(wing: str, trigger: str) -> str:
    """User-visible notification line for a completed compaction.

    🔄 for auto-compaction (hit the context limit), 📋 for a manual /compact.
    """
    icon = "🔄" if trigger == "auto" else "📋"
    return (
        f"{icon} Context compacted ({trigger}). "
        f"Your verbatim history is in the palace — "
        f"use mempalace_search to retrieve what you need."
    )


def hook_postcompact(data: dict, harness: str):
    """PostCompact: persist the compaction summary to the palace.

    PostCompact is informational-only — it cannot inject additionalContext
    or return ``decision: "block"`` — so it just notifies the user and saves
    the summary as a ``compaction`` diary entry. The actual context recovery
    happens in the SessionStart(source="compact") branch that fires next.
    """
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    transcript_path = parsed["transcript_path"]
    # `trigger` ("auto"|"manual") and `compact_summary` are top-level fields
    # in the Claude Code v2.1.187 PostCompact payload — _parse_harness_input
    # only normalizes the common three, so read these straight off `data`.
    trigger = data.get("trigger", "auto")
    compact_summary = (data.get("compact_summary") or "").strip()
    _log(
        f"POST-COMPACT for session {session_id} "
        f"(trigger={trigger}, summary={len(compact_summary)} chars)"
    )

    settings = _load_hook_settings()
    daemon_url = settings.get("daemon_url", "http://localhost:8085")
    toast = settings.get("desktop_toast", False)
    wing = _project_wing(data, transcript_path)

    # Emit the user-visible notification in the PARENT before detaching —
    # the detached child's stdout is /dev/null (see _detach_for_async_work).
    _output({"systemMessage": _theme_postcompact(wing, trigger)})

    if not _detach_for_async_work():
        return

    # Detached child: slow daemon round-trip. Anything printed here goes to
    # /dev/null; hook.log is the durable channel (mirrors hook_stop's diary
    # checkpoint). The verbatim transcript was already mined by precompact —
    # this entry preserves the derived summary on top of that.
    if not compact_summary:
        _log("Post-compact: no compact_summary in payload — nothing to save")
        return

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"COMPACTION:{session_id}|{trigger}|{ts}\n\n{compact_summary}"
    rpc_ok, diary_resp = _post_mcp(daemon_url, "mempalace_diary_write", {
        "agent_name": harness,
        "entry": entry,
        "topic": COMPACTION_TOPIC,
        "wing": wing,
    })
    # _post_mcp only fails on transport errors; a tool-level failure is a
    # success=False inside the JSON-RPC envelope — unwrap and check both.
    inner = _extract_inner(diary_resp) if rpc_ok else {}
    if rpc_ok and inner.get("success"):
        _log(f"Post-compact summary saved → {wing} ({len(compact_summary)} chars)")
    else:
        detail = inner.get("error") or (
            diary_resp.get("error") if isinstance(diary_resp, dict) else str(diary_resp)
        ) or "no success flag in response"
        _log(f"Post-compact summary save FAILED → {wing}: {detail}")
    if toast:
        _desktop_notify("MemPalace", f"Compaction summary saved ({trigger})")


def run_hook(hook_name: str, harness: str):
    # Read stdin in the parent so the child has it whether or not we detach.
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, EOFError):
        _log("WARNING: Failed to parse stdin JSON, proceeding with empty data")
        data = {}

    # Stop/precompact do slow HTTP round-trips to palace-daemon and
    # would block the harness — but the detach is now handled inside
    # each handler (after it emits its user-visible systemMessage via
    # _output) by calling _detach_for_async_work(). The dispatcher
    # just dispatches; the handlers decide when to fork.
    hooks = {
        "session-start": hook_session_start,
        "stop": hook_stop,
        "precompact": hook_precompact,
        "postcompact": hook_postcompact,
    }

    handler = hooks.get(hook_name)
    if handler is None:
        print(f"Unknown hook: {hook_name}", file=sys.stderr)
        sys.exit(1)

    handler(data, harness)


def main():
    parser = argparse.ArgumentParser(description="palace-daemon hook runner")
    parser.add_argument("--hook", required=True,
                        choices=["session-start", "stop", "precompact", "postcompact"])
    parser.add_argument("--harness", required=True)
    args = parser.parse_args()
    run_hook(args.hook, args.harness)


if __name__ == "__main__":
    main()
