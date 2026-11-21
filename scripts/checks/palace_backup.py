#!/usr/bin/env python3
"""CHECK palace-backup: is last night's palace database dump there, whole, and the size it should be?

The nightly dump (a system timer outside this repo writes ``<dir>/mempalace-YYYYMMDD.sql.gz`` and keeps
seven days) had no CHECK line at all (Flow f1f8d214): a dump that stopped, shrank or broke would have
gone unseen until a restore was needed.

Daily, read on the host that holds the dumps (``PALACE_BACKUP_DIR`` in ``~/.config/palace-checks/env``;
no path or host is named in this repo):
- the newest dump's age and size, and its size against the dump before it;
- how many dumps are kept;
- the dump unit's last result (``systemctl show``: Result, exit status) and whether its timer is active;
- the last ``--integrity`` result, and how old it is.

Weekly, ``--integrity`` decompresses the newest dump end to end, at idle I/O, and requires pg_dump's
closing line ("PostgreSQL database dump complete") in its last bytes. That proves the file is a whole
gzip stream and a finished dump. It is not a restore; a restore into a scratch database is a separate,
heavier run on a lane host.

States:
- fail: not configured or unreadable; no dump; the newest older than ``FAIL_AGE_H``; smaller than
  ``MIN_BYTES``; more than ``FAIL_SHRINK`` smaller than the previous dump; the unit's last run failed;
  the last integrity run failed.
- warn: the newest older than ``WARN_AGE_H``; more than ``WARN_SHRINK`` smaller; fewer than ``MIN_KEPT``
  dumps; the timer not active; no integrity result, or one older than ``INTEGRITY_MAX_DAYS``.

The line carries counts, sizes and dates only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import checks_dir, emit, line, run  # noqa: E402

NAME = "palace-backup"
DUMP = re.compile(r"^mempalace-(\d{8})\.sql\.gz$")
UNIT = os.environ.get("PALACE_BACKUP_UNIT", "mempalace-backup.service")
TIMER = os.environ.get("PALACE_BACKUP_TIMER", "mempalace-backup.timer")
WARN_AGE_H, FAIL_AGE_H = 26.0, 50.0
MIN_BYTES = 10 * 1024 * 1024
WARN_SHRINK, FAIL_SHRINK = 0.05, 0.20
MIN_KEPT = 3
INTEGRITY_MAX_DAYS = 8
FOOTER = b"PostgreSQL database dump complete"
TAIL = 4096


def dumps(directory: Path) -> list[tuple[str, int, float]]:
    """(name, size, mtime) of each dump file, oldest first by the date in its name."""
    out = []
    for p in directory.iterdir():
        m = DUMP.match(p.name)
        if m and p.is_file():
            st = p.stat()
            out.append((p.name, st.st_size, st.st_mtime))
    return sorted(out, key=lambda d: DUMP.match(d[0]).group(1))


def unit_state(show: str) -> dict:
    """``systemctl show -p …`` output (KEY=value lines) as a dict."""
    return dict(ln.split("=", 1) for ln in show.splitlines() if "=" in ln)


def assess(found: list[tuple[str, int, float]], now: float, unit: dict, timer_active: str,
           integrity: dict | None) -> tuple[str, str]:
    """(state, fact) from the dumps, the unit's last result, the timer and the last integrity run."""
    if not found:
        return "fail", "no dump in the backup directory"
    fails, warns = [], []
    name, size, mtime = found[-1]
    age_h = (now - mtime) / 3600
    gb = size / 1e9
    fact = [f"newest {DUMP.match(name).group(1)} ({age_h:.0f} h old, {gb:.2f} GB)"]
    if age_h > FAIL_AGE_H:
        fails.append(f"newest older than {FAIL_AGE_H:.0f} h")
    elif age_h > WARN_AGE_H:
        warns.append(f"newest older than {WARN_AGE_H:.0f} h")
    if size < MIN_BYTES:
        fails.append("newest under 10 MiB")
    if len(found) >= 2:
        prev = found[-2][1]
        change = (size - prev) / prev if prev else 0.0
        fact.append(f"{change:+.1%} on the dump before")
        if change < -FAIL_SHRINK:
            fails.append(f"shrank more than {FAIL_SHRINK:.0%}")
        elif change < -WARN_SHRINK:
            warns.append(f"shrank more than {WARN_SHRINK:.0%}")
    fact.append(f"{len(found)} kept")
    if len(found) < MIN_KEPT:
        warns.append(f"fewer than {MIN_KEPT} dumps kept")
    result, status = unit.get("Result", "?"), unit.get("ExecMainStatus", "?")
    fact.append(f"unit last result {result} (exit {status})")
    if result != "success" or status not in ("0", "?"):
        fails.append("the dump unit's last run failed")
    if timer_active != "active":
        warns.append(f"timer {timer_active or 'unknown'}")
    if not integrity:
        warns.append("no integrity run yet")
    else:
        days = (now - float(integrity.get("at", 0))) / 86400
        fact.append(f"integrity {integrity.get('result', '?')} on {integrity.get('dump', '?')} {days:.0f} d ago")
        if integrity.get("result") != "ok":
            fails.append("the last integrity run failed")
        elif days > INTEGRITY_MAX_DAYS:
            warns.append(f"integrity older than {INTEGRITY_MAX_DAYS} d")
    state = "fail" if fails else "warn" if warns else "ok"
    return state, ", ".join(fact) + (f" [{'; '.join(fails + warns)}]" if fails or warns else "")


def integrity_of(path: Path, opener=None) -> tuple[str, str]:
    """('ok'|'fail', why): the whole gzip stream decompresses and ends with pg_dump's closing line."""
    proc = (opener or (lambda p: subprocess.Popen(["gzip", "-dc", str(p)], stdout=subprocess.PIPE,
                                                  stderr=subprocess.DEVNULL)))(path)
    tail = b""
    while True:
        chunk = proc.stdout.read(1 << 20)
        if not chunk:
            break
        tail = (tail + chunk)[-TAIL:]
    rc = proc.wait()
    if rc != 0:
        return "fail", f"gzip exit {rc}"
    if FOOTER not in tail:
        return "fail", "no dump-complete footer"
    return "ok", "whole stream, footer present"


def integrity_file() -> Path:
    return checks_dir() / f"{NAME}.integrity.json"


def read_integrity() -> dict | None:
    try:
        return json.loads(integrity_file().read_text())
    except (OSError, ValueError):
        return None


def body(integrity_run: bool) -> None:
    raw = os.environ.get("PALACE_BACKUP_DIR", "")
    if not raw:
        emit(NAME, line(NAME, "fail", "PALACE_BACKUP_DIR is not set in ~/.config/palace-checks/env"))
        return
    directory = Path(raw)
    try:
        found = dumps(directory)
    except OSError as exc:
        emit(NAME, line(NAME, "fail", f"backup directory unreadable ({type(exc).__name__})"))
        return
    if integrity_run and found:
        result, why = integrity_of(directory / found[-1][0])
        rec = {"result": result, "why": why, "dump": DUMP.match(found[-1][0]).group(1),
               "at": dt.datetime.now().timestamp()}
        integrity_file().parent.mkdir(parents=True, exist_ok=True)
        integrity_file().write_text(json.dumps(rec) + "\n")
    show = subprocess.run(["systemctl", "show", UNIT, "-p", "Result", "-p", "ExecMainStatus"],
                          capture_output=True, text=True, timeout=30, check=False).stdout
    timer = subprocess.run(["systemctl", "is-active", TIMER], capture_output=True, text=True,
                           timeout=30, check=False).stdout.strip()
    state, fact = assess(found, dt.datetime.now().timestamp(), unit_state(show), timer, read_integrity())
    nxt = {"ok": "nothing",
           "warn": "owner palace lane: read the dump unit's journal, or run this check with --integrity",
           "fail": "owner palace lane: read the dump unit's journal; rerun the dump before the oldest one ages out"}[state]
    emit(NAME, line(NAME, state, fact, nxt))


if __name__ == "__main__":
    sys.exit(run(NAME, lambda: body("--integrity" in sys.argv[1:])))
