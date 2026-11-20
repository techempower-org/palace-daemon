"""The shipped systemd unit arms the hang watchdog and caps malloc arenas.

2026-11-19 the daemon hung for 7 h (every endpoint timing out, memory at its
MemoryHigh) while systemd still called it active: sd_watchdog.watchdog_loop
existed but the unit had no WatchdogSec, and Type=simple drops notifications
unless NotifyAccess is set. These read palace-daemon.service as shipped.
"""
import configparser
import pathlib
import unittest

UNIT = pathlib.Path(__file__).resolve().parent.parent / "palace-daemon.service"


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


def _environment(svc) -> dict:
    # configparser keeps the last of repeated keys; read every Environment= line.
    env = {}
    for line in UNIT.read_text().splitlines():
        if line.startswith("Environment="):
            k, _, v = line[len("Environment="):].partition("=")
            env[k] = v
    return env


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
            self.assertIn(svc.get("NotifyAccess", "none"), ("main", "all"),
                          "Type=simple without NotifyAccess drops WATCHDOG=1, so the watchdog would kill a healthy daemon")

    def test_restart_follows_a_watchdog_abort(self):
        self.assertIn(_service().get("Restart"), ("always", "on-failure", "on-watchdog", "on-abnormal"))

    def test_malloc_arenas_are_capped(self):
        env = _environment(_service())
        self.assertIn("MALLOC_ARENA_MAX", env)
        self.assertLessEqual(int(env["MALLOC_ARENA_MAX"]), 4)


if __name__ == "__main__":
    unittest.main()
