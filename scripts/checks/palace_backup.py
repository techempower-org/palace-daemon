#!/usr/bin/env python3
"""CHECK palace-backup: did last night's scheduled palace database dump run, and is the dump whole?

The nightly dump (a system timer outside this repo writes ``<dir>/mempalace-YYYYMMDD.sql.gz`` through a
``.tmp`` file it promotes only after its own ``gzip -t``, and keeps seven days) had no CHECK line at all
(Flow f1f8d214): a dump that stopped, shrank or broke would have gone unseen until a restore was needed.

Daily, read on the host that holds the dumps (``PALACE_BACKUP_DIR`` in ``~/.config/palace-checks/env``;
no path or host is named in this repo):
- the schedule: the dump timer's last trigger, and whether the dump unit finished after it and a dump
  was written after it (a missed night fails even while yesterday's dump is still young);
- the newest dump's age and size, and its size against the dump before it; how many are kept;
- the dump unit's last result and exit status, and whether its timer is active;
- the last COMPLETED ``--integrity`` run, bound to the dump it read (name, size, mtime): a dump changed
  since or a failed run fails the line; a re-run in progress only adds a note, and one that never
  finishes fails it.

Weekly, ``--integrity`` decompresses the newest dump end to end at idle I/O; gzip must exit 0 and the
last non-empty line must be pg_dump's closing line. It defers (no record) while the dump unit runs or
when the newest dump is under ``SETTLE_MIN`` minutes old. It is not a restore.

The line carries counts, sizes, dates and times only.
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
INTEGRITY_RUN_MAX_MIN = 70      # the integrity unit's TimeoutStartSec is 60 min
RUN_GRACE_H = 2.0               # a dump this soon after its trigger may still be running
SETTLE_MIN = 10
FOOTER = b"-- PostgreSQL database dump complete"
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
    """``systemctl show -p … --timestamp=unix`` output (KEY=value lines) as a dict."""
    return dict(ln.split("=", 1) for ln in show.splitlines() if "=" in ln)


def stamp(value: str | None) -> float | None:
    """``@<unix seconds>`` (systemctl --timestamp=unix) as a float; None when unset."""
    m = re.fullmatch(r"@(\d+(?:\.\d+)?)", (value or "").strip())
    return float(m.group(1)) if m else None


def hhmm(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")


def schedule(found, now: float, unit: dict, fails: list, warns: list, fact: list) -> None:
    """The dump timer's last trigger must be followed by a finished run and a new dump."""
    trig = stamp(unit.get("LastTriggerUSec"))
    if trig is None:
        warns.append("the dump timer has never triggered")
        return
    fact.append(f"last trigger {hhmm(trig)}")
    since_h = (now - trig) / 3600
    if since_h > FAIL_AGE_H:
        fails.append(f"the dump timer has not triggered for {since_h:.0f} h")
    elif since_h > WARN_AGE_H:
        warns.append(f"the dump timer has not triggered for {since_h:.0f} h")
    running = unit.get("ActiveState") in ("activating", "active", "deactivating")
    if running and since_h <= RUN_GRACE_H:
        warns.append(f"backup still running since {hhmm(trig)}")   # review r2: never ok on yesterday's dump
        return
    exited = stamp(unit.get("ExecMainExitTimestamp"))
    if exited is None or exited < trig:
        fails.append("no dump run finished after the last trigger")
    if not found or found[-1][2] < trig:
        fails.append("the last scheduled run wrote no dump")


def integrity_facts(rec: dict | None, current: tuple[int, float] | None, newest: str | None, now: float,
                    fails: list, warns: list, fact: list) -> None:
    """The last COMPLETED integrity run, judged against the dump it read as that dump is now; a run in progress
    only adds a note (review r3: starting a re-run must never hide a recorded failure)."""
    rec = rec or {}
    done = rec.get("last_completed")
    if not done:
        warns.append("no integrity run completed yet")
    else:
        dump = str(done.get("dump", "?"))
        days = (now - float(done.get("at", 0))) / 86400
        which = "" if dump == newest else " (not the newest)"
        fact.append(f"integrity {done.get('result', '?')} on "
                    f"{DUMP.match(dump).group(1) if DUMP.match(dump) else '?'}{which} {days:.0f} d ago")
        if done.get("result") != "ok":
            fails.append("the last integrity run failed")
        elif current is not None and (current[0] != done.get("size") or current[1] != done.get("mtime")):
            fails.append("the dump changed since its integrity run")
        elif days > INTEGRITY_MAX_DAYS:
            warns.append(f"integrity older than {INTEGRITY_MAX_DAYS} d")
    since = rec.get("running_since")
    if since:
        since = float(since)
        if (now - since) / 60 > INTEGRITY_RUN_MAX_MIN:
            fails.append(f"the integrity re-run started {hhmm(since)} never finished")
        else:
            fact.append(f"integrity re-run in progress since {hhmm(since)}")


def assess(found: list[tuple[str, int, float]], now: float, unit: dict, timer_active: str,
           integrity: dict | None, integrity_current: tuple[int, float] | None = None) -> tuple[str, str]:
    """(state, fact). ``integrity_current`` is (size, mtime) of the dump the integrity record names, as it
    is now, or None when that dump has aged out."""
    fails, warns, fact = [], [], []
    if found:
        name, size, mtime = found[-1]
        age_h = (now - mtime) / 3600
        fact.append(f"newest {DUMP.match(name).group(1)} ({age_h:.0f} h old, {size / 1e9:.2f} GB)")
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
    else:
        fails.append("no dump in the backup directory")
    schedule(found, now, unit, fails, warns, fact)
    result, status = unit.get("Result", "?"), unit.get("ExecMainStatus", "?")
    fact.append(f"unit last result {result} (exit {status})")
    if result != "success" or status not in ("0", "?"):
        fails.append("the dump unit's last run failed")
    if timer_active != "active":
        warns.append(f"timer {timer_active or 'unknown'}")
    integrity_facts(integrity, integrity_current, found[-1][0] if found else None, now, fails, warns, fact)
    state = "fail" if fails else "warn" if warns else "ok"
    return state, ", ".join(fact) + (f" [{'; '.join(fails + warns)}]" if fails or warns else "")


def integrity_of(path: Path, opener=None) -> tuple[str, str]:
    """('ok'|'fail', why): gzip decompresses the whole stream (exit 0) and the last non-empty line of the
    decompressed dump is exactly pg_dump's closing line."""
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
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip() and ln.strip() != b"--"]
    if lines and re.fullmatch(rb"\\unrestrict \S+", lines[-1]):
        lines.pop()                                    # pg_dump >= 17.6 closes its \restrict block after the footer
    if not lines or lines[-1] != FOOTER:
        return "fail", "the last line is not pg_dump's closing line"
    return "ok", "whole stream, closing line last"


def integrity_file() -> Path:
    return checks_dir() / f"{NAME}.integrity.json"


def read_integrity() -> dict | None:
    try:
        return json.loads(integrity_file().read_text())
    except (OSError, ValueError):
        return None


def write_integrity(rec: dict) -> None:
    f = integrity_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec) + "\n")
    os.replace(tmp, f)


def show(unit: str, *props: str) -> dict:
    args = ["systemctl", "show", unit, "--timestamp=unix"] + [x for p in props for x in ("-p", p)]
    return unit_state(subprocess.run(args, capture_output=True, text=True, timeout=30, check=False).stdout)


def integrity_run(directory: Path, found, unit: dict, now: float) -> str | None:
    """Run the integrity pass on the newest dump; a reason string when it deferred instead."""
    if not found:
        return None
    name, size, mtime = found[-1]
    if unit.get("ActiveState") in ("activating", "active", "deactivating"):
        return "integrity deferred: the dump unit is running"
    if (now - mtime) / 60 < SETTLE_MIN:
        return f"integrity deferred: the newest dump is under {SETTLE_MIN} min old"
    rec = read_integrity() or {}
    rec.update({"running_since": now, "running_dump": name})          # last_completed stays as it was
    write_integrity(rec)
    result, why = integrity_of(directory / name)
    write_integrity({"last_completed": {"result": result, "why": why, "dump": name, "size": size, "mtime": mtime,
                                        "started": now, "at": dt.datetime.now().timestamp()},
                     "running_since": None})
    return None


def body(do_integrity: bool) -> None:
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
    def snapshot():
        u = show(UNIT, "Result", "ExecMainStatus", "ExecMainExitTimestamp", "ActiveState")
        u.update(show(TIMER, "LastTriggerUSec"))
        return u
    unit = snapshot()
    deferred = integrity_run(directory, found, unit, dt.datetime.now().timestamp()) if do_integrity else None
    if do_integrity:                                   # review r2: the run can take an hour; judge the state now
        try:
            found = dumps(directory)
        except OSError as exc:
            emit(NAME, line(NAME, "fail", f"backup directory unreadable ({type(exc).__name__})"))
            return
        unit = snapshot()
    timer = subprocess.run(["systemctl", "is-active", TIMER], capture_output=True, text=True,
                           timeout=30, check=False).stdout.strip()
    rec = read_integrity()
    current = None
    done = (rec or {}).get("last_completed") or {}
    if done.get("dump"):
        try:
            st = (directory / str(done["dump"])).stat()
            current = (st.st_size, st.st_mtime)
        except OSError:
            current = None
    state, fact = assess(found, dt.datetime.now().timestamp(), unit, timer, rec, current)
    if deferred:
        fact += f" ({deferred})"
        state = "warn" if state == "ok" else state
    nxt = {"ok": "nothing",
           "warn": "owner palace lane: read the dump unit's journal, or run this check with --integrity",
           "fail": "owner palace lane: read the dump unit's journal; rerun the dump before the oldest one ages out"}[state]
    emit(NAME, line(NAME, state, fact, nxt))


if __name__ == "__main__":
    sys.exit(run(NAME, lambda: body("--integrity" in sys.argv[1:])))
