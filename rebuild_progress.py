"""Rebuild progress capture (palace-daemon#12) — extracted from main.py per #101 (tenth slice).

``mempalace.repair.rebuild_index`` prints "Staged N/M" and "Re-filed N/M"
style progress to stdout. The function doesn't accept a callback (filed
upstream as MemPalace/mempalace#1485), so the daemon captures stdout via
``contextlib.redirect_stdout`` and parses the lines into a dict that
``/repair/status`` exposes to operators.

Once mempalace#1485 lands and a new mempalace version is installed,
this can switch to a direct callback API and the regex parsing becomes
unnecessary.

main.py re-exports under ``_``-prefixed names so the /repair handler
keeps working unchanged.
"""
from __future__ import annotations

import contextlib
import io
import re
import time
from typing import Any


REBUILD_RE_STAGED = re.compile(r"Staged\s+(\d+)/(\d+)")
REBUILD_RE_REFILED = re.compile(r"Re-filed\s+(\d+)/(\d+)")
REBUILD_RE_FOUND = re.compile(r"Drawers found:\s+(\d+)")


def make_rebuild_progress_state() -> dict[str, Any]:
    """Initial progress dict, exposed via /repair/status during a rebuild."""
    return {
        "phase": "starting",
        "completed": 0,
        "expected": 0,
        "rate_per_sec": 0.0,
        "eta_seconds": None,
        "elapsed_seconds": 0.0,
        "last_message": "",
        "started_at_monotonic": time.monotonic(),
    }


class RebuildProgressBuffer(io.TextIOBase):
    """Stdout sink for rebuild_index that parses progress lines into a dict.

    Lives in the executor thread (rebuild runs synchronously there) but
    writes a small set of int/float/str fields into ``state`` — Python's
    GIL makes individual dict assignments safe enough for the /repair/status
    handler running on the main asyncio thread to read.
    """

    def __init__(self, state: dict[str, Any]):
        super().__init__()
        self._state = state
        self._buf = ""

    def writable(self) -> bool:  # io.TextIOBase contract
        return True

    def write(self, s: str) -> int:
        # Buffer + split on newlines so we don't parse partial lines.
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._handle_line(line)
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._handle_line(self._buf)
            self._buf = ""

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self._state["last_message"] = line
        m = REBUILD_RE_STAGED.search(line)
        if m:
            self._update_phase("stage", int(m.group(1)), int(m.group(2)))
            return
        m = REBUILD_RE_REFILED.search(line)
        if m:
            self._update_phase("refile", int(m.group(1)), int(m.group(2)))
            return
        m = REBUILD_RE_FOUND.search(line)
        if m:
            self._state["expected"] = int(m.group(1))
            self._state["phase"] = "extracting"
            return
        # Other status messages — let the operator see them via last_message.

    def _update_phase(self, phase: str, completed: int, expected: int) -> None:
        elapsed = time.monotonic() - self._state["started_at_monotonic"]
        rate = completed / elapsed if elapsed > 0 else 0.0
        remaining = max(0, expected - completed)
        # "Refile" phase costs an additional re-embed of the full set; reflect
        # that in the ETA by doubling the remaining work when we're in stage.
        # (See MemPalace/mempalace#1486 — temp-collection atomicity double-pass.)
        if phase == "stage":
            work_remaining = remaining + expected
        else:  # refile
            work_remaining = remaining
        eta = work_remaining / rate if rate > 0 else None
        self._state["phase"] = phase
        self._state["completed"] = completed
        self._state["expected"] = expected
        self._state["elapsed_seconds"] = round(elapsed, 1)
        self._state["rate_per_sec"] = round(rate, 2)
        self._state["eta_seconds"] = round(eta, 0) if eta is not None else None


@contextlib.contextmanager
def capture_rebuild_progress(state: dict[str, Any]):
    """Redirect stdout to a parser that updates ``state`` while we're inside.

    Used around the run_in_executor(rebuild_index) call so the executor
    thread's stdout flows through RebuildProgressBuffer instead of the
    default sys.stdout (which would go to journald, mixed in with other
    daemon output).
    """
    buf = RebuildProgressBuffer(state)
    with contextlib.redirect_stdout(buf):
        try:
            yield buf
        finally:
            buf.flush()
