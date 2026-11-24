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

    def test_the_real_refresher_starts_and_fetches_off_the_caller(self):
        """xreview r1/r2 #5/#3: run the installed mempalace's own start(), not a stand-in: with one configured
        item and a fake vault, the refresher thread fetches it and keeps the value."""
        import json
        import tempfile
        import threading
        import time

        from mempalace import redact

        if not hasattr(redact, "start_vault_refresher"):
            self.skipTest("installed mempalace predates the needle refresher")
        threads = []

        def fake_fetch(_item_id):
            threads.append(threading.current_thread().name)
            return "hunter" + "pass42"

        with tempfile.TemporaryDirectory() as cfg:
            with open(os.path.join(cfg, "config.json"), "w") as f:
                json.dump({"secret_vault_items": [{"id": "item-1", "label": "probe"}]}, f)
            with patch.dict(os.environ, {"MEMPALACE_CONFIG_DIR": cfg}), \
                 patch.object(redact, "_vault_fetch", fake_fetch):
                redact.stop_vault_refresher()
                redact._vault_values.clear()
                try:
                    self.assertTrue(main._start_needle_refresher(LOG))
                    deadline = time.monotonic() + 5
                    while "item-1" not in redact._vault_values and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertIn("item-1", redact._vault_values)
                    self.assertEqual(redact._vault_values["item-1"][0], "hunter" + "pass42")
                    self.assertEqual(threads[0], "mempalace-vault-refresh")
                finally:
                    redact.stop_vault_refresher()
                    redact._vault_values.clear()
                    redact._cache["key"] = None

    def test_static_guard_lifespan_exit_never_touches_the_refresher(self):
        """A static guard, on purpose: the property is the ABSENCE of a call on the shutdown path (xreview r2 on
        #355: any stop there can wait behind a write-path vault fetch on a thread interpreter exit joins). No
        test here runs the real lifespan (it starts the palace, watchers and subprocesses), so the source after
        ``yield`` is the instrument: it must not name the refresher in any form."""
        import inspect

        shutdown = inspect.getsource(main.lifespan).split("    yield", 1)[1]
        self.assertNotRegex(shutdown, r"(?<!#)[^\n#]*\b(stop_vault_refresher|_needle_refresher|vault_refresh)\b")


if __name__ == "__main__":
    unittest.main()
