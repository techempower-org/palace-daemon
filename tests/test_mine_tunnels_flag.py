"""`POST /mine` `tunnels` field, and the drain's automatic choice (#474).

Every projects-mode mine recomputes the whole wing's derived graph — topic
tunnels, hallways, entity tunnels — however few files changed. Measured on the
palace host: a 31-file memory sweep spent 29+ min of CPU and 1.6-4.1 GB RSS
there, holding the exclusive mine lock, with twelve more sweeps queued behind
it. mempalace#478 added `mempalace mine --no-tunnels`; this wires it to the
daemon.

The field is deliberately TRI-STATE (`true` / `false` / omitted) rather than a
plain bool defaulting to true. The drain has to be able to choose for targets
that are obviously small — a single file, or a `.claude/projects/*/memory`
sweep — but it must not override a caller who explicitly asked for tunnels. A
plain `bool = true` cannot tell "the caller wants tunnels" from "the caller
said nothing", so the automatic rule would silently do less than an explicit
request asked for. Omitted means "you decide"; explicit means "do this".

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_mine_tunnels_flag.py -q
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class TestTunnelsDecision(unittest.TestCase):
    """`_tunnels_for_target` — the automatic rule, in isolation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, name="CLAUDE.md"):
        p = self.dir / name
        p.write_text("# doc\n", encoding="utf-8")
        return p

    def test_explicit_true_is_honoured_even_for_a_single_file(self):
        self.assertIs(main._tunnels_for_target(self._file(), requested=True), True)

    def test_explicit_false_is_honoured_even_for_a_big_directory(self):
        self.assertIs(main._tunnels_for_target(self.dir, requested=False), False)

    def test_omitted_means_skip_for_a_single_file(self):
        self.assertIs(main._tunnels_for_target(self._file(), requested=None), False)

    def test_omitted_means_skip_for_a_memory_sweep_directory(self):
        mem = self.dir / "home" / "jp" / ".claude" / "projects" / "-home-jp-Projects-2g" / "memory"
        mem.mkdir(parents=True)
        self.assertIs(main._tunnels_for_target(mem, requested=None), False)

    def test_omitted_means_skip_with_a_trailing_slash(self):
        mem = self.dir / ".claude" / "projects" / "-proj" / "memory"
        mem.mkdir(parents=True)
        self.assertIs(main._tunnels_for_target(Path(str(mem) + "/"), requested=None), False)

    def test_omitted_means_compute_for_an_ordinary_project_directory(self):
        proj = self.dir / "Projects" / "2g"
        proj.mkdir(parents=True)
        self.assertIs(main._tunnels_for_target(proj, requested=None), True)

    def test_a_memory_named_dir_outside_claude_projects_still_computes(self):
        """The rule is the hook's sweep path, not any directory called memory."""
        other = self.dir / "src" / "memory"
        other.mkdir(parents=True)
        self.assertIs(main._tunnels_for_target(other, requested=None), True)


def _fake_subprocess_factory(returncode=0, stdout=b"mined 3 drawers", stderr=b""):
    async def _spawn(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        proc.returncode = returncode
        return proc

    return _spawn


class TestMineRouteTunnelsField(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.doc = os.path.join(self.dir, "CLAUDE.md")
        with open(self.doc, "w", encoding="utf-8") as fh:
            fh.write("# doc\n" * 40)
        self._patches = [
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_check_auth", side_effect=lambda *_a, **_k: None),
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

    def _request_and_body(self, **overrides):
        from search_models import MineBody

        body = {"dir": self.dir, "wing": "2g", "mode": "projects"}
        body.update(overrides)
        req = MagicMock()
        req.json = AsyncMock(return_value=body)
        req.app.state.active_mines = set()
        return req, MineBody(**body)

    async def _spawn_argv(self, **overrides):
        fake_cfg = MagicMock(backend="postgres", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), patch(
            "asyncio.create_subprocess_exec", side_effect=_fake_subprocess_factory()
        ) as spawn:
            req, body = self._request_and_body(**overrides)
            await main.mine(req, body, x_api_key=None)
        spawn.assert_called_once()
        return list(spawn.call_args.args)

    async def test_tunnels_false_passes_no_tunnels(self):
        self.assertIn("--no-tunnels", await self._spawn_argv(tunnels=False))

    async def test_tunnels_true_does_not_pass_the_flag(self):
        self.assertNotIn("--no-tunnels", await self._spawn_argv(tunnels=True))

    async def test_directory_default_computes_tunnels(self):
        """Control: an ordinary directory keeps today's behaviour."""
        self.assertNotIn("--no-tunnels", await self._spawn_argv())

    async def test_single_file_default_skips_tunnels(self):
        self.assertIn("--no-tunnels", await self._spawn_argv(dir=self.doc))

    async def test_single_file_with_explicit_true_still_computes(self):
        argv = await self._spawn_argv(dir=self.doc, tunnels=True)
        self.assertNotIn("--no-tunnels", argv)

    async def test_background_queue_preserves_the_requested_value(self):
        """A queued mine must replay with the caller's own choice, not a
        re-derived one — the payload is the record of what was asked."""
        captured = {}

        async def fake_enqueue(payload):
            captured.update(payload)

        fake_cfg = MagicMock(backend="postgres", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), patch.object(
            main, "_enqueue_pending_mine", side_effect=fake_enqueue
        ), patch.object(main, "_kick_mine_drain"):
            req, body = self._request_and_body(background=True, tunnels=True)
            await main.mine(req, body, x_api_key=None)

        self.assertIs(captured.get("tunnels"), True)


class TestDrainTunnels(unittest.IsolatedAsyncioTestCase):
    async def _drain_argv(self, payload_extra, target_is_file=True):
        import json

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if target_is_file:
            target = os.path.join(tmp.name, "CLAUDE.md")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("# doc\n" * 40)
        else:
            target = tmp.name
        queue = os.path.join(tmp.name, "pending-mines.jsonl")
        payload = {"dir": target, "wing": "2g", "mode": "projects"}
        payload.update(payload_extra)
        with open(queue, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"payload": payload}) + "\n")

        with patch.object(main, "_pending_mines_path", return_value=queue), patch.object(
            main, "_translate_client_path", side_effect=lambda p: p
        ), patch(
            "asyncio.create_subprocess_exec", side_effect=_fake_subprocess_factory()
        ) as spawn:
            await main._drain_pending_mines()
        spawn.assert_called_once()
        return list(spawn.call_args.args)

    async def test_drain_passes_no_tunnels_when_false(self):
        self.assertIn("--no-tunnels", await self._drain_argv({"tunnels": False}))

    async def test_drain_omits_the_flag_when_true(self):
        self.assertNotIn("--no-tunnels", await self._drain_argv({"tunnels": True}))

    async def test_drain_decides_for_a_single_file_when_omitted(self):
        self.assertIn("--no-tunnels", await self._drain_argv({}))

    async def test_drain_computes_for_a_plain_directory_when_omitted(self):
        argv = await self._drain_argv({}, target_is_file=False)
        self.assertNotIn("--no-tunnels", argv)


if __name__ == "__main__":
    unittest.main()


class TestStderrExcerpt(unittest.TestCase):
    """Version skew has to be legible in the drain's failure log (#474).

    An older mempalace rejects `--no-tunnels` with exit 2. The drain
    quarantines the entry correctly, but the log printed `stderr[:300]` —
    and argparse puts the usage block first and the actual reason
    ("error: unrecognized arguments: --no-tunnels") at roughly offset 556.
    So the one line naming the cause was the one line never shown.
    """

    ARGPARSE_STDERR = (
        "usage: mempalace mine [-h] [--backend BACKEND] "
        + ("[--some-very-long-option VALUE] " * 20)
        + "\nmempalace mine: error: unrecognized arguments: --no-tunnels\n"
    ).encode()

    def test_the_error_line_survives_truncation(self):
        out = main._stderr_excerpt(self.ARGPARSE_STDERR)
        self.assertIn("unrecognized arguments: --no-tunnels", out)

    def test_the_error_line_is_beyond_the_old_300_char_cut(self):
        """Control: proves the fixture actually reproduces the bug."""
        old_behaviour = self.ARGPARSE_STDERR[:1200].decode(errors="replace")[:300]
        self.assertNotIn("unrecognized arguments", old_behaviour)

    def test_plain_stderr_without_an_error_line_keeps_the_tail(self):
        noisy = ("filler line\n" * 200 + "the thing that actually broke\n").encode()
        out = main._stderr_excerpt(noisy)
        self.assertIn("the thing that actually broke", out)

    def test_empty_and_none_are_safe(self):
        self.assertEqual(main._stderr_excerpt(b""), "")
        self.assertEqual(main._stderr_excerpt(None), "")

    def test_output_stays_bounded(self):
        self.assertLessEqual(len(main._stderr_excerpt(b"x" * 100_000)), 600)
