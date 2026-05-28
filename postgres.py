"""Postgres connection helpers + JSON-RPC error mapping (#93/#96/#108) —
extracted from main.py per #101 refactor (fourth slice).

Owns:

- ``_DaemonToolError`` — exception with JSON-RPC error code + structured data.
- ``_RPC_*`` constants — standard + custom JSON-RPC error codes.
- ``postgres_dsn()`` — best-effort DSN lookup (env → mempalace config).
- ``require_postgres()`` — DSN-or-raise-BACKEND_DOWN helper.
- ``connect_postgres()`` — connect with OperationalError → BACKEND_DOWN
  mapping + automatic db_errors ring buffer recording.

main.py re-exports the names under their original ``_``-prefixed form so
existing tests + call sites keep working. Tests that patch
``main._postgres_dsn`` have been updated to patch ``postgres.postgres_dsn``
because the intra-module callers (``require_postgres``, ``connect_postgres``)
reach for the helper in their own namespace, bypassing main's re-export.
"""
from __future__ import annotations

import os


class _DaemonToolError(Exception):
    """JSON-RPC error from a daemon-native tool handler.

    Carries a JSON-RPC error code + optional structured data so the CLI
    can branch on failure mode (bad params vs backend down vs internal).
    """

    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


_RPC_INVALID_PARAMS = -32602   # standard
_RPC_BACKEND_DOWN = -32004     # custom — postgres unreachable / DSN missing
_RPC_INTERNAL = -32000         # standard


def postgres_dsn(_config_provider=None) -> "str | None":
    """Best-effort postgres DSN lookup for direct-SQL paths.

    Resolution order: ``MEMPALACE_POSTGRES_DSN`` env override, else
    ``mempalace.mcp_server._config.postgres_dsn`` attribute (None if
    that import / lookup fails). ``_config_provider`` is a callable
    returning the config-like object — injected for tests.
    """
    env_dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
    if env_dsn:
        return env_dsn
    try:
        if _config_provider is not None:
            cfg = _config_provider()
        else:
            import mempalace.mcp_server as _mp
            cfg = _mp._config
        return getattr(cfg, "postgres_dsn", None)
    except Exception:
        return None


def require_postgres():
    """Return the DSN or raise BACKEND_DOWN. Used by every daemon-native tool."""
    dsn = postgres_dsn()
    if not dsn:
        raise _DaemonToolError(
            _RPC_BACKEND_DOWN,
            "postgres backend not configured (set MEMPALACE_POSTGRES_DSN)",
        )
    return dsn


def connect_postgres(connect_timeout: int = 3):
    """Connect with proper BACKEND_DOWN error mapping for daemon-native tools.

    Without this helper, a connection-failed-to-establish error
    (postgres OOM-killed, network down, wrong DSN) raises
    psycopg2.OperationalError, which the /mcp dispatch's generic
    except-clause maps to -32000 INTERNAL. The CLI consumer then can't
    distinguish backend-down from an actual daemon bug.

    OperationalError is also recorded into the db_errors ring buffer so
    /health.db_errors stays populated.
    """
    dsn = require_postgres()
    import psycopg2
    try:
        return psycopg2.connect(dsn, connect_timeout=connect_timeout)
    except psycopg2.OperationalError as e:
        import db_errors
        db_errors.record_db_error(e)
        raise _DaemonToolError(
            _RPC_BACKEND_DOWN,
            f"postgres connection failed: {e}",
        )
