"""Spawned mines run under ``ionice -c2 -n7 nice -n 10`` (main._low_priority).

familiar, 2026-10-20 22:40: a drain mine of a 20k-chunk transcript ran while
the NVMe was 80-90% busy, and realm.db on the same disk held a write lock
for 16.7 s.
"""

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main

TOOLS = {"ionice": "/usr/bin/ionice", "nice": "/usr/bin/nice"}


class TestLowPriorityPrefix(unittest.TestCase):
    def setUp(self):
        self._env = patch.dict(os.environ, {"PALACE_MINE_LOW_PRIORITY": "1"})
        self._env.start()

    def tearDown(self):
        self._env.stop()

    def test_the_prefix_is_ionice_then_nice(self):
        with patch.object(main.shutil, "which", side_effect=TOOLS.get):
            out = main._low_priority(["/v/mempalace", "mine", "/t.jsonl"])
        self.assertEqual(
            out,
            ["/usr/bin/ionice", "-c2", "-n7", "/usr/bin/nice", "-n", "10", "/v/mempalace", "mine", "/t.jsonl"],
        )

    def test_zero_turns_it_off(self):
        with patch.dict(os.environ, {"PALACE_MINE_LOW_PRIORITY": "0"}):
            with patch.object(main.shutil, "which", side_effect=TOOLS.get):
                self.assertEqual(main._low_priority(["m"]), ["m"])

    def test_a_missing_tool_is_skipped(self):
        with patch.object(main.shutil, "which", side_effect={"nice": "/usr/bin/nice"}.get):
            self.assertEqual(main._low_priority(["m"]), ["/usr/bin/nice", "-n", "10", "m"])
        with patch.object(main.shutil, "which", return_value=None):
            self.assertEqual(main._low_priority(["m"]), ["m"])


class TestDrainSpawnsAtLowPriority(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        queue = os.path.join(self.tmp.name, "pending-mines.jsonl")
        self._patches = [
            patch.dict(os.environ, {"PALACE_MINE_LOW_PRIORITY": "1"}),
            patch.object(main, "_pending_mines_path", return_value=queue),
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_mineable_path_problem", return_value=None),
            patch.object(main.shutil, "which", side_effect=TOOLS.get),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    async def test_a_drain_mine_is_spawned_under_ionice_and_nice(self):
        await main._enqueue_pending_mine({"dir": "/t/a.jsonl", "wing": "w", "mode": "convos"})

        async def _spawn(*args, **kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock(return_value=0)
            proc.returncode = 0
            proc.pid = 4242
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_spawn) as spawn:
            await main._drain_pending_mines()
        argv = list(spawn.call_args.args)
        self.assertEqual(argv[:6], ["/usr/bin/ionice", "-c2", "-n7", "/usr/bin/nice", "-n", "10"])
        self.assertEqual(argv[7:9], ["mine", "/t/a.jsonl"])
