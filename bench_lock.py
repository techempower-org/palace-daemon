"""Bench-active lock helpers (#104) — pulled out of main.py per #101 refactor.

External bench runs (SME LongMemEval, candidate-strategy ablation, etc.) hit
the daemon hard. The auto-mine spawned by the WatcherService can concurrently
push the postgres container into OOM territory (#102) and cascade into the
connection-closed pattern (#97). A simple file-lock contract gives bench
runners a way to pause auto-mine without restarting the daemon (which would
be catastrophic mid-bench).

The contract (two compatible shapes — see #196 refcount):

1. **Legacy single-bench mutex** (#104): the lock *path* is a plain file.
   Bench runner ``touch``es ``<palace_data_dir>/.bench-active.lock`` before
   launching and ``rm``s it after. The daemon treats a fresh file as active.
   The footgun this had: bench A finishing ``rm``s the file while bench B is
   still ingesting, un-pausing auto-mine mid-bench.

2. **Refcounted** (#196): the lock *path* is a **directory** holding one
   PID-named marker file per concurrent bench (``<pid>.marker``).
   ``bench_lock_active()`` is true while *any* non-stale marker is present;
   auto-mine resumes only when the **last** bench deregisters. Dead-PID
   markers are reaped so a crashed bench can't wedge auto-mine forever.

Both shapes share the same ``PALACE_BENCH_LOCK_PATH`` / stale-age contract.
``bench_lock_active()`` auto-detects which shape is present, so old plain-file
callers and new refcounted callers interoperate against the same daemon.

This module is the daemon-side half. The operator/bench-runner CLI is
``scripts/bench-lock.sh``.
"""
from __future__ import annotations

import os


def automine_disabled() -> tuple[bool, str]:
    """Hard kill-switch for watcher auto-mine (palace-daemon#190).

    Distinct from :func:`bench_lock_active`. The bench lock is an
    *advisory*, time-limited file that gates *newly-spawned* mines — but a
    mine already in flight when the lock appears runs to completion, and
    there's a window between "mine finishes" and "the next tick sees the
    lock" where a fresh mine can slip through. For heavy sustained-ingest
    benches (SME #91 = ~24K /memory POSTs) that window is unacceptable:
    #190 documents a daemon SIGTERM'd mid-mine despite the lock.

    This is the absolute gate. When ``PALACE_DISABLE_AUTOMINE`` is truthy
    (1/true/yes/on, case-insensitive) the watcher spawns NO auto-mine for
    the lifetime of the process — set it in the daemon env, restart, run
    the bench, revert. Explicit ``POST /mine`` is unaffected (user-driven;
    a bench that wants zero mining simply doesn't call it).

    Returns ``(is_disabled, reason_string)`` — same shape as
    :func:`bench_lock_active` so the watcher can log either gate uniformly.
    """
    val = os.environ.get("PALACE_DISABLE_AUTOMINE", "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return (True, f"PALACE_DISABLE_AUTOMINE={val}")
    return (False, "")


def bench_lock_path(_config_provider=None) -> str:
    """Resolve the lock file path.

    ``PALACE_BENCH_LOCK_PATH`` env override wins; otherwise defaults to
    ``<palace_data_dir>/.bench-active.lock`` (matching #104's suggestion).

    ``_config_provider`` is a callable returning an object with a
    ``palace_path`` attribute — typically ``lambda: mempalace.mcp_server._config``.
    Injected so tests can substitute without monkey-patching the mempalace
    module. The default (``None``) does the lazy lookup that production uses.
    """
    override = os.environ.get("PALACE_BENCH_LOCK_PATH")
    if override:
        return override
    try:
        if _config_provider is not None:
            cfg = _config_provider()
        else:
            # Lazy import so test environments that haven't initialized
            # mempalace can still import this module.
            import mempalace.mcp_server as _mp
            cfg = _mp._config
        return os.path.join(cfg.palace_path, ".bench-active.lock")
    except Exception:
        # _mp._config may not be initialized in unusual contexts (tests
        # importing the module before lifespan runs). Fall back to a sensible
        # default so the daemon doesn't crash trying to compute the path.
        return os.path.join(os.path.expanduser("~"), ".palace-bench-active.lock")


def _max_age_seconds() -> float:
    """Resolve the stale-lock threshold (default 6 h)."""
    try:
        return float(os.environ.get("PALACE_BENCH_LOCK_MAX_AGE_SECONDS") or "21600")
    except (TypeError, ValueError):
        return 21600.0


def _refcount_active(dir_path: str, max_age: float, reap: bool = True) -> tuple[bool, str]:
    """Refcount mode: the lock path is a directory of ``<pid>.marker`` files.

    Active while ≥1 non-stale marker exists. A marker is stale purely by
    **age** (mtime older than ``max_age``). Stale markers are reaped
    (best-effort) so a crashed bench can't wedge auto-mine forever.

    We deliberately do **not** reap on PID-liveness. SME benches run on
    katana and SSH into the daemon host, recording their *katana* PID in the
    marker name — that PID is meaningless in the daemon host's process table,
    so a ``kill -0`` check there would wrongly reap a live remote bench. The
    age guard (default 6 h, refreshed by the bench via a heartbeat ``touch``
    if it runs longer) is the correct, transport-agnostic backstop.
    """
    import time as _time
    now = _time.time()
    try:
        entries = os.listdir(dir_path)
    except OSError as e:
        return (False, f"listdir failed: {e}")

    live = 0
    reaped = 0
    newest_age = None
    for name in entries:
        if not name.endswith(".marker"):
            continue
        marker = os.path.join(dir_path, name)
        try:
            age = now - os.stat(marker).st_mtime
        except OSError:
            continue
        if age > max_age:
            if reap:
                try:
                    os.unlink(marker)
                    reaped += 1
                except OSError:
                    pass
            continue
        live += 1
        if newest_age is None or age < newest_age:
            newest_age = age

    if live > 0:
        return (
            True,
            f"refcount dir={dir_path} active={live} reaped={reaped} "
            f"newest_age={int(newest_age) if newest_age is not None else '?'}s",
        )
    return (False, f"refcount dir={dir_path} no live markers (reaped={reaped})")


def bench_lock_active(_config_provider=None) -> tuple[bool, str]:
    """Return ``(is_active, reason_string)`` for the bench-active lock.

    Auto-detects the lock shape:
    - **directory** → refcount mode (#196): active while ≥1 non-stale
      PID-marker is present; stale/dead markers are reaped.
    - **plain file** → legacy mutex (#104): active while the file is fresh.

    A lock older than ``PALACE_BENCH_LOCK_MAX_AGE_SECONDS`` (default 6 h)
    is treated as stale and ignored — protects against bench runners that
    crashed without cleaning up. The reason string is plain text suitable
    for logging.
    """
    import time as _time
    path = bench_lock_path(_config_provider=_config_provider)
    max_age = _max_age_seconds()
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return (False, "no lock file")
    except OSError as e:
        # Any other stat failure (permissions, ENOTDIR) should not block
        # auto-mine — surface it in the reason but treat as inactive.
        return (False, f"stat failed: {e}")

    # Refcount mode: directory of per-bench PID markers.
    if os.path.isdir(path):
        return _refcount_active(path, max_age)

    # Legacy single-file mutex: a fresh file means active.
    age = _time.time() - st.st_mtime
    if age > max_age:
        return (False, f"stale (age {int(age)}s > max {int(max_age)}s)")
    return (True, f"path={path} age={int(age)}s")
