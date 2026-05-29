"""Bench-active lock helpers (#104) — pulled out of main.py per #101 refactor.

External bench runs (SME LongMemEval, candidate-strategy ablation, etc.) hit
the daemon hard. The auto-mine spawned by the WatcherService can concurrently
push the postgres container into OOM territory (#102) and cascade into the
connection-closed pattern (#97). A simple file-lock contract gives bench
runners a way to pause auto-mine without restarting the daemon (which would
be catastrophic mid-bench).

The contract:
- Bench runner touches ``<palace_data_dir>/.bench-active.lock`` (or whatever
  ``PALACE_BENCH_LOCK_PATH`` points at) before launching.
- Daemon's WatcherService checks `bench_lock_active()` at every spawn and
  skips when active.
- Stale locks (older than ``PALACE_BENCH_LOCK_MAX_AGE_SECONDS``, default 6 h)
  are treated as inactive so a crashed bench can't wedge auto-mine.

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


def bench_lock_active(_config_provider=None) -> tuple[bool, str]:
    """Return ``(is_active, reason_string)`` for the bench-active lock.

    A lock older than ``PALACE_BENCH_LOCK_MAX_AGE_SECONDS`` (default 6 h)
    is treated as stale and ignored — protects against bench runners that
    crashed without cleaning up. The reason string is plain text suitable
    for logging.
    """
    import time as _time
    path = bench_lock_path(_config_provider=_config_provider)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return (False, "no lock file")
    except OSError as e:
        # Any other stat failure (permissions, ENOTDIR) should not block
        # auto-mine — surface it in the reason but treat as inactive.
        return (False, f"stat failed: {e}")
    try:
        max_age = float(os.environ.get("PALACE_BENCH_LOCK_MAX_AGE_SECONDS") or "21600")  # 6 h
    except (TypeError, ValueError):
        max_age = 21600.0
    age = _time.time() - st.st_mtime
    if age > max_age:
        return (False, f"stale (age {int(age)}s > max {int(max_age)}s)")
    return (True, f"path={path} age={int(age)}s")
