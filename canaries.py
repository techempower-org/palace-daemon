"""Startup canary helpers (#92, #97, #116) — extracted from main.py per #101 refactor.

Two passive observability canaries that log mempalace + postgres-memcg state
at every daemon restart so silent drift / pressure becomes journal-grep-able.

- ``log_mempalace_canary`` — walks the mempalace package tree and reports
  the newest .py file's mtime. Warns when stale beyond the threshold
  (default 24h, overridable via ``PALACE_CANARY_WARN_HOURS``).

- ``log_postgres_memcg_canary`` — shells out to ``docker stats`` for the
  postgres container and reports usage / limit / percent. Warns when over
  the threshold (default 75%, overridable via
  ``PALACE_POSTGRES_MEMCG_WARN_PERCENT``). Skips silently when docker
  isn't reachable so the daemon still starts cleanly on a non-docker host.

main.py re-exports these under their original ``_log_*`` and
``_*_status`` names so existing call sites + tests keep working.
"""
from __future__ import annotations

import json
import os


def newest_mempalace_mtime() -> "tuple[float, str] | None":
    """Walk the mempalace package directory and return ``(newest_mtime, filename)``.

    #116: the canary previously read mempalace/__init__.py's mtime, but
    __init__.py is a stable file that rarely changes — every release with a
    code change but no __init__.py edit produced a false-positive WARN.
    Walking the tree and reporting the newest .py file's mtime tracks
    whole-tree freshness, which is what operators actually care about.

    Returns ``None`` if mempalace can't be imported or the directory walk
    finds no .py files (defensive — startup must not crash on this probe).
    """
    try:
        import mempalace as _mp_pkg
        path = _mp_pkg.__file__
        if not path:
            return None
        pkg_dir = os.path.dirname(path)
    except Exception:
        return None
    newest_mtime = 0.0
    newest_file = ""
    try:
        for root, _, files in os.walk(pkg_dir):
            for f in files:
                if not f.endswith(".py"):
                    continue
                full = os.path.join(root, f)
                try:
                    m = os.path.getmtime(full)
                except OSError:
                    continue
                if m > newest_mtime:
                    newest_mtime = m
                    newest_file = full
    except OSError:
        return None
    if newest_mtime == 0.0:
        return None
    return (newest_mtime, newest_file)


def log_mempalace_canary(logger, env=None) -> None:
    """Log the deployed mempalace state for drift detection (#92, #116).

    Reports the newest .py mtime across the whole mempalace/ tree (#116
    fix — __init__.py was a stable file that produced false-positive WARNs
    on releases that touched other modules but not __init__.py). If the
    age exceeds the warn threshold, log level is WARNING; otherwise INFO.
    Defaults to 24h; override via env var PALACE_CANARY_WARN_HOURS.
    """
    import datetime as _dt
    import time as _time
    env = env if env is not None else os.environ
    try:
        warn_hours = float(env.get("PALACE_CANARY_WARN_HOURS") or "24")
    except (TypeError, ValueError):
        warn_hours = 24.0
    result = newest_mempalace_mtime()
    if result is None:
        logger.info("mempalace canary: probe failed (package walk produced no .py files) — skipping")
        return
    mtime, canary = result
    mtime_iso = _dt.datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
    age_secs = max(0.0, _time.time() - mtime)
    if age_secs < 3600:
        age_h = f"{int(age_secs / 60)}m"
    elif age_secs < 86400:
        age_h = f"{age_secs / 3600:.1f}h"
    else:
        age_h = f"{age_secs / 86400:.1f}d"
    canary_basename = os.path.basename(canary) if canary else "(unknown)"
    msg = (
        "mempalace canary: newest .py = %s (mtime %s, age %s, warn-threshold %.1fh)"
    )
    if age_secs > warn_hours * 3600:
        logger.warning(
            msg + " — stale; run scripts/rsync-mempalace.sh "
            "to push from your workstation",
            canary_basename, mtime_iso, age_h, warn_hours,
        )
    else:
        logger.info(msg, canary_basename, mtime_iso, age_h, warn_hours)


def postgres_memcg_status(
    container: "str | None" = None, timeout_s: float = 2.0
) -> "dict | None":
    """Read docker stats for the postgres container to surface memcg pressure.

    Returns ``None`` when docker isn't reachable, the container isn't
    running, or the call exceeds ``timeout_s`` — every failure mode is
    non-fatal because /health must keep responding even when docker is
    grumpy. The 2s timeout is the ceiling; in practice ``docker stats
    --no-stream`` returns in ~50ms on a healthy daemon.

    Container is configurable via ``PALACE_POSTGRES_CONTAINER`` (default
    ``mempalace-db``).
    """
    import subprocess
    import time as _time
    container = container or os.environ.get("PALACE_POSTGRES_CONTAINER", "mempalace-db")
    try:
        proc = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", container],
            capture_output=True, text=True, timeout=timeout_s, check=True,
        )
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(proc.stdout.strip())
        # MemUsage shape: "2.43GiB / 3GiB" — keep as-is for display;
        # MemPerc shape: "81.00%" — strip the % and parse to float.
        # Coerce-via-`or` defends against `null` values in the JSON
        # (transient docker daemon state during container startup /
        # shutdown can produce `{"MemPerc": null, "MemUsage": null}`),
        # which `.rstrip` / `.partition` would otherwise blow up on.
        usage = data.get("MemUsage") or ""
        perc_raw = data.get("MemPerc") or "0%"
        perc_str = perc_raw.rstrip("%") if isinstance(perc_raw, str) else "0"
        percent = float(perc_str) / 100.0 if perc_str else 0.0
        usage_str, _, limit_str = usage.partition(" / ") if isinstance(usage, str) else ("", "", "")
        return {
            "container": container,
            "usage": usage_str.strip(),
            "limit": limit_str.strip(),
            "percent": round(percent, 4),
            "probed_at": int(_time.time()),
        }
    except (json.JSONDecodeError, ValueError, KeyError, AttributeError, TypeError):
        return None


def log_postgres_memcg_canary(logger, env=None) -> None:
    """Startup canary: log postgres-memcg pressure with INFO/WARN by threshold.

    Same pattern as ``log_mempalace_canary`` — passive observability via
    journalctl. INFO when usage is below the threshold (default 75%);
    WARNING when above. Skipped silently when docker isn't reachable.

    Threshold tunable via ``PALACE_POSTGRES_MEMCG_WARN_PERCENT`` (default 75.0).
    """
    env = env if env is not None else os.environ
    try:
        warn_pct = float(env.get("PALACE_POSTGRES_MEMCG_WARN_PERCENT") or "75")
    except (TypeError, ValueError):
        warn_pct = 75.0
    container = env.get("PALACE_POSTGRES_CONTAINER")
    status = postgres_memcg_status(container=container)
    if status is None:
        logger.info("postgres memcg canary: probe failed (docker unreachable or container down) — skipping")
        return
    pct = status["percent"] * 100.0
    msg = "postgres memcg canary: %s usage=%s limit=%s percent=%.1f%% (warn-threshold %.1f%%)"
    args = (status["container"], status["usage"], status["limit"], pct, warn_pct)
    if pct > warn_pct:
        logger.warning(
            msg + " — postgres container approaching OOM; consider raising the cgroup limit",
            *args,
        )
    else:
        logger.info(msg, *args)
