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
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "systemd" / "install_daemon_unit.py"
spec = importlib.util.spec_from_file_location("install_daemon_unit", SCRIPT)
idu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(idu)

FAKE = """#!/bin/sh
echo "$@" >> "$FAKE_DIR/calls"
case "$1" in
  show) if [ -e "$FAKE_DIR/reloaded" ]; then cat "$FAKE_DIR/after"; else cat "$FAKE_DIR/before"; fi ;;
  daemon-reload) touch "$FAKE_DIR/reloaded" ;;
  *) exit 9 ;;
esac
"""
BEFORE = ("MemoryMax=6442450944\nOOMPolicy=continue\nFragmentPath=/etc/systemd/system/palace-daemon.service\n"
          "DropInPaths=/a/memory.conf\nMemoryCurrent=100\nWatchdogTimestamp=Sat 13:00\nEnvironment=A=1\n")
AFTER_SAME = ("MemoryMax=6442450944\nOOMPolicy=continue\nFragmentPath=/etc/systemd/system/palace-daemon.service\n"
              "DropInPaths=\nMemoryCurrent=200\nWatchdogTimestamp=Sat 13:01\nEnvironment=A=1 B=2\n")


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

    def _install(self, minute=0):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = idu.install(self.root, self.backups, str(self.fake / "systemctl"), now=dt.datetime(2026, 11, 21, 14, minute))
        return rc, out.getvalue()

    def _restore(self, bdir):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return idu.restore(self.root, bdir, str(self.fake / "systemctl"))

    def test_install_moves_every_drop_in_from_both_dirs_into_a_dated_listed_backup(self):
        rc, out = self._install()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.unit.read_bytes(), idu.REPO_UNIT.read_bytes())
        self.assertEqual(idu.dropins(self.root), [])
        bdir = self.backups / "palace-daemon-unit-20261121-140000"
        for p, data in self.files.items():
            self.assertEqual((bdir / "files" / p.relative_to(self.root)).read_bytes(), data)
        manifest = (bdir / "MANIFEST.tsv").read_text().splitlines()
        self.assertEqual(sorted(r.split("\t")[0] for r in manifest), ["copied"] + ["moved"] * 4)
        self.assertIn("/etc/systemd/system.control/palace-daemon.service.d/50-MemoryMax.conf", "\n".join(manifest))
        self.assertTrue((bdir / "show-before.txt").is_file() and (bdir / "show-after.txt").is_file())

    def test_a_rerun_does_nothing_and_makes_no_backup(self):
        self._install(0)
        rc, out = self._install(1)
        self.assertEqual(rc, 0)
        self.assertIn("nothing to do", out)
        self.assertEqual([p.name for p in self.backups.iterdir()], ["palace-daemon-unit-20261121-140000"])

    def test_restore_puts_every_byte_back_and_a_rerun_is_safe(self):
        self._install()
        bdir = self.backups / "palace-daemon-unit-20261121-140000"
        self.assertEqual(self._restore(bdir), 0)
        self.assertEqual(self.unit.read_bytes(), self.old_unit)
        for p, data in self.files.items():
            self.assertEqual(p.read_bytes(), data)
        self.assertEqual(self._restore(bdir), 0)

    def test_restore_will_not_overwrite_a_drop_in_with_other_bytes(self):
        self._install()
        p = next(iter(self.files))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"[Service]\n# someone else's\n")
        self.assertEqual(self._restore(self.backups / "palace-daemon-unit-20261121-140000"), 4)
        self.assertEqual(p.read_bytes(), b"[Service]\n# someone else's\n")

    def test_an_effective_change_after_the_reload_exits_3(self):
        (self.fake / "after").write_text(AFTER_SAME.replace("MemoryMax=6442450944", "MemoryMax=8589934592"))
        rc, out = self._install()
        self.assertEqual(rc, 3)
        self.assertIn("EFFECTIVE CHANGE in MemoryMax", out)

    def test_only_source_documentation_environment_and_live_counters_may_change(self):
        self.assertEqual(idu.effective_changes(
            dict(ln.split("=", 1) for ln in BEFORE.splitlines()),
            dict(ln.split("=", 1) for ln in AFTER_SAME.splitlines())), [])

    def test_it_never_starts_stops_or_restarts_anything(self):
        self._install()
        self._restore(self.backups / "palace-daemon-unit-20261121-140000")
        verbs = {ln.split()[0] for ln in (self.fake / "calls").read_text().splitlines()}
        self.assertEqual(verbs, {"show", "daemon-reload"})


if __name__ == "__main__":
    unittest.main()
