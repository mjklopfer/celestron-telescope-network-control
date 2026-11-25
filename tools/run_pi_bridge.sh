#!/bin/sh
set -eu
PROJECT_DIR="${CELESTRON_PROJECT_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
TELESCOPE_HOST="${CELESTRON_TELESCOPE_HOST:-192.168.1.231}"
STABLE_SAMPLES="${CELESTRON_ROUTE_STABLE_SAMPLES:-5}"
STABLE_INTERVAL="${CELESTRON_ROUTE_STABLE_INTERVAL:-2}"
BIND_IP=""
LAST_IP=""
STABLE_COUNT=0
ATTEMPTS=0
while [ "$ATTEMPTS" -lt 30 ]; do
    CURRENT_IP=$(/usr/bin/ip -4 route get "$TELESCOPE_HOST" 2>/dev/null | /usr/bin/awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
    if [ -n "$CURRENT_IP" ] && /usr/bin/ip -4 addr show | /usr/bin/grep -q "inet $CURRENT_IP/"; then
        if [ "$CURRENT_IP" = "$LAST_IP" ]; then
            STABLE_COUNT=$((STABLE_COUNT + 1))
        else
            LAST_IP="$CURRENT_IP"
            STABLE_COUNT=1
        fi
        if [ "$STABLE_COUNT" -ge "$STABLE_SAMPLES" ]; then
            BIND_IP="$CURRENT_IP"
            break
        fi
    else
        LAST_IP=""
        STABLE_COUNT=0
    fi
    ATTEMPTS=$((ATTEMPTS + 1))
    /usr/bin/sleep "$STABLE_INTERVAL"
done
if [ -z "$BIND_IP" ]; then
    echo "No stable routed IPv4 source address for $TELESCOPE_HOST" >&2
    exit 1
fi
echo "Using stable Bridge LAN address $BIND_IP for telescope $TELESCOPE_HOST" >&2
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python" -m bridge_mode \
    --host "$TELESCOPE_HOST" --port 2000 --bind "$BIND_IP" \
    --heartbeat --heartbeat-interval 5
