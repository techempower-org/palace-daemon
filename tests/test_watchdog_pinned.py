"""The pinned-memory watchdog trigger (Flow 970b2e72).

On Sun 2026-11-22 the daemon sat at MemoryHigh with swap at MemorySwapMax from 03:54 to 05:46. /health
still answered now and then, so the keepalive kept systemd's watchdog fed for ~2 h. The loop now reads
the unit's cgroup each tick, and once memory.high events have kept rising with swap at the cap for
PINNED_HOLD_S, it sends WATCHDOG=trigger (a watchdog restart now) and records why for the CHECK.

No live daemon, cgroup or systemd socket is needed: the cgroup reader, the clock and ``_sd_notify`` are
stubbed. Run with::

    python -m unittest tests.test_watchdog_pinned -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402
import sd_watchdog  # noqa: E402

GIB = 1 << 30
SWAP_MAX = 3 * GIB


class Cgroup:
    """A fake cgroup: each read returns the next (high events, swap.current, swap.max)."""

    def __init__(self, samples):
        self.samples = list(samples)

    def __call__(self):
        return self.samples.pop(0) if len(self.samples) > 1 else self.samples[0]


def pinned_samples(n, start=100):
    return [(start + 50 * k, SWAP_MAX, SWAP_MAX) for k in range(n)]


class PinnedMemoryUnit(unittest.TestCase):
    def _run(self, samples, step=60.0):
        clock = {"t": 1000.0}

        def tick():
            return clock["t"]

        pm = sd_watchdog.PinnedMemory(read=Cgroup(samples), clock=tick)
        out = []
        for _ in samples:
            out.append(pm.observe())
            clock["t"] += step
        return out

    def test_rising_high_events_with_swap_at_the_cap_count_up(self):
        out = self._run(pinned_samples(7))
        self.assertIsNone(out[0])  # the first sample has nothing to rise from
        self.assertEqual(out[1:], [0.0, 60.0, 120.0, 180.0, 240.0, 300.0])

    def test_swap_below_the_cap_is_not_pinned(self):
        samples = [(100 + 50 * k, int(0.9 * SWAP_MAX), SWAP_MAX) for k in range(7)]
        self.assertEqual(self._run(samples), [None] * 7)

    def test_flat_high_events_are_not_pinned(self):
        samples = [(100, SWAP_MAX, SWAP_MAX)] * 7  # at the swap cap, but reclaim is not being forced
        self.assertEqual(self._run(samples), [None] * 7)

    def test_one_calm_tick_starts_the_count_again(self):
        samples = pinned_samples(4) + [(250, SWAP_MAX, SWAP_MAX)] + pinned_samples(3, start=300)
        out = self._run(samples)
        self.assertEqual(out[3], 120.0)
        self.assertIsNone(out[4])  # high did not rise
        self.assertEqual(out[5:], [0.0, 60.0, 120.0])

    def test_no_swap_cap_or_no_cgroup_never_fires(self):
        self.assertEqual(self._run([(100 + k, GIB, None) for k in range(7)]), [None] * 7)
        self.assertEqual(self._run([None] * 7), [None] * 7)

    def test_read_memory_parses_a_cgroup_dir(self):
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("memory.events", "low 0\nhigh 4711\nmax 0\noom 0\n"),
                               ("memory.swap.current", "3221225472\n"), ("memory.swap.max", "3221225472\n")):
                with open(os.path.join(d, name), "w") as f:
                    f.write(body)
            self.assertEqual(sd_watchdog.read_memory(d), (4711, SWAP_MAX, SWAP_MAX))
            with open(os.path.join(d, "memory.swap.max"), "w") as f:
                f.write("max\n")
            self.assertEqual(sd_watchdog.read_memory(d), (4711, SWAP_MAX, None))
            os.remove(os.path.join(d, "memory.events"))
            self.assertIsNone(sd_watchdog.read_memory(d))

    def test_cgroup_dir_reads_the_v2_line(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("0::/system.slice/palace-daemon.service\n")
        try:
            self.assertEqual(sd_watchdog.cgroup_dir(f.name, root="/sys/fs/cgroup"),
                             "/sys/fs/cgroup/system.slice/palace-daemon.service")
        finally:
            os.remove(f.name)


def _n_iterations(n):
    calls = {"k": 0}

    async def _sleep(_secs):
        calls["k"] += 1
        if calls["k"] <= n:
            return None
        raise asyncio.CancelledError()

    return _sleep


class PinnedMemoryLoop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._orig_repair = dict(main._repair_state)
        main._repair_state.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.trigger_path = os.path.join(self.tmp.name, "watchdog-trigger.json")

    async def asyncTearDown(self):
        main._repair_state.clear()
        main._repair_state.update(self._orig_repair)
        self.tmp.cleanup()

    async def _loop(self, samples, ticks, health=200):
        """``ticks`` loop iterations; the clock moves 60 s per pinned observation (one read each)."""
        fake = Cgroup(samples)
        clock = iter(1000.0 + 60.0 * k for k in range(1000))
        with patch("asyncio.sleep", side_effect=_n_iterations(ticks)), \
             patch.object(sd_watchdog, "read_memory", side_effect=lambda _cg: fake()), \
             patch.object(sd_watchdog, "_clock", side_effect=lambda: next(clock)), \
             patch.object(sd_watchdog, "TRIGGER_PATH", self.trigger_path), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(sd_watchdog, "self_health", return_value=health):
            await main._watchdog_loop(180)
        return [c.args[0] for c in notify.call_args_list]

    async def test_pinned_for_five_minutes_triggers_a_watchdog_restart(self):
        # /health still answers, as it did on Sun 11-22: the probe alone would have kept feeding
        sent = await self._loop(pinned_samples(8), ticks=8)
        self.assertIn("WATCHDOG=trigger\n", sent)
        self.assertEqual(sent.index("WATCHDOG=trigger\n"), 6)  # tick 7: 300 s after the first pinned tick
        with open(self.trigger_path) as f:
            rec = json.load(f)
        self.assertEqual(rec["trigger"], "pinned-memory")
        self.assertGreaterEqual(rec["pinned_s"], sd_watchdog.PINNED_HOLD_S)
        self.assertEqual(rec["swap_max"], SWAP_MAX)

    async def test_pinned_for_less_than_the_hold_keeps_feeding(self):
        sent = await self._loop(pinned_samples(5), ticks=5)
        self.assertEqual(sent, ["WATCHDOG=1\n"] * 5)
        self.assertFalse(os.path.exists(self.trigger_path))

    async def test_never_during_a_rebuild(self):
        import time

        main._repair_state.update(in_progress=True, mode="rebuild",
                                  progress={"beat": 1, "beat_at": time.monotonic(),
                                            "started_at_monotonic": time.monotonic()})
        with patch.object(sd_watchdog, "rebuild_quiet_s", return_value=5.0):
            sent = await self._loop(pinned_samples(8), ticks=8)
        self.assertNotIn("WATCHDOG=trigger\n", sent)


if __name__ == "__main__":
    unittest.main()
