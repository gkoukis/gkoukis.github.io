#!/usr/bin/env python3
"""Merge Google Scholar citation pages saved manually in a browser.

Google Scholar may allow a person to navigate its "Cited by" pages while
blocking scripted requests.  Save those pages in a directory using this name:

    cites-<cluster-id>-start-<offset>.html

For example, the first two result pages for cluster 5987383161469789974 are:

    cites-5987383161469789974-start-0.html
    cites-5987383161469789974-start-10.html

The importer reads the saved HTML, validates that each paper starts at offset
zero, has no gaps, and ends on a page without a Next link.  Only then does it
replace that paper's citing list in scholar_citations.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from fetch_scholar_citations import OUTPUT_PATH, has_next_page, parse_results


NAME = re.compile(r"^cites-(?P<cluster>\d+)-start-(?P<start>\d+)\.html?$", re.I)


def saved_pages(directory: Path) -> dict[str, list[tuple[int, Path]]]:
    grouped: dict[str, list[tuple[int, Path]]] = defaultdict(list)
    for path in directory.iterdir():
        if not path.is_file():
            continue
        match = NAME.match(path.name)
        if match:
            grouped[match["cluster"]].append((int(match["start"]), path))
    return {cluster: sorted(pages) for cluster, pages in grouped.items()}


def complete_citations(pages: list[tuple[int, Path]]) -> tuple[list[dict], str | None]:
    """Return one complete result sequence, or explain why it is incomplete."""
    expected_start = 0
    citations: list[dict] = []
    seen: set[str] = set()

    for index, (start, path) in enumerate(pages):
        if start != expected_start:
            return [], f"expected start-{expected_start}, found {path.name}"

        html = path.read_text(encoding="utf-8", errors="replace")
        batch = parse_results(html)
        if not batch:
            return [], f"no Scholar results found in {path.name}"

        for entry in batch:
            if entry["id"] not in seen:
                seen.add(entry["id"])
                citations.append(entry)

        more = has_next_page(html)
        if not more:
            if index != len(pages) - 1:
                return [], f"{path.name} is a final page, but later files were also saved"
            return citations, None

        expected_start += len(batch)

    return [], f"{pages[-1][1].name} still has a Next link; save the next page too"


def load_snapshot() -> dict:
    try:
        return json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"error: cannot read {OUTPUT_PATH.name}: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory",
        type=Path,
        help="directory containing cites-<cluster>-start-<offset>.html files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate captures and print what would change without writing JSON",
    )
    args = parser.parse_args()

    if not args.directory.is_dir():
        print(f"error: {args.directory} is not a directory", file=sys.stderr)
        return 1

    grouped = saved_pages(args.directory)
    if not grouped:
        print(
            "error: no files matched cites-<cluster>-start-<offset>.html",
            file=sys.stderr,
        )
        return 1

    snapshot = load_snapshot()
    papers = {paper.get("cluster"): paper for paper in snapshot.get("papers") or []}
    updated = 0

    for cluster, pages in grouped.items():
        paper = papers.get(cluster)
        if not paper:
            print(f"warning: {cluster} is not in scholar_citations.json; skipped", file=sys.stderr)
            continue

        citations, problem = complete_citations(pages)
        if problem:
            print(f"warning: {cluster}: {problem}; kept existing records", file=sys.stderr)
            continue

        old_count = len(paper.get("citations") or [])
        paper["citations"] = citations
        paper["cited_by"] = len(citations)
        updated += 1
        print(
            f"{cluster}: {paper.get('title', 'Untitled')} - "
            f"{old_count} -> {len(citations)} captured citing records"
        )

    if not updated:
        print("No complete Scholar citation sequences were imported.")
        return 1

    if args.dry_run:
        print(f"Dry run complete: {updated} paper(s) would be updated.")
        return 0

    snapshot["fetched_at"] = datetime.now(timezone.utc).isoformat()
    OUTPUT_PATH.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = sum(len(paper.get("citations") or []) for paper in snapshot["papers"])
    reported = sum(paper.get("cited_by") or 0 for paper in snapshot["papers"])
    print(
        f"Wrote {OUTPUT_PATH.name}: {total} captured citing records "
        f"across {reported} imported Scholar reports."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
