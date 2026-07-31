#!/usr/bin/env bash
#
# Manually refresh the literature map, including the Google Scholar layer.
#
# Use this when the weekly GitHub Actions job cannot do it: Scholar blocks
# datacenter IPs, so the runner's Scholar step almost always soft-skips and the
# "cite my work" layer only grows when you run the crawl yourself. It:
#   1. finds the capture made by scripts/collect_scholar_pages.js (see below),
#   2. folds it into scholar_citations.json,
#   3. rebuilds literature_map.json from OpenAlex + Semantic Scholar + that
#      snapshot,
#   4. reports how the layer counts moved.
#
# To make a capture: open your Scholar profile in a browser, F12 -> Console,
# paste all of scripts/collect_scholar_pages.js, and let it download
# scholar_pages.json. Without one, this still refreshes OpenAlex and Semantic
# Scholar and tries Scholar directly, which usually gets blocked -- harmlessly.
#
# It does NOT commit or push -- review the diff and push yourself when happy.
#
# Run from anywhere:
#   ./scripts/update-literature-map.sh                      # auto-find a capture
#   ./scripts/update-literature-map.sh path/to/pages.json   # or name one
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
MAP="$REPO_ROOT/literature_map.json"

# Pick an interpreter that actually runs, not merely one that exists: on Windows
# a bare "python3" is often the Microsoft Store stub, which is on PATH and fails
# when invoked.
PY=""
for candidate in ${PYTHON:-} python3 python; do
    if [ -n "$candidate" ] && "$candidate" -c "import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "error: no working Python 3 found. Install it or set PYTHON=/path/to/python." >&2
    exit 1
fi

# Where a browser download lands, most specific first. The /mnt/c glob is how
# WSL sees the Windows Downloads folder, which is where Edge and Chrome put it.
find_capture() {
    local candidate
    for candidate in "$REPO_ROOT/scholar_pages.json" \
                     "$HOME/Downloads/scholar_pages.json" \
                     /mnt/c/Users/*/Downloads/scholar_pages.json; do
        [ -f "$candidate" ] && { echo "$candidate"; return; }
    done
}

CAPTURE="${1:-$(find_capture || true)}"

layer_counts() {
    [ -f "$MAP" ] || { echo "(no map yet)"; return; }
    "$PY" - "$MAP" <<'PY'
import json, sys
counts = json.load(open(sys.argv[1], encoding="utf-8"))["counts"]
print(", ".join(f"{key}: {counts[key]}" for key in ("core", "citing", "reference", "edges")))
PY
}

echo "==> Before: $(layer_counts)"
echo

if [ -n "$CAPTURE" ]; then
    echo "==> Folding in the browser capture: $CAPTURE"
    "$PY" "$SCRIPT_DIR/fetch_scholar_citations.py" --from-pages "$CAPTURE"
else
    echo "==> No scholar_pages.json found; trying Scholar directly (usually blocked)."
    echo "    Make a capture with scripts/collect_scholar_pages.js to fix that."
    "$PY" "$SCRIPT_DIR/fetch_scholar_citations.py"
fi

echo
echo "==> Rebuilding the citation network (a few minutes; OpenAlex and Semantic"
echo "    Scholar are both rate-limited)..."
"$PY" "$SCRIPT_DIR/build_literature_map.py"

echo
echo "==> After:  $(layer_counts)"
echo
# status, not diff --stat: these files may still be untracked, and diff says
# nothing at all about an untracked file.
git -C "$REPO_ROOT" status --short -- literature_map.json scholar_citations.json || true
echo
echo "==> Done. Review, then commit and push yourself:"
echo "      git add literature_map.json scholar_citations.json"
echo "      git commit -m 'Update literature map'"
echo "      git push"
