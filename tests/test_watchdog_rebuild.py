"""Tests for the watchdog keepalive-during-rebuild fix.

``_watchdog_loop`` is health-gated: it withholds ``WATCHDOG=1`` whenever the
palace ``_get_collection()`` probe returns None or throws, so a genuinely
wedged daemon gets killed by systemd. But a ``mode=rebuild`` repair holds
``_exclusive_palace()`` with the client/collection caches nulled for the whole
(6-9h) operation, so that probe would return None and starve the keepalive —
SIGABRT-ing the daemon mid-rebuild.

The fix (#135, tightened in review #345 r2): during ``in_progress + mode=rebuild``
skip the /health probe, but ping only while the rebuild keeps printing (within
``PALACE_REBUILD_STALL_S``) and ``/repair/status``, which never touches the
collection, still answers. Outside a rebuild, /health gates the ping.

These drive exactly one loop iteration by stubbing ``asyncio.sleep`` to return
once and then raise ``CancelledError`` (which the loop catches to exit). No
live daemon, palace, or systemd socket required — ``_sd_notify`` is mocked.

Run with::

    python -m unittest tests.test_watchdog_rebuild -v
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402
import sd_watchdog  # noqa: E402


def _one_iteration_sleep():
    """An asyncio.sleep stand-in: first await returns, second cancels the loop.

    Lets _watchdog_loop run exactly one body iteration, then exit via the
    CancelledError its own ``try/except`` around sleep swallows with a return.
    """
    calls = {"n": 0}

    async def _sleep(_secs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        raise asyncio.CancelledError()

    return _sleep


class TestWatchdogRebuild(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._orig_repair = dict(main._repair_state)

    async def asyncTearDown(self):
        main._repair_state.clear()
        main._repair_state.update(self._orig_repair)

    def _rebuilding(self, quiet_s):
        import time
        main._repair_state["in_progress"] = True
        main._repair_state["mode"] = "rebuild"
        main._repair_state["progress"] = {"beat": 7, "beat_at": time.monotonic() - quiet_s,
                                          "started_at_monotonic": time.monotonic() - 3600}

    async def test_a_progressing_rebuild_pings_without_touching_the_collection(self):
        self._rebuilding(quiet_s=420)                   # one 5000-drawer batch at ~11/s: a healthy gap
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(sd_watchdog, "self_health", return_value=200) as probe:
            await main._watchdog_loop(180)
        notify.assert_called_once_with("WATCHDOG=1\n")
        self.assertEqual(probe.call_args.kwargs["path"], "/repair/status")

    async def test_a_hung_rebuild_stops_the_pings(self):
        # review #345 r2: a daemon hung mid-rebuild must not keep pinging
        self._rebuilding(quiet_s=sd_watchdog.rebuild_stall_s() + 60)
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(sd_watchdog, "self_health", return_value=200):
            await main._watchdog_loop(180)
        notify.assert_not_called()

    async def test_a_rebuild_whose_request_path_is_dead_stops_the_pings(self):
        self._rebuilding(quiet_s=5)
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(sd_watchdog, "self_health", return_value=None):
            await main._watchdog_loop(180)
        notify.assert_not_called()

    def _two_ticks(self, clock_values):
        """Run two loop iterations with sd_watchdog's clock reading ``clock_values`` in turn."""
        calls = {"n": 0}

        async def _sleep(_secs):
            calls["n"] += 1
            if calls["n"] <= 2:
                return None
            raise asyncio.CancelledError()

        return (patch("asyncio.sleep", side_effect=_sleep),
                patch.object(sd_watchdog, "_clock", side_effect=list(clock_values)))

    async def test_a_rebuild_without_progress_fails_closed(self):
        # review #345 r5: progress None, or a dict without its timestamps, used to read as "quiet 0 s" and ping
        # forever; now it counts as quiet since the watchdog first saw the rebuild, and the budget applies
        budget = sd_watchdog.rebuild_stall_s()
        for progress in (None, {"beat": 3}, {"beat_at": "soon", "started_at_monotonic": None}):
            main._repair_state.update(in_progress=True, mode="rebuild", progress=progress)
            sleep_p, clock_p = self._two_ticks([1000.0, 1000.0, 1000.0 + budget + 61])
            with sleep_p, clock_p, \
                 patch.object(main, "_sd_notify") as notify, \
                 patch.object(sd_watchdog, "self_health", return_value=200):
                await main._watchdog_loop(180)
            # first tick: just seen, quiet 0, pings; second tick: past the budget, no ping
            notify.assert_called_once_with("WATCHDOG=1\n")

    def test_rebuild_quiet_without_any_time_is_infinite(self):
        self.assertEqual(sd_watchdog.rebuild_quiet_s(None, now=5.0), float("inf"))
        self.assertEqual(sd_watchdog.rebuild_quiet_s({"beat_at": True}, now=5.0, since=2.0), 3.0)

    def test_every_rebuild_line_is_a_heartbeat(self):
        from rebuild_progress import RebuildProgressBuffer, make_rebuild_progress_state
        state = make_rebuild_progress_state()
        state["beat_at"] -= 999
        RebuildProgressBuffer(state).write("  Extracting drawers...\n")
        self.assertEqual(state["beat"], 1)
        self.assertLess(sd_watchdog.rebuild_quiet_s(state), 5)

    async def test_healthy_outside_rebuild_probes_and_pings(self):
        main._repair_state["in_progress"] = False
        main._repair_state["mode"] = None
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(sd_watchdog, "self_health", return_value=200) as probe:
            await main._watchdog_loop(20)
        probe.assert_called_once()
        notify.assert_called_once_with("WATCHDOG=1\n")

    async def test_unavailable_outside_rebuild_withholds_keepalive(self):
        """The health-gate still bites outside a rebuild: a None collection
        means the daemon is wedged, so we deliberately let systemd kill it."""
        main._repair_state["in_progress"] = False
        main._repair_state["mode"] = None
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(sd_watchdog, "self_health", return_value=None) as probe:
            await main._watchdog_loop(20)
        probe.assert_called_once()
        notify.assert_not_called()

    async def test_non_rebuild_repair_still_health_gated(self):
        """A non-rebuild repair (e.g. light/scan/prune) does NOT get the
        unconditional keepalive — only mode=rebuild nulls the caches, so
        other modes keep the normal probe."""
        main._repair_state["in_progress"] = True
        main._repair_state["mode"] = "light"
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(sd_watchdog, "self_health", return_value=200) as probe:
            await main._watchdog_loop(20)
        probe.assert_called_once()
        notify.assert_called_once_with("WATCHDOG=1\n")


class TestWatchdogRequestPath(unittest.IsolatedAsyncioTestCase):
    """#345 r1: the keepalive proves the request path (a served /health), at most every 60 s, from this process."""

    def test_tick_is_a_third_capped_at_a_minute(self):
        self.assertEqual(sd_watchdog.watchdog_tick(180), 60)
        self.assertEqual(sd_watchdog.watchdog_tick(90), 30)
        self.assertEqual(sd_watchdog.watchdog_tick(600), 60)
        self.assertEqual(sd_watchdog.watchdog_tick(12), 10)

    async def _serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        return server, server.sockets[0].getsockname()[1]

    async def test_a_served_health_answer_is_its_status(self):
        async def ok(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\n{}")
            await writer.drain()
            writer.close()
        server, port = await self._serve(ok)
        async with server:
            self.assertEqual(await sd_watchdog.self_health(port, timeout=2), 200)

    async def test_a_stalled_request_path_gets_no_status_in_time(self):
        async def stall(reader, writer):
            await reader.read()                         # accepts, never answers (the 11-19 hang) until the client hangs up
            writer.close()
        server, port = await self._serve(stall)
        async with server:
            self.assertIsNone(await sd_watchdog.self_health(port, timeout=0.3))

    async def test_nothing_listening_is_no_status(self):
        server, port = await self._serve(lambda r, w: None)
        server.close()
        await server.wait_closed()
        self.assertIsNone(await sd_watchdog.self_health(port, timeout=0.3))

    async def test_a_stalled_health_withholds_the_keepalive(self):
        main._repair_state["in_progress"] = False
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(sd_watchdog, "self_health", return_value=None):
            await main._watchdog_loop(180)
        notify.assert_not_called()

    async def test_a_503_still_pings(self):
        main._repair_state["in_progress"] = False
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(sd_watchdog, "self_health", return_value=503):
            await main._watchdog_loop(180)
        notify.assert_called_once_with("WATCHDOG=1\n")

    def test_startup_keepalive_feeds_through_a_blocked_event_loop(self):
        # review #345 r2: a synchronous lifespan step blocks the loop; a 200 s one must survive. Scaled: tick
        # 0.05 s, the loop blocked 0.4 s by a plain time.sleep, with no await anywhere.
        import time
        sent = []
        with patch.object(main, "_sd_notify", side_effect=sent.append):
            stop = sd_watchdog.startup_keepalive(180, tick=0.05)
            time.sleep(0.4)
            stop.set()
            time.sleep(0.1)
            fed = len(sent)
            time.sleep(0.2)
        self.assertGreaterEqual(fed, 5)
        self.assertEqual(len(sent), fed)                # stopped means stopped

    def test_startup_keepalive_is_bounded(self):
        import time
        sent = []
        with patch.object(main, "_sd_notify", side_effect=sent.append):
            sd_watchdog.startup_keepalive(180, max_secs=0.12, tick=0.05)
            time.sleep(0.4)
        self.assertLessEqual(len(sent), 3)              # a startup past max_secs stops being fed


class TestNotifySender(unittest.TestCase):
    """#345 r1 (3): NotifyAccess=main accepts only the main pid; the keepalive is sent from this process."""

    def test_the_datagram_carries_this_process_pid(self):
        import socket
        import struct
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "notify")
        rx = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            rx.bind(path)
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
            with patch.dict(os.environ, {"NOTIFY_SOCKET": path}):
                sd_watchdog.sd_notify("WATCHDOG=1\n")
            msg, anc, _flags, _addr = rx.recvmsg(64, socket.CMSG_SPACE(12))
            self.assertEqual(msg, b"WATCHDOG=1\n")
            creds = [data for level, kind, data in anc if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS]
            pid, _uid, _gid = struct.unpack("iII", creds[0][:12])
            self.assertEqual(pid, os.getpid())
        finally:
            rx.close()
            os.remove(path)
            os.rmdir(d)

    def test_only_the_daemon_process_sends(self):
        # no subprocess entry point imports the notifier: every sender runs in the uvicorn main process
        import pathlib
        root = pathlib.Path(_ROOT)
        senders = sorted(str(f.relative_to(root)) for f in root.glob("*.py")
                         if "_sd_notify(" in f.read_text() or "sd_notify(" in f.read_text())
        self.assertEqual(senders, ["main.py", "sd_watchdog.py"])


class TestAtomicRewrites(unittest.TestCase):
    """#345 r1 (5): a watchdog abort mid-rewrite leaves the old file whole, never a truncated one."""

    def test_a_kill_between_write_and_rename_keeps_the_old_file(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "q.processing")
        with open(path, "w") as f:
            f.write("a\nb\nc\n")
        with patch.object(main.os, "replace", side_effect=KeyboardInterrupt("abort")):
            with self.assertRaises(KeyboardInterrupt):
                main._atomic_write_lines(path, ["x"])
        with open(path) as f:
            self.assertEqual(f.read(), "a\nb\nc\n")   # reloads as it was
        self.assertEqual(os.listdir(d), ["q.processing"])   # no stray temp file
        main._atomic_write_lines(path, ["x", "y\n"])
        with open(path) as f:
            self.assertEqual(f.read(), "x\ny\n")
        os.remove(path)
        os.rmdir(d)


if __name__ == "__main__":
    unittest.main()
