"""/api/version serves the realm-sigil contract (status.realm.watch CHECK sigil-coverage, JP 2026-11-17).

Contract: unauthenticated like /health; JSON whose ``version`` reads "<Name> · <hash>" for this tree's commit,
with ``hash``, ``branch``, ``dirty`` and ``repo``; nothing about the palace itself.

    venv/bin/python -m pytest tests/test_api_version.py -v
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class ApiVersionTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_sigil_contract_for_this_commit(self):
        resp = await main.api_version()
        body = json.loads(resp.body)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(body["name"], "palace-daemon")
        self.assertRegex(body["version"], r"^.+ · [0-9a-f]{7,40}$")
        self.assertTrue(body["version"].endswith("· " + body["hash"]))
        self.assertEqual(body["repo"], "https://github.com/techempower-org/palace-daemon")
        self.assertEqual(resp.headers.get("cache-control"), "no-store")
        self.assertIsInstance(body["dirty"], bool)
        self.assertGreaterEqual(body["uptime"], 0)

    def test_the_hash_is_this_tree_not_a_placeholder(self):
        self.assertNotEqual(main._GIT["hash"], "dev")   # a git checkout: the build hash must be real
        self.assertRegex(main._GIT["hash"], r"^[0-9a-f]{7,40}$")

    def test_the_route_takes_no_api_key(self):
        route = next(r for r in main.app.routes if getattr(r, "path", "") == "/api/version")
        self.assertNotIn("x_api_key", {p.name for p in route.dependant.header_params})

    def test_outside_a_checkout_the_hash_is_dev_and_dirty(self):
        orig = main.subprocess.run

        def fail(*a, **k):
            raise FileNotFoundError("git")
        main.subprocess.run = fail
        try:
            facts = main._git_facts()
        finally:
            main.subprocess.run = orig
        self.assertEqual(facts, {"hash": "dev", "branch": "unknown", "dirty": True})


if __name__ == "__main__":
    unittest.main()
