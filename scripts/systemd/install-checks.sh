#!/usr/bin/env bash
# Install (or refresh) the palace lane's check timers as user units and run each once.
# Needs ~/.config/palace-checks/env (DREAM_OUT, GS_CHECK_HOSTS, PALACE_CHECKS_DIR); see docs/CHECKS.md.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
[ -r "$HOME/.config/palace-checks/env" ] || { echo "install-checks: ~/.config/palace-checks/env is missing (docs/CHECKS.md)" >&2; exit 4; }
# The local units' only writable path (ReadWritePaths): it must exist before they start, and
# the lines must go there.
lines="$HOME/.local/state/checks"; mkdir -p "$lines"   # where the plan's Hearth › Checks reads
want="$(sed -n 's/^PALACE_CHECKS_DIR=//p' "$HOME/.config/palace-checks/env" | tr -d '"')"
[ "$want" = "$lines" ] || { echo "install-checks: PALACE_CHECKS_DIR must be $lines (the units' writable path)" >&2; exit 4; }
dest="$HOME/.config/systemd/user"; mkdir -p "$dest"
for f in "$here"/palace-check-*.service "$here"/palace-check-*.timer; do install -m 0644 "$f" "$dest/"; done
systemctl --user daemon-reload
for t in "$here"/palace-check-*.timer; do
  name="$(basename "$t")"
  systemctl --user enable --now "$name"
  systemctl --user start "${name%.timer}.service" || echo "install-checks: first run of ${name%.timer} failed; see journalctl --user -u ${name%.timer}" >&2
done
systemctl --user list-timers 'palace-check-*' --no-pager
