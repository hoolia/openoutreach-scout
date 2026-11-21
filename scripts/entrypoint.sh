#!/bin/sh
# Start a virtual display for the headed browser, and a VNC server on localhost only
# for the one-time human login (reached with `oc port-forward`), then run oo-scout.
set -eu
export DISPLAY=:99
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp >/dev/null 2>&1 &
sleep 1
if [ "${1:-run}" = "bootstrap" ] || [ "${VNC_ENABLED:-false}" = "true" ]; then
  # localhost only: nothing outside the pod network namespace can connect
  x11vnc -display :99 -localhost -rfbport 5900 -forever -shared -nopw -quiet >/dev/null 2>&1 &
fi
exec oo-scout "$@"
