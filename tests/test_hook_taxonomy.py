"""Regression tests for clients/hook.py project-wing resolution + themed messages.

Locks in the post-2026-05-13 taxonomy fix: wing is derived from session
project (cwd / transcript_path), not from agent name. Tests cover the
mapping rule, the fallback chain, and the themed message output.

Spec reference:
    ~/Projects/familiar.realm.watch/docs/superpowers/specs/2026-05-13-palace-room-taxonomy.md

Run with::

    cd /home/jp/Projects/palace-daemon
    python -m unittest tests.test_hook_taxonomy -v
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENTS = os.path.join(os.path.dirname(_HERE), "clients")
if _CLIENTS not in sys.path:
    sys.path.insert(0, _CLIENTS)

import hook  # noqa: E402


class TestSlugify(unittest.TestCase):
    """The basic project-name → slug transformation."""

    def test_dots_and_dashes_become_underscores(self):
        self.assertEqual(hook._slugify_project("familiar.realm.watch"), "familiar_realm_watch")
        self.assertEqual(hook._slugify_project("palace-daemon"), "palace_daemon")
        self.assertEqual(hook._slugify_project("realm-sigil"), "realm_sigil")

    def test_already_clean_passthrough(self):
        self.assertEqual(hook._slugify_project("memorypalace"), "memorypalace")

    def test_uppercase_lowered(self):
        self.assertEqual(hook._slugify_project("ClaudeCodeSwitcher"), "claudecodeswitcher")

    def test_spaces_collapsed(self):
        self.assertEqual(hook._slugify_project("my project name"), "my_project_name")

    def test_strips_outer_underscores(self):
        self.assertEqual(hook._slugify_project("--weird--"), "weird")


class TestDecodeProjectId(unittest.TestCase):
    """Claude Code's destructive ~/.claude/projects/<id> encoding."""

    def test_strips_projects_prefix(self):
        self.assertEqual(
            hook._decode_project_id("-home-jp-Projects-familiar-realm-watch"),
            "familiar-realm-watch",
        )
        self.assertEqual(
            hook._decode_project_id("-home-jp-Projects-palace-daemon"),
            "palace-daemon",
        )

    def test_fallback_when_no_marker(self):
        self.assertEqual(hook._decode_project_id("-some-other-thing"), "some-other-thing")

    def test_empty(self):
        self.assertEqual(hook._decode_project_id(""), "")


class TestProjectWing(unittest.TestCase):
    """Resolution order: cwd → transcript_path → getcwd → personal fallback."""

    def test_cwd_under_projects_resolves_to_wing(self):
        data = {"cwd": "/home/jp/Projects/familiar.realm.watch"}
        with patch.object(Path, "home", return_value=Path("/home/jp")):
            wing = hook._project_wing(data, transcript_path="")
        self.assertEqual(wing, "familiar_realm_watch")

    def test_cwd_nested_under_project_still_resolves(self):
        # Working in a subdir of the project should still land in the
        # project wing, not a subdir wing.
        data = {"cwd": "/home/jp/Projects/palace-daemon/clients"}
        with patch.object(Path, "home", return_value=Path("/home/jp")):
            wing = hook._project_wing(data, transcript_path="")
        self.assertEqual(wing, "palace_daemon")

    def test_transcript_path_fallback_when_no_cwd(self):
        # Simulate Claude Code's encoded project dir in the transcript path.
        transcript = "/home/jp/.claude/projects/-home-jp-Projects-familiar-realm-watch/abc.jsonl"
        wing = hook._project_wing({}, transcript_path=transcript)
        self.assertEqual(wing, "familiar_realm_watch")

    def test_no_signal_falls_back_to_personal(self):
        # No cwd, no transcript path, and getcwd happens to be $HOME.
        with patch.object(os, "getcwd", return_value="/home/jp"):
            wing = hook._project_wing({}, transcript_path="")
        self.assertEqual(wing, "personal")

    def test_cwd_outside_projects_uses_last_segment(self):
        data = {"cwd": "/tmp/some_workdir"}
        with patch.object(Path, "home", return_value=Path("/home/jp")):
            wing = hook._project_wing(data, transcript_path="")
        self.assertEqual(wing, "some_workdir")

    def test_invalid_cwd_falls_through(self):
        # Path that triggers an OSError on resolve() should not crash,
        # just fall through to transcript_path / personal.
        data = {"cwd": "/nonexistent/\x00invalid"}
        wing = hook._project_wing(data, transcript_path="")
        # Should not raise; result is non-empty.
        self.assertTrue(wing)
        self.assertFalse(wing.startswith("wing_"),
                         "Bare slug expected, no wing_ prefix per spec")


class TestDrawerLabel(unittest.TestCase):
    """Slug-style drawer label: <topic>@<HH:MM> from ISO timestamp.

    The drawer ID itself stays opaque (sha256-derived for idempotency).
    These tests cover only the human-display surface.
    """

    def test_topic_plus_hhmm(self):
        self.assertEqual(
            hook._drawer_label("checkpoint", "2026-05-13T08:48:16.427801"),
            "checkpoint@08:48",
        )

    def test_topic_only_when_timestamp_missing(self):
        self.assertEqual(hook._drawer_label("precompact", ""), "precompact")

    def test_time_only_when_topic_missing(self):
        self.assertEqual(
            hook._drawer_label("", "2026-05-13T22:00:00"),
            "@22:00",
        )

    def test_unknown_when_both_missing(self):
        self.assertEqual(hook._drawer_label("", ""), "?")

    def test_handles_malformed_timestamp(self):
        # If timestamp doesn't have a T separator, fall back to topic only.
        self.assertEqual(
            hook._drawer_label("checkpoint", "not-an-iso-date"),
            "checkpoint",
        )


class TestDisplayWing(unittest.TestCase):
    """Defensive prefix-stripping for any legacy wing_X data read back from chromadb."""

    def test_strips_legacy_wing_prefix(self):
        # Pre-2026-05-13 writes (and 30+ wings in the live palace) used
        # the wing_X form. If we ever read one back, strip the prefix so
        # rendering is consistent with new bare-slug writes.
        self.assertEqual(hook._display_wing("wing_familiar_realm_watch"), "familiar_realm_watch")

    def test_no_prefix_passthrough(self):
        self.assertEqual(hook._display_wing("familiar_realm_watch"), "familiar_realm_watch")

    def test_empty_becomes_unknown(self):
        self.assertEqual(hook._display_wing(""), "?")


class TestThemedSaveMessage(unittest.TestCase):
    """The systemMessage rendered in Claude Code UI on a save."""

    def _make_response(self, entry_id="diary_wing_x_20260513_080000_abcd1234567f",
                       topic="checkpoint", timestamp="2026-05-13T08:48:16.427801"):
        # Mempalace returns the diary_write result nested as JSON inside
        # the MCP `content[0].text` slot.
        inner = {
            "success": True,
            "entry_id": entry_id,
            "agent": "claude-code",
            "topic": topic,
            "timestamp": timestamp,
        }
        return {
            "result": {"content": [{"type": "text", "text": json.dumps(inner)}]}
        }

    def test_shows_project_wing_not_agent(self):
        # The whole point of the taxonomy fix: wing is the project, not
        # the agent. The themed message must reflect that.
        msg = hook._theme_save_ok(
            exchange_count=42,
            trigger="count",
            response=self._make_response(),
            palace_count="183,000 drawers",
            wing="familiar_realm_watch",
        )
        self.assertIn("wing:familiar_realm_watch", msg)
        # The agent name (claude-code) MUST NOT show up as a wing — that
        # was the antipattern this fix corrects.
        self.assertNotIn("wing:claude-code", msg)

    def test_chain_includes_room_diary(self):
        # Room stays diary until mempalace exposes a room parameter to
        # tool_diary_write. The message is truthful about what was stored.
        msg = hook._theme_save_ok(
            exchange_count=10, trigger="time",
            response=self._make_response(), palace_count="",
            wing="palace_daemon",
        )
        self.assertIn("room:diary", msg)

    def test_drawer_label_uses_topic_and_time(self):
        # Drawer display is a slug-style label built from topic + HH:MM,
        # not the opaque content hash. The hash stays in metadata for
        # search; this is just the rendering for humans.
        msg = hook._theme_save_ok(
            exchange_count=1, trigger="force",
            response=self._make_response(topic="checkpoint",
                                         timestamp="2026-05-13T14:22:09.123"),
            palace_count="", wing="test",
        )
        self.assertIn("drawer:checkpoint@14:22", msg)
        # Hash should NOT appear in the message — that was the old form.
        self.assertNotIn("…", msg)

    def test_falls_back_to_agent_when_wing_missing(self):
        # Older code path: no wing passed in. We still render *something*
        # rather than crash. Uses agent from the inner response payload.
        msg = hook._theme_save_ok(
            exchange_count=1, trigger="force",
            response=self._make_response(), palace_count="",
            wing="",
        )
        # Should be a clean fallback — not raise.
        self.assertIn("✦", msg)

    def test_fail_message_includes_exchange_and_trigger(self):
        msg = hook._theme_save_fail(
            exchange_count=99, trigger="count",
            failure={"error": "HTTP 401 Unauthorized"},
        )
        self.assertIn("99", msg)
        self.assertIn("count", msg)
        self.assertIn("401", msg)


class TestThemedMineMessage(unittest.TestCase):
    """Mine doesn't have a single target wing — message must be honest about that."""

    def test_no_longer_invents_a_wing(self):
        msg = hook._theme_mine(
            mine_dir="/home/jp/.claude/projects/-home-jp-Projects-familiar-realm-watch",
            ok=True, failure=None, palace_count="183,000 drawers",
        )
        # Old behavior used `wing=<basename>` which was a fake claim.
        # New message names the source dir explicitly.
        self.assertNotIn("wing=", msg)
        self.assertIn("source:", msg)

    def test_failure_includes_error(self):
        msg = hook._theme_mine(
            mine_dir="/tmp/foo",
            ok=False, failure={"error": "HTTP 500"},
            palace_count="",
        )
        self.assertIn("HTTP 500", msg)
        self.assertIn("✘", msg)


class TestSessionStartMessage(unittest.TestCase):
    """Greeter rendering at session start — count-only, wing-scoped."""

    def _make_list_drawers_response(self, total: int, count: int = 0):
        # Shape of tool_list_drawers' MCP-wrapped reply, derived from a
        # live daemon call against wing=familiar_realm_watch on
        # 2026-05-13: total + drawers list (only content_preview, no
        # filed_at — that was a planning-time assumption that didn't
        # match reality, see _theme_session_start docstring).
        inner = {
            "drawers": [
                {
                    "drawer_id": f"diary_test_2026{i:02d}",
                    "wing": "test",
                    "room": "diary",
                    "content_preview": "...",
                } for i in range(count)
            ],
            "total": total,
            "count": count,
            "offset": 0,
            "limit": max(count, 1),
        }
        return {"result": {"content": [{"type": "text", "text": json.dumps(inner)}]}}

    def test_fresh_wing_message(self):
        resp = self._make_list_drawers_response(total=0, count=0)
        msg = hook._theme_session_start("brand_new_project", resp)
        self.assertIn("✦ palace ready", msg)
        self.assertIn("brand_new_project", msg)
        self.assertIn("fresh wing", msg)

    def test_populated_wing_pluralizes_correctly(self):
        resp_many = self._make_list_drawers_response(total=47, count=1)
        msg = hook._theme_session_start("familiar_realm_watch", resp_many)
        self.assertIn("47 diary entries", msg)
        self.assertIn("wing:familiar_realm_watch", msg)

    def test_single_entry_singular(self):
        resp_one = self._make_list_drawers_response(total=1, count=1)
        msg = hook._theme_session_start("test", resp_one)
        self.assertIn("1 diary entry", msg)
        self.assertNotIn("entries", msg)  # singular form

    def test_large_count_uses_thousands_separator(self):
        resp = self._make_list_drawers_response(total=18203, count=1)
        msg = hook._theme_session_start("mempalace", resp)
        self.assertIn("18,203 diary entries", msg)

    def test_malformed_response_falls_back_to_fresh(self):
        # If the response can't be parsed, treat as zero entries rather
        # than crash the session-start hook.
        msg = hook._theme_session_start("wing_x", {"result": {"content": []}})
        self.assertIn("fresh wing", msg)

    def test_strips_legacy_wing_prefix_in_display(self):
        # Read paths may surface legacy wing_X data; display must strip.
        resp = self._make_list_drawers_response(total=3, count=1)
        msg = hook._theme_session_start("wing_legacy_thing", resp)
        self.assertIn("wing:legacy_thing", msg)
        self.assertNotIn("wing:wing_legacy_thing", msg)


class TestPrecompactSaveMessage(unittest.TestCase):
    """Boundary marker rendering."""

    def test_uses_distinct_sigil_from_periodic_save(self):
        response = {
            "result": {"content": [{"type": "text", "text": json.dumps({
                "entry_id": "diary_wing_x_20260513_080000_BOUNDARY1",
                "topic": "precompact",
                "timestamp": "2026-05-13T08:00:00",
            })}]}
        }
        msg = hook._theme_precompact_save(
            wing="familiar_realm_watch",
            response=response,
            palace_count="183,500 drawers",
        )
        # Different sigil from _theme_save_ok's ✦
        self.assertIn("◆", msg)
        self.assertIn("wing:familiar_realm_watch", msg)
        # Drawer label now uses slug form, not the hash suffix.
        self.assertIn("drawer:precompact@08:00", msg)
        self.assertNotIn("…BOUNDARY1", msg)


if __name__ == "__main__":
    unittest.main()
