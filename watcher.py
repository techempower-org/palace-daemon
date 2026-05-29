"""
File-watcher service for palace-daemon.

Watches a configured set of (path, wing) pairs and triggers mempalace
mining when files inside those paths are created or modified. Lets the
daemon keep its corpus current without depending on hook fires.

Configuration is env-driven at startup; runtime add/remove requires
restart. Format::

    PALACE_WATCH_DIRS="/home/jp/Projects/realmwatch=wing_realmwatch,
                       /home/jp/Projects/oracle=wing_oracle"

The wing is optional — if omitted, the dirname's basename is normalized
via mempalace.config.normalize_wing_name (matches the local-spawn
behavior). ``path=wing`` and bare ``path`` entries can mix freely in
one env var.

Why watchdog rather than raw inotify: handles macOS / Windows too, has
a recursive observer out of the box, and provides debouncing primitives
through ``PatternMatchingEventHandler``. The Linux backend is
inotify-backed so the kernel-level efficiency is preserved.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # graceful degradation when the optional dep isn't installed
    Observer = None  # type: ignore[assignment]
    FileSystemEventHandler = object  # type: ignore[assignment, misc]
    FileSystemEvent = object  # type: ignore[assignment, misc]


_log = logging.getLogger("palace-daemon.watcher")

# Debounce window — when a file is modified, wait this long for
# additional events on the same path before triggering a mine. Catches
# editors that write-and-rename or that emit modify storms.
_DEBOUNCE_SECONDS = 2.0

# File extensions worth mining. Aligned with mempalace's READABLE_EXTENSIONS
# (kept here as a tighter local subset to avoid mining lock files, build
# artifacts, etc.). Anything outside this set is silently skipped at the
# event-handler level — the inotify watcher itself fires for everything.
_WATCHABLE_EXTENSIONS = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".swift",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".jsonl",
        ".sql",
        ".html",
        ".css",
        ".scss",
        ".vue",
        ".svelte",
    }
)


@dataclass
class WatchTarget:
    """A single (path, wing) pair to watch."""

    path: Path
    wing: str


def parse_watch_dirs(
    raw: str | None = None,
    translator: Callable[[str], str] | None = None,
) -> list[WatchTarget]:
    """Parse PALACE_WATCH_DIRS env var into WatchTarget list.

    Format: comma-separated entries; each entry is ``path`` or
    ``path=wing``. Entries pointing at non-existent paths are dropped
    with a warning log line so a misconfigured env doesn't kill startup.

    ``translator`` (optional) is applied to each path before the
    daemon-side ``is_dir()`` check. Operators may write watch paths in
    the client namespace (``/home/jp/Projects/...``); without translation
    those would be silently rejected as "not a directory" on the daemon
    even though the same files live at a different path via Syncthing.
    Closes Copilot finding on jphein/palace-daemon#2.
    """
    if raw is None:
        raw = os.environ.get("PALACE_WATCH_DIRS", "")
    raw = (raw or "").strip()
    if not raw:
        return []

    targets: list[WatchTarget] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" in entry:
            path_str, wing = entry.split("=", 1)
            path_str = path_str.strip()
            wing = wing.strip()
        else:
            path_str = entry
            wing = ""
        if not path_str:
            continue
        if translator is not None:
            path_str = translator(path_str)
        path = Path(path_str).expanduser().resolve()
        if not path.is_dir():
            _log.warning("PALACE_WATCH_DIRS: skipping %r (not a directory)", path_str)
            continue
        # palace-daemon#179: canonicalize wing at the parse boundary, not
        # at use time. Pre-#179 _internal_mine called _normalize_wing_slug
        # defensively right before spawning the subprocess; centralizing
        # the canonicalization here means WatchTarget objects always carry
        # canonical slugs. Path-derived wings already went through
        # normalize_wing_name; explicit env-provided wings now go through
        # the same path so ``path=Palace_Daemon`` and ``path=palace_daemon``
        # produce identical WatchTargets.
        try:
            from mempalace.config import normalize_wing_name

            if not wing:
                wing = normalize_wing_name(path.name)
            else:
                wing = normalize_wing_name(wing)
        except ImportError:
            if not wing:
                wing = path.name
            wing = wing.lower().replace(" ", "_").replace("-", "_")
        targets.append(WatchTarget(path=path, wing=wing))
    return targets


def _has_watchable_extension(path_str: str) -> bool:
    """Return True if the path has a suffix in the watch allowlist."""
    try:
        return Path(path_str).suffix.lower() in _WATCHABLE_EXTENSIONS
    except Exception:
        return False


class _DebouncedMineHandler(FileSystemEventHandler):
    """Watchdog handler that debounces mine triggers per directory.

    A burst of events from an editor's write+rename dance shouldn't fan
    out to N parallel mines. Instead, schedule a single mine per watch
    root, ``_DEBOUNCE_SECONDS`` after the most recent event.

    Subscribes only to write-shaped events (created/modified/moved/deleted);
    Linux watchdog 3.x emits ``opened``/``closed`` events on plain reads,
    and routing those through the debounce would re-mine the project on
    every file open. Closes Copilot finding on jphein/palace-daemon#2.
    """

    def __init__(self, target: WatchTarget, mine_fn: Callable[[str, str], None]):
        self._target = target
        self._mine_fn = mine_fn
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _maybe_schedule(self, event: FileSystemEvent, *, also_check_dest: bool = False) -> None:
        if event.is_directory:
            return
        # Editors save-via-rename: a temp filename (``foo.swp.tmp``) gets
        # renamed to the real filename (``foo.py``). The src_path is the
        # temp; the real change is on dest_path. For moves, allow either
        # side to satisfy the extension allowlist.
        if not _has_watchable_extension(event.src_path):
            if not (also_check_dest and _has_watchable_extension(getattr(event, "dest_path", ""))):
                return
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(_DEBOUNCE_SECONDS, self._fire)
            self._timer.daemon = True
            self._timer.start()

    # Specific event subscriptions — opened/closed are deliberately omitted.
    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event, also_check_dest=True)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._maybe_schedule(event)

    def cancel_pending(self) -> None:
        """Cancel any armed debounce timer.

        Called by WatcherService.stop() before observer teardown so an
        event right before shutdown doesn't fire _mine_fn after the
        daemon has begun teardown. Closes Copilot finding on
        jphein/palace-daemon#2.
        """
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _fire(self) -> None:
        try:
            self._mine_fn(str(self._target.path), self._target.wing)
        except Exception:
            _log.exception("watcher mine failed for %s", self._target.path)


class WatcherService:
    """Lifecycle wrapper around watchdog.Observer.

    Start in the FastAPI lifespan; stop in the lifespan teardown. Each
    WatchTarget gets a recursive watch and its own debounced handler.
    A failure scheduling one target (inotify watch limit, transient
    permission error) does NOT abort the others.
    """

    def __init__(self, mine_fn: Callable[[str, str], None]):
        self._mine_fn = mine_fn
        self._observer = None
        self._targets: list[WatchTarget] = []
        self._handlers: list[_DebouncedMineHandler] = []

    def start(self, targets: list[WatchTarget]) -> None:
        if Observer is None:
            _log.warning(
                "watchdog package not installed — file-watcher disabled. "
                "pip install watchdog>=3.0.0 to enable."
            )
            return
        if not targets:
            _log.info("PALACE_WATCH_DIRS empty — file-watcher idle.")
            return

        observer = Observer()
        scheduled: list[WatchTarget] = []
        handlers: list[_DebouncedMineHandler] = []
        for target in targets:
            handler = _DebouncedMineHandler(target, self._mine_fn)
            try:
                observer.schedule(handler, str(target.path), recursive=True)
            except Exception as e:
                # One unwatchable tree (e.g. inotify watch limit on a
                # large repo) shouldn't disable every other target.
                # Closes Copilot finding on jphein/palace-daemon#2.
                _log.warning("watcher: failed to schedule %s — %s", target.path, e)
                continue
            scheduled.append(target)
            handlers.append(handler)
            _log.info("watching %s → wing=%s", target.path, target.wing)

        if not scheduled:
            _log.warning("WatcherService: no targets scheduled successfully — staying idle.")
            return

        try:
            observer.start()
        except Exception:
            _log.exception("WatcherService observer.start() failed — staying idle.")
            return

        self._observer = observer
        self._targets = scheduled
        self._handlers = handlers
        _log.info("WatcherService started with %d target(s)", len(scheduled))

    def stop(self) -> None:
        if self._observer is None:
            return
        # Cancel armed debounce timers before tearing down the observer
        # so a file event right before shutdown doesn't fire _mine_fn
        # mid-teardown (Copilot finding on jphein/palace-daemon#2).
        for handler in self._handlers:
            try:
                handler.cancel_pending()
            except Exception:
                _log.exception("watcher: cancel_pending failed for %s", handler._target.path)
        try:
            self._observer.stop()
            self._observer.join(timeout=5.0)
        except Exception:
            _log.exception("WatcherService stop failed")
        finally:
            self._observer = None
            self._handlers = []

    @property
    def is_running(self) -> bool:
        """True only when at least one target is actively being observed."""
        return self._observer is not None and bool(self._targets)

    def list_targets(self) -> list[dict[str, str]]:
        return [{"path": str(t.path), "wing": t.wing} for t in self._targets]


def _log_future_exception(future: "concurrent.futures.Future") -> None:
    """Surface exceptions raised inside the scheduled mine coroutine.

    ``asyncio.run_coroutine_threadsafe`` returns a
    ``concurrent.futures.Future`` (NOT ``asyncio.Future``) — the
    callback receives the cross-thread variant. Catch its
    cancellation/state errors plus the asyncio variants so the
    callback can't itself crash on a concurrent cancellation.
    Closes Copilot finding on jphein/palace-daemon#3.
    """
    try:
        exc = future.exception()
    except (
        concurrent.futures.CancelledError,
        concurrent.futures.InvalidStateError,
        asyncio.CancelledError,
    ):
        return
    if exc is not None:
        _log.error("watcher-scheduled mine raised: %r", exc, exc_info=exc)


def make_async_mine_fn(loop: asyncio.AbstractEventLoop, internal_mine: Callable):
    """Build a sync mine callback that schedules an async daemon /mine call.

    The watchdog handler runs on a background thread, but the daemon's
    /mine subprocess + semaphore is async. Bridge by scheduling the
    coroutine onto the daemon's event loop via run_coroutine_threadsafe,
    and observe the returned Future so coroutine errors land in the log.
    """

    def _trigger(path: str, wing: str) -> None:
        _log.info("watcher fired mine: dir=%s wing=%s", path, wing)
        try:
            future = asyncio.run_coroutine_threadsafe(internal_mine(path, wing), loop)
        except Exception:
            _log.exception("scheduling internal_mine failed")
            return
        future.add_done_callback(_log_future_exception)

    return _trigger
