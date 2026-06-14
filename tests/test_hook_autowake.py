"""Tests for clients/hook.py wake-on-demand + replay journal.

The daemon often runs on a host that suspends to save power, so
"connection refused / no route" is a routine state for the stop/precompact
hooks, not a fault. Before this feature the hook had NEITHER a wake
mechanism NOR a replay queue — so a save fired against a sleeping host
was silently lost (the user even got an optimistic "memories woven"
systemMessage emitted BEFORE the detached child failed).

These tests lock in the recovery path:

  (a) a connection-level ingest failure → wake attempted → ingest retried
      on a successful wake;
  (b) an HTTP / tool-level failure → NO wake (the daemon answered);
  (c) auto_wake disabled / garbage config → no wake, transcript journaled;
  (d) session-start drains the journal when the daemon is reachable and
      removes succeeded entries (keeps failures);
  (e) the per-host wake lock prevents a second concurrent wake command.

Everything is mocked — no real daemon, no real Wake-on-LAN, no fork.

Run with::

    cd /home/jp/Projects/palace-daemon
    PYTHONPATH=. venv/bin/python -m pytest tests/test_hook_autowake.py -q
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure clients/ is on sys.path so `import hook` resolves.
_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENTS = os.path.join(os.path.dirname(_HERE), "clients")
if _CLIENTS not in sys.path:
    sys.path.insert(0, _CLIENTS)

import hook  # noqa: E402


class _StateDirMixin:
    """Redirect STATE_DIR (and its derived paths) into a temp dir."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hook-autowake-")
        self._orig = {
            "STATE_DIR": hook.STATE_DIR,
            "PENDING_DIR": hook.PENDING_DIR,
            "WAKE_LOCK_PATH": hook.WAKE_LOCK_PATH,
        }
        hook.STATE_DIR = Path(self._tmp)
        hook.PENDING_DIR = hook.STATE_DIR / "pending"
        hook.WAKE_LOCK_PATH = hook.STATE_DIR / ".wake_inflight"
        hook.STATE_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(hook, k, v)
        shutil.rmtree(self._tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# _load_auto_wake — config reader mirrors mempalace.config.MempalaceConfig
# --------------------------------------------------------------------------
class TestLoadAutoWake(_StateDirMixin, unittest.TestCase):
    def _write_config(self, obj):
        cfg = hook.STATE_DIR / "config.json"
        # ensure_ascii=False + explicit utf-8 so the utf-8 read path is
        # genuinely exercised (raw multi-byte chars on disk, not \uXXXX).
        cfg.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return cfg

    def test_string_shorthand(self):
        cfg = self._write_config({"auto_wake": "wakeonlan aa:bb:cc:dd:ee:ff"})
        with patch.object(hook, "MEMPALACE_CONFIG_PATH", cfg), \
             patch.dict(os.environ, {}, clear=True):
            settings = hook._load_auto_wake()
        self.assertIsNotNone(settings)
        self.assertEqual(settings["command"], "wakeonlan aa:bb:cc:dd:ee:ff")
        self.assertEqual(settings["timeout_seconds"], 45.0)
        self.assertEqual(settings["poll_interval_seconds"], 2.0)

    def test_object_with_tuning(self):
        cfg = self._write_config({"auto_wake": {
            "command": "ipmitool power on",
            "timeout_seconds": 90,
            "poll_interval_seconds": 5,
        }})
        with patch.object(hook, "MEMPALACE_CONFIG_PATH", cfg), \
             patch.dict(os.environ, {}, clear=True):
            settings = hook._load_auto_wake()
        self.assertEqual(settings["command"], "ipmitool power on")
        self.assertEqual(settings["timeout_seconds"], 90.0)
        self.assertEqual(settings["poll_interval_seconds"], 5.0)

    def test_timeouts_are_clamped(self):
        cfg = self._write_config({"auto_wake": {
            "command": "x",
            "timeout_seconds": 99999,        # clamps to 300
            "poll_interval_seconds": 0.001,  # clamps to 0.5
        }})
        with patch.object(hook, "MEMPALACE_CONFIG_PATH", cfg), \
             patch.dict(os.environ, {}, clear=True):
            settings = hook._load_auto_wake()
        self.assertEqual(settings["timeout_seconds"], 300.0)
        self.assertEqual(settings["poll_interval_seconds"], 0.5)

    def test_garbage_timeouts_fall_back_to_default(self):
        cfg = self._write_config({"auto_wake": {
            "command": "x", "timeout_seconds": "not-a-number",
        }})
        with patch.object(hook, "MEMPALACE_CONFIG_PATH", cfg), \
             patch.dict(os.environ, {}, clear=True):
            settings = hook._load_auto_wake()
        self.assertEqual(settings["timeout_seconds"], 45.0)

    def test_empty_command_disables(self):
        cfg = self._write_config({"auto_wake": {"command": "   "}})
        with patch.object(hook, "MEMPALACE_CONFIG_PATH", cfg), \
             patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(hook._load_auto_wake())

    def test_non_dict_non_str_disables(self):
        cfg = self._write_config({"auto_wake": 12345})
        with patch.object(hook, "MEMPALACE_CONFIG_PATH", cfg), \
             patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(hook._load_auto_wake())

    def test_missing_key_disables(self):
        cfg = self._write_config({"other": "x"})
        with patch.object(hook, "MEMPALACE_CONFIG_PATH", cfg), \
             patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(hook._load_auto_wake())

    def test_missing_file_disables_silently(self):
        # A missing config file is the normal "not set up" state — it must
        # disable WITHOUT logging (no log spam on every hook fire).
        missing = hook.STATE_DIR / "does-not-exist.json"
        logged = []
        with patch.object(hook, "MEMPALACE_CONFIG_PATH", missing), \
             patch.object(hook, "_log", logged.append), \
             patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(hook._load_auto_wake())
        self.assertEqual(logged, [], "missing config file must not log")

    def test_malformed_json_disables_but_logs(self):
        # A config file that EXISTS but is malformed must disable AND log —
        # a silent disable here once cost hours of "why isn't it working?".
        cfg = hook.STATE_DIR / "config.json"
        cfg.write_text("{ this is not valid json ", encoding="utf-8")
        logged = []
        with patch.object(hook, "MEMPALACE_CONFIG_PATH", cfg), \
             patch.object(hook, "_log", logged.append), \
             patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(hook._load_auto_wake())
        self.assertTrue(any("malformed" in m for m in logged),
                        f"malformed config should log; got {logged!r}")

    def test_config_read_uses_utf8(self):
        # A non-ASCII command (e.g. a comment or path with accented chars)
        # must round-trip via the explicit utf-8 read.
        cfg = self._write_config({"auto_wake": "wakeonlan café-host"})
        with patch.object(hook, "MEMPALACE_CONFIG_PATH", cfg), \
             patch.dict(os.environ, {}, clear=True):
            settings = hook._load_auto_wake()
        self.assertEqual(settings["command"], "wakeonlan café-host")

    def test_env_override_disables(self):
        cfg = self._write_config({"auto_wake": "wakeonlan aa:bb:cc:dd:ee:ff"})
        for val in ("0", "false", "no", "NO", "False"):
            with patch.object(hook, "MEMPALACE_CONFIG_PATH", cfg), \
                 patch.dict(os.environ, {"PALACE_AUTO_WAKE": val}, clear=True):
                self.assertIsNone(hook._load_auto_wake(), f"value {val!r} should disable")


# --------------------------------------------------------------------------
# _is_wake_eligible_error — only connection-level failures qualify
# --------------------------------------------------------------------------
class TestWakeEligibility(unittest.TestCase):
    def test_connection_level_errors_eligible(self):
        for err in (
            "network/transport: <urlopen error [Errno 113] No route to host>",
            "network/transport: <urlopen error [Errno 111] Connection refused>",
            "No route to host",
            "Connection refused",
            "The operation timed out",
            "Name or service not known",
        ):
            self.assertTrue(hook._is_wake_eligible_error(err), err)

    def test_http_errors_not_eligible(self):
        for err in ("HTTP 401 Unauthorized", "HTTP 500 Internal Server Error",
                    "HTTP 404 Not Found"):
            self.assertFalse(hook._is_wake_eligible_error(err), err)

    def test_empty_or_unknown_not_eligible(self):
        self.assertFalse(hook._is_wake_eligible_error(""))
        self.assertFalse(hook._is_wake_eligible_error("unknown"))
        self.assertFalse(hook._is_wake_eligible_error("something weird"))


# --------------------------------------------------------------------------
# (a) wake-eligible failure → wake attempted → retry on success
# --------------------------------------------------------------------------
class TestWakeAndRetry(_StateDirMixin, unittest.TestCase):
    def test_eligible_failure_wakes_then_retries_and_succeeds(self):
        wake_settings = {"command": "wol", "timeout_seconds": 10,
                         "poll_interval_seconds": 0.1}

        # First ingest fails with a connection-level error; second succeeds.
        calls = {"n": 0}

        def fake_ingest(daemon_url, tp, wing, failure_out=None):
            calls["n"] += 1
            if calls["n"] == 1:
                if failure_out is not None:
                    failure_out["error"] = "network/transport: No route to host"
                    failure_out["eligible"] = True
                return False
            return True

        with patch.object(hook, "_ingest_transcript_via_daemon", side_effect=fake_ingest), \
             patch.object(hook, "_load_auto_wake", return_value=wake_settings), \
             patch.object(hook, "_attempt_wake", return_value=True) as wake, \
             patch.object(hook, "_journal_failed_ingest") as journal:
            ok = hook._ingest_with_wake_and_journal(
                "http://daemon:8085", "/tmp/x.jsonl", "wing", "sess")

        self.assertTrue(ok)
        wake.assert_called_once()
        journal.assert_not_called()
        self.assertEqual(calls["n"], 2, "ingest should be retried exactly once")

    def test_eligible_failure_wake_fails_then_journals(self):
        wake_settings = {"command": "wol", "timeout_seconds": 10,
                         "poll_interval_seconds": 0.1}

        def fake_ingest(daemon_url, tp, wing, failure_out=None):
            if failure_out is not None:
                failure_out["error"] = "network/transport: Connection refused"
                failure_out["eligible"] = True
            return False

        with patch.object(hook, "_ingest_transcript_via_daemon", side_effect=fake_ingest), \
             patch.object(hook, "_load_auto_wake", return_value=wake_settings), \
             patch.object(hook, "_attempt_wake", return_value=False) as wake, \
             patch.object(hook, "_journal_failed_ingest") as journal:
            ok = hook._ingest_with_wake_and_journal(
                "http://daemon:8085", "/tmp/x.jsonl", "wing", "sess")

        self.assertFalse(ok)
        wake.assert_called_once()
        journal.assert_called_once_with("/tmp/x.jsonl", "wing", "sess")


# --------------------------------------------------------------------------
# (b) HTTP / tool-level failure → NO wake (not eligible)
# --------------------------------------------------------------------------
class TestNoWakeOnHttpFailure(_StateDirMixin, unittest.TestCase):
    def test_http_failure_does_not_wake_but_journals(self):
        def fake_ingest(daemon_url, tp, wing, failure_out=None):
            if failure_out is not None:
                failure_out["error"] = "HTTP 401 Unauthorized"
                failure_out["eligible"] = False
            return False

        with patch.object(hook, "_ingest_transcript_via_daemon", side_effect=fake_ingest), \
             patch.object(hook, "_load_auto_wake", return_value={"command": "wol"}) as load, \
             patch.object(hook, "_attempt_wake", return_value=True) as wake, \
             patch.object(hook, "_journal_failed_ingest") as journal:
            ok = hook._ingest_with_wake_and_journal(
                "http://daemon:8085", "/tmp/x.jsonl", "wing", "sess")

        self.assertFalse(ok)
        wake.assert_not_called()
        load.assert_not_called()  # eligibility gate short-circuits before config read
        journal.assert_called_once_with("/tmp/x.jsonl", "wing", "sess")


# --------------------------------------------------------------------------
# (c) auto_wake disabled / garbage config → no wake, journal written
# --------------------------------------------------------------------------
class TestDisabledConfigJournals(_StateDirMixin, unittest.TestCase):
    def test_eligible_failure_but_autowake_disabled_journals_no_wake(self):
        def fake_ingest(daemon_url, tp, wing, failure_out=None):
            if failure_out is not None:
                failure_out["error"] = "network/transport: No route to host"
                failure_out["eligible"] = True
            return False

        with patch.object(hook, "_ingest_transcript_via_daemon", side_effect=fake_ingest), \
             patch.object(hook, "_load_auto_wake", return_value=None), \
             patch.object(hook, "_attempt_wake") as wake, \
             patch.object(hook, "_journal_failed_ingest") as journal:
            ok = hook._ingest_with_wake_and_journal(
                "http://daemon:8085", "/tmp/x.jsonl", "wing", "sess")

        self.assertFalse(ok)
        wake.assert_not_called()
        journal.assert_called_once_with("/tmp/x.jsonl", "wing", "sess")

    def test_journal_writes_a_real_line(self):
        hook._journal_failed_ingest("/tmp/conv.jsonl", "my_wing", "sess-1")
        files = list((hook.PENDING_DIR).glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        lines = [ln for ln in files[0].read_text().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        self.assertEqual(obj["transcript_path"], "/tmp/conv.jsonl")
        self.assertEqual(obj["wing"], "my_wing")
        self.assertEqual(obj["session_id"], "sess-1")
        self.assertIn("ts", obj)


# --------------------------------------------------------------------------
# (d) session-start drains journal (reachable daemon) and removes succeeded
# --------------------------------------------------------------------------
class TestDrainJournal(_StateDirMixin, unittest.TestCase):
    def _seed_journal(self, entries):
        hook.PENDING_DIR.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        path = hook.PENDING_DIR / f"{day}.jsonl"
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    def _make_transcript(self):
        # The drain now validates the transcript still exists and is >= 100
        # bytes before replaying, so test entries must point at real files
        # with enough content to clear the size gate.
        fd, p = tempfile.mkstemp(prefix="drain-tr-", suffix=".jsonl", dir=self._tmp)
        body = b'{"message": {"role": "user", "content": "%s"}}\n' % (b"x" * 120)
        os.write(fd, body)
        os.close(fd)
        return p

    def test_drain_removes_succeeded_keeps_failed(self):
        a = self._make_transcript()
        b = self._make_transcript()
        path = self._seed_journal([
            {"transcript_path": a, "wing": "w1", "session_id": "s", "ts": "2026-06-14T01:00:00"},
            {"transcript_path": b, "wing": "w2", "session_id": "s", "ts": "2026-06-14T02:00:00"},
        ])

        def fake_ingest(daemon_url, tp, wing):
            return tp == a  # a succeeds, b fails (daemon-level, retryable)

        with patch.object(hook, "_daemon_healthy", return_value=True), \
             patch.object(hook, "_ingest_transcript_via_daemon", side_effect=fake_ingest):
            hook._drain_pending_journal("http://daemon:8085")

        remaining = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["transcript_path"], b)

    def test_drain_all_succeed_removes_file(self):
        a = self._make_transcript()
        path = self._seed_journal([
            {"transcript_path": a, "wing": "w1", "session_id": "s", "ts": "2026-06-14T01:00:00"},
        ])
        with patch.object(hook, "_daemon_healthy", return_value=True), \
             patch.object(hook, "_ingest_transcript_via_daemon", return_value=True):
            hook._drain_pending_journal("http://daemon:8085")
        self.assertFalse(path.exists(), "fully drained journal file should be removed")

    def test_drain_dedups_by_transcript_path_keeping_newest(self):
        # Same transcript journaled twice (one per Stop fire during outage).
        a = self._make_transcript()
        self._seed_journal([
            {"transcript_path": a, "wing": "old", "session_id": "s", "ts": "2026-06-14T01:00:00"},
            {"transcript_path": a, "wing": "new", "session_id": "s", "ts": "2026-06-14T03:00:00"},
        ])
        seen = []

        def fake_ingest(daemon_url, tp, wing):
            seen.append((tp, wing))
            return True

        with patch.object(hook, "_daemon_healthy", return_value=True), \
             patch.object(hook, "_ingest_transcript_via_daemon", side_effect=fake_ingest):
            hook._drain_pending_journal("http://daemon:8085")

        self.assertEqual(len(seen), 1, "duplicate transcript should be replayed once")
        self.assertEqual(seen[0], (a, "new"), "newest entry should win")

    def test_drain_discards_missing_transcript(self):
        # Entry points at a transcript that no longer exists on disk: it is
        # unrecoverable, so the drain must DISCARD it (not re-queue) and must
        # NOT call the daemon for it — otherwise it clogs the journal forever.
        path = self._seed_journal([
            {"transcript_path": "/tmp/gone-forever-xyz.jsonl", "wing": "w",
             "session_id": "s", "ts": "2026-06-14T01:00:00"},
        ])
        with patch.object(hook, "_daemon_healthy", return_value=True), \
             patch.object(hook, "_ingest_transcript_via_daemon") as ingest:
            hook._drain_pending_journal("http://daemon:8085")
        ingest.assert_not_called()
        self.assertFalse(path.exists(),
                         "journal file should be removed once the dead entry is discarded")

    def test_drain_discards_truncated_transcript_but_keeps_retryable(self):
        # A too-small (truncated) transcript is discarded; a valid one whose
        # ingest fails at the daemon level is kept for retry.
        tiny_fd, tiny = tempfile.mkstemp(prefix="tiny-", suffix=".jsonl", dir=self._tmp)
        os.write(tiny_fd, b"{}")  # < 100 bytes
        os.close(tiny_fd)
        good = self._make_transcript()
        path = self._seed_journal([
            {"transcript_path": tiny, "wing": "w1", "session_id": "s", "ts": "2026-06-14T01:00:00"},
            {"transcript_path": good, "wing": "w2", "session_id": "s", "ts": "2026-06-14T02:00:00"},
        ])

        with patch.object(hook, "_daemon_healthy", return_value=True), \
             patch.object(hook, "_ingest_transcript_via_daemon", return_value=False):
            hook._drain_pending_journal("http://daemon:8085")

        remaining = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
        kept = {r["transcript_path"] for r in remaining}
        self.assertNotIn(tiny, kept, "truncated transcript must be discarded")
        self.assertIn(good, kept, "valid-but-failed transcript must be kept for retry")

    def test_drain_skips_when_daemon_unreachable(self):
        a = self._make_transcript()
        path = self._seed_journal([
            {"transcript_path": a, "wing": "w1", "session_id": "s", "ts": "2026-06-14T01:00:00"},
        ])
        with patch.object(hook, "_daemon_healthy", return_value=False), \
             patch.object(hook, "_ingest_transcript_via_daemon") as ingest:
            hook._drain_pending_journal("http://daemon:8085")
        ingest.assert_not_called()
        self.assertTrue(path.exists(), "journal must survive an unreachable daemon")

    def test_session_start_invokes_drain(self):
        # End-to-end: hook_session_start should call the drain. We stub the
        # greeting calls so the test stays offline-deterministic.
        data = {"session_id": "sess", "transcript_path": ""}
        with patch.object(hook, "_drain_pending_journal") as drain, \
             patch.object(hook, "_post_mcp", return_value=(False, {"error": "stub"})), \
             patch("sys.stdout", new=io.StringIO()):
            hook.hook_session_start(data, "claude-code")
        drain.assert_called_once()


# --------------------------------------------------------------------------
# (e) wake lock prevents a second concurrent wake command
#
# The lock is now an fcntl.flock held for the whole wake+poll (no TTL, no
# O_EXCL): the leader gets a file handle, a second claimant gets None while
# that handle is open, and closing the handle drops the flock immediately.
# --------------------------------------------------------------------------
class TestWakeLock(_StateDirMixin, unittest.TestCase):
    def test_first_claim_succeeds_second_is_follower_while_held(self):
        lock = hook._try_claim_wake_lock()
        self.assertIsNotNone(lock, "first claimant gets the lock handle (leader)")
        try:
            # A second independent open() can't take the flock while the
            # first handle is open → follower.
            self.assertIsNone(hook._try_claim_wake_lock(),
                              "second claimant is a follower while the lock is held")
        finally:
            lock.close()

    def test_release_allows_reclaim(self):
        lock = hook._try_claim_wake_lock()
        self.assertIsNotNone(lock)
        lock.close()  # drops the flock
        lock2 = hook._try_claim_wake_lock()
        self.assertIsNotNone(lock2, "lock should be reclaimable after the handle closes")
        lock2.close()

    def test_open_failure_fails_open_to_leader(self):
        # If the lock file can't be opened at all, we must NOT silently skip
        # the wake — fail open by returning a usable (no-op) handle.
        with patch.object(hook, "open", side_effect=OSError("denied"), create=True):
            lock = hook._try_claim_wake_lock()
        self.assertIsNotNone(lock, "open failure should fail open to leader")
        # The returned handle must close() cleanly (it's the null sentinel).
        lock.close()

    def test_concurrent_wake_fires_command_once(self):
        # Two _attempt_wake calls racing: only the flock leader runs the
        # wake command; the follower just polls /health.
        run_calls = {"n": 0}

        def fake_run(cmd):
            run_calls["n"] += 1
            return True

        wake_settings = {"command": "wol", "timeout_seconds": 5,
                         "poll_interval_seconds": 0.01}

        # Leader: lock free → runs command, health comes up immediately, and
        # the flock is released in _attempt_wake's finally.
        with patch.object(hook, "_run_wake_command", side_effect=fake_run), \
             patch.object(hook, "_daemon_healthy", return_value=True):
            leader_ok = hook._attempt_wake("http://daemon:8085", wake_settings)
        self.assertTrue(leader_ok)
        self.assertEqual(run_calls["n"], 1)

        # Now simulate a real concurrent holder: keep a flock open (stand-in
        # for the other hook) so _attempt_wake sees it as a follower.
        held = hook._try_claim_wake_lock()
        self.assertIsNotNone(held)
        try:
            with patch.object(hook, "_run_wake_command", side_effect=fake_run), \
                 patch.object(hook, "_daemon_healthy", return_value=True):
                follower_ok = hook._attempt_wake("http://daemon:8085", wake_settings)
            self.assertTrue(follower_ok, "follower still succeeds via /health poll")
            self.assertEqual(run_calls["n"], 1, "follower must NOT fire a second wake command")
        finally:
            held.close()


if __name__ == "__main__":
    unittest.main()
