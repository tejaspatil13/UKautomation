# Run Copilot from source on the Windows test PC: the real Windows backend, data folder and window,
# with your latest pushed code. Re-run after each `git push` from the Mac to test backend changes
# natively on Windows without building an installer. Needs Git, Python 3.12 and Node 22 (test PC only;
# the client never needs them).
#   powershell -ExecutionPolicy Bypass -File scripts\windows-run-from-source.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
git pull --ff-only
if (-not (Test-Path .venv)) { py -3.12 -m venv .venv }
.\.venv\Scripts\python -m pip install -q -r desktop\requirements.txt
Push-Location frontend; npm ci --no-audit --no-fund; npm run build; Pop-Location
# Shares %LOCALAPPDATA%\Copilot\data with the installed builds, so the datasets aren't downloaded twice.
# Licensing is off in source runs (no build_config.json).
.\.venv\Scripts\python -m desktop --debug
