"""The shipped systemd unit arms the hang watchdog.

2026-11-19 the daemon hung for 7 h (every endpoint timing out, memory at its
MemoryHigh) while systemd still called it active: sd_watchdog.watchdog_loop
existed but the unit had no WatchdogSec, so systemd never armed a watchdog (with WatchdogSec set, an unset
NotifyAccess defaults to main). These read palace-daemon.service as shipped.
"""
import configparser
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
UNIT = ROOT / "palace-daemon.service"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sd_watchdog  # noqa: E402  (stdlib-only: the startup feed's bound)


def _service():
    cp = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
    cp.optionxform = str  # systemd keys are case-sensitive
    cp.read_string(UNIT.read_text())
    return cp["Service"]


def _seconds(value: str) -> int:
    v = value.strip()
    for suffix, mult in (("min", 60), ("s", 1)):
        if v.endswith(suffix):
            return int(v[: -len(suffix)]) * mult
    return int(v)


class ServiceUnit(unittest.TestCase):
    def test_watchdog_is_armed(self):
        svc = _service()
        self.assertIn("WatchdogSec", svc, "no WatchdogSec: watchdog_loop never pings and a hang is never killed")
        secs = _seconds(svc["WatchdogSec"])
        # Startup measured at 3-8 s; the loop's first ping lands at WatchdogSec/2 after startup.
        self.assertGreaterEqual(secs, 60)
        self.assertLessEqual(secs, 600, "a watchdog slower than 10 min leaves the palace down too long")

    def test_notifications_reach_systemd(self):
        svc = _service()
        if svc.get("Type", "simple") != "notify":
            # explicit, not left to the WatchdogSec default: the sender must be the main process
            # (tests/test_watchdog_rebuild.py TestNotifySender), and "none" would drop WATCHDOG=1
            self.assertIn(svc.get("NotifyAccess", "main"), ("main", "all"))

    def test_restarts_are_spaced_and_limited(self):
        # review #345 r1: a daemon aborted again and again stops (failed) instead of a restart storm
        cp = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
        cp.optionxform = str
        cp.read_string(UNIT.read_text())
        self.assertGreaterEqual(_seconds(_service().get("RestartSec", "0")), 5)
        self.assertGreater(int(cp["Unit"].get("StartLimitBurst", "0")), 1)
        self.assertGreaterEqual(_seconds(cp["Unit"].get("StartLimitIntervalSec", "0")),
                                5 * _seconds(_service()["WatchdogSec"]))

    def test_the_start_limit_window_holds_every_watchdog_storm(self):
        # review #345 r3-r5: Burst of the longest abort cycle must fit inside the window, or the counter never
        # reaches the burst and that storm never ends. One activation can spend every feed bound in sequence
        # (a slow start, then a silent rebuild), so the longest cycle is their SUM, plus one tick of detection
        # lag, plus WatchdogSec + RestartSec: 600 + 1800 + 60 + 180 + 10 = 2650 s.
        cp = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
        cp.optionxform = str
        cp.read_string(UNIT.read_text())
        svc = _service()
        burst = int(cp["Unit"]["StartLimitBurst"])
        window = _seconds(cp["Unit"]["StartLimitIntervalSec"])
        wdog = _seconds(svc["WatchdogSec"])
        base = wdog + _seconds(svc["RestartSec"])
        self.assertEqual(set(sd_watchdog.FEED_BOUNDS_S), {"running", "startup", "rebuild"})
        longest = sum(sd_watchdog.FEED_BOUNDS_S.values()) + sd_watchdog.watchdog_tick(wdog) + base
        self.assertLess(burst * longest, window, {"longest_cycle_s": longest, "burst": burst})
        # the weaker per-bound check, kept as a second guard
        self.assertLess(burst * (max(sd_watchdog.FEED_BOUNDS_S.values()) + base), window)

    def test_every_feed_bound_in_the_module_is_counted(self):
        # a *_MAX_S bound added to sd_watchdog without an entry in FEED_BOUNDS_S would escape the window test
        bounds = {v for k, v in vars(sd_watchdog).items() if k.endswith("_MAX_S") and isinstance(v, int)}
        self.assertTrue(bounds)
        self.assertLessEqual(bounds, set(sd_watchdog.FEED_BOUNDS_S.values()))

    def test_the_rebuild_stall_budget_can_only_tighten(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"PALACE_REBUILD_STALL_S": "99999"}):
            self.assertEqual(sd_watchdog.rebuild_stall_s(), sd_watchdog.REBUILD_STALL_MAX_S)
        with mock.patch.dict(os.environ, {"PALACE_REBUILD_STALL_S": "600"}):
            self.assertEqual(sd_watchdog.rebuild_stall_s(), 600)

    def test_the_startup_feed_defaults_to_its_bound(self):
        import inspect
        default = inspect.signature(sd_watchdog.startup_keepalive).parameters["max_secs"].default
        self.assertEqual(default, sd_watchdog.STARTUP_FEED_MAX_S)
        self.assertLessEqual(sd_watchdog.STARTUP_FEED_MAX_S, 600)

    def test_restart_follows_a_watchdog_abort(self):
        self.assertIn(_service().get("Restart"), ("always", "on-failure", "on-watchdog", "on-abnormal"))


if __name__ == "__main__":
    unittest.main()
