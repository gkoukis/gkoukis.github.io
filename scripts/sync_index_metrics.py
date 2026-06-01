#!/usr/bin/env python3
"""Write the metrics from scholar_metrics.json into the hard-coded numbers in index.html.

index.html shows the Scholar metrics at runtime via JavaScript (fetching
scholar_metrics.json), but it also carries hard-coded fallback numbers that are
visible before the script runs or if the fetch fails. This keeps those fallbacks
in sync with the latest snapshot so the static page is always current.

Run scripts/update_scholar_metrics.py first to refresh the JSON, then this.
Exits non-zero (without writing) if anything looks wrong, so a bad fetch can
never corrupt index.html.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = REPO_ROOT / "scholar_metrics.json"
HTML_PATH = REPO_ROOT / "index.html"

# Each metric -> the id of the <div class="metric-value"> that displays it.
FIELD_TO_ELEMENT_ID = {
    "publications": "metric-publications",
    "citations": "metric-citations",
    "h_index": "metric-h-index",
    "i10_index": "metric-i10-index",
}


def main() -> int:
    metrics = json.loads(JSON_PATH.read_text(encoding="utf-8"))

    # Read with newline="" so the file's existing CRLF/LF endings are preserved
    # exactly (otherwise the whole file shows up as changed).
    with open(HTML_PATH, "r", encoding="utf-8", newline="") as handle:
        html = handle.read()

    changes: list[str] = []
    for field, element_id in FIELD_TO_ELEMENT_ID.items():
        value = metrics.get(field)
        if not isinstance(value, int) or value < 0:
            print(f"Refusing to write invalid {field}={value!r}", file=sys.stderr)
            return 1

        # Match the digits inside e.g.  id="metric-citations">88</div>
        pattern = re.compile(
            r'(id="' + re.escape(element_id) + r'"[^>]*>)\s*(\d+)\s*(</div>)'
        )
        matches = pattern.findall(html)
        if len(matches) != 1:
            print(
                f"Expected exactly one element id={element_id!r} in index.html, "
                f"found {len(matches)}.",
                file=sys.stderr,
            )
            return 1

        old_value = matches[0][1]
        if old_value != str(value):
            changes.append(f"{field}: {old_value} -> {value}")
        html = pattern.sub(rf"\g<1>{value}\g<3>", html)

    if not changes:
        print("index.html already matches scholar_metrics.json - nothing to change.")
        return 0

    with open(HTML_PATH, "w", encoding="utf-8", newline="") as handle:
        handle.write(html)

    print("Updated index.html:")
    for change in changes:
        print(f"  {change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
