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
import time
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


class TestLastSaveRebase(unittest.TestCase):
    """Self-heal when exchange_count goes backward.

    Regression for the 2026-05-13 count-fix side effect: tightening the
    'what counts as a human message' rule (filtering out tool_result
    roundtrips) made counts drop ~11x. Existing state files held the
    pre-fix inflated counts. Without a rebase, since_last goes negative
    and all save triggers wedge.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="hook-rebase-")
        self._original_state_dir = hook.STATE_DIR
        hook.STATE_DIR = Path(self._tmp)
        hook.STATE_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        hook.STATE_DIR = self._original_state_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_state(self, session_id, value):
        (hook.STATE_DIR / f"{session_id}_last_save").write_text(str(value))
        # Also seed last_save_ts to "now" so time_trigger doesn't fire
        # in tests where we expect no save (an unseeded ts file makes
        # time_since_last huge, triggering a time-based save).
        (hook.STATE_DIR / f"{session_id}_last_save_ts").write_text(str(time.time()))

    def _read_state(self, session_id):
        return int((hook.STATE_DIR / f"{session_id}_last_save").read_text().strip())

    def _write_transcript(self, n_real_user_turns):
        # Build a transcript with N real human turns. Use tempfile because
        # the production hook uses _validate_transcript_path which requires
        # absolute path + .jsonl extension.
        import tempfile
        fd, path = tempfile.mkstemp(prefix="rebase-tr-", suffix=".jsonl")
        os.close(fd)
        entries = []
        for i in range(n_real_user_turns):
            entries.append({"message": {"role": "user", "content": f"turn {i}"}})
            entries.append({"message": {"role": "assistant", "content": f"response {i}"}})
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    def _drive_hook_stop(self, session, transcript):
        # Drive hook_stop with HTTP calls stubbed so the test is fast
        # and deterministic. Stubs return failure-shaped tuples so the
        # save path treats the daemon as unreachable; we only care about
        # state-file mutations from the rebase logic.
        import io
        data = {"session_id": session, "stop_hook_active": False,
                "transcript_path": transcript}
        with patch.object(hook, "_post_mcp", return_value=(False, {"error": "stub"})), \
             patch.object(hook, "_post_mine", return_value=(False, {"error": "stub"})), \
             patch.object(hook, "_get_palace_stats", return_value={}), \
             patch.object(hook, "_ingest_transcript_via_daemon"), \
             patch("sys.stdout", new=io.StringIO()):
            hook.hook_stop(data, "claude-code")

    def test_rebase_when_last_save_exceeds_count(self):
        # Pre-fix saved a count of 955 (inflated). Post-fix the same
        # transcript counts as 87. Without rebase, since_last = -868.
        session = "rebase-test"
        self._write_state(session, 955)
        transcript = self._write_transcript(87)
        try:
            self._drive_hook_stop(session, transcript)
            self.assertEqual(self._read_state(session), 87,
                             "state should rebase from 955 down to current count")
        finally:
            os.unlink(transcript)

    def test_no_rebase_when_count_exceeds_last_save(self):
        # Normal case — counter going forward, no trigger fires.
        # last_save should not change.
        session = "normal-test"
        self._write_state(session, 5)
        transcript = self._write_transcript(10)
        try:
            self._drive_hook_stop(session, transcript)
            # since_last (5) < SAVE_INTERVAL (15), force_min_interval not met
            # because last_save_ts is seeded to now → no trigger.
            self.assertEqual(self._read_state(session), 5)
        finally:
            os.unlink(transcript)

    def test_rebase_unblocks_save_triggers(self):
        # End-to-end: pre-fix last_save 100, current count 10. Without
        # rebase, since_last would be -90 and no trigger fires. With
        # rebase, since_last becomes 0 — still no save this turn (good:
        # we're at "fresh checkpoint"), but the path is unblocked for
        # future fires.
        session = "unblock-test"
        self._write_state(session, 100)
        transcript = self._write_transcript(10)
        try:
            self._drive_hook_stop(session, transcript)
            # Rebased to 10; future calls have since_last >= 0 working again.
            self.assertEqual(self._read_state(session), 10)
        finally:
            os.unlink(transcript)


class TestHumanMessageCount(unittest.TestCase):
    """Regression for the tool_result inflation bug (mirrors upstream #549).

    Claude Code's tool roundtrips arrive as role:user messages whose
    content is a list of tool_result blocks. They aren't human exchanges
    and must not count toward the save-interval trigger.
    """

    def setUp(self):
        import tempfile
        fd, self._path = tempfile.mkstemp(prefix="transcript-", suffix=".jsonl")
        os.close(fd)

    def tearDown(self):
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def _write(self, entries):
        with open(self._path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_plain_string_user_message_counts(self):
        self._write([{"message": {"role": "user", "content": "hello there"}}])
        self.assertEqual(hook._count_human_messages(self._path), 1)

    def test_list_of_text_blocks_counts(self):
        self._write([{"message": {"role": "user", "content": [
            {"type": "text", "text": "what does this code do?"}
        ]}}])
        self.assertEqual(hook._count_human_messages(self._path), 1)

    def test_tool_result_only_does_not_count(self):
        # The bug: this is a tool roundtrip, not a human turn.
        self._write([{"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "output"}
        ]}}])
        self.assertEqual(hook._count_human_messages(self._path), 0)

    def test_multiple_tool_results_in_one_message_does_not_count(self):
        self._write([{"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "out1"},
            {"type": "tool_result", "tool_use_id": "b", "content": "out2"},
            {"type": "tool_result", "tool_use_id": "c", "content": "out3"},
        ]}}])
        self.assertEqual(hook._count_human_messages(self._path), 0)

    def test_mixed_text_and_tool_result_counts_once(self):
        # If Claude Code ever sends a user turn that delivers both text
        # AND tool results in one message, count it as one human turn.
        self._write([{"message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x", "content": "output"},
            {"type": "text", "text": "and also, can you do X?"},
        ]}}])
        self.assertEqual(hook._count_human_messages(self._path), 1)

    def test_command_message_skipped(self):
        self._write([
            {"message": {"role": "user", "content": "<command-message>/save</command-message>"}},
            {"message": {"role": "user", "content": [
                {"type": "text", "text": "<command-message>/foo</command-message>"}
            ]}},
        ])
        self.assertEqual(hook._count_human_messages(self._path), 0)

    def test_empty_text_skipped(self):
        self._write([
            {"message": {"role": "user", "content": ""}},
            {"message": {"role": "user", "content": [{"type": "text", "text": "  "}]}},
        ])
        self.assertEqual(hook._count_human_messages(self._path), 0)

    def test_realistic_session_mix(self):
        # Realistic shape: 3 real human turns interleaved with 8 tool roundtrips.
        # Pre-fix this would count as 11; post-fix should be 3.
        entries = [
            {"message": {"role": "user", "content": "first question"}},
            {"message": {"role": "assistant", "content": "let me check"}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "1", "content": "ls output"}
            ]}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "2", "content": "grep output"}
            ]}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "3", "content": "cat output"}
            ]}},
            {"message": {"role": "user", "content": "second question"}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "4", "content": "find output"}
            ]}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "5", "content": "ps output"}
            ]}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "6", "content": "df output"}
            ]}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "7", "content": "du output"}
            ]}},
            {"message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "8", "content": "wc output"}
            ]}},
            {"message": {"role": "user", "content": [
                {"type": "text", "text": "third question"}
            ]}},
        ]
        self._write(entries)
        self.assertEqual(hook._count_human_messages(self._path), 3,
                         "should count 3 real human turns, not 11 total user-role messages")


class TestLogRotation(unittest.TestCase):
    """Size-gated hook.log rotation."""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="hook-log-rotate-")
        self._original_state_dir = hook.STATE_DIR
        hook.STATE_DIR = Path(self._tmp)

    def tearDown(self):
        import shutil
        hook.STATE_DIR = self._original_state_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_no_rotation_when_under_size(self):
        log = hook.STATE_DIR / "hook.log"
        log.write_text("tiny log\n")
        hook._rotate_log_if_needed(log)
        self.assertTrue(log.exists())
        self.assertFalse((hook.STATE_DIR / "hook.log.1").exists())

    def test_rotates_at_threshold(self):
        log = hook.STATE_DIR / "hook.log"
        # Use a small threshold for the test — keep production semantics
        # by patching the constant rather than writing 10MB.
        original = hook._LOG_MAX_BYTES
        try:
            hook._LOG_MAX_BYTES = 100
            log.write_text("x" * 200)
            hook._rotate_log_if_needed(log)
            self.assertFalse(log.exists(), "current log should have rotated away")
            self.assertTrue((hook.STATE_DIR / "hook.log.1").exists())
        finally:
            hook._LOG_MAX_BYTES = original

    def test_shifts_existing_rotations(self):
        original = hook._LOG_MAX_BYTES
        try:
            hook._LOG_MAX_BYTES = 100
            log = hook.STATE_DIR / "hook.log"
            (hook.STATE_DIR / "hook.log.1").write_text("first rotation")
            (hook.STATE_DIR / "hook.log.2").write_text("second rotation")
            log.write_text("x" * 200)
            hook._rotate_log_if_needed(log)
            self.assertEqual((hook.STATE_DIR / "hook.log.2").read_text(), "first rotation")
            self.assertEqual((hook.STATE_DIR / "hook.log.3").read_text(), "second rotation")
        finally:
            hook._LOG_MAX_BYTES = original

    def test_drops_oldest_beyond_keep_limit(self):
        original = hook._LOG_MAX_BYTES
        try:
            hook._LOG_MAX_BYTES = 100
            log = hook.STATE_DIR / "hook.log"
            # All slots already filled (.1 .2 .3) — oldest must drop.
            for i in range(1, hook._LOG_KEEP + 1):
                (hook.STATE_DIR / f"hook.log.{i}").write_text(f"slot {i}")
            old_3_content = (hook.STATE_DIR / f"hook.log.{hook._LOG_KEEP}").read_text()
            log.write_text("x" * 200)
            hook._rotate_log_if_needed(log)
            # The old .3 (slot 3) is gone — it's not in .4 because we only keep 3.
            self.assertEqual(
                (hook.STATE_DIR / "hook.log.3").read_text(),
                "slot 2",  # shifted from .2
                "old slot 3 should have been dropped, slot 2 shifted in"
            )
        finally:
            hook._LOG_MAX_BYTES = original


class TestMineSlotDedup(unittest.TestCase):
    """Lock-based per-target mine dedup.

    Mirrors upstream's hooks_cli._claim_mine_slot semantics: two near-
    simultaneous hook fires for the same (dir, mode, wing) target collapse
    to one /mine POST; the second silently skips while the first holds
    the slot. Different targets get independent slots and never block.
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.mkdtemp(prefix="hook-mine-slot-")
        self._original_state_dir = hook.STATE_DIR
        hook.STATE_DIR = Path(self._tmp)

    def tearDown(self):
        import shutil
        hook.STATE_DIR = self._original_state_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_same_target_collapses_to_one_slot(self):
        slot1 = hook._try_claim_mine_slot("/a", "convos", "wing_x")
        self.assertIsNotNone(slot1, "first acquisition should succeed")
        try:
            slot2 = hook._try_claim_mine_slot("/a", "convos", "wing_x")
            self.assertIsNone(slot2, "second acquisition for same target should fail")
        finally:
            slot1.close()
        # After slot1 closes, a fresh claim should succeed again.
        slot3 = hook._try_claim_mine_slot("/a", "convos", "wing_x")
        self.assertIsNotNone(slot3, "claim should succeed after lock release")
        slot3.close()

    def test_different_targets_get_independent_slots(self):
        s1 = hook._try_claim_mine_slot("/a", "convos", "wing_x")
        s2 = hook._try_claim_mine_slot("/b", "convos", "wing_x")
        s3 = hook._try_claim_mine_slot("/a", "convos", "wing_y")
        s4 = hook._try_claim_mine_slot("/a", "projects", "wing_x")
        try:
            self.assertIsNotNone(s1)
            self.assertIsNotNone(s2, "different dir = independent slot")
            self.assertIsNotNone(s3, "different wing = independent slot")
            self.assertIsNotNone(s4, "different mode = independent slot")
        finally:
            for s in (s1, s2, s3, s4):
                if s is not None:
                    s.close()

    def test_key_is_stable_across_runs(self):
        k1 = hook._mine_target_key("/a", "convos", "wing_x")
        k2 = hook._mine_target_key("/a", "convos", "wing_x")
        k3 = hook._mine_target_key("/a", "convos", "wing_y")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, k3)


class TestPostMineDefaults(unittest.TestCase):
    """Lock in the post-2026-05-13 _post_mine signature.

    Pre-fix, _post_mine hardcoded mode=\"auto\" which the daemon would 400
    (its valid modes are {convos, projects}). Now: mode defaults to
    \"convos\", and an optional wing forwards to the daemon when truthy.
    """

    def _make_response(self, status=200):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read = MagicMock(return_value=b'{}')
        return resp

    def test_default_mode_is_convos(self):
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return self._make_response(200)
        with patch.dict(os.environ, {"PALACE_API_KEY": "k"}, clear=True), \
             patch.object(hook.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok, _ = hook._post_mine("http://daemon:8085", "/tmp/foo")
        self.assertTrue(ok)
        body = json.loads(captured["body"])
        self.assertEqual(body["mode"], "convos")
        # No wing key when not supplied — daemon's default "general" applies.
        self.assertNotIn("wing", body)

    def test_wing_forwarded_when_provided(self):
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return self._make_response(200)
        with patch.dict(os.environ, {"PALACE_API_KEY": "k"}, clear=True), \
             patch.object(hook.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok, _ = hook._post_mine("http://daemon:8085", "/tmp/foo",
                                    mode="convos", wing="familiar_realm_watch")
        self.assertTrue(ok)
        body = json.loads(captured["body"])
        self.assertEqual(body["wing"], "familiar_realm_watch")


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
