#!/usr/bin/env python3
"""CHECK palace-daemon-restarts: did palace-daemon stop without being asked in the last 24 h, and why?

On Sun 2026-11-22 the daemon sat pinned at MemoryHigh with swap at MemorySwapMax from 03:54 to 05:46 before
a watchdog timeout ended it, and nothing said so until the next morning's hand count. The watchdog now
asks for a restart itself when memory stays pinned (Flow 970b2e72) and records why in
``~/.cache/palace-daemon/watchdog-trigger.json``. This check names every unrequested stop in the window,
by the kind systemd logged, and the trigger for each watchdog restart the daemon asked for:

- ``watchdog: pinned-memory``: the daemon's own WATCHDOG=trigger (systemd: "Watchdog request"), with the
  trigger file's swap and pinned seconds;
- ``watchdog: hang``: a keepalive that stopped (systemd: "Watchdog timeout");
- any other ``Failed with result '<r>'`` (oom-kill, signal, exit-code, …) by result.

- **ok**: none in 24 h.
- **warn**: one or two.
- **fail**: three or more, or the journal could not be read (a zero from a journal nobody read is not one).
"""

from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from checklib import emit, line, run  # noqa: E402

NAME = "palace-daemon-restarts"
WINDOW_H = int(os.environ.get("PALACE_RESTARTS_WINDOW_H", "24"))  # wider only for a positive control
JOURNAL_CMD = ["journalctl", "-u", "palace-daemon", "--since", f"-{WINDOW_H}h", "--no-pager", "-o",
               "short-unix"]
TRIGGER_PATH = Path.home() / ".cache" / "palace-daemon" / "watchdog-trigger.json"
REQUEST = re.compile(r"palace-daemon\.service: Watchdog request")
TIMEOUT = re.compile(r"palace-daemon\.service: Watchdog timeout")
FAILED = re.compile(r"palace-daemon\.service: Failed with result '([a-z-]+)'")


def classify(lines, trigger: dict | None) -> list:
    """[(epoch, kind, detail)] per unrequested stop, from ``short-unix`` journal lines. A watchdog
    request takes its name from the trigger file when the file was written within 120 s before it."""
    stops = []
    pending = None  # the watchdog line waiting for its "Failed with result 'watchdog'"
    for ln in lines:
        try:
            at = float(ln.split(" ", 1)[0])
        except ValueError:
            continue
        if REQUEST.search(ln):
            name, detail = "watchdog: requested", ""
            if trigger and isinstance(trigger.get("at"), (int, float)) and 0 <= at - trigger["at"] <= 120:
                name = f"watchdog: {trigger.get('trigger', 'requested')}"
                if trigger.get("trigger") == "pinned-memory":
                    detail = (f"swap {int(trigger.get('swap', 0)) >> 20} of {int(trigger.get('swap_max', 0)) >> 20}"
                              f" MiB, pinned {int(trigger.get('pinned_s', 0))} s")
            pending = (at, name, detail)
        elif TIMEOUT.search(ln):
            pending = (at, "watchdog: hang", "keepalive stopped")
        else:
            m = FAILED.search(ln)
            if not m:
                continue
            if m.group(1) == "watchdog" and pending is not None:
                stops.append(pending)
            else:
                stops.append((at, m.group(1), ""))
            pending = None
    return stops


def read_trigger(path: Path = TRIGGER_PATH) -> dict | None:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def build(stops: list, journal_ok: bool, why: str = "") -> str:
    if not journal_ok:
        return line(NAME, "fail", f"the palace-daemon journal could not be read ({why}); restarts unknown",
                    "owner palace lane: fix the check (journal access)")
    if not stops:
        return line(NAME, "ok", f"no unrequested stop in {WINDOW_H} h")
    kinds = collections.Counter(k for _a, k, _d in stops)
    last_at, last_kind, last_detail = stops[-1]
    fact = (f"{len(stops)} unrequested stop(s) in {WINDOW_H} h: "
            + ", ".join(f"{k} ×{n}" for k, n in sorted(kinds.items()))
            + f"; last {time.strftime('%a %H:%M', time.localtime(last_at))} {last_kind}"
            + (f" ({last_detail})" if last_detail else ""))
    state = "fail" if len(stops) >= 3 else "warn"
    return line(NAME, state, fact, "owner palace lane: read the journal around each stop")


def main() -> None:
    try:
        out = subprocess.run(JOURNAL_CMD, capture_output=True, text=True, errors="replace", timeout=60)
        ok = out.returncode == 0
        why = f"rc {out.returncode}: {' '.join(out.stderr.split())[:120]}"
        lines = out.stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        ok, why, lines = False, type(exc).__name__, []
    emit(NAME, build(classify(lines, read_trigger()), ok, why))


if __name__ == "__main__":
    sys.exit(run(NAME, main))
