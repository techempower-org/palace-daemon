"""Warnings must reach the daemon's own logger, not the root logger (#289).

The daemon logs through ``palace-daemon`` (``main._log``) and
``palace-daemon.rooms`` (``rooms._log``). Two sites still called
``logging.warning(...)`` on the ROOT logger. With no root handler those lines
fall to ``lastResort`` → stderr → the journal, so nothing is lost under
systemd — but they bypass the daemon's handlers and formatter and are absent
from the stream anyone greps for daemon messages. #288 fixed the sibling at
``rooms.present_rooms``; these are the two that remained.

WHY THE NEGATIVE CONTROL IS THE LOAD-BEARING HALF. A handler that catches
everything would make "the record arrived" true no matter where it was logged,
and the assertion would prove nothing about routing. So the handler is attached
to ``palace-daemon`` ONLY, and one test asserts that a root-logger warning does
NOT reach it. Without that, every other assertion here is vacuous.

Records are identified by message CONTENT, never by line number (#289 quotes
line 3631; the fix moves it).
"""
import logging
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main
import rooms


class _Capture(logging.Handler):
    """Collects records reaching the logger it is attached to."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def messages(self):
        return [r.getMessage() for r in self.records]


class _DaemonLoggerCase(unittest.TestCase):
    """Attaches the capture to `palace-daemon` only — never to root."""

    def setUp(self):
        self.cap = _Capture()
        self.logger = logging.getLogger("palace-daemon")
        self.logger.addHandler(self.cap)
        self._lvl = self.logger.level
        self.logger.setLevel(logging.DEBUG)

    def tearDown(self):
        self.logger.removeHandler(self.cap)
        self.logger.setLevel(self._lvl)

    def assertCaptured(self, needle):
        hits = [m for m in self.cap.messages() if needle in m]
        self.assertTrue(
            hits, f"no record containing {needle!r} on 'palace-daemon'; got {self.cap.messages()}"
        )


class TestTheControlItself(_DaemonLoggerCase):
    def test_a_root_logger_warning_does_NOT_reach_the_daemon_handler(self):
        """The negative control. If this ever passes a root call through, every
        other assertion in this file becomes meaningless."""
        logging.warning("morpheus-289 control: emitted on the root logger")
        self.assertEqual(
            [m for m in self.cap.messages() if "morpheus-289 control" in m],
            [],
            "the capture is not selective — it sees root-logger records too",
        )

    def test_and_the_daemon_logger_DOES_reach_it(self):
        """The other half of the control: the handler works at all."""
        main._log.warning("morpheus-289 control: emitted on palace-daemon")
        self.assertCaptured("emitted on palace-daemon")

    def test_a_child_logger_propagates_to_it(self):
        """rooms logs on 'palace-daemon.rooms'; the capture sits on the parent."""
        rooms._log.warning("morpheus-289 control: emitted on palace-daemon.rooms")
        self.assertCaptured("emitted on palace-daemon.rooms")


class TestRoomsFallbackWarning(_DaemonLoggerCase):
    def test_canonical_rooms_fallback_logs_through_the_daemon_logger(self):
        """Driven through the real code path: make the lookup raise."""
        # The real failure this warning exists for: backend IS postgres and the
        # lookup fails. Without forcing the backend the function returns at the
        # `backend != "postgres"` branch above and never reaches the except —
        # the first version of this test asserted against a path it never ran.
        import mempalace.mcp_server as _mp

        rooms._canonical_rooms_cache = None
        with patch.object(_mp, "_config", MagicMock(backend="postgres")), patch.dict(
            os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql://x/y"}
        ), patch("psycopg2.connect", side_effect=RuntimeError("morpheus-289 simulated outage")):
            out = rooms.canonical_rooms()
        # DEFAULTS is a function-local in rooms.canonical_rooms, so assert the
        # fallback by its CONTENT (the spec's room names), not by importing a
        # symbol that does not exist at module scope.
        self.assertIn("decisions", out, "the fallback itself must still work")
        self.assertIn("problems", out)
        self.assertCaptured("canonical_rooms: lookup failed")
        rooms._canonical_rooms_cache = None


def _fake_subprocess(returncode=0, stdout=b"", stderr=b""):
    async def _spawn(*_a, **_k):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        proc.returncode = returncode
        return proc

    return _spawn


class TestMineNoOutputWarning(unittest.IsolatedAsyncioTestCase):
    """The main.py site, driven through the real POST /mine handler.

    Mirrors tests/test_mine_tunnels_flag.py's pattern: the subprocess is faked
    (empty stdout AND stderr, which is the branch under test), auth and path
    translation are stubbed. Nothing touches a palace.
    """

    async def asyncSetUp(self):
        self.cap = _Capture()
        self.logger = logging.getLogger("palace-daemon")
        self.logger.addHandler(self.cap)
        self._lvl = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.tmp = tempfile.TemporaryDirectory()
        self._patches = [
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_check_auth", side_effect=lambda *_a, **_k: None),
        ]
        for p in self._patches:
            p.start()
        self._orig_repair = dict(main._repair_state)
        main._repair_state["in_progress"] = False

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self.logger.removeHandler(self.cap)
        self.logger.setLevel(self._lvl)
        main._repair_state.clear()
        main._repair_state.update(self._orig_repair)
        self.tmp.cleanup()

    async def test_no_output_warning_goes_to_the_daemon_logger(self):
        from search_models import MineBody

        body = {"dir": self.tmp.name, "wing": "2g", "mode": "projects"}
        req = MagicMock()
        req.json = AsyncMock(return_value=body)
        req.app.state.active_mines = set()
        fake_cfg = MagicMock(backend="postgres", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), patch(
            "asyncio.create_subprocess_exec", side_effect=_fake_subprocess()
        ):
            result = await main.mine(req, MineBody(**body), x_api_key=None)

        self.assertIn("warning", result, "the no-output branch must be the one exercised")
        hits = [m for m in (r.getMessage() for r in self.cap.records)
                if "produced no output" in m]
        self.assertTrue(hits, f"no 'produced no output' record on 'palace-daemon'; "
                              f"got {[r.getMessage() for r in self.cap.records]}")


if __name__ == "__main__":
    unittest.main()
