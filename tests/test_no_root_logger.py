"""No daemon module logs through the root logger (#292).

kg_reader.py, search_routes.py and postgres.py made 18 calls straight to
`logging.warning(...)` and friends, which go to the ROOT logger, not the
`palace-daemon` hierarchy. Their messages were missing from the stream
anyone filters for daemon output, and one of them was the only witness to
#295's swallowed count failure.

Two checks:
- static: an AST scan finds zero `logging.<level>(...)` calls in daemon code
  (tests excluded), so a new one fails CI instead of quietly reappearing;
- behavioural: a real failure path in kg_reader logs on
  `palace-daemon.kg_reader`, and nothing on the root logger.
"""
import ast
import logging
import os
import pathlib
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEVELS = {"debug", "info", "warning", "error", "exception", "critical"}
SKIP = ("tests", ".claude", ".venv", "venv", "site-packages", ".git")


def _root_logger_calls():
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if any(part in SKIP for part in rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LEVELS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logging"
            ):
                found.append(f"{rel}:{node.lineno} logging.{node.func.attr}(...)")
    return found


class NoRootLogger(unittest.TestCase):
    def test_no_daemon_module_calls_the_root_logger(self):
        calls = _root_logger_calls()
        self.assertEqual(
            calls, [],
            "use a module logger (logging.getLogger('palace-daemon.<module>')), "
            "never the root logger (#292):\n  " + "\n  ".join(calls),
        )

    def test_the_scan_can_see_a_root_logger_call(self):
        """Positive control: the scanner flags the exact pattern it guards against."""
        tree = ast.parse('import logging\nlogging.warning("x")\n')
        hit = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in LEVELS and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "logging"
        ]
        self.assertEqual(len(hit), 1)

    def test_kg_reader_failure_logs_on_its_module_logger_not_root(self):
        import kg_reader

        class _RootWatch(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                if record.name == "root":
                    self.records.append(record)

        watch = _RootWatch()
        logging.getLogger().addHandler(watch)
        try:
            with (
                patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql://x@127.0.0.1:9/x"}),
                patch("psycopg2.connect", side_effect=RuntimeError("boom")),
                self.assertLogs("palace-daemon.kg_reader", level="WARNING") as logs,
            ):
                wings, rooms = kg_reader.read_wings_rooms_postgres()
        finally:
            logging.getLogger().removeHandler(watch)
        self.assertEqual((wings, rooms), ({}, []), "the documented degrade is unchanged")
        self.assertTrue(any("read_wings_rooms_postgres" in m for m in logs.output), logs.output)
        self.assertEqual(watch.records, [], "nothing may reach the root logger")


if __name__ == "__main__":
    unittest.main()
