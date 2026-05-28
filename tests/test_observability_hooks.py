"""Tests for the #97 observability hooks (DB-error counter + memcg canary).

Today's morning OOM cluster (postgres killed twice inside its docker memcg
at 08:57 + 09:19) surfaced 26+ ``OperationalError: connection is closed``
events that were invisible to /health — the daemon process stayed up the
whole time, but in-flight queries returned errors. Same silent-failure-
under-healthy-surface shape #92 was filed to close, just for postgres.

These tests cover the three observability mechanisms:
  * `_classify_db_error` — bucketing by surface message
  * `_record_db_error` + `_db_errors_summary` — ring buffer + 5min rollup
  * `_postgres_memcg_status` + `_log_postgres_memcg_canary` — docker stats
    integration with graceful degradation

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_observability_hooks.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class _BaseObs(unittest.TestCase):
    def setUp(self):
        # Each test gets a clean ring buffer.
        main._DB_ERROR_LOG.clear()


class TestClassifyDbError(_BaseObs):
    """Each pattern bucket has a distinguishing substring."""

    def test_in_recovery(self):
        e = Exception("FATAL: the database system is in recovery mode")
        self.assertEqual(main._classify_db_error(e), "in_recovery")

    def test_connection_closed(self):
        e = Exception("the connection is closed")
        self.assertEqual(main._classify_db_error(e), "connection_closed")

    def test_server_closed(self):
        e = Exception("consuming input failed: server closed the connection unexpectedly")
        self.assertEqual(main._classify_db_error(e), "server_closed")

    def test_server_closed_alt_phrasing(self):
        """A different variant of 'server closed' still buckets correctly."""
        e = Exception("ERROR: server closed the connection unexpectedly")
        self.assertEqual(main._classify_db_error(e), "server_closed")

    def test_connection_lost(self):
        e = Exception("the connection is lost")
        self.assertEqual(main._classify_db_error(e), "connection_lost")

    def test_connect_failed(self):
        e = Exception("connection failed: could not connect to server")
        self.assertEqual(main._classify_db_error(e), "connect_failed")

    def test_timeout(self):
        e = Exception("canceling statement due to statement timeout")
        self.assertEqual(main._classify_db_error(e), "timeout")

    def test_other_fallback(self):
        e = Exception("some completely unrelated error message")
        self.assertEqual(main._classify_db_error(e), "other")


class TestRecordAndSummarize(_BaseObs):
    """`_record_db_error` appends; `_db_errors_summary` aggregates by window."""

    def test_empty_log_summary(self):
        s = main._db_errors_summary()
        self.assertEqual(s["total_last_window"], 0)
        self.assertEqual(s["by_pattern"], {})
        self.assertIsNone(s["newest_ts"])

    def test_record_then_summarize(self):
        main._record_db_error(Exception("the connection is closed"))
        main._record_db_error(Exception("the connection is closed"))
        main._record_db_error(Exception("server closed the connection unexpectedly"))
        s = main._db_errors_summary()
        self.assertEqual(s["total_last_window"], 3)
        self.assertEqual(s["by_pattern"]["connection_closed"], 2)
        self.assertEqual(s["by_pattern"]["server_closed"], 1)
        self.assertIsNotNone(s["newest_ts"])

    def test_old_entries_excluded_from_window(self):
        # Manually inject an old entry (1 hour ago) — it shouldn't count.
        old_ts = time.time() - 3600
        main._DB_ERROR_LOG.append((old_ts, "connection_closed", "old"))
        main._record_db_error(Exception("the connection is closed"))
        s = main._db_errors_summary(window_s=300.0)  # 5 min
        self.assertEqual(s["total_last_window"], 1)  # only the new one

    def test_ring_buffer_bounded(self):
        # The deque maxlen caps the ring buffer at 1000 entries even under
        # a flood. Just verify the maxlen is set.
        self.assertEqual(main._DB_ERROR_LOG.maxlen, 1000)

    def test_long_message_truncated(self):
        # 500-char message → preview gets cut to 200
        msg = "x" * 500
        main._record_db_error(Exception(msg))
        _, _, preview = main._DB_ERROR_LOG[0]
        self.assertEqual(len(preview), 200)

    def test_summary_window_size_reported(self):
        s = main._db_errors_summary(window_s=120.0)
        self.assertEqual(s["window_seconds"], 120)


class TestPostgresMemcgStatus(_BaseObs):
    """`_postgres_memcg_status` parses docker stats output safely."""

    def _fake_run(self, json_payload, returncode=0):
        proc = MagicMock()
        proc.stdout = json_payload + "\n"
        proc.returncode = returncode
        return proc

    def test_happy_path(self):
        out = json.dumps({
            "Container": "mempalace-db",
            "MemUsage": "2.43GiB / 3GiB",
            "MemPerc": "81.00%",
        })
        with patch("subprocess.run", return_value=self._fake_run(out)):
            status = main._postgres_memcg_status()
        self.assertIsNotNone(status)
        self.assertEqual(status["container"], "mempalace-db")
        self.assertEqual(status["usage"], "2.43GiB")
        self.assertEqual(status["limit"], "3GiB")
        self.assertEqual(status["percent"], 0.81)
        self.assertIsInstance(status["probed_at"], int)

    def test_docker_not_found_returns_none(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("docker")):
            status = main._postgres_memcg_status()
        self.assertIsNone(status)

    def test_docker_timeout_returns_none(self):
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("docker", 2.0),
        ):
            status = main._postgres_memcg_status()
        self.assertIsNone(status)

    def test_docker_nonzero_exit_returns_none(self):
        """Container not running → docker stats exits 1 → CalledProcessError."""
        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "docker"),
        ):
            status = main._postgres_memcg_status()
        self.assertIsNone(status)

    def test_malformed_json_returns_none(self):
        proc = MagicMock()
        proc.stdout = "not-json\n"
        proc.returncode = 0
        with patch("subprocess.run", return_value=proc):
            status = main._postgres_memcg_status()
        self.assertIsNone(status)

    def test_missing_memperc_returns_none(self):
        out = json.dumps({"MemUsage": "1GiB / 2GiB"})  # no MemPerc
        proc = MagicMock()
        proc.stdout = out
        proc.returncode = 0
        with patch("subprocess.run", return_value=proc):
            status = main._postgres_memcg_status()
        # Missing MemPerc defaults to "0%" → parses to 0.0 → still returns
        # a status with percent=0.0 rather than None (graceful)
        self.assertIsNotNone(status)
        self.assertEqual(status["percent"], 0.0)

    def test_container_override_via_env(self):
        out = json.dumps({
            "Container": "other-db",
            "MemUsage": "1GiB / 4GiB",
            "MemPerc": "25.00%",
        })
        with patch("subprocess.run", return_value=self._fake_run(out)) as mock_run, \
             patch.dict(os.environ, {"PALACE_POSTGRES_CONTAINER": "other-db"}):
            status = main._postgres_memcg_status()
        self.assertEqual(status["container"], "other-db")
        # Verify the env override actually reached subprocess.run as the arg
        call_args = mock_run.call_args.args[0]
        self.assertIn("other-db", call_args)


class TestMemcgCanary(_BaseObs):
    """`_log_postgres_memcg_canary` chooses INFO vs WARNING by threshold."""

    def test_below_threshold_logs_info(self):
        fake_status = {
            "container": "mempalace-db",
            "usage": "1.2GiB", "limit": "3GiB",
            "percent": 0.40, "probed_at": int(time.time()),
        }
        logger = MagicMock()
        with patch.object(main, "_postgres_memcg_status", return_value=fake_status):
            main._log_postgres_memcg_canary(logger, env={})
        logger.info.assert_called_once()
        logger.warning.assert_not_called()

    def test_above_threshold_logs_warning(self):
        fake_status = {
            "container": "mempalace-db",
            "usage": "2.5GiB", "limit": "3GiB",
            "percent": 0.83, "probed_at": int(time.time()),
        }
        logger = MagicMock()
        with patch.object(main, "_postgres_memcg_status", return_value=fake_status):
            main._log_postgres_memcg_canary(logger, env={})
        logger.warning.assert_called_once()
        logger.info.assert_not_called()
        msg = logger.warning.call_args.args[0] % logger.warning.call_args.args[1:]
        self.assertIn("approaching OOM", msg)

    def test_custom_threshold_via_env(self):
        fake_status = {
            "container": "mempalace-db",
            "usage": "1.5GiB", "limit": "3GiB",
            "percent": 0.55, "probed_at": int(time.time()),
        }
        logger = MagicMock()
        # Below 75 default, but above 50 custom
        with patch.object(main, "_postgres_memcg_status", return_value=fake_status):
            main._log_postgres_memcg_canary(
                logger, env={"PALACE_POSTGRES_MEMCG_WARN_PERCENT": "50"}
            )
        logger.warning.assert_called_once()

    def test_non_numeric_threshold_falls_back_to_default(self):
        fake_status = {
            "container": "mempalace-db",
            "usage": "2.0GiB", "limit": "3GiB",
            "percent": 0.65, "probed_at": int(time.time()),
        }
        logger = MagicMock()
        with patch.object(main, "_postgres_memcg_status", return_value=fake_status):
            main._log_postgres_memcg_canary(
                logger, env={"PALACE_POSTGRES_MEMCG_WARN_PERCENT": "not-a-number"}
            )
        # Default 75% → 65% is below → INFO
        logger.info.assert_called_once()

    def test_docker_unreachable_logs_skip(self):
        logger = MagicMock()
        with patch.object(main, "_postgres_memcg_status", return_value=None):
            main._log_postgres_memcg_canary(logger, env={})
        logger.info.assert_called_once()
        msg = logger.info.call_args.args[0]
        self.assertIn("docker unreachable", msg)
        logger.warning.assert_not_called()


class TestConnectPostgresRecordsErrors(_BaseObs):
    """`_connect_postgres` should record OperationalError into the ring buffer."""

    def test_connect_failure_records_error(self):
        import psycopg2
        with patch.object(main, "_postgres_dsn", return_value="postgres://x"), \
             patch("psycopg2.connect",
                   side_effect=psycopg2.OperationalError("connection refused")):
            try:
                main._connect_postgres()
            except main._DaemonToolError:
                pass
        self.assertEqual(len(main._DB_ERROR_LOG), 1)
        _, pattern, preview = main._DB_ERROR_LOG[0]
        self.assertEqual(pattern, "connect_failed")
        self.assertIn("connection refused", preview)


if __name__ == "__main__":
    unittest.main()
