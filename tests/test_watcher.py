"""Unit tests for the file-watcher service.

Run with::

    python -m unittest tests.test_watcher -v

Tests are pure-function: parse_watch_dirs() reads the env var and
returns a list, with no observer threads or filesystem watching
involved. The Observer-side debouncing is tested in isolation via
the _DebouncedMineHandler class — instantiated with a fake mine
callback, fed events, asserted on call count after the debounce
window has passed.
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import watcher as _watcher  # noqa: E402


class TestParseWatchDirs(unittest.TestCase):
    def setUp(self):
        # Use the test-class temp dir so created paths exist for the
        # is_dir() guard in parse_watch_dirs.
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_returns_empty(self):
        self.assertEqual(_watcher.parse_watch_dirs(""), [])
        self.assertEqual(_watcher.parse_watch_dirs("   "), [])

    def test_path_only_derives_wing(self):
        d = os.path.join(self.tmp, "my-project")
        os.mkdir(d)
        targets = _watcher.parse_watch_dirs(d)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].wing, "my_project")
        self.assertEqual(str(targets[0].path), d)

    def test_path_with_explicit_wing(self):
        d = os.path.join(self.tmp, "anything")
        os.mkdir(d)
        targets = _watcher.parse_watch_dirs(f"{d}=wing_specific")
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].wing, "wing_specific")

    def test_multiple_entries(self):
        d1 = os.path.join(self.tmp, "alpha")
        d2 = os.path.join(self.tmp, "beta")
        os.mkdir(d1)
        os.mkdir(d2)
        targets = _watcher.parse_watch_dirs(f"{d1}=wing_a, {d2}")
        self.assertEqual(len(targets), 2)
        wings = {t.wing for t in targets}
        self.assertEqual(wings, {"wing_a", "beta"})

    def test_skips_nonexistent_paths(self):
        d = os.path.join(self.tmp, "exists")
        os.mkdir(d)
        missing = os.path.join(self.tmp, "missing-dir-never-created")
        targets = _watcher.parse_watch_dirs(f"{d}=ok,{missing}=bad")
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].wing, "ok")

    def test_skips_files(self):
        f = os.path.join(self.tmp, "regular-file")
        open(f, "w").close()
        targets = _watcher.parse_watch_dirs(f"{f}=should_skip")
        self.assertEqual(targets, [])

    def test_reads_env_var_when_no_arg(self):
        d = os.path.join(self.tmp, "from-env")
        os.mkdir(d)
        with patch.dict(os.environ, {"PALACE_WATCH_DIRS": f"{d}=env_wing"}, clear=True):
            targets = _watcher.parse_watch_dirs()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].wing, "env_wing")

    def test_explicit_wing_canonicalized_at_parse_time(self):
        """palace-daemon#179: wing canonicalization at the parse boundary
        (was use-time in _internal_mine pre-#179). ``Palace_Daemon`` and
        ``palace_daemon`` env entries must produce identical WatchTargets
        so post-#175 reads can find the drawers they wrote.

        normalize_wing_name lowercases + replaces spaces/dashes with
        underscores; absent the dep (test environments without mempalace
        installed) the fallback inside parse_watch_dirs still lowercases
        and underscores. Either way, both inputs canonicalize equal.
        """
        d = os.path.join(self.tmp, "canon")
        os.mkdir(d)
        targets_a = _watcher.parse_watch_dirs(f"{d}=Palace_Daemon")
        targets_b = _watcher.parse_watch_dirs(f"{d}=palace_daemon")
        self.assertEqual(targets_a[0].wing, targets_b[0].wing)
        self.assertEqual(targets_a[0].wing, "palace_daemon")


class TestDebouncedMineHandler(unittest.TestCase):
    """Integration-ish: instantiate the real handler, feed events, watch
    the debounce timer fire."""

    def _make_event(self, src_path: str, is_directory: bool = False, dest_path: str = ""):
        # Real watchdog event objects need the watchdog dep. Use a
        # minimal stand-in that satisfies the handler's attribute reads.
        class _E:
            def __init__(self):
                self.src_path = src_path
                self.is_directory = is_directory
                self.dest_path = dest_path

        return _E()

    def test_single_event_fires_after_debounce(self):
        target = _watcher.WatchTarget(path=_watcher.Path("/x"), wing="w")
        fired: list[tuple[str, str]] = []
        cv = threading.Condition()

        def mine_fn(p, w):
            with cv:
                fired.append((p, w))
                cv.notify_all()

        # Shorten the debounce so the test stays fast.
        original = _watcher._DEBOUNCE_SECONDS
        _watcher._DEBOUNCE_SECONDS = 0.05
        try:
            handler = _watcher._DebouncedMineHandler(target, mine_fn)
            handler.on_modified(self._make_event("/x/foo.md"))
            with cv:
                cv.wait(timeout=1.0)
            self.assertEqual(fired, [("/x", "w")])
        finally:
            _watcher._DEBOUNCE_SECONDS = original

    def test_burst_collapses_to_one_fire(self):
        """A storm of events on the same target produces exactly one mine."""
        target = _watcher.WatchTarget(path=_watcher.Path("/x"), wing="w")
        fired: list[tuple[str, str]] = []
        cv = threading.Condition()

        def mine_fn(p, w):
            with cv:
                fired.append((p, w))
                cv.notify_all()

        original = _watcher._DEBOUNCE_SECONDS
        _watcher._DEBOUNCE_SECONDS = 0.05
        try:
            handler = _watcher._DebouncedMineHandler(target, mine_fn)
            for i in range(20):
                handler.on_modified(self._make_event(f"/x/file{i}.py"))
                time.sleep(0.005)
            with cv:
                cv.wait(timeout=1.0)
            self.assertEqual(len(fired), 1)
        finally:
            _watcher._DEBOUNCE_SECONDS = original

    def test_skips_directory_events(self):
        target = _watcher.WatchTarget(path=_watcher.Path("/x"), wing="w")
        fired: list[tuple[str, str]] = []
        original = _watcher._DEBOUNCE_SECONDS
        _watcher._DEBOUNCE_SECONDS = 0.05
        try:
            handler = _watcher._DebouncedMineHandler(target, lambda p, w: fired.append((p, w)))
            handler.on_modified(self._make_event("/x/subdir", is_directory=True))
            time.sleep(0.15)
            self.assertEqual(fired, [])
        finally:
            _watcher._DEBOUNCE_SECONDS = original

    def test_skips_unwatched_extensions(self):
        target = _watcher.WatchTarget(path=_watcher.Path("/x"), wing="w")
        fired: list[tuple[str, str]] = []
        original = _watcher._DEBOUNCE_SECONDS
        _watcher._DEBOUNCE_SECONDS = 0.05
        try:
            handler = _watcher._DebouncedMineHandler(target, lambda p, w: fired.append((p, w)))
            # Extensions not in _WATCHABLE_EXTENSIONS — editor swap, lock,
            # build artifact, binary.
            for path in ("/x/file.swp", "/x/file.lock", "/x/file.pyc", "/x/file.png"):
                handler.on_modified(self._make_event(path))
            time.sleep(0.15)
            self.assertEqual(fired, [])
        finally:
            _watcher._DEBOUNCE_SECONDS = original

    def test_rename_to_watched_extension_fires(self):
        """Editor save-via-rename: src_path is a temp filename (no
        watchable extension), dest_path is the real filename. Closes
        Copilot finding on jphein/palace-daemon#2 — moved events must
        check dest_path too."""
        target = _watcher.WatchTarget(path=_watcher.Path("/x"), wing="w")
        fired: list[tuple[str, str]] = []
        cv = threading.Condition()

        def mine_fn(p, w):
            with cv:
                fired.append((p, w))
                cv.notify_all()

        original = _watcher._DEBOUNCE_SECONDS
        _watcher._DEBOUNCE_SECONDS = 0.05
        try:
            handler = _watcher._DebouncedMineHandler(target, mine_fn)
            handler.on_moved(
                self._make_event("/x/.notes.md.swp.tmp", dest_path="/x/notes.md")
            )
            with cv:
                cv.wait(timeout=1.0)
            self.assertEqual(fired, [("/x", "w")])
        finally:
            _watcher._DEBOUNCE_SECONDS = original

    def test_cancel_pending_drops_armed_timer(self):
        """WatcherService.stop() cancels in-flight debounce timers so an
        event right before shutdown doesn't fire after teardown. Closes
        Copilot finding on jphein/palace-daemon#2."""
        target = _watcher.WatchTarget(path=_watcher.Path("/x"), wing="w")
        fired: list[tuple[str, str]] = []
        original = _watcher._DEBOUNCE_SECONDS
        _watcher._DEBOUNCE_SECONDS = 0.1
        try:
            handler = _watcher._DebouncedMineHandler(target, lambda p, w: fired.append((p, w)))
            handler.on_modified(self._make_event("/x/foo.md"))
            handler.cancel_pending()
            time.sleep(0.25)
            self.assertEqual(fired, [])
        finally:
            _watcher._DEBOUNCE_SECONDS = original


class TestWatcherServiceFailureIsolation(unittest.TestCase):
    """A schedule() failure on one target must not abort the others.

    Closes Copilot finding on jphein/palace-daemon#2 — the bare for-loop
    used to let an exception in observer.schedule() fall out and disable
    the entire service.
    """

    def test_one_failed_target_doesnt_disable_others(self):
        # Stub observer that fails on the second schedule() call but
        # accepts the others. We don't import the real Observer because
        # we don't want a kernel inotify watch in unit tests.
        scheduled: list[str] = []

        class _StubObserver:
            def __init__(self):
                self._n = 0

            def schedule(self, handler, path, recursive=True):
                self._n += 1
                if self._n == 2:
                    raise OSError("simulated inotify watch limit")
                scheduled.append(path)

            def start(self):
                pass

            def stop(self):
                pass

            def join(self, timeout=None):
                pass

        targets = [
            _watcher.WatchTarget(path=_watcher.Path("/a"), wing="a"),
            _watcher.WatchTarget(path=_watcher.Path("/b"), wing="b"),
            _watcher.WatchTarget(path=_watcher.Path("/c"), wing="c"),
        ]
        with patch.object(_watcher, "Observer", _StubObserver):
            svc = _watcher.WatcherService(lambda p, w: None)
            svc.start(targets)
            self.assertTrue(svc.is_running)
            self.assertEqual(len(svc.list_targets()), 2)
            self.assertEqual({t["wing"] for t in svc.list_targets()}, {"a", "c"})
            svc.stop()


class TestParseWatchDirsTranslator(unittest.TestCase):
    """Translator runs before the is_dir() check so client-namespace
    watch paths reach the daemon's filesystem view (Copilot finding
    on jphein/palace-daemon#2)."""

    def test_translator_runs_before_is_dir(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            real_path = os.path.join(tmp, "daemon-side")
            os.mkdir(real_path)
            # Pretend the env var lists a client-namespace path that doesn't
            # exist on this filesystem; translator rewrites to the real one.
            client_path = "/CLIENT/projects/whatever"

            def _translate(p: str) -> str:
                return real_path if p == client_path else p

            with patch.dict(os.environ, {}, clear=True):
                targets = _watcher.parse_watch_dirs(
                    raw=f"{client_path}=mywing", translator=_translate
                )
        self.assertEqual(len(targets), 1)
        self.assertEqual(str(targets[0].path), real_path)
        self.assertEqual(targets[0].wing, "mywing")


if __name__ == "__main__":
    unittest.main()
