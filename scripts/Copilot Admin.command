#!/bin/bash
# Double-click to open the Copilot admin panel in your browser (this Mac only). Close this window to stop it.
cd "$(dirname "$0")/.." || exit 1
exec .venv/bin/python license-server/admin_panel.py
