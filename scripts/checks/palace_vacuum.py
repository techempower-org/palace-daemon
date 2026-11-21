#!/usr/bin/env python3
"""CHECK palace-vacuum: a manual ``VACUUM (ANALYZE)`` of named palace tables that the role's own
``statement_timeout`` cannot cancel.

The palace database role carries ``statement_timeout = 30s``, so the daemon's queries can never run
away. A manual VACUUM in a session of that role inherits the 30 s, and postgres cancels it: on
2026-11-21, five of seven VACUUMs died at 30 s, and the drawers table alone then took 63 min. So
this script lifts the timeout **for its own session only** (``SET``, never ``ALTER ROLE``) before
anything else, and adds a ``lock_timeout`` so it never queues behind an exclusive lock for long.

Usage, on the palace host (the run-book is docs/ops/manual-vacuum.md)::

    palace_vacuum.py [schema.table ...]     # default: public.mempalace_drawers

AGE graph tables keep their case: ``mempalace_kg.MENTIONS``. Each part must be a plain identifier;
anything else is refused before a connection opens.

- **ok**: every table vacuumed, and none has autovacuum switched off in its reloptions.
- **warn**: done, but autovacuum is off on a table (2026-11-17 it had been left off by a restore).
- **fail**: a name was refused, the DSN is missing, or postgres raised.

The DSN is read quietly from the daemon env file; no value or row ever reaches the line.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import emit, line, read_var  # noqa: E402

NAME = "palace-vacuum"
DEFAULT_TABLES = ("public.mempalace_drawers",)
ENV_FILE = Path("~/.config/palace-daemon/env").expanduser()
LOCK_TIMEOUT = "60s"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# The first statements of every session, in this order, before any VACUUM.
SESSION_SETTINGS = (
    "SET statement_timeout = 0",
    f"SET lock_timeout = '{LOCK_TIMEOUT}'",
)


def parse_table(name: str) -> tuple[str, str]:
    """``schema.table`` (or ``table``, meaning public) → its two parts; ValueError for anything else."""
    parts = name.split(".")
    if len(parts) == 1:
        parts = ["public", parts[0]]
    if len(parts) != 2 or not all(_IDENT.match(p) for p in parts):
        raise ValueError("not a plain schema.table name")
    return parts[0], parts[1]


def quoted(schema: str, table: str) -> str:
    return f'"{schema}"."{table}"'


def statements(tables) -> list[str]:
    """The session's statements in order: the settings first, then one VACUUM per table."""
    return [*SESSION_SETTINGS, *(f"VACUUM (ANALYZE) {quoted(*parse_table(t))}" for t in tables)]


def autovacuum_off(reloptions) -> bool:
    return any(str(o).replace(" ", "").lower() == "autovacuum_enabled=false" for o in (reloptions or ()))


def vacuum(conn, tables) -> list[tuple[str, int, bool, float]]:
    """Run the session on an autocommit connection; (table, dead tuples, autovacuum off, seconds) each."""
    sql = statements(tables)
    for s in sql[: len(SESSION_SETTINGS)]:
        conn.execute(s)
    # Read it back: the line says the timeout was off, so measure it, never assume it.
    shown = conn.execute("SHOW statement_timeout").fetchone()
    if not shown or str(shown[0]) != "0":
        raise RuntimeError("statement_timeout still set")
    out = []
    for t, s in zip(tables, sql[len(SESSION_SETTINGS):]):
        schema, table = parse_table(t)
        t0 = time.monotonic()
        conn.execute(s)
        secs = time.monotonic() - t0
        row = conn.execute(
            "SELECT s.n_dead_tup, c.reloptions FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid WHERE n.nspname = %s AND c.relname = %s",
            (schema, table),
        ).fetchone()
        dead, opts = (row or (0, None))
        out.append((f"{schema}.{table}", int(dead or 0), autovacuum_off(opts), secs))
    return out


def summary(results) -> tuple[str, str]:
    off = [r[0] for r in results if r[2]]
    parts = [f"{name} {f'{secs:.0f} s' if secs < 120 else f'{secs / 60:.0f} min'}, {dead:,} dead left"
             for name, dead, _off, secs in results]
    fact = f"VACUUM (ANALYZE) {len(results)} table(s), statement_timeout off for the session: " + "; ".join(parts)
    if off:
        return "warn", fact + f"; autovacuum OFF on {', '.join(off)}"
    return "ok", fact


def main(argv=None, connect=None) -> int:
    tables = list(argv if argv is not None else sys.argv[1:]) or list(DEFAULT_TABLES)
    t0 = time.monotonic()
    try:
        for t in tables:
            parse_table(t)
    except ValueError:
        emit(NAME, line(NAME, "fail", f"refused {len(tables)} table name(s): only plain schema.table",
                        "owner palace lane: fix the arguments"))
        return 0
    try:
        if connect is None:
            import psycopg

            dsn = read_var(ENV_FILE, "MEMPALACE_POSTGRES_DSN")
            if not dsn:
                raise LookupError("no DSN")
            connect = lambda: psycopg.connect(dsn, connect_timeout=20, autocommit=True)  # noqa: E731
        with connect() as conn:
            results = vacuum(conn, tables)
    except Exception as exc:  # noqa: BLE001 -- a check reports, it never raises
        emit(NAME, line(NAME, "fail", f"{type(exc).__name__} after {time.monotonic() - t0:.0f}s",
                        "owner palace lane: see the run-book, docs/ops/manual-vacuum.md"))
        return 0
    state, fact = summary(results)
    emit(NAME, line(NAME, state, fact, "owner palace lane" if state == "ok" else "owner palace lane: turn autovacuum back on"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
