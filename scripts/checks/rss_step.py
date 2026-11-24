#!/usr/bin/env python3
"""CHECK palace-rss-step: did the daemon's memory jump between two samples five minutes apart?

On 2026-11-04 (#323) the daemon went from 1.19 GB RSS at 12:13 to 5.38 GB at 12:18, then sat at the
cgroup's MemoryHigh for 20 minutes: /mcp's in-flight cap filled with hung calls, every lane's diary save
was refused, and systemd-oomd killed a lead session's scope. The only instrument was a hand-run 5-minute
RSS log, read after the restart. This check is that log with a threshold: every 5 minutes it reads the
daemon's MainPID (``systemctl show``) and that process's ``VmRSS`` + ``VmSwap`` from ``/proc/<pid>/status``,
and compares them with the previous sample, kept beside the line as ``.palace-rss-step.sample``.

Memory is RSS plus swap: above MemoryHigh the kernel pushes the daemon's pages to swap, so RSS alone
can FALL while the process keeps growing (11-04: RSS 5.13 GB with 3.14 GB more in swap).

- **fail**: the daemon grew by ``PALACE_RSS_STEP_GIB`` (default 2) or more since a sample from the same
  pid at most ``WINDOW_S`` old; or no daemon process could be read (MainPID 0, /proc unreadable).
- **warn**: RSS + swap at or above ``PALACE_RSS_WARN_GIB`` (default 4; MemoryHigh is 5G), or a step
  seen by the same pid in the last ``HOLD_S`` (so the step outlives the next sample's line).
- **ok**: neither. The line says when the daemon restarted since the last sample.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import checks_dir, emit, line, run  # noqa: E402

NAME = "palace-rss-step"
UNIT = os.environ.get("PALACE_UNIT_NAME", "palace-daemon.service")
WINDOW_S = 15 * 60        # an older sample (a missed run, a suspend) is not a step baseline
HOLD_S = 6 * 3600         # how long a seen step keeps the line at warn
OWNER = "owner palace lane"
GIB = 1024 * 1024         # kB per GiB (/proc reports kB)


def gib_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def sample_file() -> Path:
    return checks_dir() / f".{NAME}.sample"


def main_pid(unit: str = UNIT) -> int:
    out = subprocess.run(
        ["systemctl", "show", unit, "-p", "MainPID"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    for ln in out.splitlines():
        if ln.startswith("MainPID="):
            return int(ln.split("=", 1)[1] or 0)
    return 0


def read_status(pid: int, proc: Path = Path("/proc")) -> dict | None:
    """{rss_kb, swap_kb} of the process, or None when it can't be read."""
    if pid <= 0:
        return None
    try:
        text = (proc / str(pid) / "status").read_text()
    except OSError:
        return None
    got = {}
    for ln in text.splitlines():
        key, _, rest = ln.partition(":")
        if key in ("VmRSS", "VmSwap"):
            try:
                got[key] = int(rest.split()[0])
            except (IndexError, ValueError):
                return None
    if "VmRSS" not in got:
        return None  # a zombie: nothing resident to measure
    return {"rss_kb": got["VmRSS"], "swap_kb": got.get("VmSwap", 0)}


def _gib(kb: int) -> str:
    return f"{kb / GIB:.2f}"


def _clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def judge(prev: dict | None, cur: dict | None, now: float,
          step_gib: float, warn_gib: float) -> tuple[str, str, str, dict | None]:
    """(state, fact, next, sample to keep). ``cur`` carries pid, rss_kb, swap_kb; None = unreadable."""
    if cur is None:
        return ("fail", f"no {UNIT} process to measure (MainPID 0, or /proc unreadable)",
                f"{OWNER}: is the daemon running? (systemctl status {UNIT})", None)
    total = cur["rss_kb"] + cur["swap_kb"]
    keep = {"pid": cur["pid"], "rss_kb": cur["rss_kb"], "swap_kb": cur["swap_kb"], "at": now}
    facts = [f"daemon RSS {_gib(cur['rss_kb'])} GiB + swap {_gib(cur['swap_kb'])} GiB"]
    state = "ok"
    if not prev:
        facts.append("first sample")
    elif prev.get("pid") != cur["pid"]:
        facts.append("restarted since the last sample")
    else:
        age = now - float(prev.get("at", 0))
        delta = total - (int(prev.get("rss_kb", 0)) + int(prev.get("swap_kb", 0)))
        if 0 < age <= WINDOW_S:
            facts.append(f"{'+' if delta >= 0 else '-'}{_gib(abs(delta))} GiB in {age / 60:.0f} min")
            if delta >= step_gib * GIB:
                state = "fail"
                keep["step"] = {"kb": delta, "at": now}
        else:
            facts.append("no sample in the last 15 min to compare")
        # A step seen earlier by this same process stays on the line for HOLD_S.
        held = prev.get("step")
        if "step" not in keep and isinstance(held, dict) and 0 <= now - float(held.get("at", 0)) <= HOLD_S:
            keep["step"] = held
            facts.append(f"stepped +{_gib(int(held.get('kb', 0)))} GiB at {_clock(float(held['at']))}")
            state = "warn"
    if total >= warn_gib * GIB:
        facts.append(f"at or above {warn_gib:g} GiB")
        if state == "ok":
            state = "warn"
    nxt = {
        "fail": f"{OWNER}: read the journal's /mcp in-flight lines now; a restart clears it (#323)",
        "warn": f"{OWNER}: watch for /mcp busy refusals; restart if saves are refused (#323)",
        "ok": OWNER,
    }[state]
    return state, "; ".join(facts), nxt, keep


def load_prev() -> dict | None:
    try:
        data = json.loads(sample_file().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save(sample: dict | None) -> None:
    if sample is None:
        return
    path = sample_file()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(sample))
        os.replace(tmp, path)
    except OSError as exc:
        print(f"{NAME}: sample not saved: {type(exc).__name__}", file=sys.stderr)


def body() -> None:
    pid = main_pid()
    status = read_status(pid)
    cur = {"pid": pid, **status} if status else None
    state, fact, nxt, keep = judge(
        load_prev(), cur, time.time(),
        gib_env("PALACE_RSS_STEP_GIB", 2.0), gib_env("PALACE_RSS_WARN_GIB", 4.0),
    )
    save(keep)
    emit(NAME, line(NAME, state, fact, nxt))


if __name__ == "__main__":
    sys.exit(run(NAME, body))
