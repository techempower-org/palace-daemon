"""Client-to-daemon path translation — extracted from main.py per #101 (eleventh slice).

Hooks running on a client machine (e.g. katana) speak in their own
filesystem namespace (``/home/jp/.claude/...``); the daemon may see the
same files at a different path (e.g. via Syncthing). When client and
daemon paths are identical (Syncthing to same absolute paths), the map
is identity — still set it so the mechanism is explicit.

Configured via the ``PALACE_DAEMON_PATH_MAP`` env var:

    PALACE_DAEMON_PATH_MAP="/home/jp/.claude/=/mnt/raid/claude-config/,..."

Comma-separated ``client_prefix=daemon_prefix`` entries, ordered first-
match-wins. Non-matching paths pass through unchanged so daemon-side
absolute paths still work.

main.py re-exports under ``_``-prefixed names so call sites in /mine,
/silent-save, and the watcher's parse_watch_dirs translator keep working.
"""
from __future__ import annotations

import os


# Sentinel for "no value passed" — distinguishes parse_path_map() (read env)
# from parse_path_map(None) (no mapping). Closes Copilot's test-isolation
# concern on jphein/palace-daemon#1: the previous None default coupled tests
# to whatever PALACE_DAEMON_PATH_MAP happened to be in the test process env.
PATH_MAP_USE_ENV: object = object()


def parse_path_map(raw=PATH_MAP_USE_ENV) -> list[tuple[str, str]]:
    """Parse PALACE_DAEMON_PATH_MAP into ordered (client_prefix, daemon_prefix) pairs.

    Format: comma-separated ``client_prefix=daemon_prefix`` entries. Whitespace
    around each token is stripped. Empty entries and entries missing ``=`` are
    skipped silently. Order is preserved so the operator can put more-specific
    prefixes first.

    Args:
        raw: When omitted, reads from ``PALACE_DAEMON_PATH_MAP``. Pass an
            explicit string (or ``""``/``None``) to bypass env entirely —
            tests use this to stay deterministic regardless of CI / dev env.

    Example::

        PALACE_DAEMON_PATH_MAP="/home/jp/.claude/=/home/jp/.claude/,/home/jp/Projects/=/home/jp/Projects/"
    """
    if raw is PATH_MAP_USE_ENV:
        raw = os.environ.get("PALACE_DAEMON_PATH_MAP", "")
    raw = (raw or "").strip()
    if not raw:
        return []
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        client_prefix, daemon_prefix = entry.split("=", 1)
        client_prefix = client_prefix.strip()
        daemon_prefix = daemon_prefix.strip()
        if client_prefix and daemon_prefix:
            pairs.append((client_prefix, daemon_prefix))
    return pairs


def translate_client_path(path: str) -> str:
    """Translate a client-side absolute path to a daemon-side path.

    Hooks running on a client machine (e.g. katana) speak in their own
    filesystem namespace (``/home/jp/.claude/...``); the daemon may see the
    same files at a different path (e.g. via Syncthing). When client and
    daemon paths are identical (Syncthing to same absolute paths), the map
    is identity — still set it so the mechanism is explicit.

    The first matching prefix wins; non-matching paths pass through
    unchanged so daemon-side absolute paths still work.

    Joining is normalized so mismatched trailing/leading slashes between
    the two prefixes can't produce mangled paths
    (Copilot finding on jphein/palace-daemon#1).
    """
    for client_prefix, daemon_prefix in parse_path_map():
        if path.startswith(client_prefix):
            suffix = path[len(client_prefix):]
            return daemon_prefix.rstrip("/") + "/" + suffix.lstrip("/")
    return path
