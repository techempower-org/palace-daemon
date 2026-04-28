#!/usr/bin/env bash
# reset-test-palace.sh — wipe the test palace and restart the test container
#
# Usage:
#   bash scripts/reset-test-palace.sh              # wipe only
#   bash scripts/reset-test-palace.sh --start      # wipe + start
#   bash scripts/reset-test-palace.sh --start --seed

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_DIR="$(dirname "$SCRIPT_DIR")"
TEST_PALACE="${TEST_PALACE_PATH:-$HOME/.mempalace/test_palace}"
COMPOSE_FILE="$DAEMON_DIR/docker-compose.test.yml"

START=0; SEED=0
for arg in "$@"; do
    case "$arg" in --start) START=1;; --seed) SEED=1;; esac
done

echo "Test palace : $TEST_PALACE"

# 1. Stop test container
if docker ps --format '{{.Names}}' | grep -q '^palace-test$'; then
    echo "Stopping palace-test..."
    docker compose -f "$COMPOSE_FILE" down
else
    echo "palace-test not running — skipping stop"
fi

# 2. Safety: never wipe the real palace
REAL="$(realpath "$HOME/.mempalace/palace" 2>/dev/null || true)"
THIS="$(realpath "$TEST_PALACE" 2>/dev/null || echo "$TEST_PALACE")"
if [[ -n "$REAL" && "$THIS" == "$REAL" ]]; then
    echo "ERROR: TEST_PALACE_PATH is the production palace. Aborting." >&2
    exit 1
fi

# 3. Wipe ChromaDB data using Python (reliable UUID matching across platforms)
echo "Wiping ChromaDB data..."
mkdir -p "$TEST_PALACE"
python3 - "$TEST_PALACE" << 'PYEOF'
import sys, os, re, shutil
palace = sys.argv[1]
uuid_re = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
wipe = ['chroma.sqlite3', 'chroma.sqlite3-wal', 'chroma.sqlite3-shm',
        'chroma.sqlite3.pre-recovery', 'header.bin.bak',
        'daemon-pending.jsonl', 'corrupt_ids.txt']
removed = []
for name in os.listdir(palace):
    path = os.path.join(palace, name)
    if uuid_re.match(name):
        shutil.rmtree(path, ignore_errors=True); removed.append(name)
    elif name in wipe and os.path.isfile(path):
        os.remove(path); removed.append(name)
print(f"Removed {len(removed)} items.")
remaining = os.listdir(palace)
print("Remaining:", remaining if remaining else "(empty)")
PYEOF

# 4. Optionally start
if [[ $START -eq 1 ]]; then
    echo ""
    echo "Building and starting palace-test on port 8086..."
    docker compose -f "$COMPOSE_FILE" up -d --build

    echo -n "Waiting for health"
    for i in $(seq 1 40); do
        sleep 2
        status="$(curl -sf http://localhost:8086/health 2>/dev/null \
            | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","?"))' 2>/dev/null \
            || echo '?')"
        if [[ "$status" == "ok" ]]; then
            echo "  ok"
            echo "palace-test is healthy on port 8086"
            break
        fi
        echo -n "."
        if [[ $i -eq 40 ]]; then
            echo ""
            echo "WARNING: not healthy after 80s — check: docker logs palace-test"
        fi
    done
fi

# 5. Optionally seed fixtures
if [[ $SEED -eq 1 && $START -eq 1 ]]; then
    echo ""
    echo "Seeding test fixtures..."
    BASE="http://localhost:8086"
    _store() {
        curl -sf -X POST "$BASE/memory" -H "Content-Type: application/json" \
            -d "{\"content\":\"$1\",\"wing\":\"$2\",\"room\":\"$3\"}" > /dev/null \
            && echo "  stored: $2/$3"
    }
    _store "TEST: Project Alpha — status: in_progress, owner: Radu" "lab_projects" "projects"
    _store "TEST: mempalace-chat Android app — Kotlin + LiteRT + PalaceToolSet" "lab_mempalace_chat" "notes"
    _store "TEST: palace-daemon v1.5.1 — FastAPI + ChromaDB + MCP proxy" "lab_projects" "notes"
    echo "Seeded 3 drawers."
    echo ""
    echo "Verify:"
    curl -sf "$BASE/search?q=test&limit=3" \
        | python3 -c 'import sys,json; [print("  -", r["text"][:80]) for r in json.load(sys.stdin).get("results",[])]'
fi

echo ""
echo "Monitor: python3 $DAEMON_DIR/monitor.py --url http://localhost:8086 --interval 3"
