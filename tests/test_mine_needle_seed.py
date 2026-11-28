"""Mine children take the daemon's redact needles through one inherited pipe (main._needle_seed).

Flow 3a002409 (2026-11-27): each ``mempalace mine`` child fetched every vault needle itself, serially,
30 s limit each; under a re-mine burst a fetch took ~8 s and some passed 30 s, so a child with no last
good value failed closed. The daemon's refresher already holds good values: it hands them over.
"""

import ast
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main

SEED_ENV = "MEMPALACE_NEEDLE_FD"


def _fake_redact(make_fd, version=1):
    mod = types.ModuleType("mempalace.redact")
    if version is not None:
        mod.NEEDLE_SEED_VERSION = version
        mod.NEEDLE_SEED_ENV = SEED_ENV
        mod.needle_seed_fd = make_fd
    return mod


def _pipe_fd():
    r, w = os.pipe()
    os.write(w, b"{}")
    os.close(w)
    return r


def _is_open(fd):
    try:
        os.fstat(fd)
        return True
    except OSError:
        return False


class TestNeedleSeed(unittest.TestCase):
    def _with(self, mod):
        pkg = sys.modules.get("mempalace")
        return patch.dict(sys.modules, {"mempalace.redact": mod}), patch.object(
            pkg, "redact", mod, create=True
        )

    def test_a_seed_is_one_fd_named_in_the_env_and_closed_after_the_spawn(self):
        made = []

        def make():
            made.append(_pipe_fd())
            return made[-1]

        a, b = self._with(_fake_redact(make))
        with a, b:
            with main._needle_seed() as (fds, env):
                self.assertEqual(fds, (made[0],))
                self.assertEqual(env, {SEED_ENV: str(made[0])})
                self.assertTrue(_is_open(made[0]))
        self.assertFalse(_is_open(made[0]))

    def test_an_older_mempalace_spawns_as_before(self):
        a, b = self._with(_fake_redact(None, version=None))
        with a, b:
            with main._needle_seed() as seed:
                self.assertEqual(seed, ((), {}))

    def test_an_unknown_seed_version_is_not_used(self):
        a, b = self._with(_fake_redact(lambda: self.fail("called"), version=2))
        with a, b:
            with main._needle_seed() as seed:
                self.assertEqual(seed, ((), {}))

    def test_no_values_or_a_failing_seed_spawns_as_before(self):
        for make in (lambda: None, lambda: (_ for _ in ()).throw(OSError("no pipe"))):
            a, b = self._with(_fake_redact(make))
            with a, b:
                with main._needle_seed() as seed:
                    self.assertEqual(seed, ((), {}))

    def test_the_fd_is_closed_even_when_the_spawn_raises(self):
        made = []

        def make():
            made.append(_pipe_fd())
            return made[-1]

        a, b = self._with(_fake_redact(make))
        with a, b:
            with self.assertRaises(RuntimeError):
                with main._needle_seed():
                    raise RuntimeError("spawn failed")
        self.assertFalse(_is_open(made[0]))


class TestEveryMineSpawnPassesTheSeed(unittest.TestCase):
    def test_every_subprocess_spawn_in_main_passes_pass_fds_and_env(self):
        # A new spawn site that forgets the seed would fetch every needle again in its child.
        tree = ast.parse(pathlib.Path(main.__file__).read_text())
        spawns = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "create_subprocess_exec"
        ]
        self.assertEqual(len(spawns), 3)
        for call in spawns:
            kws = {k.arg for k in call.keywords}
            self.assertTrue({"pass_fds", "env"} <= kws, f"line {call.lineno}: {sorted(kws)}")


class TestDrainSpawnCarriesTheSeed(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        queue = os.path.join(self.tmp.name, "pending-mines.jsonl")
        self._patches = [
            patch.object(main, "_pending_mines_path", return_value=queue),
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_mineable_path_problem", return_value=None),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    async def test_a_drain_mine_gets_the_fd_and_the_parent_closes_it(self):
        await main._enqueue_pending_mine({"dir": "/t/a.jsonl", "wing": "w", "mode": "convos"})
        made = []

        def make():
            made.append(_pipe_fd())
            return made[-1]

        seen = {}

        async def _spawn(*args, **kwargs):
            seen.update(kwargs, open_at_spawn=_is_open(made[0]))
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(b"", b""))
            proc.wait = AsyncMock(return_value=0)
            proc.returncode = 0
            proc.pid = 4242
            return proc

        mod = _fake_redact(make)
        with patch.dict(sys.modules, {"mempalace.redact": mod}), patch.object(
            sys.modules["mempalace"], "redact", mod, create=True
        ):
            with patch("asyncio.create_subprocess_exec", side_effect=_spawn):
                await main._drain_pending_mines()
        self.assertEqual(seen["pass_fds"], (made[0],))
        self.assertEqual(seen["env"][SEED_ENV], str(made[0]))
        self.assertIn(main._MINE_PHASE_ENV, seen["env"])
        self.assertTrue(seen["open_at_spawn"])
        self.assertFalse(_is_open(made[0]))


if __name__ == "__main__":
    unittest.main()
