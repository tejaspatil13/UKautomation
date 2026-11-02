#!/usr/bin/env bash
# Live development, Mac side: backend with auto-restart + dashboard with hot reload, shown on your
# Windows test PC inside the real Copilot window (see scripts/windows-dev.cmd).
#
#   scripts/dev-mac.sh            # uses the datasets in ./data
#
# The backend listens on 127.0.0.1 only; the dashboard dev server listens on this Mac's local-network
# address only, so your Windows PC on the same Wi-Fi/LAN can reach it. Use it on your own network only
# (not public Wi-Fi). The client's app can never connect to it: client builds refuse --dev-url.
set -euo pipefail
cd "$(dirname "$0")/.."
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
if [ -z "$IP" ]; then echo "Couldn't find this Mac's local network address (are you on Wi-Fi/LAN?)."; exit 1; fi

export COPILOT_API_PORT="${COPILOT_API_PORT:-8010}"  # the dashboard dev server forwards /api here
if lsof -nP -iTCP:"$COPILOT_API_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $COPILOT_API_PORT is already used by another program. Run with COPILOT_API_PORT=<free port>."; exit 1
fi

trap 'kill 0' EXIT
(cd backend && UKA_AUTO_REFRESH=false ../.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 \
  --port "$COPILOT_API_PORT" --reload) &
(cd frontend && npx vite --host "$IP" --port 5173 --strictPort) &
sleep 3
echo
echo "  On the Windows test PC run:   scripts\\windows-dev.cmd $IP"
echo "  (or: Copilot.exe --dev-url http://$IP:5173 )"
echo "  Ctrl+C here stops both servers."
echo
wait
