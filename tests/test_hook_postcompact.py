"""Tests for clients/hook.py post-compaction context recovery.

Two unwired Claude Code events get handlers here:

  * ``PostCompact`` (``hook_postcompact``) — informational only; it cannot
    inject context, so it emits a user-visible systemMessage and saves the
    compaction summary as a ``compaction`` diary entry in the detached child.
  * ``SessionStart(source="compact")`` (``_handle_compact_resume``) — runs
    SYNCHRONOUSLY (no fork) and returns recent palace state as
    ``hookSpecificOutput.additionalContext`` so the model recovers what the
    lossy summary dropped. Falls back to a minimal nudge when the daemon is
    unreachable or returns nothing.

Everything is mocked — no real daemon, no real Wake-on-LAN, no fork.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_hook_postcompact.py -q
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

# Ensure clients/ is on sys.path so `import hook` resolves.
_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENTS = os.path.join(os.path.dirname(_HERE), "clients")
if _CLIENTS not in sys.path:
    sys.path.insert(0, _CLIENTS)

import hook  # noqa: E402


def _diary_ok():
    """A JSON-RPC envelope whose inner payload reports success=True."""
    return True, {"result": {"content": [{"text": json.dumps({"success": True})}]}}


def _list_drawers_resp(total=3):
    return True, {"result": {"content": [{"text": json.dumps({"total": total})}]}}


# --------------------------------------------------------------------------
# PostCompact handler
# --------------------------------------------------------------------------
class TestPostCompact(unittest.TestCase):
    def _run(self, data, post_return=None):
        """Drive hook_postcompact inline (detach returns True = child path)."""
        logged = []
        post_calls = []

        def fake_post(daemon_url, tool, params):
            post_calls.append((tool, params))
            return post_return if post_return is not None else _diary_ok()

        with patch.object(hook, "_log", logged.append), \
             patch.object(hook, "_output") as output, \
             patch.object(hook, "_detach_for_async_work", return_value=True), \
             patch.object(hook, "_project_wing", return_value="memorypalace"), \
             patch.object(hook, "_post_mcp", side_effect=fake_post), \
             patch.object(hook, "_desktop_notify"):
            hook.hook_postcompact(data, "claude-code")
        return logged, output, post_calls

    def test_manual_trigger_notifies_and_saves(self):
        logged, output, post_calls = self._run({
            "session_id": "sess-1",
            "transcript_path": "/nonexistent.jsonl",
            "trigger": "manual",
            "compact_summary": "We wired the postcompact handler and ran the suite.",
        })
        # systemMessage emitted in the parent, manual icon.
        msg = output.call_args_list[0].args[0]
        self.assertIn("systemMessage", msg)
        self.assertIn("📋", msg["systemMessage"])
        self.assertIn("manual", msg["systemMessage"])
        # diary_write fired with the right topic + tagged entry.
        self.assertEqual(len(post_calls), 1)
        tool, params = post_calls[0]
        self.assertEqual(tool, "mempalace_diary_write")
        self.assertEqual(params["topic"], "compaction")
        self.assertEqual(params["wing"], "memorypalace")
        self.assertEqual(params["agent_name"], "claude-code")
        self.assertTrue(params["entry"].startswith("COMPACTION:sess-1|manual|"))
        self.assertIn("wired the postcompact handler", params["entry"])
        self.assertTrue(any("Post-compact summary saved" in m for m in logged))

    def test_auto_trigger_uses_refresh_icon(self):
        _, output, _ = self._run({
            "session_id": "sess-2",
            "transcript_path": "/nonexistent.jsonl",
            "trigger": "auto",
            "compact_summary": "auto summary",
        })
        msg = output.call_args_list[0].args[0]["systemMessage"]
        self.assertIn("🔄", msg)
        self.assertIn("auto", msg)

    def test_missing_trigger_defaults_to_auto(self):
        _, output, post_calls = self._run({
            "session_id": "sess-2b",
            "transcript_path": "/nonexistent.jsonl",
            "compact_summary": "summary, no trigger field",
        })
        self.assertIn("🔄", output.call_args_list[0].args[0]["systemMessage"])
        self.assertTrue(post_calls[0][1]["entry"].startswith("COMPACTION:sess-2b|auto|"))

    def test_empty_summary_notifies_but_does_not_save(self):
        logged, output, post_calls = self._run({
            "session_id": "sess-3",
            "transcript_path": "/nonexistent.jsonl",
            "trigger": "auto",
            "compact_summary": "   ",
        })
        self.assertIn("systemMessage", output.call_args_list[0].args[0])
        self.assertEqual(post_calls, [])  # nothing to save
        self.assertTrue(any("nothing to save" in m for m in logged))

    def test_parent_returns_before_save(self):
        """When _detach returns False (we are the parent) only the
        systemMessage is emitted; the diary write happens in the child."""
        logged = []
        post_calls = []
        with patch.object(hook, "_log", logged.append), \
             patch.object(hook, "_output") as output, \
             patch.object(hook, "_detach_for_async_work", return_value=False), \
             patch.object(hook, "_project_wing", return_value="memorypalace"), \
             patch.object(hook, "_post_mcp", side_effect=lambda *a, **k: post_calls.append(a) or _diary_ok()):
            hook.hook_postcompact({
                "session_id": "sess-4",
                "transcript_path": "/nonexistent.jsonl",
                "trigger": "manual",
                "compact_summary": "x",
            }, "claude-code")
        output.assert_called_once()  # only the systemMessage
        self.assertEqual(post_calls, [])

    def test_save_failure_is_logged_not_raised(self):
        logged, _, _ = self._run(
            {
                "session_id": "sess-5",
                "transcript_path": "/nonexistent.jsonl",
                "trigger": "auto",
                "compact_summary": "summary",
            },
            post_return=(False, {"error": "network/transport: refused"}),
        )
        self.assertTrue(any("save FAILED" in m for m in logged))


# --------------------------------------------------------------------------
# SessionStart(source="compact") branch
# --------------------------------------------------------------------------
class TestCompactResume(unittest.TestCase):
    BASE = {"session_id": "sess-c", "transcript_path": "/nonexistent.jsonl", "source": "compact"}

    def _drive(self, *, healthy, search_impl=None, kick=None):
        logged = []
        outputs = []
        patches = [
            patch.object(hook, "_log", logged.append),
            patch.object(hook, "_output", side_effect=outputs.append),
            patch.object(hook, "_project_wing", return_value="memorypalace"),
            patch.object(hook, "_load_hook_settings", return_value={"daemon_url": "http://x:8085"}),
            patch.object(hook, "_daemon_healthy", return_value=healthy),
            patch.object(hook, "_kick_wake_nonblocking", side_effect=kick or (lambda: None)),
        ]
        if search_impl is not None:
            patches.append(patch.object(hook, "_search_fast", side_effect=search_impl))
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            hook.hook_session_start(dict(self.BASE), "claude-code")
        return logged, outputs

    def _ac(self, outputs):
        self.assertEqual(len(outputs), 1)
        env = outputs[0]  # side_effect=outputs.append stores the raw dict arg
        self.assertEqual(env["hookSpecificOutput"]["hookEventName"], "SessionStart")
        # The compact branch must NEVER emit a user-only systemMessage.
        self.assertNotIn("systemMessage", env)
        return env["hookSpecificOutput"]["additionalContext"]

    def test_daemon_down_returns_fallback_and_kicks_wake(self):
        woke = {"n": 0}
        logged, outputs = self._drive(healthy=False, kick=lambda: woke.__setitem__("n", woke["n"] + 1))
        ac = self._ac(outputs)
        self.assertIn("410K+ drawers", ac)  # the minimal nudge
        self.assertIn("mempalace_search", ac)
        self.assertEqual(woke["n"], 1)

    def test_daemon_up_with_hits_injects_rich_packet(self):
        def search(daemon_url, query, limit=3, timeout=2.0):
            if query.startswith("checkpoint"):
                return [{"snippet": "AUTO-SAVE:sess-c|87.msgs|2026-06-29|hook.count",
                         "room": "sessions", "wing": "memorypalace"}]
            return [{"snippet": "recovered task: postcompact recovery",
                     "room": "sessions", "wing": "memorypalace"}]
        logged, outputs = self._drive(healthy=True, search_impl=search)
        ac = self._ac(outputs)
        self.assertIn("recovered task: postcompact recovery", ac)
        self.assertIn("Last checkpoint:", ac)
        self.assertIn("[mempalace:compact-recovery]", ac)
        self.assertIn("[/mempalace:compact-recovery]", ac)

    def test_daemon_up_zero_hits_falls_back(self):
        logged, outputs = self._drive(healthy=True, search_impl=lambda *a, **k: [])
        ac = self._ac(outputs)
        self.assertIn("410K+ drawers", ac)

    def test_search_transport_failure_falls_back(self):
        # _search_fast returns None on transport/parse error → treated as no hits.
        logged, outputs = self._drive(healthy=True, search_impl=lambda *a, **k: None)
        ac = self._ac(outputs)
        self.assertIn("410K+ drawers", ac)

    def test_normal_start_does_not_take_compact_path(self):
        """Without source=compact the greeting path runs (no _search_fast,
        no additionalContext) — regression guard for the new branch."""
        searched = {"n": 0}
        with patch.object(hook, "_log"), \
             patch.object(hook, "_output") as output, \
             patch.object(hook, "_project_wing", return_value="memorypalace"), \
             patch.object(hook, "_load_hook_settings", return_value={"daemon_url": "http://x:8085"}), \
             patch.object(hook, "_write_last_save_ts"), \
             patch.object(hook, "_prune_state_files"), \
             patch.object(hook, "_drain_pending_journal"), \
             patch.object(hook, "_search_fast", side_effect=lambda *a, **k: searched.__setitem__("n", 1)), \
             patch.object(hook, "_post_mcp", side_effect=lambda url, tool, params: (
                 _list_drawers_resp() if tool == "mempalace_list_drawers"
                 else (True, {"result": {"content": [{"text": json.dumps({"entries": []})}]}}))):
            hook.hook_session_start({"session_id": "sess-n", "transcript_path": "/x.jsonl"}, "claude-code")
        env = output.call_args_list[-1].args[0]
        self.assertIn("systemMessage", env)
        self.assertNotIn("hookSpecificOutput", env)
        self.assertEqual(searched["n"], 0)  # compact-only search never ran


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class TestHelpers(unittest.TestCase):
    def test_fallback_context_is_tagged(self):
        fb = hook._compact_fallback_context()
        self.assertTrue(fb.startswith("[mempalace:compact-recovery]"))
        self.assertTrue(fb.endswith("[/mempalace:compact-recovery]"))
        self.assertIn("mempalace_search", fb)

    def test_compact_output_envelope(self):
        env = hook._compact_output("BODY")
        self.assertEqual(env, {"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": "BODY"}})

    def test_format_packet_clips_long_snippets(self):
        long = "x" * 4000
        packet = hook._format_compact_packet(
            "memorypalace",
            checkpoint_hits=[{"snippet": long, "room": "sessions"}],
            session_hits=[{"snippet": long, "room": "sessions", "wing": "memorypalace"}],
        )
        # Each rendered snippet is clipped (220 chars), so the packet stays
        # far under the raw 8000 chars of input.
        self.assertLess(len(packet), 2000)
        self.assertIn("Recent context (top 1 matches)", packet)

    def test_format_packet_handles_no_hits(self):
        packet = hook._format_compact_packet("memorypalace", [], [])
        self.assertIn("Wing: memorypalace", packet)
        self.assertNotIn("Recent context", packet)

    def test_format_packet_skips_non_dict_hits(self):
        # A malformed /search/fast response (non-dict rows) must not raise
        # AttributeError in the SessionStart critical path — bad rows are
        # skipped, valid rows still render.
        packet = hook._format_compact_packet(
            "memorypalace",
            checkpoint_hits=["not-a-dict"],
            session_hits=["nope", {"snippet": "good row", "room": "sessions",
                                   "wing": "memorypalace"}, 42],
        )
        self.assertIn("good row", packet)
        self.assertNotIn("Last checkpoint:", packet)  # bad checkpoint skipped
        self.assertIn("[/mempalace:compact-recovery]", packet)

    def test_theme_postcompact_icons(self):
        self.assertIn("🔄", hook._theme_postcompact("w", "auto"))
        self.assertIn("📋", hook._theme_postcompact("w", "manual"))

    def test_search_fast_parses_json_array(self):
        rows = [{"id": "d1", "snippet": "hi", "room": "sessions", "wing": "w"}]

        class _Resp:
            status = 200
            def read(self_inner):
                return json.dumps(rows).encode("utf-8")
            def __enter__(self_inner):
                return self_inner
            def __exit__(self_inner, *a):
                return False

        with patch.object(hook.urllib.request, "urlopen", return_value=_Resp()):
            out = hook._search_fast("http://x:8085", "session state w", limit=3)
        self.assertEqual(out, rows)

    def test_search_fast_returns_none_on_error(self):
        with patch.object(hook.urllib.request, "urlopen", side_effect=OSError("refused")), \
             patch.object(hook, "_log"):
            self.assertIsNone(hook._search_fast("http://x:8085", "q"))

    def test_kick_wake_uses_shlex_split_and_devnull(self):
        captured = {}

        def fake_popen(args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            class _P:  # noqa: D401
                pass
            return _P()

        with patch.object(hook, "_load_auto_wake", return_value={"command": "wakeonlan aa:bb:cc"}), \
             patch.object(hook.subprocess, "Popen", side_effect=fake_popen), \
             patch.object(hook, "_log"):
            hook._kick_wake_nonblocking()
        # shlex.split, not a raw string / shell=True.
        self.assertEqual(captured["args"], ["wakeonlan", "aa:bb:cc"])
        self.assertNotIn("shell", captured["kwargs"])
        self.assertEqual(captured["kwargs"]["stdout"], hook.subprocess.DEVNULL)
        self.assertEqual(captured["kwargs"]["stderr"], hook.subprocess.DEVNULL)
        self.assertTrue(captured["kwargs"]["start_new_session"])

    def test_kick_wake_noop_without_config(self):
        with patch.object(hook, "_load_auto_wake", return_value=None), \
             patch.object(hook.subprocess, "Popen") as popen:
            hook._kick_wake_nonblocking()
        popen.assert_not_called()


# --------------------------------------------------------------------------
# dispatch wiring
# --------------------------------------------------------------------------
class TestDispatch(unittest.TestCase):
    def test_run_hook_dispatches_postcompact(self):
        with patch.object(hook, "hook_postcompact") as h, \
             patch("sys.stdin", new=__import__("io").StringIO("{}")):
            hook.run_hook("postcompact", "claude-code")
        h.assert_called_once()


if __name__ == "__main__":
    unittest.main()
