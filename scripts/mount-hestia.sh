#!/bin/bash
# Mount Hestia NFS share, auto-detecting local LAN vs VPN.
# Usage: sudo mount-hestia.sh
#
# Install: sudo cp scripts/mount-hestia.sh /usr/local/bin/mount-hestia
#          sudo chmod +x /usr/local/bin/mount-hestia

set -euo pipefail

MOUNT_POINT="/mnt/hestia_ai"
EXPORT="/mnt/ai_storage"
LOCAL_IP="192.168.0.2"
VPN_IP="10.8.0.6"
MOUNT_OPTS="ro,soft,timeo=50,retrans=2"

if mountpoint -q "$MOUNT_POINT"; then
    echo "Already mounted at $MOUNT_POINT."
    exit 0
fi

if ping -c 1 -W 1 "$LOCAL_IP" &>/dev/null; then
    HOST="$LOCAL_IP"
    echo "Hestia reachable on local LAN ($LOCAL_IP)."
else
    HOST="$VPN_IP"
    echo "Hestia not on local LAN, trying VPN ($VPN_IP)."
fi

mount -t nfs -o "$MOUNT_OPTS" "$HOST:$EXPORT" "$MOUNT_POINT"
echo "Mounted $HOST:$EXPORT at $MOUNT_POINT."
