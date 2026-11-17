#!/usr/bin/env python3
"""CHECK dream-night: did last night's palace-dream run publish, exit 0, and have a judge?

Reads the published nights (``$DREAM_OUT``: ``YYYY-MM-DD.json``) and the unit's last result
(``systemctl --user show palace-dream.service``).

- fail: no published night; the newest day file is unreadable or has no aware generated_at;
  the newest is older than ``STALE_HOURS`` (the timer did not run, or published nothing)
- warn: the unit's state could not be read; its last run did not succeed (Result or exit);
  its last start was skipped by its start condition (ExecCondition) after the newest night;
  the contradictions pass recorded an error; the judge was off on each of the last
  ``OFF_NIGHTS`` nights; a generated_at in the future
- ok otherwise ("no run since boot" is fine when the newest night is fresh)

The miss behind it (2026-11-14): the judge had been off for nights with 60 pairs waiting, and
nobody saw it until a person asked why the Dreams tab never showed a contradiction. And
2026-11-16/17: the unit's MemAvailable ExecCondition skipped two 03:30 starts; a skip leaves no
exit timestamp, so the line read "ok ... no run since boot" until the night went stale.
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
from checklib import OWNER, emit, line, run

NAME = "palace-dream-night"
STALE_HOURS = 27
OFF_NIGHTS = 3
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
UNREADABLE = {"_unreadable": True}


def unit_result(props: str) -> dict[str, str]:
    """``systemctl show --timestamp=unix -p Result -p ExecMainStatus …`` output → dict."""
    out = {}
    for row in props.splitlines():
        if "=" in row:
            k, v = row.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _con(night: dict) -> dict:
    run_ = night.get("run") if isinstance(night, dict) else None
    steps = run_.get("steps") if isinstance(run_, dict) else None
    con = steps.get("contradictions") if isinstance(steps, dict) else None
    return con if isinstance(con, dict) else {}


# The errors the judge's pass records (urllib, sockets, the parser): named only from this list.
KNOWN_ERRORS = frozenset(
    {
        "URLError",
        "HTTPError",
        "TimeoutError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "ConnectionError",
        "RemoteDisconnected",
        "IncompleteRead",
        "OSError",
        "ValueError",
        "KeyError",
        "JSONDecodeError",
        "socket.timeout",
        "stopped",
    }
)
_ERRTYPE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]{0,60})\b")


def _num(v) -> str:
    return str(v) if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else "?"


def error_type(err) -> str:
    """An exception type name from a recorded error, or a fixed marker: never its text."""
    m = _ERRTYPE.match(str(err))
    return m.group(1) if m and m.group(1) in KNOWN_ERRORS else "an error"


def judge_state(con: dict) -> str:
    """One night's judge, in words."""
    if con.get("judge") == "off":
        return f"off ({_num(con.get('waiting'))} waiting, {_num(con.get('cached', 0))} cached)"
    if con.get("error"):
        return "error"
    if con.get("skipped"):
        return "skipped"
    return f"{_num(con.get('judged', 0))} judged, {_num(con.get('cached', 0))} cached"


SKIP_UNTIMED = dt.datetime.max.replace(tzinfo=dt.timezone.utc)


def skipped_at(unit: dict[str, str]) -> dt.datetime | None:
    """When the unit's last start was skipped by its ExecCondition (``Result=exec-condition``);
    None when it was not. The time is ``ConditionTimestamp`` (systemd stamps it at the start job
    the condition skipped: 11-17 03:30:39 live), else ``InactiveEnterTimestamp``, both
    ``@<unix>``. A skip whose time cannot be read is :data:`SKIP_UNTIMED`, never older than a
    night, so it warns rather than reading as recovered."""
    if unit.get("Result") != "exec-condition":
        return None
    for key in ("ConditionTimestamp", "InactiveEnterTimestamp"):
        m = re.fullmatch(r"@(\d{1,12})", unit.get(key, ""))
        if m:
            return dt.datetime.fromtimestamp(int(m.group(1)), dt.timezone.utc)
    return SKIP_UNTIMED


def assess(nights: list[dict], unit: dict[str, str], now: dt.datetime) -> str:
    """The line, from the published nights (newest first; an unreadable one as UNREADABLE) and
    the unit's last result (empty when it could not be read)."""
    if not nights:
        return line(
            NAME,
            "fail",
            "no published dream night",
            "owner palace lane: run palace-dream by hand, read its journal",
        )
    latest = nights[0]
    if latest is UNREADABLE or not isinstance(latest, dict):
        return line(
            NAME,
            "fail",
            "the newest published night is unreadable",
            "owner palace lane: read it, re-run palace-dream",
        )
    try:
        made = dt.datetime.fromisoformat(str(latest["generated_at"]))
    except (KeyError, ValueError):
        made = None
    if made is None or made.tzinfo is None:
        return line(
            NAME,
            "fail",
            f"night {latest.get('day')} has no timezone-aware generated_at",
            OWNER,
        )
    age_h = (now - made).total_seconds() / 3600
    con = _con(latest)
    props = latest.get("proposals")
    n_props = len(props) if isinstance(props, list) else "?"
    ran = unit.get("ExecMainExitTimestamp", "")
    status = unit.get("ExecMainStatus", "")
    skip = skipped_at(unit)
    if skip is SKIP_UNTIMED:
        exit_part = "last start skipped by its start condition (time unreadable)"
    elif skip is not None:
        when = skip.astimezone(now.tzinfo)
        exit_part = f"last start {when:%a %H:%M} skipped by its start condition"
    else:
        exit_part = f"exit {status}" if ran else "no run since boot"
    fact = (
        f"{latest.get('day')} published {made:%a %H:%M} ({age_h:.0f} h ago), {exit_part}, "
        f"{n_props} proposals, judge {judge_state(con)}"
    )
    if age_h > STALE_HOURS:
        return line(
            NAME,
            "fail",
            fact + f"; older than {STALE_HOURS} h",
            "owner palace lane: the timer did not publish; read journalctl --user -u palace-dream",
        )
    warns = []
    if age_h < -0.1:
        warns.append("generated_at is in the future")
    if unit.get("LoadState") != "loaded":
        warns.append("unit state unreadable or the unit is not loaded")
    elif skip is not None:
        # a night published after the skip (by hand) is the recovery; an exit timestamp kept
        # from an earlier run says nothing about this start, so the branch below is not used
        if skip >= made:
            warns.append("no night since the skip: read the unit's ExecCondition")
    elif ran and (unit.get("Result") not in ("success", None) or status != "0"):
        warns.append(f"last run {unit.get('Result')}, exit {status or '?'}")
    if con.get("error"):
        # the error's type only: its text could quote anything the judge was sent
        warns.append(f"judge error ({error_type(con['error'])})")
    recent = nights[:OFF_NIGHTS]
    if len(recent) == OFF_NIGHTS and all(_con(n).get("judge") == "off" for n in recent):
        warns.append(f"judge off {OFF_NIGHTS} nights running")
    if warns:
        return line(
            NAME,
            "warn",
            fact + "; " + "; ".join(warns),
            "owner palace lane: start the judge or route the pairs to another one",
        )
    return line(NAME, "ok", fact)


def read_nights(out: Path, keep: int = OFF_NIGHTS) -> list[dict]:
    """The newest ``keep`` day files, newest first; an unreadable one is UNREADABLE (so a corrupt
    newest night can never hide behind yesterday's)."""
    nights = []
    for p in sorted((p for p in out.iterdir() if _DAY.match(p.name)), reverse=True)[
        :keep
    ]:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            nights.append(doc if isinstance(doc, dict) else UNREADABLE)
        except (OSError, ValueError):
            nights.append(UNREADABLE)
    return nights


def body() -> None:
    out = Path(os.environ.get("DREAM_OUT", "")).expanduser()
    now = dt.datetime.now().astimezone()
    if not os.environ.get("DREAM_OUT") or not out.is_dir():
        emit(
            NAME,
            line(
                NAME, "fail", f"DREAM_OUT is not a directory (read {now:%H:%M})", OWNER
            ),
        )
        return
    try:
        p = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                "--timestamp=unix",
                "palace-dream.service",
                "-p",
                "LoadState",
                "-p",
                "Result",
                "-p",
                "ExecMainStatus",
                "-p",
                "ExecMainExitTimestamp",
                "-p",
                "ConditionTimestamp",
                "-p",
                "InactiveEnterTimestamp",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        props = p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        props = ""
    emit(NAME, assess(read_nights(out), unit_result(props), now))


def main() -> int:
    return run(NAME, body)


if __name__ == "__main__":
    sys.exit(main())
