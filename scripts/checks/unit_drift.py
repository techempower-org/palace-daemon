#!/usr/bin/env python3
"""CHECK palace-unit-drift: is the installed palace-daemon.service the one in this repo, with nothing on top?

On 2026-11-21 the installed unit was a May copy plus seven drop-ins and two `systemctl set-property`
overrides, none of them in the repo: the watchdog the repo armed (#345) had to be deployed as yet another
drop-in, and the repo unit's comments described caps that were no longer in force. Nothing noticed, because
nothing compared them. This check does, once a day. It gathers three facts, then builds one line from all
of them:

1. the installed unit file (systemd's FragmentPath) against ``palace-daemon.service`` in the same tree as this
   script, compared as raw bytes (line endings and a missing or extra final newline count). A difference is
   reported as the first differing byte offset and how many lines differ, never as text;
2. the drop-ins systemd has loaded (DropInPaths), by file name; a ``systemctl set-property`` override, which
   lives under ``/etc/systemd/system.control/``, is named ``(set-property)``;
3. whether systemd needs a daemon-reload (NeedDaemonReload). Files changed on disk show up here, not in 2.

- **fail**: the files differ, or the unit is not a file systemd loads.
- **warn**: identical, but drop-ins on top, or a reload pending.
- **ok**: identical, no drop-in, nothing pending.
Every fact that holds is in the line, whatever the state.
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import emit, label, line, run  # noqa: E402

NAME = "palace-unit-drift"
UNIT = os.environ.get("PALACE_UNIT_NAME", "palace-daemon.service")
REPO_UNIT = Path(__file__).resolve().parents[2] / "palace-daemon.service"
OWNER = "owner palace lane"


def show(unit: str) -> dict[str, str]:
    """FragmentPath, DropInPaths and NeedDaemonReload of the unit, from `systemctl show`."""
    out = subprocess.run(
        ["systemctl", "show", unit, "-p", "FragmentPath", "-p", "DropInPaths", "-p", "NeedDaemonReload"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    return dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)


def first_difference(a: bytes, b: bytes) -> int | None:
    """The first byte offset where a and b differ (the shorter length when one is a prefix), else None."""
    if a == b:
        return None
    return next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))


def changed_lines(a: bytes, b: bytes) -> int:
    """How many lines differ, a diagnostic only: lines keep their terminators, so CRLF and a final newline count."""
    la, lb = a.splitlines(keepends=True), b.splitlines(keepends=True)
    return sum(1 for d in difflib.ndiff([x.decode("utf-8", "replace") for x in la],
                                        [x.decode("utf-8", "replace") for x in lb]) if d[:2] in ("+ ", "- "))


def dropin_names(dropins: list[str]) -> str:
    # `systemctl set-property` writes under /etc/systemd/system.control/: easy to miss, and a future one silently
    # recreates an override such as the old MemoryMax=8G. Named apart, so the line says what made it.
    return ", ".join(
        f"{label(Path(p).name)} (set-property)" if "/system.control/" in p else label(Path(p).name) for p in dropins
    )


def judge(installed: bytes | None, repo: bytes, dropins: list[str], need_reload: bool) -> tuple[str, str, str]:
    """(state, fact, next) from all three facts."""
    facts = []
    if installed is None:
        state = "fail"
        facts.append(f"{UNIT} is not installed as a file systemd loads")
    else:
        at = first_difference(installed, repo)
        if at is not None:
            state = "fail"
            facts.append(f"installed {UNIT} differs from the repo's from byte {at} "
                         f"({changed_lines(installed, repo)} line(s) differ)")
        else:
            state = "ok"
            facts.append(f"installed {UNIT} matches the repo byte for byte")
    if dropins:
        facts.append(f"{len(dropins)} drop-in(s) on top: {dropin_names(dropins)}")
    if need_reload:
        facts.append("systemd needs a daemon-reload")
    if state == "ok" and (dropins or need_reload):
        state = "warn"
    if state == "ok":
        facts.append("no drop-ins")
    nxt = {
        "fail": f"{OWNER}: install the repo unit (docs/ops/palace-daemon-unit.md)",
        "warn": f"{OWNER}: fold a lasting drop-in into the repo unit, remove a finished trial, or reload",
        "ok": OWNER,
    }[state]
    return state, "; ".join(facts), nxt


def body() -> None:
    props = show(UNIT)
    frag = props.get("FragmentPath", "")
    installed = Path(frag).read_bytes() if frag and Path(frag).is_file() else None
    dropins = [p for p in props.get("DropInPaths", "").split() if p]
    state, fact, nxt = judge(installed, REPO_UNIT.read_bytes(), dropins, props.get("NeedDaemonReload", "no") == "yes")
    emit(NAME, line(NAME, state, fact, nxt))


if __name__ == "__main__":
    sys.exit(run(NAME, body))
