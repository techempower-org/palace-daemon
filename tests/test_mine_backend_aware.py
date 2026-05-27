"""Tests for the backend-aware /mine guard (#29).

On the chroma backend, /mine must wrap the mine subprocess in the
lock-and-reopen choreography: enter ``_exclusive_palace()``, drop+close
the daemon's client (``_drop_chroma_client(close=True)``), spawn the
subprocess, then reopen via ``_mp._get_collection(True)`` in a finally.
On postgres it must keep the original lightweight path — no exclusive
lock, no client close — because postgres tolerates concurrent clients.

These invoke the ``mine()`` handler coroutine directly with a mocked
Request and a mocked backend; the subprocess is stubbed by patching
``asyncio.create_subprocess_exec``. No live daemon or palace required.

Run with::

    python -m unittest tests.test_mine_backend_aware -v
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class _FakeExclusive:
    """Stand-in for _exclusive_palace() that records whether it was entered,
    without touching the real semaphores."""

    def __init__(self):
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True
        return False


def _fake_subprocess_factory(returncode=0, stdout=b"mined 3 drawers", stderr=b""):
    async def _spawn(*args, **kwargs):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        proc.returncode = returncode
        return proc
    return _spawn


class TestMineBackendAware(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # A real directory so the handler's is_absolute/exists/is_dir checks pass.
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

        self._patches = [
            patch.object(main, "_translate_client_path", side_effect=lambda p: p),
            patch.object(main, "_check_auth", side_effect=lambda *_a, **_k: None),
        ]
        for p in self._patches:
            p.start()
        # Not in a rebuild — skip the repair-queue branch.
        self._orig_repair = dict(main._repair_state)
        main._repair_state["in_progress"] = False

    async def asyncTearDown(self):
        for p in self._patches:
            p.stop()
        main._repair_state.clear()
        main._repair_state.update(self._orig_repair)
        self.tmp.cleanup()

    def _request(self, **body_overrides):
        body = {"dir": self.dir, "wing": "general", "mode": "convos"}
        body.update(body_overrides)
        req = MagicMock()
        req.json = AsyncMock(return_value=body)
        return req

    async def test_chroma_enters_exclusive_closes_and_reopens(self):
        fake_excl = _FakeExclusive()
        fake_cfg = MagicMock(backend="chroma", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), \
             patch.object(main, "_exclusive_palace", lambda: fake_excl), \
             patch.object(main, "_drop_chroma_client") as drop, \
             patch.object(main._mp, "_get_collection") as reopen, \
             patch("asyncio.create_subprocess_exec",
                   side_effect=_fake_subprocess_factory()) as spawn:
            result = await main.mine(self._request(), x_api_key=None)

        self.assertTrue(fake_excl.entered, "chroma must hold the exclusive palace lock")
        self.assertTrue(fake_excl.exited)
        drop.assert_called_once_with(close=True)
        spawn.assert_called_once()
        reopen.assert_called_once_with(True)
        self.assertEqual(result["returncode"], 0)

    async def test_postgres_keeps_lightweight_path(self):
        fake_excl = _FakeExclusive()
        fake_cfg = MagicMock(backend="postgres", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), \
             patch.object(main, "_exclusive_palace", lambda: fake_excl), \
             patch.object(main, "_drop_chroma_client") as drop, \
             patch.object(main._mp, "_get_collection") as reopen, \
             patch("asyncio.create_subprocess_exec",
                   side_effect=_fake_subprocess_factory()) as spawn:
            result = await main.mine(self._request(), x_api_key=None)

        self.assertFalse(fake_excl.entered, "postgres must NOT hold the exclusive lock")
        drop.assert_not_called()
        reopen.assert_not_called()
        spawn.assert_called_once()
        self.assertEqual(result["returncode"], 0)

    async def test_chroma_reopens_even_when_mine_fails(self):
        """The reopen lives in a finally — a non-zero subprocess exit must
        still restore the daemon's client."""
        fake_excl = _FakeExclusive()
        fake_cfg = MagicMock(backend="chroma", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), \
             patch.object(main, "_exclusive_palace", lambda: fake_excl), \
             patch.object(main, "_drop_chroma_client") as drop, \
             patch.object(main._mp, "_get_collection") as reopen, \
             patch("asyncio.create_subprocess_exec",
                   side_effect=_fake_subprocess_factory(returncode=1, stdout=b"", stderr=b"boom")):
            result = await main.mine(self._request(), x_api_key=None)

        drop.assert_called_once_with(close=True)
        # Reopen lives in a finally — must fire even though the mine failed.
        reopen.assert_called_once_with(True)
        self.assertEqual(result["returncode"], 1)

    async def test_chroma_reopens_when_teardown_raises(self):
        """Teardown lives inside the try, so a _drop_chroma_client failure
        must still hit the finally and reopen — the error then propagates."""
        fake_excl = _FakeExclusive()
        fake_cfg = MagicMock(backend="chroma", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), \
             patch.object(main, "_exclusive_palace", lambda: fake_excl), \
             patch.object(main, "_drop_chroma_client",
                          side_effect=RuntimeError("close boom")), \
             patch.object(main._mp, "_get_collection") as reopen, \
             patch("asyncio.create_subprocess_exec",
                   side_effect=_fake_subprocess_factory()):
            with self.assertRaises(RuntimeError):
                await main.mine(self._request(), x_api_key=None)

        reopen.assert_called_once_with(True)
        self.assertTrue(fake_excl.exited, "exclusive lock must release after reopen")

    async def test_chroma_reopens_on_cancelled_body(self):
        """A cancellation-shaped failure in the body must still run the
        finally reopen before the CancelledError propagates (#29 race)."""
        fake_excl = _FakeExclusive()
        fake_cfg = MagicMock(backend="chroma", palace_path="/tmp/palace")

        async def _cancel(*_a, **_k):
            raise asyncio.CancelledError()

        with patch.object(main._mp, "_config", fake_cfg), \
             patch.object(main, "_exclusive_palace", lambda: fake_excl), \
             patch.object(main, "_drop_chroma_client") as drop, \
             patch.object(main._mp, "_get_collection") as reopen, \
             patch("asyncio.create_subprocess_exec", side_effect=_cancel):
            with self.assertRaises(asyncio.CancelledError):
                await main.mine(self._request(), x_api_key=None)

        drop.assert_called_once_with(close=True)
        reopen.assert_called_once_with(True)
        self.assertTrue(fake_excl.exited)

    async def test_chroma_self_heals_when_reopen_throws(self):
        """If the reopen itself throws, the handler must not crash — caches
        stay None and the next request lazily reopens. The mine result is
        still returned."""
        fake_excl = _FakeExclusive()
        fake_cfg = MagicMock(backend="chroma", palace_path="/tmp/palace")
        with patch.object(main._mp, "_config", fake_cfg), \
             patch.object(main, "_exclusive_palace", lambda: fake_excl), \
             patch.object(main, "_drop_chroma_client"), \
             patch.object(main._mp, "_get_collection", side_effect=RuntimeError("reopen boom")), \
             patch("asyncio.create_subprocess_exec",
                   side_effect=_fake_subprocess_factory()):
            result = await main.mine(self._request(), x_api_key=None)

        # Handler swallows the reopen failure (logged CRITICAL) and returns.
        self.assertEqual(result["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
