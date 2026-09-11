"""Tests for /mine queue + drain during repair=rebuild.

Mirrors the silent-save queue contract: while a rebuild is in progress,
/mine requests are appended to a jsonl file at ``_pending_mines_path()``
and replayed by ``_drain_pending_mines()`` after the rebuild completes.

Run with::

    python -m unittest tests.test_mine_queue -v

Pure-function and pure-IO tests; no live daemon required. The drain
test stubs the chromadb subprocess invocation by monkey-patching
``asyncio.create_subprocess_exec``.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class TestPendingMinesPath(unittest.TestCase):
    """The pending-mines file lives alongside the silent-save pending file
    but with a distinct name, so a daemon-busy save and a daemon-busy mine
    can both queue independently."""

    def test_path_is_separate_from_writes_path(self):
        # Both derive from _config.palace_path's parent, but the basenames differ.
        writes = main._pending_writes_path()
        mines = main._pending_mines_path()
        self.assertNotEqual(writes, mines)
        self.assertTrue(mines.endswith("palace-daemon-pending-mines.jsonl"))


class TestEnqueueAndDrain(unittest.IsolatedAsyncioTestCase):
    """End-to-end: enqueue a few payloads, then drain — verify dedup + replay."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Point the queue file inside the tmp dir
        self._queue_path = os.path.join(self.tmp.name, "pending-mines.jsonl")
        self._patches = [
            patch.object(main, "_pending_mines_path", return_value=self._queue_path),
            # Skip path translation for tests
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
        ]
        for p in self._patches:
            p.start()
        # Make every replayed dir "exist" so the drain doesn't skip
        self._is_dir_patch = patch("pathlib.Path.is_dir", return_value=True)
        self._is_dir_patch.start()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self._is_dir_patch.stop()
        self.tmp.cleanup()

    async def test_enqueue_then_drain_replays_each_target(self):
        await main._enqueue_pending_mine({"dir": "/a", "wing": "wa", "mode": "convos"})
        await main._enqueue_pending_mine({"dir": "/b", "wing": "wb", "mode": "convos"})
        self.assertTrue(os.path.isfile(self._queue_path))

        # Stub the subprocess: each call returns rc=0
        async def _fake_subprocess(*args, **kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_subprocess) as spawn:
            count = await main._drain_pending_mines()
        self.assertEqual(count, 2)
        # Queue file is gone after a clean drain
        self.assertFalse(os.path.isfile(self._queue_path))
        # Subprocess was invoked twice (once per unique target)
        self.assertEqual(spawn.call_count, 2)

    async def test_drain_dedups_repeated_target(self):
        """A storm of hook fires queues the same (dir, wing, mode) many times.
        Drain replays once per unique target — a single mine catches up all
        the queued requests via convo_miner's mtime-based dedup anyway."""
        for _ in range(10):
            await main._enqueue_pending_mine({"dir": "/a", "wing": "wa", "mode": "convos"})

        async def _fake_subprocess(*args, **kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_subprocess) as spawn:
            count = await main._drain_pending_mines()
        self.assertEqual(count, 1)
        self.assertEqual(spawn.call_count, 1)

    async def test_drain_quarantines_failed_replays(self):
        """A non-zero subprocess exit doesn't lose the queue entry — it
        moves to a timestamped .failed-* file so the next drain doesn't
        replay it again."""
        await main._enqueue_pending_mine({"dir": "/a", "wing": "wa", "mode": "convos"})

        async def _fake_subprocess(*args, **kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b"boom"))
            proc.returncode = 1
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_subprocess):
            count = await main._drain_pending_mines()
        self.assertEqual(count, 0)
        # Original queue file was removed; a .failed-* sibling exists
        self.assertFalse(os.path.isfile(self._queue_path))
        siblings = os.listdir(self.tmp.name)
        failed = [s for s in siblings if ".failed-" in s]
        self.assertEqual(len(failed), 1)

    async def test_drain_empty_queue_returns_zero(self):
        """No queue file → no work → return 0, no error."""
        count = await main._drain_pending_mines()
        self.assertEqual(count, 0)

    async def test_drain_replays_extract_and_limit_options(self):
        """Closes Copilot finding on jphein/palace-daemon#4 — drain
        previously dropped optional ``extract`` / ``limit`` fields,
        so a queue entry that included them got replayed without."""
        await main._enqueue_pending_mine({
            "dir": "/a", "wing": "wa", "mode": "convos",
            "extract": "exchange", "limit": 100,
        })

        captured_argv = []

        async def _fake_subprocess(*args, **kwargs):
            captured_argv.append(list(args))
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_subprocess):
            count = await main._drain_pending_mines()

        self.assertEqual(count, 1)
        self.assertEqual(len(captured_argv), 1)
        argv = captured_argv[0]
        # extract and limit make it onto the replay command
        self.assertIn("--extract", argv)
        self.assertEqual(argv[argv.index("--extract") + 1], "exchange")
        self.assertIn("--limit", argv)
        self.assertEqual(argv[argv.index("--limit") + 1], "100")

    async def test_drain_replays_session_mode(self):
        """Regression for Copilot finding on jphein/palace-daemon#5 — the
        drain's local VALID_MODES had drifted to {convos, projects},
        silently dropping queued ``session`` mines that the live /mine
        endpoint accepts. Both paths now share _MINE_VALID_MODES."""
        self.assertIn("session", main._MINE_VALID_MODES)
        await main._enqueue_pending_mine({"dir": "/a", "wing": "wa", "mode": "session"})

        captured_argv = []

        async def _fake_subprocess(*args, **kwargs):
            captured_argv.append(list(args))
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_subprocess):
            count = await main._drain_pending_mines()

        self.assertEqual(count, 1, "session-mode mine must survive the drain")
        argv = captured_argv[0]
        self.assertEqual(argv[argv.index("--mode") + 1], "session")

    async def test_drain_skips_invalid_payload_fields(self):
        """Closes Copilot finding on jphein/palace-daemon#4 — drain
        previously skipped only is_dir() check; now also enforces
        the same valid-mode / valid-extract / int-limit / no-traversal
        guards as the live /mine endpoint."""
        for bad in (
            {"dir": "../../etc/passwd", "wing": "wa", "mode": "convos"},  # traversal
            {"dir": "/a", "wing": "wa", "mode": "wrong-mode"},  # invalid mode
            {"dir": "/a", "wing": "wa", "mode": "convos", "extract": "wrong"},  # invalid extract
            {"dir": "/a", "wing": "wa", "mode": "convos", "limit": "not-a-number"},  # invalid limit
            {"dir": None, "wing": "wa", "mode": "convos"},  # invalid dir type
        ):
            await main._enqueue_pending_mine(bad)

        async def _fake_subprocess(*args, **kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_subprocess) as spawn:
            count = await main._drain_pending_mines()
        # All five entries skipped, no subprocess spawned, count = 0
        self.assertEqual(count, 0)
        self.assertEqual(spawn.call_count, 0)


if __name__ == "__main__":
    unittest.main()


class TestMineablePath:
    """/mine and the drain path accept a directory OR a single .jsonl transcript.

    Hooks post one transcript per checkpoint (mempalace#414/#426); posting the
    parent directory re-mined the whole project every time.
    """

    def test_directory_is_mineable(self, tmp_path):
        from main import _is_mineable_path

        assert _is_mineable_path(tmp_path) is True

    def test_jsonl_file_is_mineable(self, tmp_path):
        from main import _is_mineable_path

        f = tmp_path / "session.jsonl"
        f.write_text("{}\n")
        assert _is_mineable_path(f) is True

    def test_other_file_is_not_mineable(self, tmp_path):
        from main import _is_mineable_path

        f = tmp_path / "notes.txt"
        f.write_text("x")
        assert _is_mineable_path(f) is False

    def test_missing_path_is_not_mineable(self, tmp_path):
        from main import _is_mineable_path

        assert _is_mineable_path(tmp_path / "nope.jsonl") is False


class TestDrainRequeuesOnLockContention(unittest.IsolatedAsyncioTestCase):
    """A replay that fails only because another process holds the palace
    flock is requeued for the next pass, not quarantined (a sweep running
    while hooks post would otherwise silently drop every ingest)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._queue_path = os.path.join(self.tmp.name, "pending-mines.jsonl")
        self._patches = [
            patch.object(main, "_pending_mines_path", return_value=self._queue_path),
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch("pathlib.Path.is_dir", return_value=True),
            patch.object(main, "_LOCK_REQUEUE_DELAY_S", 0),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    async def test_lock_held_is_requeued_not_quarantined(self):
        await main._enqueue_pending_mine({"dir": "/a", "wing": "wa", "mode": "convos"})

        async def _spawn(*args, **kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(
                return_value=(b"", b"mempalace: palace /p is held by PID 42 (mine); wait for it")
            )
            proc.returncode = 1
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_spawn):
            drained = await main._drain_pending_mines()
        self.assertEqual(drained, 0)
        self.assertTrue(os.path.isfile(self._queue_path), "entry must be back on the live queue")
        with open(self._queue_path) as f:
            entry = json.loads(f.read().strip())
        self.assertEqual(entry["lock_retries"], 1)
        self.assertEqual(entry["payload"]["dir"], "/a")
        failed = [n for n in os.listdir(self.tmp.name) if ".failed-" in n]
        self.assertEqual(failed, [], "lock contention must not quarantine")

    async def test_gives_up_after_max_retries(self):
        await main._enqueue_pending_mine({"dir": "/a", "wing": "wa", "mode": "convos"})
        # pre-set the retry counter at the ceiling
        with open(self._queue_path) as f:
            entry = json.loads(f.read().strip())
        entry["lock_retries"] = main._LOCK_REQUEUE_MAX
        with open(self._queue_path, "w") as f:
            f.write(json.dumps(entry) + "\n")

        async def _spawn(*args, **kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b"palace /p is held by PID 42"))
            proc.returncode = 1
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_spawn):
            await main._drain_pending_mines()
        self.assertFalse(os.path.isfile(self._queue_path))
        failed = [n for n in os.listdir(self.tmp.name) if ".failed-" in n]
        self.assertEqual(len(failed), 1, "exhausted retries are quarantined")


class TestRecoverOrphanedProcessing(unittest.IsolatedAsyncioTestCase):
    """An interrupted drain batch (.processing left behind by a restart) is
    folded back into the live queue and drained, not stranded forever."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._queue_path = os.path.join(self.tmp.name, "pending-mines.jsonl")
        self._patches = [
            patch.object(main, "_pending_mines_path", return_value=self._queue_path),
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch("pathlib.Path.is_dir", return_value=True),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    async def test_orphaned_batch_is_recovered_and_drained(self):
        orphan = self._queue_path + ".processing"
        with open(orphan, "w") as f:
            f.write(json.dumps({"payload": {"dir": "/a", "wing": "wa", "mode": "convos"}}) + "\n")

        async def _spawn(*args, **kwargs):
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"ok", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_spawn) as spawn:
            drained = await main._drain_pending_mines()
        self.assertEqual(drained, 1)
        spawn.assert_called_once()
        self.assertFalse(os.path.exists(orphan))
        self.assertFalse(os.path.exists(self._queue_path))

    def test_recover_returns_zero_when_nothing_orphaned(self):
        self.assertEqual(
            main._recover_orphaned_processing(self._queue_path, self._queue_path + ".processing"), 0
        )


class TestMineStatusEndpoint(unittest.IsolatedAsyncioTestCase):
    """GET /mine/status reports queue depth, processing batch, active mines, drainer state."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._queue_path = os.path.join(self.tmp.name, "pending-mines.jsonl")
        self._patches = [
            patch.object(main, "_pending_mines_path", return_value=self._queue_path),
            patch.object(main, "_check_auth", side_effect=lambda *_a, **_k: None),
        ]
        for p in self._patches:
            p.start()
        main._mine_drain_task = None

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    async def test_reports_counts_and_peek(self):
        await main._enqueue_pending_mine({"dir": "/a.jsonl", "wing": "wa", "mode": "convos"})
        await main._enqueue_pending_mine({"dir": "/b.jsonl", "wing": "wb", "mode": "convos"})
        with open(self._queue_path + ".processing", "w") as f:
            f.write(json.dumps({"payload": {"dir": "/c.jsonl", "wing": "wc", "mode": "convos"}}) + "\n")
        main.app.state.active_mines = {object()}
        try:
            out = await main.mine_status(x_api_key=None)
        finally:
            main.app.state.active_mines = set()
        self.assertEqual(out["queued"], 2)
        self.assertEqual(out["processing"], 1)
        self.assertEqual(out["active_mines"], 1)
        self.assertFalse(out["drainer_running"])
        self.assertEqual([n["dir"] for n in out["next"]], ["/a.jsonl", "/b.jsonl", "/c.jsonl"])

    async def test_empty_queue(self):
        out = await main.mine_status(x_api_key=None)
        self.assertEqual((out["queued"], out["processing"], out["next"]), (0, 0, []))


class TestDrainBatchCapAndPriority(unittest.IsolatedAsyncioTestCase):
    """The drain must not swallow the whole queue in one pass (daemon#260).

    Measured 2026-09-10: after the 21:17 restart the drainer renamed the
    entire pending file into ONE .processing batch -- 884 entries -- and
    replayed it sequentially. Every /mine posted during that pass waited
    hours, including the small memory-dir mines a live session depends on.

    A captured production queue (142 entries, 2026-09-03) shows the mix the
    ordering rule exists for:

        70  ('convos',   jsonl transcript)
        70  ('session',  jsonl transcript)
         2  ('projects', memory dir)        <- the ones that were stuck

    28 distinct (dir, mode) pairs across 142 lines, so dedup already
    collapses the queue ~5:1; the cap is applied to the DEDUPED entries, or
    a batch of 20 would be spent on duplicates of one transcript.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._queue_path = os.path.join(self.tmp.name, "pending-mines.jsonl")
        self._patches = [
            patch.object(main, "_pending_mines_path", return_value=self._queue_path),
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            # Every replayed target passes the mineability gate; this suite is
            # about scheduling, not about which paths are mineable (L2 owns
            # _mineable_path_problem).
            patch.object(main, "_mineable_path_problem", return_value=None),
        ]
        for p in self._patches:
            p.start()
        self.spawned: list = []

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _ok_subprocess(self):
        async def _fake(*args, **kwargs):
            self.spawned.append(list(args))
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        return _fake

    def _dirs_spawned(self) -> list:
        # cmd = [bin, "mine", <dir>, "--mode", ...]
        return [a[2] for a in self.spawned]

    def _queue_dirs(self) -> list:
        if not os.path.isfile(self._queue_path):
            return []
        out = []
        with open(self._queue_path, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    out.append(json.loads(ln)["payload"]["dir"])
        return out

    async def test_one_pass_takes_at_most_the_batch_cap(self):
        for i in range(25):
            await main._enqueue_pending_mine(
                {"dir": f"/t/{i}.jsonl", "wing": "w", "mode": "convos"}
            )

        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "20"}, clear=False):
            with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
                count = await main._drain_pending_mines()

        self.assertEqual(count, 20)
        self.assertEqual(len(self.spawned), 20)

    async def test_the_remainder_stays_queued_rather_than_being_dropped(self):
        for i in range(25):
            await main._enqueue_pending_mine(
                {"dir": f"/t/{i}.jsonl", "wing": "w", "mode": "convos"}
            )

        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "20"}, clear=False):
            with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
                await main._drain_pending_mines()

        left = self._queue_dirs()
        self.assertEqual(len(left), 5, "the 5 untaken entries must survive the pass")
        self.assertEqual(left, [f"/t/{i}.jsonl" for i in range(20, 25)])

    async def test_a_post_arriving_after_the_pass_is_not_stuck_behind_the_backlog(self):
        """The whole point: a new mine waits one pass, not the whole queue."""
        for i in range(25):
            await main._enqueue_pending_mine(
                {"dir": f"/t/{i}.jsonl", "wing": "w", "mode": "convos"}
            )

        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "20"}, clear=False):
            with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
                await main._drain_pending_mines()
            # A live session posts a small memory-dir mine now.
            await main._enqueue_pending_mine(
                {"dir": "/proj/memory", "wing": "w", "mode": "projects"}
            )
            self.spawned.clear()
            with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
                await main._drain_pending_mines()

        self.assertIn("/proj/memory", self._dirs_spawned())
        self.assertEqual(
            self._dirs_spawned()[0],
            "/proj/memory",
            "a small mine posted after the backlog must not queue behind its remainder",
        )

    async def test_projects_mines_run_before_transcript_remines(self):
        await main._enqueue_pending_mine({"dir": "/t/a.jsonl", "wing": "w", "mode": "convos"})
        await main._enqueue_pending_mine({"dir": "/t/b.jsonl", "wing": "w", "mode": "session"})
        await main._enqueue_pending_mine({"dir": "/p/memory", "wing": "w", "mode": "projects"})
        await main._enqueue_pending_mine({"dir": "/t/c.jsonl", "wing": "w", "mode": "convos"})

        with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
            await main._drain_pending_mines()

        self.assertEqual(self._dirs_spawned()[0], "/p/memory")
        self.assertEqual(len(self._dirs_spawned()), 4, "ordering only; nothing is dropped")

    async def test_order_is_stable_within_a_priority_class(self):
        for name in ("a", "b", "c"):
            await main._enqueue_pending_mine(
                {"dir": f"/t/{name}.jsonl", "wing": "w", "mode": "convos"}
            )

        with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
            await main._drain_pending_mines()

        self.assertEqual(self._dirs_spawned(), ["/t/a.jsonl", "/t/b.jsonl", "/t/c.jsonl"])

    async def test_cap_counts_deduped_targets_not_raw_lines(self):
        """142 production lines were 28 targets; a raw-line cap wastes the pass."""
        for _ in range(30):
            await main._enqueue_pending_mine(
                {"dir": "/t/same.jsonl", "wing": "w", "mode": "convos"}
            )
        await main._enqueue_pending_mine({"dir": "/t/other.jsonl", "wing": "w", "mode": "convos"})

        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "2"}, clear=False):
            with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
                count = await main._drain_pending_mines()

        self.assertEqual(count, 2)
        self.assertEqual(sorted(self._dirs_spawned()), ["/t/other.jsonl", "/t/same.jsonl"])
        self.assertEqual(self._queue_dirs(), [], "nothing left: 31 lines were 2 targets")

    async def test_batch_size_is_configurable_and_survives_a_bad_value(self):
        for i in range(6):
            await main._enqueue_pending_mine(
                {"dir": f"/t/{i}.jsonl", "wing": "w", "mode": "convos"}
            )

        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "3"}, clear=False):
            with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
                self.assertEqual(await main._drain_pending_mines(), 3)

        self.spawned.clear()
        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "not-a-number"}, clear=False):
            with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
                # Falls back to the default cap, which is > the 3 that remain.
                self.assertEqual(await main._drain_pending_mines(), 3)

    async def test_an_absurd_batch_size_is_clamped_not_honoured(self):
        """The override must not be a way to silently remove the cap.

        A number large enough to exceed any real queue restores exactly the
        whole-file-in-one-pass behaviour the cap exists to prevent, and it
        parses cleanly, so the non-numeric guard never sees it.
        """
        for i in range(4):
            await main._enqueue_pending_mine(
                {"dir": f"/t/{i}.jsonl", "wing": "w", "mode": "convos"}
            )

        with patch.dict(
            os.environ, {"MEMPALACE_DRAIN_BATCH": "99999999999999999999"}, clear=False
        ):
            self.assertEqual(main._drain_batch_size(), main._DRAIN_BATCH_MAX)

        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "501"}, clear=False):
            self.assertEqual(main._drain_batch_size(), main._DRAIN_BATCH_MAX)

        # The ceiling is still a working cap, not a disguised "unlimited".
        self.assertLess(main._DRAIN_BATCH_MAX, 99999999999999999999)

    async def test_a_repeatedly_deferred_entry_is_promoted(self):
        """Priority must not become starvation.

        Today's mix makes this unlikely -- the high-priority class was 2 of
        142 entries -- but "unlikely given the current mix" is a premise that
        expires, and an unbounded deferral is not something to leave to it.
        """
        await main._enqueue_pending_mine({"dir": "/t/old.jsonl", "wing": "w", "mode": "convos"})

        with patch.dict(os.environ, {"MEMPALACE_DRAIN_BATCH": "1"}, clear=False):
            for i in range(main._DRAIN_PRIORITY_ESCAPE_DEFERRALS):
                # Each pass, a fresh high-priority mine arrives and wins the
                # single slot -- until the deferred transcript is promoted.
                await main._enqueue_pending_mine(
                    {"dir": f"/p/mem{i}", "wing": "w", "mode": "projects"}
                )
                self.spawned.clear()
                with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
                    await main._drain_pending_mines()

            await main._enqueue_pending_mine({"dir": "/p/memN", "wing": "w", "mode": "projects"})
            self.spawned.clear()
            with patch("asyncio.create_subprocess_exec", side_effect=self._ok_subprocess()):
                await main._drain_pending_mines()

        self.assertEqual(
            self._dirs_spawned(),
            ["/t/old.jsonl"],
            "after enough deferrals the transcript outranks a fresh projects mine",
        )


class TestSkipUnchangedTranscriptRemines(unittest.IsolatedAsyncioTestCase):
    """A transcript whose bytes have not moved must not be re-embedded (#260).

    Every checkpoint re-queues the session's ``.jsonl``. When the file has
    not grown since the last SUCCESSFUL mine, replaying it re-reads,
    re-chunks and re-embeds byte-identical content, and holds the palace
    write lock while doing it.

    The witness is (mtime, size), recorded daemon-side in a small JSON file
    keyed by path. Stated bound, because it is not content identity: an
    in-place edit that preserves both would be missed. Transcripts are
    append-only, so that is not a shape they take — and the check is
    deliberately restricted to FILES, since a directory's mtime says nothing
    about what changed inside it.
    """

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._queue_path = os.path.join(self.tmp.name, "pending-mines.jsonl")
        self._state_path = os.path.join(self.tmp.name, "mined-state.json")
        self._patches = [
            patch.object(main, "_pending_mines_path", return_value=self._queue_path),
            patch.object(main, "_mined_state_path", return_value=self._state_path),
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_mineable_path_problem", return_value=None),
        ]
        for p in self._patches:
            p.start()
        self.spawned: list = []

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _transcript(self, name="s.jsonl", body='{"a":1}\n'):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def _ok_subprocess(self):
        async def _fake(*args, **kwargs):
            self.spawned.append(list(args))
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        return _fake

    def _failing_subprocess(self):
        async def _fake(*args, **kwargs):
            self.spawned.append(list(args))
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b"boom"))
            proc.returncode = 1
            return proc

        return _fake

    async def _drain(self, subprocess_factory=None):
        factory = subprocess_factory or self._ok_subprocess()
        with patch("asyncio.create_subprocess_exec", side_effect=factory):
            return await main._drain_pending_mines()

    async def test_first_mine_of_a_transcript_runs(self):
        t = self._transcript()
        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        self.assertEqual(await self._drain(), 1)
        self.assertEqual(len(self.spawned), 1)

    async def test_unchanged_transcript_is_skipped_on_the_next_pass(self):
        t = self._transcript()
        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        await self._drain()
        self.spawned.clear()

        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        count = await self._drain()

        self.assertEqual(self.spawned, [], "byte-identical transcript must not be re-embedded")
        self.assertEqual(count, 0, "a skip is not a mine")
        self.assertFalse(os.path.isfile(self._queue_path), "the skipped entry is consumed")

    async def test_a_grown_transcript_is_mined_again(self):
        t = self._transcript()
        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        await self._drain()
        self.spawned.clear()

        with open(t, "a", encoding="utf-8") as f:
            f.write('{"b":2}\n')
        os.utime(t, (time.time() + 5, time.time() + 5))

        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        self.assertEqual(await self._drain(), 1)
        self.assertEqual(len(self.spawned), 1)

    async def test_same_mtime_but_different_size_still_mines(self):
        """Either half of the witness moving is enough to re-mine."""
        t = self._transcript()
        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        await self._drain()
        stamp = os.stat(t)
        self.spawned.clear()

        with open(t, "a", encoding="utf-8") as f:
            f.write('{"b":2}\n')
        os.utime(t, (stamp.st_atime, stamp.st_mtime))  # mtime restored, size changed

        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        self.assertEqual(await self._drain(), 1)

    async def test_a_failed_mine_is_not_recorded_as_done(self):
        """Only a successful replay may suppress the next one."""
        t = self._transcript()
        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        await self._drain(self._failing_subprocess())
        self.spawned.clear()

        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        self.assertEqual(await self._drain(), 1, "a failed mine must not mark the file done")

    async def test_directories_are_never_skipped(self):
        """A directory's mtime says nothing about the files inside it."""
        d = os.path.join(self.tmp.name, "memory")
        os.mkdir(d)
        for _ in range(2):
            await main._enqueue_pending_mine({"dir": d, "wing": "w", "mode": "projects"})
            await self._drain()
        self.assertEqual(len(self.spawned), 2)

    async def test_a_different_mode_for_the_same_file_still_runs(self):
        """convos and session mine the same transcript into different shapes."""
        t = self._transcript()
        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        await self._drain()
        self.spawned.clear()

        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "session"})
        self.assertEqual(await self._drain(), 1)

    async def test_state_survives_a_reload_and_is_json_keyed_by_path(self):
        t = self._transcript()
        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        await self._drain()

        with open(self._state_path, encoding="utf-8") as f:
            state = json.load(f)
        self.assertIn(f"{t}::convos", state)
        self.assertIn("size", state[f"{t}::convos"])
        self.assertIn("mtime", state[f"{t}::convos"])

    async def test_bytes_appended_during_the_mine_are_not_marked_as_read(self):
        """The witness must describe what the mine READ, not what it left.

        A live session appends while its checkpoint mine runs. Statting the
        file AFTER the subprocess returns bakes those bytes into the witness
        without the mine ever having seen them, and the next checkpoint then
        finds the file "unchanged" and skips the tail — silently. The
        PreCompact / final save is precisely the mine most likely to race a
        session's last writes, so the lost tail is the most valuable one.
        """
        t = self._transcript(body="x" * 1000 + "\n")
        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})

        async def _appends_midway(*args, **kwargs):
            self.spawned.append(list(args))
            # The session writes two more exchanges while the mine reads.
            with open(t, "a", encoding="utf-8") as f:
                f.write("y" * 200 + "\n")
            os.utime(t, (time.time() + 5, time.time() + 5))
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_appends_midway):
            await main._drain_pending_mines()
        self.spawned.clear()

        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        count = await self._drain()

        self.assertEqual(count, 1, "the appended tail was never read; it must be mined")
        self.assertEqual(len(self.spawned), 1)

    async def test_the_recorded_witness_is_the_pre_spawn_one(self):
        """Same mechanism, asserted on the stored bytes rather than behaviour."""
        t = self._transcript(body="x" * 1000 + "\n")
        size_before = os.stat(t).st_size
        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})

        async def _appends_midway(*args, **kwargs):
            with open(t, "a", encoding="utf-8") as f:
                f.write("y" * 200 + "\n")
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=_appends_midway):
            await main._drain_pending_mines()

        with open(self._state_path, encoding="utf-8") as f:
            recorded = json.load(f)[f"{t}::convos"]
        self.assertEqual(recorded["size"], size_before)
        self.assertNotEqual(recorded["size"], os.stat(t).st_size)

    async def test_a_corrupt_state_file_does_not_block_mining(self):
        """Unreadable bookkeeping must fail toward doing the work."""
        with open(self._state_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        t = self._transcript()
        await main._enqueue_pending_mine({"dir": t, "wing": "w", "mode": "convos"})
        self.assertEqual(await self._drain(), 1)

    async def test_an_unstatable_path_is_mined_rather_than_skipped(self):
        """The daemon may not see a path its own miner can (path mapping)."""
        await main._enqueue_pending_mine(
            {"dir": "/definitely/not/here.jsonl", "wing": "w", "mode": "convos"}
        )
        self.assertEqual(await self._drain(), 1)
