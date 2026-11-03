#!/bin/bash
# Makes "Copilot Admin.app" in /Applications on this Mac: opens the admin panel (license-server/admin_panel.py)
# in the browser, with no Terminal window. Only for the Mac that holds ~/.copilot-license/secrets.env.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="/Applications/Copilot Admin.app"
LOG="$HOME/Library/Logs/Copilot Admin.log"
SCRIPT="do shell script \"cd \" & quoted form of \"$ROOT\" & \" && nohup .venv/bin/python license-server/admin_panel.py >> \" & quoted form of \"$LOG\" & \" 2>&1 &\""
rm -rf "${APP:?}"
osacompile -o "$APP" -e "$SCRIPT"
cp "$ROOT/desktop/assets/copilot.icns" "$APP/Contents/Resources/applet.icns"
touch "$APP"
echo "Made $APP"
