"""The drain says so when a transcript is over the miner's size cap.

2026-11-04: a 601 MB transcript was "mined" every 30 minutes for days. The
miner skipped it (500 MB cap) and exited 0, so the drain recorded a mine that
read nothing and logged nothing. Now the drain checks the miner's own cap
first, logs a warning, spawns nothing and records no witness.
"""
import unittest
from unittest.mock import patch

import main
from tests.test_drain_rolling_claim import _DrainHarness, _entry


class TestOverCapTranscriptIsReportedNotMined(_DrainHarness):
    async def test_over_cap_transcript_is_logged_and_not_spawned(self):
        self.write_queue([_entry("/d/big.jsonl"), _entry("/d/small.jsonl")])
        sizes = {"/d/big.jsonl": (601 * 1024 * 1024, 500 * 1024 * 1024)}
        saved = []
        main._cap_skip_logged_at.clear()
        with patch.object(main._instruments, "transcript_over_cap", sizes.get), \
                patch.object(main, "_save_mined_state", lambda st: saved.append(dict(st))), \
                self.assertLogs("palace-daemon", level="WARNING") as logs:
            await self.drain()
        self.assertEqual(self.ran, ["/d/small.jsonl"])
        self.assertTrue(
            any("NOT mining /d/big.jsonl" in m and "601 MB" in m and "500 MB" in m
                for m in logs.output),
            logs.output,
        )
        self.assertFalse(any(k.startswith("/d/big.jsonl") for st in saved for k in st))

    async def test_projects_mode_is_never_cap_checked(self):
        self.write_queue([_entry("/d/proj", mode="projects")])
        main._cap_skip_logged_at.clear()
        with patch.object(main._instruments, "transcript_over_cap",
                          lambda p: (10, 1)):
            await self.drain()
        self.assertEqual(self.ran, ["/d/proj"])

    async def test_warning_is_throttled_per_target(self):
        main._cap_skip_logged_at.clear()
        over = lambda p: (601 * 1024 * 1024, 500 * 1024 * 1024)  # noqa: E731
        with patch.object(main._instruments, "transcript_over_cap", over):
            self.write_queue([_entry("/d/big.jsonl")])
            with self.assertLogs("palace-daemon", level="WARNING") as first:
                await self.drain()
            self.write_queue([_entry("/d/big.jsonl")])
            with self.assertNoLogs("palace-daemon", level="WARNING"):
                await self.drain()
        self.assertEqual(sum("NOT mining" in m for m in first.output), 1)
        self.assertEqual(self.ran, [])


if __name__ == "__main__":
    unittest.main()
