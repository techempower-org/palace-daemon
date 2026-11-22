#!/usr/bin/env python3
"""Install palace-daemon.service from this repo over the host's, with every drop-in moved into a dated backup.

    sudo python3 scripts/systemd/install_daemon_unit.py [--backup-root DIR] [--expect KEY ...]
    sudo python3 scripts/systemd/install_daemon_unit.py --restore DIR

Run-book: docs/ops/palace-daemon-unit.md. Stdlib only. **It never restarts the daemon**; it runs
`systemctl daemon-reload` and nothing else, so a change that needs a restart waits for one you choose.

Install:
1. If the installed unit already equals the repo's byte for byte and no drop-in file exists in either drop-in
   directory, there is nothing to do: exit 0, no backup. If systemd still reports NeedDaemonReload, the reload is
   run then.
2. Otherwise make a new ``<backup-root>/palace-daemon-unit-YYYYmmdd-HHMMSS-<random>/`` (two runs in one second never
   share one) holding:
   - ``show-before.txt``, all of ``systemctl show``;
   - ``files/<original path>``: the old unit copied, and each drop-in **moved**, from
     ``/etc/systemd/system/palace-daemon.service.d/`` and ``/etc/systemd/system.control/palace-daemon.service.d/``
     (the latter is where ``systemctl set-property`` writes);
   - ``MANIFEST.tsv`` (action, original path, sha256), the listing. It records ``absent`` when there was no unit
     before, and each drop-in row is written before that file moves.
3. Write the repo unit in place (a private temp file in the same directory, mode 0644, then rename), then
   ``daemon-reload``. If the reload fails, everything is restored from that backup and the exit code is 5.
4. ``show-after.txt``, diffed against before. Only the unit's own source (FragmentPath, DropInPaths,
   NeedDaemonReload), Documentation and the counters that move on their own (CPU, memory, watchdog timestamps)
   may differ. Any other key, Environment included, is an effective change. A key named with ``--expect`` is
   printed as expected; any other is printed with the restore command, and the exit code is 3. The install itself
   stays in place.

Restore checks every conflict first and changes nothing if one is found (exit 4): a drop-in that is back with
different bytes, or one missing from the backup. Then it puts every file back (the unit from its copy, or removed
if it was ``absent``; drop-ins moved back) and runs one ``daemon-reload``. A drop-in already back with the same
bytes is skipped, so a rerun is safe.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

UNIT = "palace-daemon.service"
REPO_UNIT = Path(__file__).resolve().parents[2] / UNIT
UNIT_REL = Path("etc/systemd/system") / UNIT
DROPIN_RELS = (Path("etc/systemd/system") / f"{UNIT}.d", Path("etc/systemd/system.control") / f"{UNIT}.d")
# The unit's own source and its docs link. Everything else, Environment included (it changes the next start), is
# compared; a change the operator intends is named with --expect KEY and still printed.
MAY_CHANGE = {"FragmentPath", "DropInPaths", "NeedDaemonReload", "Documentation"}
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
    """A private, unpredictable temp file in the destination's directory, mode 0644 before the rename."""
    fd = tempfile.NamedTemporaryFile(dir=dest.parent, prefix=f".{dest.name}.", delete=False)
    try:
        with fd:
            fd.write(data)
            fd.flush()
            os.fsync(fd.fileno())
        os.chmod(fd.name, 0o644)
        os.replace(fd.name, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(fd.name)
        raise


def install(root: Path, backup_root: Path, systemctl: str, now: dt.datetime | None = None,
            expect: frozenset = frozenset()) -> int:
    unit = root / UNIT_REL
    repo = REPO_UNIT.read_bytes()
    found = dropins(root)
    if unit.is_file() and unit.read_bytes() == repo and not found:
        if show(systemctl).get("NeedDaemonReload") == "yes":  # a reload that never happened (or failed) last time
            reload(systemctl)
            print(f"install: {UNIT} already matches the repo; the pending daemon-reload is done now")
            return 0
        print(f"install: {UNIT} already matches the repo and has no drop-ins; nothing to do")
        return 0
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup_root.mkdir(parents=True, exist_ok=True)
    bdir = Path(tempfile.mkdtemp(prefix=f"palace-daemon-unit-{stamp}-", dir=backup_root))  # never collides
    before = show(systemctl)
    (bdir / "show-before.txt").write_text("".join(f"{k}={v}\n" for k, v in before.items()))
    rows = []
    if unit.is_file():
        dest = bdir / "files" / UNIT_REL
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(unit, dest)
        rows.append(("copied", "/" + str(UNIT_REL), sha(dest)))
    else:
        rows.append(("absent", "/" + str(UNIT_REL), "-"))  # restore removes the unit this install creates
    (bdir / "MANIFEST.tsv").write_text("".join("\t".join(r) + "\n" for r in rows))
    for p in found:
        rel = p.relative_to(root)
        dest = bdir / "files" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = sha(p)
        rows.append(("moved", "/" + str(rel), digest))
        (bdir / "MANIFEST.tsv").write_text("".join("\t".join(r) + "\n" for r in rows))  # listed before it moves
        shutil.move(str(p), dest)
    write_atomic(unit, repo)
    try:
        reload(systemctl)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"install: daemon-reload failed ({type(exc).__name__}); restoring from {bdir}", file=sys.stderr)
        rc = restore(root, bdir, systemctl)
        print(f"install: restore exit {rc} (0 = files back and reloaded; 6 = files back, reload failed again)",
              file=sys.stderr)
        return 5
    after = show(systemctl)
    (bdir / "show-after.txt").write_text("".join(f"{k}={v}\n" for k, v in after.items()))
    print(f"install: backup {bdir}")
    for action, path, digest in rows:
        print(f"  {action}\t{path}\t{digest[:12]}")
    changed = effective_changes(before, after)
    expected = [k for k in changed if k in expect]
    unexpected = [k for k in changed if k not in expect]
    if expected:
        print(f"install: expected change in {', '.join(expected)} (takes effect as systemd applies it; "
              f"an Environment change at the next start)")
    if unexpected:
        print(f"install: EFFECTIVE CHANGE in {', '.join(unexpected)}: see show-before/after.txt. "
              f"To undo: {sys.argv[0]} --restore {bdir}")
        return 3
    if not changed:
        print("install: no effective change (only the unit's source, Documentation and live counters)")
    return 0


def restore(root: Path, bdir: Path, systemctl: str) -> int:
    """Check every conflict first, then put every file back, then reload once."""
    rows = [ln.split("\t") for ln in (bdir / "MANIFEST.tsv").read_text().splitlines() if ln]
    plan = []
    for action, path, digest in rows:
        rel = Path(path.lstrip("/"))
        src, dest = bdir / "files" / rel, root / rel
        if action == "moved":
            if dest.exists():
                if sha(dest) == digest:
                    continue  # already back
                print(f"restore: {path} exists with different bytes; nothing restored", file=sys.stderr)
                return 4
            if not src.is_file():
                print(f"restore: {path} is missing from the backup; nothing restored", file=sys.stderr)
                return 4
        plan.append((action, src, dest))
    for action, src, dest in plan:
        if action == "copied":
            write_atomic(dest, src.read_bytes())
        elif action == "absent":
            with contextlib.suppress(FileNotFoundError):
                dest.unlink()
        elif action == "moved":
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dest)
    try:
        reload(systemctl)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"restore: files are back from {bdir}, but daemon-reload failed ({type(exc).__name__}): "
              f"run systemctl daemon-reload", file=sys.stderr)
        return 6
    print(f"restore: {len(rows)} manifest row(s) from {bdir}; daemon-reload done, no restart")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--restore", type=Path, help="a backup directory made by an install")
    ap.add_argument("--backup-root", type=Path, default=Path(os.environ.get(
        "PALACE_UNIT_BACKUP_ROOT", "/var/backups/palace-daemon-unit")))
    ap.add_argument("--expect", action="append", default=[], metavar="KEY",
                    help="a systemctl show key this install is meant to change (e.g. Environment); still printed")
    ap.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)  # tests
    ap.add_argument("--systemctl", default="systemctl", help=argparse.SUPPRESS)  # tests
    a = ap.parse_args(argv)
    if a.restore:
        return restore(a.root, a.restore, a.systemctl)
    return install(a.root, a.backup_root, a.systemctl, expect=frozenset(a.expect))


if __name__ == "__main__":
    sys.exit(main())
