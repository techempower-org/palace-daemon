"""A transcript mined less than PALACE_MINE_MIN_INTERVAL_S ago is requeued, not re-mined.

familiar, 2026-10-21: 381 drain mines in 13 h, 45 of one 33,443-chunk transcript, and each
re-mine re-UPDATEs every chunk across all eight drawer indexes (mempalace#575).
"""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main


class TestDebounce(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = os.path.join(self.tmp.name, "pending-mines.jsonl")
        self.src = os.path.join(self.tmp.name, "session.jsonl")
        with open(self.src, "w") as f:
            f.write('{"type":"user"}\n')
        self._patches = [
            patch.dict(os.environ, {"PALACE_MINE_MIN_INTERVAL_S": "1800"}),
            patch.object(main, "_pending_mines_path", return_value=self.queue),
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_mineable_path_problem", return_value=None),
            patch.object(main, "_LOCK_REQUEUE_DELAY_S", 0),
        ]
        for p in self._patches:
            p.start()
        main._deferral_logged_at.clear()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _state(self, mined_at, mode="convos", unchanged=False):
        w = main._source_witness(self.src)
        rec = {"mtime": w["mtime"], "size": w["size"] + (0 if unchanged else 1), "mined_at": mined_at}
        with open(main._mined_state_path(), "w") as f:
            json.dump({main._mined_state_key(self.src, mode): rec}, f)

    def _queued(self):
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

    async def _drain(self, mode="convos"):
        await main._enqueue_pending_mine({"dir": self.src, "wing": "w", "mode": mode})
        with patch("asyncio.create_subprocess_exec", side_effect=self._spawn) as spawn:
            await main._drain_pending_mines()
        return spawn

    async def test_a_recently_mined_transcript_is_requeued_not_mined(self):
        self._state(time.time() - 60)
        spawn = await self._drain()
        spawn.assert_not_called()
        self.assertEqual(self._queued(), [self.src])

    async def test_after_the_interval_it_mines_and_records_mined_at(self):
        self._state(time.time() - 3600)
        spawn = await self._drain()
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(self._queued(), [])
        rec = main._load_mined_state()[main._mined_state_key(self.src, "convos")]
        self.assertGreater(rec["mined_at"], time.time() - 30)

    async def test_projects_mines_are_never_deferred(self):
        self._state(time.time() - 60, mode="projects")
        spawn = await self._drain(mode="projects")
        self.assertEqual(spawn.call_count, 1)

    async def test_zero_turns_it_off(self):
        self._state(time.time() - 60)
        with patch.dict(os.environ, {"PALACE_MINE_MIN_INTERVAL_S": "0"}):
            spawn = await self._drain()
        self.assertEqual(spawn.call_count, 1)

    async def test_a_state_without_mined_at_runs(self):
        w = main._source_witness(self.src)
        with open(main._mined_state_path(), "w") as f:
            json.dump({main._mined_state_key(self.src, "convos"): {"mtime": w["mtime"], "size": w["size"] + 1}}, f)
        spawn = await self._drain()
        self.assertEqual(spawn.call_count, 1)

    async def test_unchanged_still_skips_before_the_debounce(self):
        self._state(time.time() - 60, unchanged=True)
        spawn = await self._drain()
        spawn.assert_not_called()
        self.assertEqual(self._queued(), [])  # skipped outright, not requeued
