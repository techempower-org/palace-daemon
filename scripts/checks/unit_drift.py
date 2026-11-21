#!/usr/bin/env python3
"""CHECK palace-unit-drift: is the installed palace-daemon.service the one in this repo, with nothing on top?

On 2026-11-21 the installed unit was a May copy plus seven drop-ins and two `systemctl set-property`
overrides, none of them in the repo: the watchdog the repo armed (#345) had to be deployed as yet another
drop-in, and the repo unit's comments described caps that were no longer in force. Nothing noticed, because
nothing compared them. This check does, once a day:

- **fail**: the installed unit file differs from ``palace-daemon.service`` in the same tree as this script
  (the line gives how many lines differ, never their text), or the unit cannot be read.
- **warn**: identical, but drop-ins are loaded on top (named by file name), or systemd still needs a
  daemon-reload. A deliberate, dated drop-in (a trial) shows here until it is folded in or removed.
- **ok**: identical, no drop-in, nothing pending.
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


def changed_lines(installed: str, repo: str) -> int:
    """How many lines differ (added + removed), comparing exact text."""
    return sum(1 for d in difflib.ndiff(installed.splitlines(), repo.splitlines()) if d[:2] in ("+ ", "- "))


def judge(installed: str | None, repo: str, dropins: list[str], need_reload: bool) -> tuple[str, str, str]:
    """(state, fact, next) for one reading."""
    if installed is None:
        return "fail", f"{UNIT} is not installed as a file systemd loads", f"{OWNER}: install it from the repo"
    n = changed_lines(installed, repo)
    names = ", ".join(label(Path(p).name) for p in dropins)
    if n:
        extra = f"; {len(dropins)} drop-in(s) on top: {names}" if dropins else ""
        return ("fail", f"installed {UNIT} differs from the repo's in {n} line(s){extra}",
                f"{OWNER}: install the repo unit (docs/CHECKS.md, palace-unit-drift)")
    if dropins or need_reload:
        parts = []
        if dropins:
            parts.append(f"{len(dropins)} drop-in(s) on top: {names}")
        if need_reload:
            parts.append("systemd needs a daemon-reload")
        return "warn", f"installed {UNIT} matches the repo, but " + "; ".join(parts), \
            f"{OWNER}: fold a lasting drop-in into the repo unit, or remove a finished trial"
    return "ok", f"installed {UNIT} matches the repo byte for byte, no drop-ins", OWNER


def body() -> None:
    props = show(UNIT)
    frag = props.get("FragmentPath", "")
    installed = Path(frag).read_text(encoding="utf-8") if frag and Path(frag).is_file() else None
    dropins = [p for p in props.get("DropInPaths", "").split() if p]
    state, fact, nxt = judge(installed, REPO_UNIT.read_text(encoding="utf-8"), dropins,
                             props.get("NeedDaemonReload", "no") == "yes")
    emit(NAME, line(NAME, state, fact, nxt))


if __name__ == "__main__":
    sys.exit(run(NAME, body))
