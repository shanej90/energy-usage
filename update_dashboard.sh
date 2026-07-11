#!/usr/bin/env bash
#
# Rebuild the dashboard locally and serve it for preview.
#
# Runs dashboard/build_dashboard.py, which pulls the latest Octopus
# consumption/tariff and weather data itself (via the Parquet cache in
# data/cache/ — only new records are fetched from the API) before
# regenerating outputs/dashboard.html. No commit/push happens here;
# commit outputs/dashboard.html yourself once you're happy with it.
#
# Usage:
#   ./update_dashboard.sh          # build, then serve at http://localhost:8000
#   PORT=8080 ./update_dashboard.sh  # serve on a different port

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${PORT:-8000}"

# Activate the local virtualenv if present.
if [[ -f ".venv/Scripts/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/Scripts/activate"
elif [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

echo "==> Building dashboard (pulls latest data via the local cache)..."
python dashboard/build_dashboard.py

URL="http://localhost:$PORT/dashboard.html"
echo "==> Serving outputs/ at $URL (Ctrl+C to stop)"

# Best-effort: open the dashboard in the default browser.
if command -v explorer.exe >/dev/null 2>&1; then
  explorer.exe "$URL" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then
  open "$URL" >/dev/null 2>&1 || true
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$URL" >/dev/null 2>&1 || true
fi

cd outputs
python -m http.server "$PORT"
