"""Mining waits for disk space (main._mine_disk_floor_problem).

familiar, 2026-10-21 02:50: root at 95%, 13 GB free, with the palace DB's
files the ones growing.
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import main

GIB = 1024**3


def _statvfs(free_gib):
    return SimpleNamespace(f_bavail=int(free_gib * GIB) // 4096, f_frsize=4096)


class TestFloor(unittest.TestCase):
    def test_under_the_floor_names_the_path_and_numbers(self):
        with patch.dict(os.environ, {"PALACE_MINE_MIN_FREE_GB": "8", "PALACE_MINE_FREE_PATH": "/db"}):
            with patch.object(main.os, "statvfs", return_value=_statvfs(5.0)):
                why = main._mine_disk_floor_problem()
        self.assertIn("5.0 GB free on /db", why)
        self.assertIn("8 GB", why)

    def test_over_the_floor_is_fine(self):
        with patch.dict(os.environ, {"PALACE_MINE_MIN_FREE_GB": "8"}):
            with patch.object(main.os, "statvfs", return_value=_statvfs(12.0)):
                self.assertIsNone(main._mine_disk_floor_problem())

    def test_zero_turns_it_off(self):
        with patch.dict(os.environ, {"PALACE_MINE_MIN_FREE_GB": "0"}):
            with patch.object(main.os, "statvfs", return_value=_statvfs(0.1)):
                self.assertIsNone(main._mine_disk_floor_problem())

    def test_an_unreadable_db_path_falls_back_to_root(self):
        def fake(path):
            if path == "/db":
                raise PermissionError(path)
            return _statvfs(3.0)

        with patch.dict(os.environ, {"PALACE_MINE_MIN_FREE_GB": "8", "PALACE_MINE_FREE_PATH": "/db"}):
            with patch.object(main.os, "statvfs", side_effect=fake):
                self.assertIn("on /", main._mine_disk_floor_problem())


class TestDrainHoldsTheQueue(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = os.path.join(self.tmp.name, "pending-mines.jsonl")
        self._patches = [
            patch.dict(os.environ, {"PALACE_MINE_MIN_FREE_GB": "8"}),
            patch.object(main, "_pending_mines_path", return_value=self.queue),
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_mineable_path_problem", return_value=None),
            patch.object(main, "_LOCK_REQUEUE_DELAY_S", 0),  # the requeue backoff
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _queued_dirs(self):
        if not os.path.exists(self.queue):
            return []
        with open(self.queue) as f:
            return [json.loads(ln)["payload"]["dir"] for ln in f if ln.strip()]

    async def _spawn(self, *args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        proc.pid = 4242
        return proc

    async def test_low_disk_requeues_every_entry_and_spawns_nothing(self):
        for i in range(3):
            await main._enqueue_pending_mine({"dir": f"/t/{i}.jsonl", "wing": "w", "mode": "convos"})
        with patch.object(main.os, "statvfs", return_value=_statvfs(4.0)):
            with patch("asyncio.create_subprocess_exec", side_effect=self._spawn) as spawn:
                await main._drain_pending_mines()
        spawn.assert_not_called()
        self.assertEqual(sorted(self._queued_dirs()), [f"/t/{i}.jsonl" for i in range(3)])

    async def test_with_space_the_same_queue_drains(self):
        for i in range(3):
            await main._enqueue_pending_mine({"dir": f"/t/{i}.jsonl", "wing": "w", "mode": "convos"})
        with patch.object(main.os, "statvfs", return_value=_statvfs(40.0)):
            with patch("asyncio.create_subprocess_exec", side_effect=self._spawn) as spawn:
                await main._drain_pending_mines()
        self.assertEqual(spawn.call_count, 3)
        self.assertEqual(self._queued_dirs(), [])


class TestBlockingMineIsSkipped(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = os.path.join(self.tmp.name, "CLAUDE.md")
        with open(self.doc, "w", encoding="utf-8") as handle:
            handle.write("# doc\n" * 40)
        self._patches = [
            patch.dict(os.environ, {"PALACE_MINE_MIN_FREE_GB": "8"}),
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_check_auth", side_effect=lambda *_a, **_k: None),
            patch.object(main._mp, "_config", MagicMock(backend="postgres", palace_path="/tmp/palace")),
        ]
        for p in self._patches:
            p.start()
        self._orig_repair = dict(main._repair_state)
        main._repair_state["in_progress"] = False

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        main._repair_state.clear()
        main._repair_state.update(self._orig_repair)
        self.tmp.cleanup()

    def _request_and_body(self):
        from search_models import MineBody

        body = {"dir": self.doc, "wing": "2g", "mode": "projects"}
        req = MagicMock()
        req.json = AsyncMock(return_value=body)
        req.app.state.active_mines = set()
        return req, MineBody(**body)

    async def test_low_disk_skips_without_spawning(self):
        with patch.object(main.os, "statvfs", return_value=_statvfs(2.0)):
            with patch("asyncio.create_subprocess_exec") as spawn:
                req, body = self._request_and_body()
                out = await main.mine(req, body, x_api_key=None)
        spawn.assert_not_called()
        self.assertTrue(out["skipped"])
        self.assertIn("mining floor", out["reason"])
