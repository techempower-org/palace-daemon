#!/bin/bash
# Deploy palace-daemon source to a target host.
#
# Run LOCALLY on the build host (typically katana). Rsyncs the daemon
# source into the host's WorkingDirectory (matches /etc/systemd/system/
# palace-daemon.service), installs venv deps, restarts the systemd
# service, and smoke-tests the /health endpoint.
#
# The canonical WorkingDirectory on familiar is /mnt/raid/projects/palace-
# daemon/ — same path Caddy / our health checks expect to find it.
# Manual scp's to /home/jp/.local/share/palace-daemon/ were kept in
# sync by syncthing but that's a hidden dependency; this script writes
# directly to the canonical path.
#
# Usage:
#   deploy-palace-daemon.sh                            # defaults: --host familiar --root /mnt/raid/projects/palace-daemon
#   deploy-palace-daemon.sh --host <h> --root <path>
#   deploy-palace-daemon.sh --no-restart               # rsync only, skip systemctl
#   deploy-palace-daemon.sh --venv-path <p>            # alt venv (default /home/jp/.local/share/palace-daemon/venv)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST_HOST="familiar"
DEST_ROOT="/mnt/raid/projects/palace-daemon"
VENV_PATH="/home/jp/.local/share/palace-daemon/venv"
DO_RESTART=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) DEST_HOST="$2"; shift 2 ;;
    --root) DEST_ROOT="$2"; shift 2 ;;
    --venv-path) VENV_PATH="$2"; shift 2 ;;
    --no-restart) DO_RESTART=0; shift ;;
    -h|--help)
      sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//; /^set -euo/d'
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

echo ">>> Target: ${DEST_HOST}:${DEST_ROOT}"
echo ">>> Venv:   ${VENV_PATH}"

# ── Stage to /var/tmp (disk-backed, not tmpfs) — same lesson learned ──
# from familiar-api deploy (2026-05-14): /tmp on the target may be a
# 512MB tmpfs that fills up mid-rsync, leaving /srv/familiar partially
# wiped from --delete. /var/tmp is always disk-backed.
STAGE="/var/tmp/palace-daemon-src"

echo ">>> rsync source → ${DEST_HOST}:${STAGE}"
rsync -az --delete \
  --exclude .git --exclude .worktrees --exclude __pycache__ \
  --exclude '*.pyc' --exclude '.pytest_cache' --exclude 'venv' \
  --exclude '.env' --exclude 'scratch' \
  -e ssh \
  "${REPO_ROOT}/" \
  "${DEST_HOST}:${STAGE}/"

echo ">>> sudo rsync ${STAGE}/ → ${DEST_ROOT}/ (preserves .env)"
ssh "${DEST_HOST}" "sudo rsync -a --delete --exclude .env ${STAGE}/ ${DEST_ROOT}/"

if [[ -d "${REPO_ROOT}/requirements.txt" || -f "${REPO_ROOT}/requirements.txt" ]]; then
  echo ">>> pip install -r requirements.txt"
  ssh "${DEST_HOST}" "sudo -u jp ${VENV_PATH}/bin/pip install -r ${DEST_ROOT}/requirements.txt"
fi

if [[ "${DO_RESTART}" -eq 1 ]]; then
  echo ">>> systemctl restart palace-daemon"
  ssh "${DEST_HOST}" "sudo systemctl restart palace-daemon"
  sleep 4
fi

echo ">>> /health smoke test"
HEALTH_HOST="${DEST_HOST}"
# If DEST_HOST is short-name (no dots), assume the .jphe.in TLD for
# external probes that work over the homelab's split-horizon DNS.
if [[ "${DEST_HOST}" != *.* ]]; then
  HEALTH_HOST="${DEST_HOST}.jphe.in"
fi
if curl -sS --max-time 5 "http://${HEALTH_HOST}:8085/health" | grep -q '"status":"ok"'; then
  echo "    ✓ daemon healthy"
else
  echo "    ✘ /health did NOT return status=ok — investigate:" >&2
  ssh "${DEST_HOST}" "sudo journalctl -u palace-daemon -n 20"
  exit 1
fi

echo ">>> Deploy done."
