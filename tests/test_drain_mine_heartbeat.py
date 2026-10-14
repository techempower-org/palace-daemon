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
import shutil
import sys
import tempfile
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
        self.assertIn("await _communicate_with_heartbeat(\n", src)
        self.assertIn("proc, directory, phase_file", src)

    def test_drainer_hands_the_mine_a_phase_file(self):
        """The spawn passes MEMPALACE_MINE_PHASE_FILE, and the file is removed afterwards."""
        with open(main.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("env={**os.environ, _MINE_PHASE_ENV: phase_file}", src)
        self.assertIn("_drop_mine_phase_file(phase_file)", src)
        spawn = src.index("env={**os.environ, _MINE_PHASE_ENV: phase_file}")
        self.assertIn("except BaseException:", src[spawn:spawn + 400],
                      "a failed spawn must drop the private directory")

    def test_phase_file_lives_in_a_private_directory(self):
        path = main._new_mine_phase_file()
        parent = os.path.dirname(path)
        try:
            self.assertEqual(os.stat(parent).st_mode & 0o777, 0o700)
            self.assertNotEqual(parent, tempfile.gettempdir())
            self.assertFalse(os.path.exists(path), "the mine, not the daemon, creates the file")
            with open(path, "w") as f:
                f.write("{}")
            with open(path + ".99.tmp", "w") as f:  # a write the mine left half-done
                f.write("{")
        finally:
            main._drop_mine_phase_file(path)
        self.assertFalse(os.path.exists(parent), "the whole directory is removed")

    def test_drop_never_removes_a_directory_it_did_not_make(self):
        shared = tempfile.mkdtemp(prefix="not-ours-")
        bystander = os.path.join(shared, "someone-elses.log")
        with open(bystander, "w") as f:
            f.write("keep me")
        try:
            main._drop_mine_phase_file(os.path.join(shared, "phase.json"))
            self.assertTrue(os.path.exists(bystander), "a foreign directory must survive")
        finally:
            shutil.rmtree(shared, ignore_errors=True)
        self.assertEqual(main._MINE_PHASE_ENV, "MEMPALACE_MINE_PHASE_FILE")


# A stand-in mine that writes its phase file the way mempalace.mine_phase does.
_PHASE_WRITER = """
import json, os, sys, time
path = sys.argv[1]
def put(rec):
    with open(path + ".tmp", "w") as f:
        json.dump(rec, f)
    os.replace(path + ".tmp", path)
now = time.time()
put({"phase": "file", "since": now - 42, "updated": now, "pid": os.getpid(),
     "file": 3, "files_total": 7, "chunks": 120})
time.sleep(0.9)
"""


class DrainMinePhase(unittest.TestCase):
    def run_mine(self, code, phase_file, args=()):
        env = {"MEMPALACE_DRAIN_HEARTBEAT_S": "0.25", "MEMPALACE_DRAIN_MINE_BUDGET_S": "0.4"}
        seen = {}

        async def go():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", code, *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )

            async def peek():
                await asyncio.sleep(0.6)
                seen.update(dict(main._ACTIVE_MINE_STATE))

            peeker = asyncio.ensure_future(peek())
            await main._communicate_with_heartbeat(proc, "/t/target.jsonl", phase_file)
            await peeker

        with patch.dict(os.environ, env), self.assertLogs(main._log.name, level="INFO") as logs:
            asyncio.run(go())
        return logs.output, seen

    def test_beat_and_budget_warning_name_the_phase(self):
        path = main._new_mine_phase_file()
        try:
            out, seen = self.run_mine(_PHASE_WRITER, path, args=(path,))
        finally:
            main._drop_mine_phase_file(path)
        beats = [line for line in out if "still running" in line]
        self.assertTrue(beats, out)
        self.assertIn("phase file for 4", beats[-1])  # since = now - 42 s
        self.assertIn("file 3/7, 120 chunks", beats[-1])
        warnings = [line for line in out if line.startswith("WARNING")]
        self.assertEqual(len(warnings), 1, out)
        self.assertIn("phase file", warnings[0])
        self.assertEqual((seen.get("phase") or {}).get("phase"), "file", seen)

    def test_absent_phase_file_reads_as_unknown(self):
        path = main._new_mine_phase_file()
        self.assertFalse(os.path.exists(path), "the mine, not the daemon, creates the file")
        try:
            out, seen = self.run_mine("import time; time.sleep(0.7)", path)
        finally:
            main._drop_mine_phase_file(path)
        beats = [line for line in out if "still running" in line]
        self.assertTrue(beats and all(b.endswith("phase ?") for b in beats), out)
        self.assertIsNone(seen.get("phase"))

    def test_unreadable_or_foreign_records_are_ignored(self):
        path = main._new_mine_phase_file()
        try:
            for body in ("{not json", "[1, 2]", '{"phase": 7}'):
                with open(path, "w") as f:
                    f.write(body)
                self.assertIsNone(main._read_mine_phase(path), body)
        finally:
            main._drop_mine_phase_file(path)
        self.assertIsNone(main._read_mine_phase(None))
        self.assertEqual(main._describe_mine_phase({"phase": "open"}), "phase open")
        # json.load accepts Infinity; int(inf) raises OverflowError, which
        # must not escape into the heartbeat loop and cancel communicate().
        for since in (float("inf"), float("nan"), "soon"):
            self.assertEqual(main._describe_mine_phase({"phase": "file", "since": since}), "phase file")


if __name__ == "__main__":
    unittest.main()
