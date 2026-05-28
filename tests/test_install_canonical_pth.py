"""Tests for scripts/install-canonical-pth.sh (issue #79).

The .pth-installer is one of three places we restore the canonical-mapping
import path after a venv rebuild (the other two are auto-repair-if-empty.sh's
call to it, and deploy.sh's pre-restart ssh invocation). Exercising it directly
covers all three by composition.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_install_canonical_pth.py -q
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPT = _HERE.parent / "scripts" / "install-canonical-pth.sh"


def _run(venv: Path, source: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PALACE_VENV": str(venv),
        "PALACE_SOURCE": str(source),
    }
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


class TestInstallCanonicalPth(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # Build a venv layout
        self.venv = self.tmp / "venv"
        self.site = self.venv / "lib" / "python3.12" / "site-packages"
        self.site.mkdir(parents=True)
        # And a source dir
        self.source = self.tmp / "source"
        self.source.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _pth(self) -> Path:
        return self.site / "palace-daemon-source.pth"

    def test_writes_pth_on_first_run(self):
        r = _run(self.venv, self.source)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertTrue(self._pth().exists(), "pth was not written")
        self.assertEqual(self._pth().read_text().strip(), str(self.source))

    def test_idempotent(self):
        # Two runs in a row produce identical state.
        _run(self.venv, self.source)
        first_mtime = self._pth().stat().st_mtime_ns
        r = _run(self.venv, self.source)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(self._pth().read_text().strip(), str(self.source))
        # Second run must not rewrite — atomic .pth creation would change mtime.
        # On filesystems with subsecond mtime, mtime should be identical.
        self.assertEqual(self._pth().stat().st_mtime_ns, first_mtime,
                         "second run rewrote the .pth (not idempotent)")
        self.assertIn("already correct", r.stdout)

    def test_rewrites_when_content_diverges(self):
        # Pre-populate the .pth with the wrong content.
        self._pth().write_text("/wrong/path\n")
        r = _run(self.venv, self.source)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertEqual(self._pth().read_text().strip(), str(self.source))
        self.assertIn("wrote", r.stdout)

    def test_finds_any_python_subdir(self):
        # python3.10, python3.13, etc. — the script must glob, not hard-code 3.12.
        # Re-layout the venv under python3.13.
        for child in self.venv.iterdir():
            subprocess.run(["rm", "-rf", str(child)], check=True)
        site = self.venv / "lib" / "python3.13" / "site-packages"
        site.mkdir(parents=True)

        r = _run(self.venv, self.source)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        pth = site / "palace-daemon-source.pth"
        self.assertTrue(pth.exists(), "pth not written under python3.13/site-packages")
        self.assertEqual(pth.read_text().strip(), str(self.source))

    def test_skips_when_venv_missing(self):
        # A missing venv path should NOT crash — it logs and exits 0 so that
        # deploy.sh / auto-repair-if-empty.sh stay safe to call unconditionally.
        r = _run(self.tmp / "no-such-venv", self.source)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("no venv", r.stderr)

    def test_skips_when_source_missing(self):
        r = _run(self.venv, self.tmp / "no-such-source")
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("no source", r.stderr)
        self.assertFalse(self._pth().exists())

    def test_atomic_write_no_half_pth(self):
        # The script uses mktemp + mv-f to write atomically. Confirm no
        # leftover *.XXXXXX file is present after a successful run.
        _run(self.venv, self.source)
        leftovers = list(self.site.glob("palace-daemon-source.pth.*"))
        self.assertEqual(leftovers, [], f"atomic-write left temp files behind: {leftovers}")


if __name__ == "__main__":
    unittest.main()
