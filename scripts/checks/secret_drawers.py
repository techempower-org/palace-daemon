#!/usr/bin/env python3
"""CHECK secret-drawers: did a new secret reach the palace in the last day?

Every write is redacted (mempalace.redact), so a credential printed into a session arrives as
``[redacted <kind> sha256:<8 hex>]``. Transcripts are mined again as they grow, so one secret
recurs in many drawers; what matters is a hash the palace has never held before.

- **fail**: text the current rules would still redact, in a drawer filed after the rules last
  loaded (palace-daemon's start): a writer skipped redaction. Wings and kinds, never a value.
- **warn**: a hashed placeholder whose first appearance anywhere in the palace is inside the
  last ``WINDOW_HOURS``: a secret printed into a session was mined (wings and kinds named; the
  owner judges and rotates). "First mined" is not "first printed": the palace keeps no
  reliable print time (``authored_at`` is the mining time too), so an old transcript mined
  late shows here (2026-11-14: three of four were older lines mined after a reboot). Hashes
  of known-public values are listed in ``SECRET_CHECK_IGNORE`` (``kind:sha8`` items, local
  config; never the value) and skipped. Also warn: raw text in drawers filed *before* the rules last loaded, which
  a rule added since now matches (sweep candidates; 2026-11-14 the litellm shape matched 4).
- **ok** otherwise; the line still counts the window's unhashed redactions (``assignment``,
  ``code``, ``custom``: guessable values carry no hash, so they cannot be told apart).

The miss behind it (2026-11-14): a LiteLLM key fragment printed into a transcript was found by
a person reading the transcript, not by anything watching the palace.
"""

from __future__ import annotations

import collections
import datetime as dt
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import emit, label, line, read_var, run

NAME = "secret-drawers"
WINDOW_HOURS = 24
PLACEHOLDER = re.compile(r"\[redacted ([a-z]+)(?: sha256:[0-9a-f]{8})?\]")
HASHED_SQL = r"\[redacted ([a-z]+) sha256:([0-9a-f]{8})\]"
TABLES = ("mempalace_drawers", "mempalace_private_drawers")


def filed(stamp: str | None) -> dt.datetime | None:
    """A drawer's filed_at: "…Z" is UTC, a naive stamp is local time (both are written)."""
    if not stamp:
        return None
    try:
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


def ignored(spec: str) -> set[tuple[str, str]]:
    """``kind:sha8`` items (space separated) → {(kind, sha8)}; anything else is skipped."""
    out = set()
    for item in spec.split():
        kind, _, digest = item.partition(":")
        if re.fullmatch(r"[a-z]+", kind) and re.fullmatch(r"[0-9a-f]{8}", digest):
            out.add((kind, digest))
    return out


def new_secrets(
    rows: Iterable[tuple[str, str, str, str]],
    since: dt.datetime,
    ignore: set[tuple[str, str]] = frozenset(),
) -> list[tuple[str, list[str]]]:
    """[(kind, wings)] for each hash whose earliest drawer anywhere was filed at or after
    ``since``, from (kind, hash, wing, filed_at) rows. The minimum is taken over parsed times,
    never as text ("…Z" and naive local stamps do not sort together). A hash none of whose
    stamps parse is not called new: its first appearance is unknown."""
    first: dict[tuple[str, str], dt.datetime] = {}
    wings: dict[tuple[str, str], set] = collections.defaultdict(set)
    for kind, digest, wing, stamp in rows:
        if (kind, digest) in ignore:
            continue
        when = filed(stamp)
        key = (kind, digest)
        wings[key].add(wing)
        if when is not None and (key not in first or when < first[key]):
            first[key] = when
    return sorted(
        (kind, sorted(wings[(kind, digest)]))
        for (kind, digest), when in first.items()
        if when >= since
    )


def scan_window(
    rows: Iterable[tuple[str, str, str]], redact, rules_since: dt.datetime | None
):
    """(raw hits since the rules loaded, raw hits in older drawers, unhashed redactions, rows)
    from (wing, filed_at, document); the hits by (wing, kind). With ``rules_since`` unknown every
    raw hit counts as current."""
    raw: dict[tuple[str, str], int] = collections.Counter()
    older: dict[tuple[str, str], int] = collections.Counter()
    unhashed = 0
    n = 0
    for wing, stamp, doc in rows:
        n += 1
        doc = doc or ""
        unhashed += sum(
            1 for m in PLACEHOLDER.finditer(doc) if "sha256:" not in m.group(0)
        )
        cleaned = redact(doc)
        if cleaned == doc:
            continue
        added = collections.Counter(PLACEHOLDER.findall(cleaned)) - collections.Counter(
            PLACEHOLDER.findall(doc)
        )
        when = filed(stamp)
        bucket = older if rules_since and when and when < rules_since else raw
        for kind, k in (added or {"unknown": 1}).items():
            bucket[(wing, kind)] += k
    return raw, older, unhashed, n


def _by_wing(counts: dict[tuple[str, str], int]) -> str:
    wings: dict[str, dict[str, int]] = collections.defaultdict(collections.Counter)
    for (wing, kind), k in counts.items():
        wings[wing][kind] += k
    parts = []
    for wing in sorted(wings, key=lambda w: (-sum(wings[w].values()), w)):
        kinds = ",".join(
            f"{label(kind)}×{k}" for kind, k in sorted(wings[wing].items())
        )
        parts.append(f"{label(wing)} ({kinds})")
    return "; ".join(parts[:6]) + (
        f"; +{len(parts) - 6} wings" if len(parts) > 6 else ""
    )


def assess(new, raw, older, unhashed: int, n: int, since: dt.datetime) -> str:
    window = f"{n} drawers filed since {since:%a %H:%M}"
    if raw:
        return line(
            NAME,
            "fail",
            f"{sum(raw.values())} raw secret-shaped run(s) in {window}: {_by_wing(raw)}",
            "owner palace lane: redact those drawers in place and find the writer that skipped redaction",
        )
    warns = []
    if new:
        tally = collections.Counter()
        for kind, wings in new:
            for w in wings:
                tally[(w, kind)] += 1
        warns.append(
            f"{len(new)} secret(s) first mined into the palace (not necessarily printed today): {_by_wing(tally)}"
        )
    if older:
        warns.append(
            f"{sum(older.values())} run(s) in older drawers match a rule added since: {_by_wing(older)}"
        )
    if warns:
        nxt = "owner of each wing: rotate what was printed (palace lane names the drawers)"
        if older:
            nxt += "; palace lane: sweep the older drawers"
        return line(NAME, "warn", f"{'; '.join(warns)} ({window})", nxt)
    return line(
        NAME, "ok", f"0 new secrets, 0 raw in {window}; {unhashed} unhashed redactions"
    )


def daemon_start() -> dt.datetime | None:
    """When palace-daemon (whose writes are redacted with the rules it loaded) last started."""
    try:
        out = subprocess.run(
            [
                "systemctl",
                "show",
                "palace-daemon",
                "-p",
                "ActiveEnterTimestamp",
                "--timestamp=unix",
                "--value",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout.strip()
        return (
            dt.datetime.fromtimestamp(int(out.lstrip("@")), dt.timezone.utc)
            if out.startswith("@")
            else None
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def body() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=WINDOW_HOURS)
    try:
        import psycopg
        from mempalace.redact import redact
    except ImportError as exc:
        emit(
            NAME,
            line(
                NAME,
                "fail",
                f"cannot import {exc.name} (run with the palace-daemon venv)",
                "owner palace lane",
            ),
        )
        return
    # Only the DSN, read quietly from the daemon's env file: the unit never loads that file
    # (systemd logs a line it cannot parse in full, secrets included).
    dsn_file = Path(
        os.environ.get("PALACE_CHECKS_DSN_FILE") or "~/.config/palace-daemon/env"
    ).expanduser()
    dsn = read_var(dsn_file, "MEMPALACE_POSTGRES_DSN")
    if not dsn:
        emit(
            NAME,
            line(
                NAME,
                "fail",
                "no MEMPALACE_POSTGRES_DSN in the DSN file",
                "owner palace lane: set PALACE_CHECKS_DSN_FILE",
            ),
        )
        return
    rules_since = daemon_start()
    # filed_at is ISO text, "…Z" or naive local: fetch 14 h wide, then parse and cut exactly.
    stamp = (since - dt.timedelta(hours=14)).strftime("%Y-%m-%dT%H:%M")
    try:
        with psycopg.connect(dsn, connect_timeout=20) as conn:
            conn.read_only = True  # a check only reads
            conn.execute(
                "set statement_timeout = '150s'"
            )  # 4 statements fit the unit's 15 min
            hashed = []
            window = []
            for table in TABLES:
                cur = conn.execute(
                    f"select distinct m[1], m[2], wing, metadata->>'filed_at' from (select wing, metadata, "
                    f"regexp_matches(document, %s, 'g') m from {table} where document like '%%[redacted %%') x",
                    (HASHED_SQL,),
                )
                hashed += cur.fetchall()
                cur = conn.execute(
                    f"select wing, metadata->>'filed_at', document from {table} where metadata->>'filed_at' >= %s",
                    (stamp,),
                )
                window += [r for r in cur.fetchall() if (filed(r[1]) or now) >= since]
            raw, older, unhashed, n = scan_window(window, redact, rules_since)
    except Exception as exc:  # noqa: BLE001 -- a check reports, it never raises
        emit(
            NAME,
            line(
                NAME,
                "fail",
                f"query failed: {type(exc).__name__} (read {now.astimezone():%H:%M})",
                "owner palace lane",
            ),
        )
        return
    emit(
        NAME,
        assess(new_secrets(hashed, since), raw, older, unhashed, n, since.astimezone()),
    )


def main() -> int:
    return run(NAME, body)


if __name__ == "__main__":
    sys.exit(main())
