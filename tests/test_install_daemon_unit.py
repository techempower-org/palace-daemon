"""scripts/systemd/install_daemon_unit.py: backup, install, show-diff, restore; never a restart (row 34831d29).

Runs against a throwaway root and a fake `systemctl` that logs every call and serves `systemctl show`
from files, so nothing here touches the host.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "systemd" / "install_daemon_unit.py"
spec = importlib.util.spec_from_file_location("install_daemon_unit", SCRIPT)
idu = importlib.util.module_from_spec(spec)
_dwb, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ under scripts/systemd
spec.loader.exec_module(idu)
sys.dont_write_bytecode = _dwb

FAKE = """#!/bin/sh
echo "$@" >> "$FAKE_DIR/calls"
case "$1" in
  show) if [ -e "$FAKE_DIR/reloaded" ]; then cat "$FAKE_DIR/after"; else cat "$FAKE_DIR/before"; fi ;;
  daemon-reload) [ -e "$FAKE_DIR/reload-fails" ] && exit 1; touch "$FAKE_DIR/reloaded" ;;
  *) exit 9 ;;
esac
"""
BEFORE = ("MemoryMax=6442450944\nOOMPolicy=continue\nFragmentPath=/etc/systemd/system/palace-daemon.service\n"
          "DropInPaths=/a/memory.conf\nMemoryCurrent=100\nWatchdogTimestamp=Sat 13:00\nEnvironment=A=1\n"
          "NeedDaemonReload=no\n")
AFTER_SAME = ("MemoryMax=6442450944\nOOMPolicy=continue\nFragmentPath=/etc/systemd/system/palace-daemon.service\n"
              "DropInPaths=\nMemoryCurrent=200\nWatchdogTimestamp=Sat 13:01\nEnvironment=A=1\nNeedDaemonReload=no\n")
NOW = dt.datetime(2026, 11, 21, 14, 0)


class InstallDaemonUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root, self.fake, self.backups = self.tmp / "root", self.tmp / "fake", self.tmp / "backups"
        self.fake.mkdir()
        sc = self.fake / "systemctl"
        sc.write_text(FAKE)
        sc.chmod(sc.stat().st_mode | stat.S_IXUSR)
        (self.fake / "before").write_text(BEFORE)
        (self.fake / "after").write_text(AFTER_SAME)
        os.environ["FAKE_DIR"] = str(self.fake)
        self.addCleanup(os.environ.pop, "FAKE_DIR", None)
        self.unit = self.root / idu.UNIT_REL
        self.unit.parent.mkdir(parents=True)
        self.unit.write_bytes(b"[Service]\nMay copy\n")
        self.files = {}
        for rel, name in ((idu.DROPIN_RELS[0], "memory.conf"), (idu.DROPIN_RELS[0], "zzz-watchdog-345.conf"),
                          (idu.DROPIN_RELS[1], "50-MemoryMax.conf"), (idu.DROPIN_RELS[1], "50-MemorySwapMax.conf")):
            p = self.root / rel / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(f"[Service]\n# {name}\n".encode())
            self.files[p] = p.read_bytes()
        self.old_unit = self.unit.read_bytes()

    def _install(self, expect=frozenset()):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            rc = idu.install(self.root, self.backups, str(self.fake / "systemctl"), now=NOW, expect=expect)
        return rc, out.getvalue()

    def _restore(self, bdir):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return idu.restore(self.root, bdir, str(self.fake / "systemctl"))

    def _bdirs(self):
        return sorted(self.backups.iterdir()) if self.backups.exists() else []

    def _calls(self):
        p = self.fake / "calls"
        return p.read_text().splitlines() if p.exists() else []

    def _assert_all_back(self):
        self.assertEqual(self.unit.read_bytes(), self.old_unit)
        for p, data in self.files.items():
            self.assertEqual(p.read_bytes(), data)

    def test_install_moves_every_drop_in_from_both_dirs_into_a_dated_listed_backup(self):
        rc, out = self._install()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.unit.read_bytes(), idu.REPO_UNIT.read_bytes())
        self.assertEqual(idu.dropins(self.root), [])
        [bdir] = self._bdirs()
        self.assertTrue(bdir.name.startswith("palace-daemon-unit-20261121-140000-"), bdir.name)
        for p, data in self.files.items():
            self.assertEqual((bdir / "files" / p.relative_to(self.root)).read_bytes(), data)
        manifest = (bdir / "MANIFEST.tsv").read_text().splitlines()
        self.assertEqual(sorted(r.split("\t")[0] for r in manifest), ["copied"] + ["moved"] * 4)
        self.assertIn("/etc/systemd/system.control/palace-daemon.service.d/50-MemoryMax.conf", "\n".join(manifest))
        self.assertTrue((bdir / "show-before.txt").is_file() and (bdir / "show-after.txt").is_file())

    def test_a_rerun_does_nothing_and_makes_no_backup(self):
        self._install()
        rc, out = self._install()
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", out)
        self.assertEqual(len(self._bdirs()), 1)

    def test_restore_puts_every_byte_back_and_a_rerun_is_safe(self):
        self._install()
        [bdir] = self._bdirs()
        self.assertEqual(self._restore(bdir), 0)
        self._assert_all_back()
        self.assertEqual(self._restore(bdir), 0)

    def test_it_never_starts_stops_or_restarts_anything(self):
        self._install()
        self._restore(self._bdirs()[0])
        self.assertEqual({c.split()[0] for c in self._calls()}, {"show", "daemon-reload"})

    # review #349 r2 (1): Environment is compared; an intended change is named with --expect
    def test_an_environment_change_is_effective_unless_expected(self):
        (self.fake / "after").write_text(AFTER_SAME.replace("Environment=A=1", "Environment=A=1 B=2"))
        rc, out = self._install()
        self.assertEqual(rc, 3)
        self.assertIn("EFFECTIVE CHANGE in Environment", out)
        self._restore(self._bdirs()[0])
        (self.fake / "reloaded").unlink()
        rc, out = self._install(expect=frozenset({"Environment"}))
        self.assertEqual(rc, 0, out)
        self.assertIn("expected change in Environment", out)

    def test_a_memory_change_is_effective_even_with_environment_expected(self):
        (self.fake / "after").write_text(AFTER_SAME.replace("MemoryMax=6442450944", "MemoryMax=8589934592"))
        rc, out = self._install(expect=frozenset({"Environment"}))
        self.assertEqual(rc, 3)
        self.assertIn("EFFECTIVE CHANGE in MemoryMax", out)

    # (2): a failed reload restores everything and fails; a rerun runs a pending reload
    def test_a_failed_reload_restores_and_exits_nonzero(self):
        (self.fake / "reload-fails").touch()
        rc, _ = self._install()
        self.assertEqual(rc, 5)
        self._assert_all_back()

    def test_a_rerun_with_a_pending_reload_runs_it(self):
        self.unit.write_bytes(idu.REPO_UNIT.read_bytes())
        for p in self.files:
            p.unlink()
        (self.fake / "before").write_text(BEFORE.replace("NeedDaemonReload=no", "NeedDaemonReload=yes"))
        rc, out = self._install()
        self.assertEqual(rc, 0)
        self.assertIn("pending daemon-reload is done now", out)
        self.assertIn("daemon-reload", self._calls())

    # (3): the temp file is private and unpredictable; a planted file at the old fixed name is not followed
    def test_the_unit_is_written_through_an_unpredictable_temp_file_with_mode_0644(self):
        victim = self.tmp / "victim"
        victim.write_bytes(b"untouched")
        (self.unit.parent / f".{self.unit.name}.install-tmp").symlink_to(victim)
        old = os.umask(0o077)
        try:
            idu.write_atomic(self.unit, b"new\n")
        finally:
            os.umask(old)
        self.assertEqual(victim.read_bytes(), b"untouched")
        self.assertEqual(self.unit.read_bytes(), b"new\n")
        self.assertEqual(stat.S_IMODE(self.unit.stat().st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in self.unit.parent.iterdir() if p.name.startswith(".")),
                         [f".{self.unit.name}.install-tmp"])

    # (4): restore checks every conflict first and changes nothing on one
    def test_a_conflict_stops_restore_before_any_file_changes(self):
        self._install()
        [bdir] = self._bdirs()
        p = sorted(self.files)[-1]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"[Service]\n# someone else's\n")
        reloads = self._calls().count("daemon-reload")
        self.assertEqual(self._restore(bdir), 4)
        self.assertEqual(self.unit.read_bytes(), idu.REPO_UNIT.read_bytes())
        self.assertEqual(p.read_bytes(), b"[Service]\n# someone else's\n")
        self.assertEqual(self._calls().count("daemon-reload"), reloads)

    # (5): two installs in the same second get two backups
    def test_backups_made_in_the_same_second_do_not_collide(self):
        self._install()
        self._restore(self._bdirs()[0])
        (self.fake / "reloaded").unlink()
        rc, _ = self._install()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._bdirs()), 2)

    # (6): no unit before means restore removes the one the install wrote
    def test_restore_removes_a_unit_that_was_absent_before(self):
        self.unit.unlink()
        self._install()
        [bdir] = self._bdirs()
        self.assertIn("absent\t/etc/systemd/system/palace-daemon.service", (bdir / "MANIFEST.tsv").read_text())
        self.assertTrue(self.unit.is_file())
        self.assertEqual(self._restore(bdir), 0)
        self.assertFalse(self.unit.exists())

    # Sen's #349 ruling (a): every backup copy is checked against its manifest sha256 before any write
    def test_a_tampered_backup_copy_stops_restore_before_any_write(self):
        for which in ("copied", "moved"):
            with self.subTest(which):
                self.setUp()
                self._install()
                [bdir] = self._bdirs()
                row = next(r.split("\t") for r in (bdir / "MANIFEST.tsv").read_text().splitlines()
                           if r.startswith(which))
                (bdir / "files" / row[1].lstrip("/")).write_bytes(b"[Service]\n# tampered\n")
                reloads = self._calls().count("daemon-reload")
                self.assertEqual(self._restore(bdir), 4)
                self.assertEqual(self.unit.read_bytes(), idu.REPO_UNIT.read_bytes())  # nothing written
                self.assertEqual(idu.dropins(self.root), [])
                self.assertEqual(self._calls().count("daemon-reload"), reloads)

    # (b): a symlinked drop-in (or unit) is refused before anything changes
    def test_a_symlinked_drop_in_is_refused_untouched(self):
        target = self.tmp / "elsewhere.conf"
        target.write_bytes(b"[Service]\n# linked\n")
        link = self.root / idu.DROPIN_RELS[0] / "linked.conf"
        link.symlink_to(target)
        rc, _ = self._install()
        self.assertEqual(rc, 3)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.unit.read_bytes(), self.old_unit)
        self.assertEqual(self._bdirs(), [])
        self.assertNotIn("daemon-reload", self._calls())

    # (c): --expect takes only Environment, so a cap change can never be waved through
    def test_expect_refuses_any_key_but_environment(self):
        (self.fake / "after").write_text(AFTER_SAME.replace("MemoryMax=6442450944", "MemoryMax=8589934592"))
        rc, _ = self._install(expect=frozenset({"MemoryMax"}))
        self.assertEqual(rc, 2)
        self.assertEqual(self.unit.read_bytes(), self.old_unit)
        self.assertEqual(self._bdirs(), [])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            idu.main(["--expect", "MemoryMax", "--root", str(self.root), "--systemctl", str(self.fake / "systemctl")])

    def test_only_source_documentation_and_live_counters_may_change(self):
        self.assertEqual(idu.effective_changes(idu.parse_show(BEFORE), idu.parse_show(AFTER_SAME)), [])


# The 2026-11-21 17:18 deploy: these were the only differences, and none was a change (row 34831d29).
PRE = "ExecStartPre={ path=/bin/bash ; argv[]=/bin/bash -c until pg_isready; do sleep 2; done ; ignore_errors=no ; "
RAN = ("start_time=[Sat 2026-11-21 00:07:40 PST] ; stop_time=[Sat 2026-11-21 00:07:40 PST] ; "
       "pid=3218544 ; code=exited ; status=0 }")
RELOADED = "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
SHOW_BEFORE = ("After=docker.service home-jp-Projects.mount\nBefore=shutdown.target multi-user.target\n"
               "Requires=docker.service sysinit.target\n"
               "ExecStartPre={ path=/usr/bin/fuser ; argv[]=/usr/bin/fuser -k 8085/tcp ; ignore_errors=yes ; " + RAN + "\n"
               + PRE + RAN + "\n")
SHOW_AFTER = ("After=home-jp-Projects.mount docker.service\nBefore=multi-user.target shutdown.target\n"
              "Requires=sysinit.target docker.service\n"
              "ExecStartPre={ path=/usr/bin/fuser ; argv[]=/usr/bin/fuser -k 8085/tcp ; ignore_errors=yes ; " + RELOADED
              + "\n" + PRE + RELOADED + "\n")


class ShowDiffNoise(unittest.TestCase):
    """A reload reorders dependency lists and resets each command's last run; neither is an effective change."""

    def test_a_reordered_dependency_list_is_the_same_list(self):
        self.assertEqual(idu.effective_changes(idu.parse_show(SHOW_BEFORE), idu.parse_show(SHOW_AFTER)), [])

    def test_a_dependency_list_that_gains_a_member_is_a_change(self):
        after = SHOW_AFTER.replace("Requires=sysinit.target docker.service", "Requires=sysinit.target")
        self.assertEqual(idu.effective_changes(idu.parse_show(SHOW_BEFORE), idu.parse_show(after)), ["Requires"])

    def test_a_reset_last_run_is_not_a_change_but_a_new_argv_is(self):
        after = SHOW_AFTER.replace("-k 8085/tcp", "-k 8086/tcp")
        self.assertEqual(idu.effective_changes(idu.parse_show(SHOW_BEFORE), idu.parse_show(after)), ["ExecStartPre"])
        after = SHOW_AFTER.replace("ignore_errors=yes", "ignore_errors=no")
        self.assertEqual(idu.effective_changes(idu.parse_show(SHOW_BEFORE), idu.parse_show(after)), ["ExecStartPre"])

    def test_every_repeated_key_is_kept_not_only_the_last(self):
        props = idu.parse_show(SHOW_BEFORE)
        self.assertEqual(props["ExecStartPre"].count("path="), 2)
        # a change to the FIRST of two ExecStartPre commands is still seen
        after = SHOW_AFTER.replace("/usr/bin/fuser -k", "/usr/bin/fuser -9 -k")
        self.assertEqual(idu.effective_changes(props, idu.parse_show(after)), ["ExecStartPre"])


if __name__ == "__main__":
    unittest.main()
