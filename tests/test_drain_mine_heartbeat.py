"""A running drain mine is observable (techempower-org/mempalace#548).

On 2026-09-18 a drainer-spawned transcript mine waited 11 h 26 m at ~0 % CPU
and left no log line anywhere, because the drainer only awaited communicate().
These tests drive `_communicate_with_heartbeat` with a REAL subprocess and a
heartbeat and budget shrunk to fractions of a second. They check that it:

- returns the same (stdout, stderr) as communicate(),
- logs a heartbeat naming the target with a CPU delta while the mine runs,
- warns once, and only once, past the wall-clock budget, and never kills,
- reports the live mine in the shared state that /mine/status serves,
  then clears it.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

import main


def _spawn(code):
    return asyncio.create_subprocess_exec(
        sys.executable, "-c", code,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )


class DrainMineHeartbeat(unittest.TestCase):
    def run_with_env(self, code, beat, budget, observe=None):
        env = {"MEMPALACE_DRAIN_HEARTBEAT_S": str(beat), "MEMPALACE_DRAIN_MINE_BUDGET_S": str(budget)}

        async def go():
            proc = await _spawn(code)
            seen = {}
            if observe:
                async def peek():
                    await asyncio.sleep(observe)
                    seen.update(dict(main._ACTIVE_MINE_STATE))
                peeker = asyncio.ensure_future(peek())
            result = await main._communicate_with_heartbeat(proc, "/t/target.jsonl")
            if observe:
                await peeker
            return proc, result, seen

        with patch.dict(os.environ, env), self.assertLogs(main._log.name, level="INFO") as logs:
            proc, result, seen = asyncio.run(go())
        return proc, result, seen, logs.output

    def test_returns_what_communicate_returns(self):
        with patch.dict(os.environ, {"MEMPALACE_DRAIN_HEARTBEAT_S": "5"}):
            async def go():
                proc = await _spawn("import sys; print('out'); print('err', file=sys.stderr)")
                return await main._communicate_with_heartbeat(proc, "/t/x")
            stdout, stderr = asyncio.run(go())
        self.assertEqual(stdout.strip(), b"out")
        self.assertEqual(stderr.strip(), b"err")
        self.assertEqual(main._ACTIVE_MINE_STATE, {}, "state must clear after the mine ends")

    def test_heartbeat_names_the_target_and_reports_cpu(self):
        _, _, _, out = self.run_with_env("import time; time.sleep(0.9)", beat=0.25, budget=60)
        beats = [line for line in out if "still running /t/target.jsonl" in line]
        self.assertGreaterEqual(len(beats), 2, out)
        self.assertTrue(all("CPU s since last beat" in b for b in beats), beats)

    def test_budget_warns_exactly_once_and_does_not_kill(self):
        proc, _, _, out = self.run_with_env("import time; time.sleep(1.2)", beat=0.2, budget=0.3)
        warnings = [line for line in out if "over its" in line and line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1, out)
        self.assertIn("py-spy dump --pid", warnings[0])
        self.assertEqual(proc.returncode, 0, "the mine must run to completion, not be killed")

    def test_live_state_is_served_then_cleared(self):
        _, _, seen, _ = self.run_with_env("import time; time.sleep(1.0)", beat=0.2, budget=0.3, observe=0.7)
        self.assertEqual(seen.get("target"), "/t/target.jsonl")
        self.assertGreater(seen.get("elapsed_s", -1) + 1, 0)
        self.assertTrue(seen.get("over_budget"), seen)
        self.assertEqual(main._ACTIVE_MINE_STATE, {})

    def test_bad_env_values_fall_back_to_defaults(self):
        for raw in ("abc", "-5", "0"):
            with patch.dict(os.environ, {"X_TEST_S": raw}):
                self.assertEqual(main._env_seconds("X_TEST_S", 300.0), 300.0)

    def test_drainer_uses_the_heartbeat_wrapper(self):
        """The call site, not just the helper: a revert to bare communicate() fails here."""
        with open(main.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("await _communicate_with_heartbeat(proc, directory)", src)


if __name__ == "__main__":
    unittest.main()
