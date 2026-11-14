"""Shared by the palace lane's checks: one line in a fixed shape, written where the lead reads it.

    CHECK <name> <ok|warn|fail> · <the measured fact, with its number and time> · <next / owner>

A check reports; it never gates: every check exits 0 and its state is in the line. The line goes
to stdout (the unit's journal) and to ``$PALACE_CHECKS_DIR/<name>.line`` (default
``~/Projects/palace-daemon/scratch/checks``), replaced atomically.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

STATES = ("ok", "warn", "fail")
OWNER = "owner palace lane"


MAX_FIELD = 700
_SPACE = re.compile(r"\s+")


def clean(text: object, limit: int = MAX_FIELD) -> str:
    """One field of a line: whitespace (newlines too) collapsed, bounded."""
    text = _SPACE.sub(" ", str(text)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def label(text: object) -> str:
    """A name from data (a wing, a kind): only [A-Za-z0-9_.-], bounded."""
    return re.sub(r"[^A-Za-z0-9_.-]", "?", str(text))[:48] or "?"


def line(name: str, state: str, fact: str, nxt: str = OWNER) -> str:
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    return f"CHECK {name} {state} · {clean(fact)} · {clean(nxt, 200)}"


def checks_dir() -> Path:
    default = Path.home() / "Projects" / "palace-daemon" / "scratch" / "checks"
    return Path(os.environ.get("PALACE_CHECKS_DIR") or default)


def emit(name: str, text: str) -> None:
    """Save the line, then print exactly one line (marked when the save failed). Never raises."""
    try:
        out = checks_dir()
        out.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=out, prefix=f".{name}.")
        with os.fdopen(fd, "w") as f:
            f.write(text + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, out / f"{name}.line")
    except OSError as exc:
        text += f" [line not saved: {type(exc).__name__}]"
    try:
        print(text, flush=True)
    except OSError:
        pass


def run(name: str, body) -> int:
    """Run a check's body; anything it raises becomes its fail line. Always 0."""
    try:
        body()
    except Exception as exc:  # noqa: BLE001 -- a check reports, it never raises
        emit(
            name,
            line(
                name,
                "fail",
                f"the check itself raised {type(exc).__name__}",
                "owner palace lane: fix the check",
            ),
        )
    return 0


def read_var(path: Path, name: str) -> str:
    """One ``NAME=value`` (or ``export NAME=value``) from an env file, read quietly: the file is
    never handed to systemd, which logs a line it cannot parse in full (2026-11-14)."""
    try:
        for row in path.read_text(encoding="utf-8").splitlines():
            row = row.strip()
            if row.startswith("export "):
                row = row[7:].lstrip()
            if row.startswith(name + "="):
                return row.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""
