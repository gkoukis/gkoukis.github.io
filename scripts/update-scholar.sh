#!/usr/bin/env bash
#
# Manually refresh the Google Scholar metrics and write them into index.html.
#
# Use this when the weekly GitHub Actions job is blocked by Scholar (GitHub's
# datacenter IPs are usually blocked; your home / WSL connection is not). It:
#   1. fetches the latest public metrics from your Scholar profile,
#   2. writes them into scholar_metrics.json,
#   3. updates the hard-coded numbers in index.html,
#   4. shows you the diff.
#
# It does NOT commit or push -- review the diff and push yourself when happy.
#
# Run from anywhere:
#   ./scripts/update-scholar.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "error: '$PY' not found. Install Python 3 or set PYTHON=... " >&2
    exit 1
fi

echo "==> Fetching latest Google Scholar metrics..."
"$PY" "$SCRIPT_DIR/update_scholar_metrics.py"

echo
echo "==> Writing values into index.html..."
"$PY" "$SCRIPT_DIR/sync_index_metrics.py"

echo
echo "==> Done. Review the changes above, then commit and push yourself:"
echo "      git add scholar_metrics.json index.html"
echo "      git commit -m 'Update Google Scholar metrics'"
echo "      git push"
