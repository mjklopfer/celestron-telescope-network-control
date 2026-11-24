#!/bin/sh
set -eu

PROJECT_DIR="${CELESTRON_PROJECT_DIR:-$HOME/celestron-telescope-network-control}"
UNIT_SOURCE="$PROJECT_DIR/packaging/systemd/user"
UNIT_TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$UNIT_TARGET"
cp "$UNIT_SOURCE/celestron-bridge.service" "$UNIT_TARGET/"
cp "$UNIT_SOURCE/telescope-vision-dashboard.service" "$UNIT_TARGET/"
systemctl --user daemon-reload
systemctl --user enable celestron-bridge.service telescope-vision-dashboard.service
systemctl --user restart celestron-bridge.service
systemctl --user restart telescope-vision-dashboard.service

printf '%s\n' 'Installed and started user services:'
systemctl --user --no-pager --full status \
  celestron-bridge.service telescope-vision-dashboard.service || true
printf '%s\n' 'These services start with this user session. On the configured Pi desktop, automatic login starts them after boot.'
