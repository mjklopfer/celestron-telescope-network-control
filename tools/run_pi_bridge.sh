#!/bin/sh
set -eu
PROJECT_DIR="${CELESTRON_PROJECT_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
TELESCOPE_HOST="${CELESTRON_TELESCOPE_HOST:-192.168.1.231}"
BIND_IP=$(/usr/bin/ip -4 route get "$TELESCOPE_HOST" | /usr/bin/awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
if [ -z "$BIND_IP" ]; then
    echo "No routed IPv4 source address for $TELESCOPE_HOST" >&2
    exit 1
fi
cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/python" -m bridge_mode \
    --host "$TELESCOPE_HOST" --port 2000 --bind "$BIND_IP"
