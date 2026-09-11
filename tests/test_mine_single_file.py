"""``POST /mine`` accepts a single non-.jsonl file in projects mode (#252).

A mine target used to be a directory, or a single ``.jsonl`` conversation
file. That covers transcripts, which hooks post one at a time, and nothing
else — so refreshing ONE curated document (a project's ``CLAUDE.md`` after a
claim in it was refuted) meant posting the whole project directory and
holding the palace write lock for the length of that walk. The corpus that
surfaced this is 111 MB; the measured worst case for a whole-project mine on
this palace is 6 hours (mempalace#414/#426).

Projects mode now accepts a regular file. The guards that stay: the suffix
must be one mempalace would actually read as text (its own
``READABLE_EXTENSIONS``, so the daemon cannot accept a file the miner will
then silently drop), and the file must be under the single-file size cap —
a 50 MB text file chunks into the same lock-hold this feature exists to
avoid. Convos and session modes are unchanged: ``.jsonl`` only.

The mempalace side is techempower-org/mempalace#451.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_mine_single_file.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


# ---------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------


class TestMineablePathProjectsMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name, content="x" * 64):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        from pathlib import Path

        return Path(path)

    def test_markdown_file_is_mineable_in_projects_mode(self):
        self.assertIs(main._is_mineable_path(self._file("CLAUDE.md"), mode="projects"), True)

    def test_text_file_is_mineable_in_projects_mode(self):
        self.assertIs(main._is_mineable_path(self._file("notes.txt"), mode="projects"), True)

    def test_same_file_is_not_mineable_in_convos_mode(self):
        """Regression guard: convos/session stay .jsonl-only."""
        target = self._file("notes.txt")
        self.assertIs(main._is_mineable_path(target), False)
        self.assertIs(main._is_mineable_path(target, mode="convos"), False)
        self.assertIs(main._is_mineable_path(target, mode="session"), False)

    def test_jsonl_stays_mineable_in_the_transcript_modes(self):
        target = self._file("session.jsonl", '{"a": 1}\n')
        self.assertIs(main._is_mineable_path(target), True)
        self.assertIs(main._is_mineable_path(target, mode="convos"), True)
        self.assertIs(main._is_mineable_path(target, mode="session"), True)

    def test_directory_stays_mineable_in_every_mode(self):
        from pathlib import Path

        self.assertIs(main._is_mineable_path(Path(self.dir), mode="projects"), True)
        self.assertIs(main._is_mineable_path(Path(self.dir), mode="convos"), True)

    def test_jsonl_is_refused_in_projects_mode(self):
        """`.jsonl` is IN mempalace's READABLE_EXTENSIONS (61 of them), so the
        suffix whitelist alone admits a transcript into projects mode. The
        mempalace CLI then exits 2 (mempalace#455), which on the background
        path is visible only in the daemon log — where a clean 400 was
        available at the gate. Refuse here and name the mode that works.
        """
        target = self._file("session.jsonl", '{"a": 1}\n')
        self.assertIs(main._is_mineable_path(target, mode="projects"), False)
        problem = main._mineable_path_problem(target, mode="projects")
        self.assertIn("--mode convos", problem)

    def test_a_directory_of_transcripts_is_still_mineable_in_projects_mode(self):
        """Only a NAMED .jsonl is refused. A directory containing transcripts
        is a normal projects mine — `.jsonl` is in READABLE_EXTENSIONS on
        purpose (mempalace's test_miner_jsonl_visibility) and a tree walk
        should keep picking them up."""
        from pathlib import Path

        self._file("session.jsonl", '{"a": 1}\n')
        self.assertIs(main._is_mineable_path(Path(self.dir), mode="projects"), True)

    def test_binary_suffix_is_refused_in_projects_mode(self):
        """The miner would not read it, so the daemon must not accept it."""
        self.assertIs(main._is_mineable_path(self._file("photo.png"), mode="projects"), False)

    def test_oversized_file_is_refused_in_projects_mode(self):
        target = self._file("huge.md")
        with patch.object(main, "_MINE_MAX_SINGLE_FILE_BYTES", 8):
            self.assertIs(main._is_mineable_path(target, mode="projects"), False)
        # Positive control: the same file passes under the real cap.
        self.assertIs(main._is_mineable_path(target, mode="projects"), True)

    def test_missing_path_is_refused_in_projects_mode(self):
        from pathlib import Path

        missing = Path(self.dir) / "nope.md"
        self.assertIs(main._is_mineable_path(missing, mode="projects"), False)

    def test_problem_helper_explains_each_refusal(self):
        """The 400 body has to name the actual reason, not a generic one."""
        from pathlib import Path

        self.assertIsNone(main._mineable_path_problem(self._file("ok.md"), mode="projects"))
        self.assertIn("png", main._mineable_path_problem(self._file("p.png"), mode="projects"))
        self.assertIn(
            ".jsonl", main._mineable_path_problem(self._file("n.txt"), mode="convos")
        )
        self.assertTrue(main._mineable_path_problem(Path(self.dir) / "gone.md", mode="projects"))


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------


def _fake_subprocess_factory(returncode=0, stdout=b"mined 3 drawers", stderr=b""):
    async def _spawn(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        proc.returncode = returncode
        return proc

    return _spawn


class TestMineRouteAcceptsOneFile(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = os.path.join(self.tmp.name, "CLAUDE.md")
        with open(self.doc, "w", encoding="utf-8") as handle:
            handle.write("# doc\n" * 40)
        self._patches = [
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_check_auth", side_effect=lambda *_a, **_k: None),
        ]
        for patcher in self._patches:
            patcher.start()
        self._orig_repair = dict(main._repair_state)
        main._repair_state["in_progress"] = False

    async def asyncTearDown(self):
        for patcher in self._patches:
            patcher.stop()
        main._repair_state.clear()
        main._repair_state.update(self._orig_repair)
        self.tmp.cleanup()

    def _request_and_body(self, **overrides):
        from search_models import MineBody

        body = {"dir": self.doc, "wing": "2g", "mode": "projects"}
        body.update(overrides)
        req = MagicMock()
        req.json = AsyncMock(return_value=body)
        req.app.state.active_mines = set()
        return req, MineBody(**body)

    async def test_projects_mode_file_spawns_a_mine_for_that_path(self):
        fake_cfg = MagicMock(backend="postgres", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), patch(
            "asyncio.create_subprocess_exec", side_effect=_fake_subprocess_factory()
        ) as spawn:
            req, body = self._request_and_body()
            await main.mine(req, body, x_api_key=None)

        spawn.assert_called_once()
        argv = list(spawn.call_args.args)
        self.assertIn(self.doc, argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "projects")
        self.assertEqual(argv[argv.index("--wing") + 1], "2g")

    async def test_convos_mode_still_rejects_a_non_jsonl_file(self):
        from fastapi import HTTPException

        fake_cfg = MagicMock(backend="postgres", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), patch(
            "asyncio.create_subprocess_exec", side_effect=_fake_subprocess_factory()
        ) as spawn:
            req, body = self._request_and_body(mode="convos")
            with self.assertRaises(HTTPException) as ctx:
                await main.mine(req, body, x_api_key=None)

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn(".jsonl", str(ctx.exception.detail))
        spawn.assert_not_called()

    async def test_refusal_detail_names_the_path_and_the_reason(self):
        from fastapi import HTTPException

        blob = os.path.join(self.tmp.name, "photo.png")
        with open(blob, "wb") as handle:
            handle.write(b"\x89PNG\r\n")
        fake_cfg = MagicMock(backend="postgres", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg):
            req, body = self._request_and_body(dir=blob)
            with self.assertRaises(HTTPException) as ctx:
                await main.mine(req, body, x_api_key=None)

        detail = str(ctx.exception.detail)
        self.assertIn(blob, detail)
        self.assertIn("png", detail)


# ---------------------------------------------------------------------------
# the drain replays the same shape
# ---------------------------------------------------------------------------


class TestDrainAcceptsProjectsModeFile(unittest.IsolatedAsyncioTestCase):
    async def test_queued_projects_mode_file_is_replayed_not_skipped(self):
        import json

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        doc = os.path.join(tmp.name, "CLAUDE.md")
        with open(doc, "w", encoding="utf-8") as handle:
            handle.write("# doc\n" * 40)
        queue = os.path.join(tmp.name, "pending-mines.jsonl")
        with open(queue, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"payload": {"dir": doc, "wing": "2g", "mode": "projects"}}
                )
                + "\n"
            )

        with patch.object(main, "_pending_mines_path", return_value=queue), patch.object(
            main, "_translate_client_path", side_effect=lambda p: p
        ), patch(
            "asyncio.create_subprocess_exec", side_effect=_fake_subprocess_factory()
        ) as spawn:
            await main._drain_pending_mines()

        spawn.assert_called_once()
        argv = list(spawn.call_args.args)
        self.assertIn(doc, argv)
        self.assertEqual(argv[argv.index("--mode") + 1], "projects")


if __name__ == "__main__":
    unittest.main()


assert asyncio  # imported for the IsolatedAsyncioTestCase machinery above
