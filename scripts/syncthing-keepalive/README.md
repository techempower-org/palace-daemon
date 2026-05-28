# syncthing-keepalive

A systemd-timer-driven oneshot that probes `syncthing@<user>.service` every 5 minutes and starts it if it has clean-exited. Covers the failure mode that took down mempalace deploys on 2026-05-28: Syncthing exited cleanly with status=0 (so `Restart=on-failure` wouldn't trigger), nobody noticed for ~1.5 hours, and the daemon happily kept serving 2-day-old code.

See [palace-daemon#92](https://github.com/techempower-org/palace-daemon/issues/92) for background.

## What it does

- `syncthing-keepalive@<user>.service` — oneshot that runs `systemctl is-active syncthing@<user>.service` and starts it on a non-zero exit code. No-op when Syncthing is healthy.
- `syncthing-keepalive@<user>.timer` — fires the oneshot every 5 minutes (first probe 2 minutes after boot to let the system-installed Syncthing unit come up on its own).

The `@<user>` instance form mirrors `syncthing@<user>.service`'s shape so you can target any user's Syncthing.

## Install (familiar, for user `jp`)

```bash
# Install the unit files
sudo cp scripts/syncthing-keepalive/syncthing-keepalive.service /etc/systemd/system/syncthing-keepalive@.service
sudo cp scripts/syncthing-keepalive/syncthing-keepalive.timer   /etc/systemd/system/syncthing-keepalive@.timer

# Enable the timer for user 'jp'
sudo systemctl daemon-reload
sudo systemctl enable --now syncthing-keepalive@jp.timer
```

## Verify

```bash
# Timer should be active and waiting
systemctl list-timers syncthing-keepalive@jp.timer

# Trigger a probe manually
sudo systemctl start syncthing-keepalive@jp.service
journalctl -u syncthing-keepalive@jp.service --since '1 minute ago'
```

## Why not just set `Restart=always` on Syncthing?

The system-installed `syncthing@.service` ships with the syncthing package — modifying it directly creates a maintenance burden (re-applying after every package update). A separate keepalive timer is a clean overlay that doesn't touch the upstream unit.

If you'd rather modify the syncthing unit directly, the one-liner is:

```bash
sudo systemctl edit syncthing@jp.service
# Add:
# [Service]
# Restart=always
# RestartSec=30s
```

Both approaches close the same gap; the keepalive timer was chosen here because it's overlay-only.
