"""Mine children get an allowlisted environment and the daemon's redact needles through one inherited pipe.

Flow 3a002409 (2026-11-27): each ``mempalace mine`` child fetched every vault needle itself, serially,
30 s limit each; under a re-mine burst a fetch took ~8 s and some passed 30 s, so a child with no last
good value failed closed. The daemon's refresher already holds good values: it hands them over
(main._needle_seed, main._spawn_mine). Lead ruling on #360: the child's environment is an allowlist (no
vault session, no credential-shaped name, no needle value), and one end-to-end test runs the real spawn
path with a real child reading the real fd.
"""

import ast
import asyncio
import hashlib
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
import types
import unittest
from unittest.mock import patch

import main

SEED_ENV = "MEMPALACE_NEEDLE_FD"
# Built at run time; lower case only, so no shape rule catches it and only a needle can.
NEEDLE = "canary" + "needlevalue"


def _real_redact():
    try:
        from mempalace import redact
    except Exception:  # noqa: BLE001
        return None
    return redact if getattr(redact, "NEEDLE_SEED_VERSION", None) == 1 else None


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


def _with(mod):
    return patch.dict(sys.modules, {"mempalace.redact": mod}), patch.object(
        sys.modules["mempalace"], "redact", mod, create=True
    )


class TestNeedleSeed(unittest.TestCase):
    def test_a_seed_is_one_fd_named_in_the_env_and_closed_after_the_spawn(self):
        made = []

        def make():
            made.append(_pipe_fd())
            return made[-1]

        a, b = _with(_fake_redact(make))
        with a, b:
            with main._needle_seed() as (fds, env):
                self.assertEqual(fds, (made[0],))
                self.assertEqual(env, {SEED_ENV: str(made[0])})
                self.assertTrue(_is_open(made[0]))
        self.assertFalse(_is_open(made[0]))

    def test_an_older_mempalace_spawns_as_before(self):
        a, b = _with(_fake_redact(None, version=None))
        with a, b:
            with main._needle_seed() as seed:
                self.assertEqual(seed, ((), {}))

    def test_an_unknown_seed_version_is_not_used(self):
        a, b = _with(_fake_redact(lambda: self.fail("called"), version=2))
        with a, b:
            with main._needle_seed() as seed:
                self.assertEqual(seed, ((), {}))

    def test_a_failing_seed_spawns_as_before_and_logs_only_the_exception_type(self):
        def boom():
            raise RuntimeError(f"wrote {NEEDLE} somewhere")

        for make in (lambda: None, boom):
            a, b = _with(_fake_redact(make))
            with a, b, self.assertLogs(level="DEBUG") as logs:
                main._log.warning("marker")  # assertLogs needs one record either way
                with main._needle_seed() as seed:
                    self.assertEqual(seed, ((), {}))
            text = "\n".join(logs.output)
            self.assertNotIn(NEEDLE, text)
            if make is boom:
                self.assertIn("RuntimeError", text)

    def test_the_fd_is_closed_even_when_the_spawn_raises(self):
        made = []

        def make():
            made.append(_pipe_fd())
            return made[-1]

        a, b = _with(_fake_redact(make))
        with a, b:
            with self.assertRaises(RuntimeError):
                with main._needle_seed():
                    raise RuntimeError("spawn failed")
        self.assertFalse(_is_open(made[0]))


class TestEveryMineGoesThroughOneSpawn(unittest.TestCase):
    def test_main_has_one_subprocess_spawn_and_three_mine_sites_use_it(self):
        # A new spawn site that bypassed _spawn_mine would hand its child the daemon's whole environment
        # and make the child fetch every needle again.
        tree = ast.parse(pathlib.Path(main.__file__).read_text())
        spawns = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("create_subprocess_exec", "create_subprocess_shell")
        ]
        self.assertEqual(len(spawns), 1)
        owner = next(
            f for f in ast.walk(tree)
            if isinstance(f, ast.AsyncFunctionDef) and f.name == "_spawn_mine"
        )
        self.assertIn(spawns[0], list(ast.walk(owner)))
        kws = {k.arg: k.value for k in spawns[0].keywords}
        self.assertEqual(ast.unparse(kws["env"]), "_mine_child_env({**(extra_env or {}), **seed_env})")
        self.assertEqual(ast.unparse(kws["pass_fds"]), "seed_fds")
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_spawn_mine"
        ]
        self.assertEqual(len(calls), 3)


PRINT_ENV = "import json, os; print(json.dumps(sorted(os.environ)))"


class TestMineChildEnvironment(unittest.IsolatedAsyncioTestCase):
    """A real child, started through the real spawn path, prints the names it inherited."""

    PARENT = {
        # allowlisted, harmless
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/home/test",
        "LANG": "C.UTF-8",
        "LC_TIME": "C.UTF-8",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "MEMPALACE_BACKEND": "postgres",
        "MEMPALACE_POSTGRES_DSN": "postgresql://u@localhost:5433/db",
        "MEMPALACE_KG_WRITETHROUGH": "1",
        "PALACE_DAEMON_URL": "http://localhost:8085",
        "PALACE_DAEMON_STRICT": "0",
        "PALACE_KG_CANONICAL_THRESHOLD": "0.9",
        # never
        "BW_SESSION": "sentinel-session",
        "PALACE_API_KEY": "sentinel-api-key",
        "MEMPALACE_LLM_KEY": "sentinel-llm-key",
        "MEMPALACE_MCP_HTTP_TOKEN": "sentinel-token",
        "MEMPALACE_QDRANT_API_KEY": "sentinel-qdrant",
        "HF_TOKEN": "sentinel-hf",
        "GITHUB_TOKEN": "sentinel-gh",
        "AWS_SECRET_ACCESS_KEY": "sentinel-aws",
        "MEMPALACE_DB_PASSWORD": "sentinel-pw",
        "MEMPALACE_AUTH_HEADER": "sentinel-auth",
        "MEMPALACE_NOTE": f"carries {NEEDLE} inside",
        "PALACE_RERANK_MODEL": "daemon-only",
        "INVOCATION_ID": "systemd",
        "NOTIFY_SOCKET": "/run/systemd/notify",
        "PALACE_MINE_LOW_PRIORITY": "0",
    }
    ALLOWED = {
        "PATH", "HOME", "LANG", "LC_TIME", "XDG_RUNTIME_DIR", "MEMPALACE_BACKEND",
        "MEMPALACE_POSTGRES_DSN", "MEMPALACE_KG_WRITETHROUGH", "PALACE_DAEMON_URL",
        "PALACE_DAEMON_STRICT", "PALACE_KG_CANONICAL_THRESHOLD",
    }

    async def test_a_real_child_inherits_exactly_the_allowlist(self):
        held = {"item-env": (NEEDLE, time.monotonic() - 5)}
        redact = sys.modules.get("mempalace.redact") or __import__("mempalace.redact").redact
        with patch.dict(os.environ, self.PARENT, clear=True), patch.object(
            redact, "_vault_values", held, create=True
        ):
            proc = await main._spawn_mine([sys.executable, "-c", PRINT_ENV], {"MEMPALACE_MINE_PHASE_FILE": "/p"})
            out, err = await proc.communicate()
        self.assertEqual(proc.returncode, 0, err.decode()[-2000:])
        names = set(json.loads(out))
        expected = self.ALLOWED | {"MEMPALACE_MINE_PHASE_FILE"}
        if _real_redact() is not None:
            expected |= {SEED_ENV}  # the one name the seed adds: an fd number
        self.assertEqual(names, expected)

    def test_the_filter_drops_names_and_needle_values(self):
        with patch.dict(os.environ, self.PARENT, clear=True):
            redact = __import__("mempalace.redact").redact
            with patch.object(redact, "_vault_values", {"i": (NEEDLE, 0.0)}, create=True):
                env = main._mine_child_env()
        self.assertEqual(set(env), self.ALLOWED)
        self.assertFalse(any(NEEDLE in v or "sentinel" in v for v in env.values()))


READER = """
import hashlib, json, os, sys
fd = int(os.environ["MEMPALACE_NEEDLE_FD"])
data = b""
while True:
    chunk = os.read(fd, 65536)
    if not chunk:
        break
    data += chunk
os.close(fd)
doc = json.loads(data)
print(json.dumps({"ids": sorted(doc["items"]),
                  "sha": {i: hashlib.sha256(v.encode()).hexdigest() for i, (v, _t) in doc["items"].items()}}))
"""

HAS_FD = "import os, sys; fd = int(sys.argv[1]); print('open' if os.path.exists(f'/proc/self/fd/{fd}') else 'closed')"


@unittest.skipUnless(sys.platform.startswith("linux"), "pipes handed over with pass_fds, /proc")
@unittest.skipUnless(_real_redact(), "mempalace without NEEDLE_SEED_VERSION 1 (the seed is inert)")
class TestRealSpawnPathEndToEnd(unittest.IsolatedAsyncioTestCase):
    """The daemon's real spawn path, the real mempalace producer, a real child reading the real fd."""

    async def asyncSetUp(self):
        self.redact = _real_redact()
        self._held = patch.object(
            self.redact, "_vault_values", {"item-e2e": (NEEDLE, time.monotonic() - 5)}
        )
        self._held.start()
        self._env = patch.dict(os.environ, {"PALACE_MINE_LOW_PRIORITY": "0"})
        self._env.start()

    async def asyncTearDown(self):
        self._held.stop()
        self._env.stop()

    async def test_the_child_reads_the_seed_and_no_other_process_can(self):
        real_pipe, real_seed = os.pipe, self.redact.needle_seed_fd
        seen = {}

        def spy_pipe():
            r, w = real_pipe()
            seen["r"], seen["w"] = r, w
            seen["ino"] = os.fstat(r).st_ino
            return r, w

        def spy_seed():
            fd = real_seed()
            # Right after the producer returns, before the spawn opens anything: its write end is closed.
            seen["w_closed"] = not _is_open(seen["w"])
            seen["r_inheritable"] = os.get_inheritable(fd)
            # A second child started now, inheriting every inheritable fd, must not get the read end.
            second = subprocess.run(
                [sys.executable, "-c", HAS_FD, str(fd)], close_fds=False, capture_output=True, text=True
            )
            seen["second_child"] = second.stdout.strip()
            return fd

        with patch.object(os, "pipe", spy_pipe), patch.object(self.redact, "needle_seed_fd", spy_seed):
            proc = await main._spawn_mine([sys.executable, "-c", READER])
            out, err = await proc.communicate()
        self.assertEqual(proc.returncode, 0, err.decode()[-2000:])
        got = json.loads(out)
        self.assertEqual(got["ids"], ["item-e2e"])
        self.assertEqual(got["sha"]["item-e2e"], hashlib.sha256(NEEDLE.encode()).hexdigest())
        self.assertNotIn(NEEDLE.encode(), out + err)
        self.assertTrue(seen["w_closed"], "the producer left the pipe's write end open")
        self.assertFalse(seen["r_inheritable"])
        self.assertEqual(seen["second_child"], "closed")
        # The parent's read end is gone after the spawn (a reused number would be another file).
        r = seen["r"]
        self.assertTrue(not _is_open(r) or os.fstat(r).st_ino != seen["ino"])

    async def test_a_forced_write_failure_logs_the_type_only_and_the_child_fetches(self):
        def failing_write(fd, data):
            raise OSError(28, f"no space for {NEEDLE}")

        root = logging.getLogger()
        with patch.object(os, "write", failing_write), self.assertLogs(root, level="WARNING") as logs:
            proc = await main._spawn_mine(
                [sys.executable, "-c", "import os; print('MEMPALACE_NEEDLE_FD' in os.environ)"]
            )
            out, err = await proc.communicate()
        self.assertEqual(out.strip(), b"False")  # no seed: the child fetches as before
        text = "\n".join(logs.output)
        self.assertIn("OSError", text)
        self.assertNotIn(NEEDLE, text)
        self.assertNotIn("no space", text)


if __name__ == "__main__":
    unittest.main()
