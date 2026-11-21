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

    sudo python3 scripts/systemd/install_daemon_unit.py

1. If the installed unit already matches and no drop-in exists, it does nothing.
2. Otherwise it makes `/var/backups/palace-daemon-unit/palace-daemon-unit-<date>-<time>/`, containing:
   - the whole `systemctl show` output from before;
   - the old unit (copied);
   - every drop-in, moved, under its original path. That covers both
     `/etc/systemd/system/palace-daemon.service.d/` and `/etc/systemd/system.control/palace-daemon.service.d/`;
   - `MANIFEST.tsv`, the listing.
3. It writes the repo unit and runs `systemctl daemon-reload`. **It never restarts the daemon.**
4. It diffs `systemctl show` before and after. Only the unit's source, `Documentation`, `Environment` (which takes
   effect at the next start) and the live counters may change. Any other key is an effective change: the script
   prints it and exits 3 with the restore command.

Then:

- Restart only if the change needs one (an `Environment=` or `ExecStart=` change does). Never between 01:00 and
  03:00, the nightly dump.
- Run `systemctl --user start palace-check-unit-drift.service`. The line should read `ok`.

**Rollback:** `sudo python3 scripts/systemd/install_daemon_unit.py --restore <backup dir>`. It puts every file back
where it was and reloads. It is safe to rerun, and it refuses to overwrite a drop-in that has since changed
(exit 4).
