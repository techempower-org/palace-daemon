# Installing palace-daemon.service

The unit in this repo is the unit on the palace host, byte for byte, with nothing on top. `CHECK palace-unit-drift`
([CHECKS.md](../CHECKS.md)) compares them every day at 07:05.

## Change a setting

Edit `palace-daemon.service` here, by PR, then install it (below). Do not add a drop-in, and do not run
`systemctl set-property`:

- A drop-in under `/etc/systemd/system/palace-daemon.service.d/` shows in the check as `warn … drop-in(s) on top`.
  A dated trial is allowed (for example a 24 h allocator trial); fold it into the unit or remove it when the trial
  ends.
- `systemctl set-property palace-daemon …` writes a drop-in under **`/etc/systemd/system.control/`**, a directory
  that is easy to miss. On 2026-11-21 two lived there: `50-MemoryMax.conf` (8G) and `50-MemorySwapMax.conf` (3G). The 8G
  override only lost to the 6G cap because another drop-in happened to sort after it. Running `set-property` again
  would silently bring it back. The check names these as `(set-property)`.

## Install

On the palace host, from the synced tree after it is aligned to `main`:

    sudo python3 scripts/systemd/install_daemon_unit.py [--expect Environment]

0. It refuses to start, changing nothing, in two cases:
   - `--expect` names any key but `Environment` (exit 2);
   - the unit or a drop-in is a symlink (exit 3, named), because a backup would restore it as a plain file.
1. If the installed unit already matches and no drop-in exists, it does nothing, except that it runs a pending
   daemon-reload.
2. Otherwise it makes `/var/backups/palace-daemon-unit/palace-daemon-unit-<date>-<time>-<random>/`, containing:
   - the whole `systemctl show` output from before;
   - the old unit (copied);
   - every drop-in, moved, under its original path. That covers both
     `/etc/systemd/system/palace-daemon.service.d/` and `/etc/systemd/system.control/palace-daemon.service.d/`;
   - `MANIFEST.tsv`, the listing. It says `absent` when there was no unit before.
3. It writes the repo unit and runs `systemctl daemon-reload`. **It never restarts the daemon.** If the reload fails,
   it restores everything from the backup it just made and exits 5.
4. It diffs `systemctl show` before and after. Only the unit's source, `Documentation` and the live counters may
   change unannounced. Any other key is an effective change, `Environment` included (it takes effect at the next
   start). A key named with `--expect` is printed as expected; any other key makes it print the restore command and
   exit 3.
   - On 2026-11-21 the install was expected to change `Environment` and nothing else. The repo pins
     `PALACE_RERANK_MODEL`; the old unit didn't, and the pinned value is the code's default.

Then:

- Restart only if the change needs one (an `Environment=` or `ExecStart=` change does). Never between 01:00 and
  03:00, the nightly dump.
- Run `systemctl --user start palace-check-unit-drift.service`. The line should read `ok`.

**Rollback:** `sudo python3 scripts/systemd/install_daemon_unit.py --restore <backup dir>`.

- It checks every manifest row first. Each backup copy must exist and match its recorded sha256, and a drop-in
  already back must have the same bytes. If any row fails, it changes nothing and exits 4.
- Otherwise it puts every file back (it removes the unit if it was `absent`) and reloads once. If that reload
  fails, it exits 6 with the files already back.
- It is safe to rerun.
