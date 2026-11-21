#!/usr/bin/env python3
"""Install palace-daemon.service from this repo over the host's, with every drop-in moved into a dated backup.

    sudo python3 scripts/systemd/install_daemon_unit.py [--backup-root DIR]
    sudo python3 scripts/systemd/install_daemon_unit.py --restore DIR

Run-book: docs/ops/palace-daemon-unit.md. Stdlib only. **It never restarts the daemon**; it runs
`systemctl daemon-reload` and nothing else, so a change that needs a restart waits for one you choose.

Install:
1. If the installed unit already equals the repo's byte for byte and no drop-in file exists in either drop-in
   directory, there is nothing to do: exit 0, no backup.
2. Otherwise make ``<backup-root>/palace-daemon-unit-YYYYmmdd-HHMMSS/``: ``show-before.txt`` (all of
   ``systemctl show``), ``files/<original path>`` (the old unit copied, each drop-in **moved**, from
   ``/etc/systemd/system/palace-daemon.service.d/`` and ``/etc/systemd/system.control/palace-daemon.service.d/``,
   the latter being where ``systemctl set-property`` writes), and ``MANIFEST.tsv`` (action, original path,
   sha256), the listing.
3. Write the repo unit in place (temp file, then rename, mode 0644), then ``daemon-reload``.
4. ``show-after.txt``, diffed against before. Keys that may change: the unit's own source (FragmentPath,
   DropInPaths, NeedDaemonReload), Documentation and Environment (which take effect only at the next start), and
   the counters that move on their own (CPU, memory, watchdog timestamps). Any other difference is an effective
   change: it is printed and the exit code is 3, with the restore command. The install itself stays in place.

Restore: put every file in the manifest back where it was (the unit overwritten from its copy; drop-ins moved
back), then ``daemon-reload``. A drop-in already back with the same bytes is skipped, so a rerun is safe; one
with different bytes stops the restore (exit 4) rather than overwrite it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

UNIT = "palace-daemon.service"
REPO_UNIT = Path(__file__).resolve().parents[2] / UNIT
UNIT_REL = Path("etc/systemd/system") / UNIT
DROPIN_RELS = (Path("etc/systemd/system") / f"{UNIT}.d", Path("etc/systemd/system.control") / f"{UNIT}.d")
MAY_CHANGE = {"FragmentPath", "DropInPaths", "NeedDaemonReload", "Documentation", "Environment"}
MOVES_ON_ITS_OWN = re.compile(r"^(CPUUsageNSec|Memory(Available|Current|SwapCurrent|ZSwapCurrent|Peak|SwapPeak)|"
                              r"Tasks(Current)?|IO\w+Bytes|IO\w+Operations|IP\w+(Bytes|Packets)|Watchdog\w*Timestamp\w*|"
                              r"StatusText|StatusErrno)$")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def show(systemctl: str) -> dict[str, str]:
    out = subprocess.run([systemctl, "show", UNIT], capture_output=True, text=True, check=True, timeout=60).stdout
    return dict(ln.split("=", 1) for ln in out.splitlines() if "=" in ln)


def reload(systemctl: str) -> None:
    subprocess.run([systemctl, "daemon-reload"], check=True, timeout=120)


def effective_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    keys = sorted(set(before) | set(after))
    return [k for k in keys
            if before.get(k) != after.get(k) and k not in MAY_CHANGE and not MOVES_ON_ITS_OWN.match(k)]


def dropins(root: Path) -> list[Path]:
    return sorted(p for rel in DROPIN_RELS for p in (root / rel).glob("*.conf") if p.is_file())


def write_atomic(dest: Path, data: bytes) -> None:
    tmp = dest.with_name(f".{dest.name}.install-tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest)


def install(root: Path, backup_root: Path, systemctl: str, now: dt.datetime | None = None) -> int:
    unit = root / UNIT_REL
    repo = REPO_UNIT.read_bytes()
    found = dropins(root)
    if unit.is_file() and unit.read_bytes() == repo and not found:
        print(f"install: {UNIT} already matches the repo and has no drop-ins; nothing to do")
        return 0
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    bdir = backup_root / f"palace-daemon-unit-{stamp}"
    bdir.mkdir(parents=True, exist_ok=False)
    before = show(systemctl)
    (bdir / "show-before.txt").write_text("".join(f"{k}={v}\n" for k, v in before.items()))
    rows = []
    if unit.is_file():
        dest = bdir / "files" / UNIT_REL
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(unit, dest)
        rows.append(("copied", "/" + str(UNIT_REL), sha(dest)))
    for p in found:
        rel = p.relative_to(root)
        dest = bdir / "files" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = sha(p)
        shutil.move(str(p), dest)
        rows.append(("moved", "/" + str(rel), digest))
    (bdir / "MANIFEST.tsv").write_text("".join("\t".join(r) + "\n" for r in rows))
    write_atomic(unit, repo)
    reload(systemctl)
    after = show(systemctl)
    (bdir / "show-after.txt").write_text("".join(f"{k}={v}\n" for k, v in after.items()))
    print(f"install: backup {bdir}")
    for action, path, digest in rows:
        print(f"  {action}\t{path}\t{digest[:12]}")
    changed = effective_changes(before, after)
    if changed:
        print(f"install: EFFECTIVE CHANGE in {', '.join(changed)}: see show-before/after.txt. "
              f"To undo: {sys.argv[0]} --restore {bdir}")
        return 3
    print("install: no effective change (only the unit's source, Documentation, Environment and live counters)")
    return 0


def restore(root: Path, bdir: Path, systemctl: str) -> int:
    rows = [ln.split("\t") for ln in (bdir / "MANIFEST.tsv").read_text().splitlines() if ln]
    for action, path, digest in rows:
        rel = Path(path.lstrip("/"))
        src, dest = bdir / "files" / rel, root / rel
        if action == "copied":
            write_atomic(dest, src.read_bytes())
        elif action == "moved":
            if dest.exists():
                if sha(dest) == digest:
                    continue
                print(f"restore: {path} exists with different bytes; not overwriting", file=sys.stderr)
                return 4
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dest)
    reload(systemctl)
    print(f"restore: {len(rows)} file(s) back from {bdir}; daemon-reload done, no restart")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--restore", type=Path, help="a backup directory made by an install")
    ap.add_argument("--backup-root", type=Path, default=Path(os.environ.get(
        "PALACE_UNIT_BACKUP_ROOT", "/var/backups/palace-daemon-unit")))
    ap.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)  # tests
    ap.add_argument("--systemctl", default="systemctl", help=argparse.SUPPRESS)  # tests
    a = ap.parse_args(argv)
    if a.restore:
        return restore(a.root, a.restore, a.systemctl)
    return install(a.root, a.backup_root, a.systemctl)


if __name__ == "__main__":
    sys.exit(main())
