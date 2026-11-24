"""The daemon starts mempalace's redact vault-needle refresher at startup (Flow 0ce300be).

One slow or locked vault answer used to refuse every palace write: the needles were fetched on the write
path and a failed refresh cleared them. mempalace now keeps each item's last good value and can refresh
them on a timer thread; the daemon, the long-lived writer, has to start that thread. An older mempalace
without it must not stop startup.

Run with::

    python -m unittest tests.test_needle_refresher -v
"""
import logging
import os
import sys
import types
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402

LOG = logging.getLogger("test_needle_refresher")


class NeedleRefresherStart(unittest.TestCase):
    def _with_redact(self, module):
        import mempalace

        return patch.object(mempalace, "redact", module, create=True), patch.dict(
            sys.modules, {"mempalace.redact": module}
        )

    def test_lifespan_starts_the_refresher(self):
        calls = []
        fake = types.SimpleNamespace(start_vault_refresher=lambda: calls.append(1) or True)
        a, b = self._with_redact(fake)
        with a, b:
            self.assertTrue(main._start_needle_refresher(LOG))
        self.assertEqual(calls, [1])

    def test_lifespan_calls_the_starter(self):
        import inspect

        src = inspect.getsource(main.lifespan)
        self.assertIn("_start_needle_refresher(logger)", src)

    def test_an_older_mempalace_without_it_does_not_stop_startup(self):
        a, b = self._with_redact(types.SimpleNamespace())
        with a, b, self.assertLogs(LOG, level="WARNING") as cm:
            self.assertFalse(main._start_needle_refresher(LOG))
        self.assertIn("no start_vault_refresher", cm.output[0])

    def test_a_starter_that_raises_does_not_stop_startup(self):
        def boom():
            raise RuntimeError("no thread for you")

        a, b = self._with_redact(types.SimpleNamespace(start_vault_refresher=boom))
        with a, b, self.assertLogs(LOG, level="WARNING") as cm:
            self.assertFalse(main._start_needle_refresher(LOG))
        self.assertIn("RuntimeError", cm.output[0])

    def test_lifespan_exit_stops_the_refresher(self):
        import inspect

        calls = []
        fake = types.SimpleNamespace(stop_vault_refresher=lambda timeout: calls.append(timeout))
        a, b = self._with_redact(fake)
        with a, b:
            main._stop_needle_refresher(LOG)
        self.assertEqual(calls, [2.0])
        src = inspect.getsource(main.lifespan)
        self.assertIn("_stop_needle_refresher, logger", src.split("    yield", 1)[1])  # after the yield

    def test_the_real_mempalace_has_it(self):
        from mempalace import redact

        if not hasattr(redact, "start_vault_refresher"):
            self.skipTest("installed mempalace predates the needle refresher")
        self.assertTrue(callable(redact.start_vault_refresher))
        self.assertTrue(callable(redact.stop_vault_refresher))


if __name__ == "__main__":
    unittest.main()
