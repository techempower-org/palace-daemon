"""Tests for clients/hook.py SessionStart search nudge.

The model has the mempalace_search tool (search-only MCP mode) but no habit
of using it. hook_session_start now injects a short additionalContext nudge
on EVERY non-compact SessionStart so "search the palace first" is in-context
from message one — combined into the SAME _output object as the existing
systemMessage greeting (a hook may print only one JSON object to stdout).

Compact sessions are unaffected: they branch to _handle_compact_resume and
get the richer compact-recovery packet instead.

Everything is mocked — no real daemon, no fork.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_hook_session_nudge.py -q
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure clients/ is on sys.path so `import hook` resolves.
_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENTS = os.path.join(os.path.dirname(_HERE), "clients")
if _CLIENTS not in sys.path:
    sys.path.insert(0, _CLIENTS)

import hook  # noqa: E402


def _list_drawers_resp(total=3):
    return True, {"result": {"content": [{"text": json.dumps({"total": total})}]}}


def _diary_resp(entries=None):
    return True, {"result": {"content": [{"text": json.dumps({"entries": entries or []})}]}}


class _StateDirMixin:
    """Redirect STATE_DIR into a temp dir so the normal path's mkdir /
    state writes never touch the real ~/.mempalace."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hook-nudge-")
        self._orig_state = hook.STATE_DIR
        hook.STATE_DIR = Path(self._tmp)

    def tearDown(self):
        hook.STATE_DIR = self._orig_state
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestSessionNudge(_StateDirMixin, unittest.TestCase):
    def _drive(self, data, post_impl):
        outputs = []
        post_calls = []

        def fake_post(daemon_url, tool, params):
            post_calls.append(tool)
            return post_impl(tool, params)

        with patch.object(hook, "_log"), \
             patch.object(hook, "_output", side_effect=outputs.append), \
             patch.object(hook, "_project_wing", return_value="memorypalace"), \
             patch.object(hook, "_load_hook_settings", return_value={"daemon_url": "http://x:8085"}), \
             patch.object(hook, "_write_last_save_ts"), \
             patch.object(hook, "_prune_state_files"), \
             patch.object(hook, "_drain_pending_journal"), \
             patch.object(hook, "_post_mcp", side_effect=fake_post):
            hook.hook_session_start(data, "claude-code")
        return outputs, post_calls

    # -- normal session start (daemon reachable) --------------------------
    def test_normal_start_injects_nudge_and_greeting(self):
        def post(tool, params):
            return _list_drawers_resp() if tool == "mempalace_list_drawers" else _diary_resp()

        outputs, post_calls = self._drive(
            {"session_id": "s1", "transcript_path": "/x.jsonl"}, post)

        # Exactly one _output object (two would be invalid stdout).
        self.assertEqual(len(outputs), 1)
        env = outputs[0]
        # Greeting preserved.
        self.assertIn("systemMessage", env)
        self.assertTrue(env["systemMessage"])
        # Nudge injected via additionalContext.
        hso = env["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        ac = hso["additionalContext"]
        self.assertIn("[mempalace:session-context]", ac)
        self.assertIn("[/mempalace:session-context]", ac)
        self.assertIn("mempalace_search", ac)
        self.assertIn("SEARCH FIRST", ac)
        self.assertIn("Wing: memorypalace", ac)

    def test_normal_start_is_a_single_output_call(self):
        def post(tool, params):
            return _list_drawers_resp() if tool == "mempalace_list_drawers" else _diary_resp()
        with patch.object(hook, "_log"), \
             patch.object(hook, "_output") as output, \
             patch.object(hook, "_project_wing", return_value="memorypalace"), \
             patch.object(hook, "_load_hook_settings", return_value={"daemon_url": "http://x:8085"}), \
             patch.object(hook, "_write_last_save_ts"), \
             patch.object(hook, "_prune_state_files"), \
             patch.object(hook, "_drain_pending_journal"), \
             patch.object(hook, "_post_mcp", side_effect=lambda u, t, p: post(t, p)):
            hook.hook_session_start({"session_id": "s1", "transcript_path": "/x.jsonl"}, "claude-code")
        output.assert_called_once()

    # -- daemon unreachable ----------------------------------------------
    def test_daemon_down_still_injects_nudge_without_greeting(self):
        def post(tool, params):
            return (False, {"error": "network/transport: refused"})

        outputs, post_calls = self._drive(
            {"session_id": "s2", "transcript_path": "/x.jsonl"}, post)

        self.assertEqual(len(outputs), 1)
        env = outputs[0]
        # No live greeting when the daemon is down...
        self.assertNotIn("systemMessage", env)
        # ...but the nudge still fires.
        ac = env["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[mempalace:session-context]", ac)
        self.assertIn("Wing: memorypalace", ac)
        # We bailed before the diary_read round-trip.
        self.assertNotIn("mempalace_diary_read", post_calls)

    # -- compact session is NOT this path --------------------------------
    def test_compact_source_takes_recovery_path_not_nudge(self):
        outputs = []
        with patch.object(hook, "_log"), \
             patch.object(hook, "_output", side_effect=outputs.append), \
             patch.object(hook, "_handle_compact_resume") as resume:
            hook.hook_session_start(
                {"session_id": "s3", "transcript_path": "/x.jsonl", "source": "compact"},
                "claude-code")
        # Delegated to the compact-resume handler; the nudge path never ran.
        resume.assert_called_once()
        self.assertEqual(outputs, [])  # _handle_compact_resume owns the output

    def test_compact_additionalcontext_is_recovery_not_session_context(self):
        """End-to-end through the real _handle_compact_resume: a compact
        session yields the compact-recovery packet, never the session nudge."""
        outputs = []
        with patch.object(hook, "_log"), \
             patch.object(hook, "_output", side_effect=outputs.append), \
             patch.object(hook, "_project_wing", return_value="memorypalace"), \
             patch.object(hook, "_load_hook_settings", return_value={"daemon_url": "http://x:8085"}), \
             patch.object(hook, "_daemon_healthy", return_value=False), \
             patch.object(hook, "_kick_wake_nonblocking"):
            hook.hook_session_start(
                {"session_id": "s4", "transcript_path": "/x.jsonl", "source": "compact"},
                "claude-code")
        ac = outputs[0]["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[mempalace:compact-recovery]", ac)
        self.assertNotIn("[mempalace:session-context]", ac)


class TestNudgeHelper(unittest.TestCase):
    def test_nudge_is_tagged_and_compact(self):
        n = hook._session_nudge("memorypalace")
        self.assertTrue(n.startswith("[mempalace:session-context]"))
        self.assertTrue(n.endswith("[/mempalace:session-context]"))
        self.assertIn("mempalace_search", n)
        self.assertIn("Wing: memorypalace", n)
        # ~50 tokens — keep it short. Guard against accidental bloat.
        self.assertLess(len(n), 600, f"nudge grew to {len(n)} chars")

    def test_nudge_strips_wing_prefix(self):
        self.assertIn("Wing: memorypalace", hook._session_nudge("wing_memorypalace"))


if __name__ == "__main__":
    unittest.main()
