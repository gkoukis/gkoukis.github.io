#!/usr/bin/env python3
"""Refresh the public Google Scholar metrics snapshot used by index_test.html."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SCHOLAR_ID = "E2bGWsUAAAAJ"
PAGE_SIZE = 100
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "scholar_metrics.json"


class ScholarProfileParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.article_count = 0
        self.metric_values: list[int] = []
        self._in_metrics_table = False
        self._capture_metric = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())

        if tag == "table" and attributes.get("id") == "gsc_rsb_st":
            self._in_metrics_table = True
        elif tag == "tr" and "gsc_a_tr" in classes:
            self.article_count += 1
        elif tag == "td" and self._in_metrics_table and "gsc_rsb_std" in classes:
            self._capture_metric = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._in_metrics_table:
            self._in_metrics_table = False
        elif tag == "td":
            self._capture_metric = False

    def handle_data(self, data: str) -> None:
        if self._capture_metric:
            value = data.strip().replace(",", "")
            if value.isdigit():
                self.metric_values.append(int(value))


def fetch_profile_page(start: int) -> ScholarProfileParser:
    query = urlencode(
        {
            "user": SCHOLAR_ID,
            "hl": "en",
            "cstart": start,
            "pagesize": PAGE_SIZE,
        }
    )
    request = Request(
        f"https://scholar.google.com/citations?{query}",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            )
        },
    )

    with urlopen(request, timeout=30) as response:
        html = response.read().decode("utf-8", errors="replace")

    parser = ScholarProfileParser()
    parser.feed(html)
    return parser


def get_publication_count(first_page: ScholarProfileParser) -> int:
    publication_count = first_page.article_count
    start = PAGE_SIZE

    while publication_count >= start:
        page = fetch_profile_page(start)
        publication_count += page.article_count
        if page.article_count < PAGE_SIZE:
            break
        start += PAGE_SIZE

    return publication_count


def main() -> int:
    first_page = fetch_profile_page(0)

    if len(first_page.metric_values) < 6:
        print("Could not find Google Scholar profile metrics. Scholar may be rate-limiting requests.", file=sys.stderr)
        return 1

    metrics = {
        "scholar_id": SCHOLAR_ID,
        "publications": get_publication_count(first_page),
        "citations": first_page.metric_values[0],
        "h_index": first_page.metric_values[2],
        "i10_index": first_page.metric_values[4],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": f"https://scholar.google.com/citations?user={SCHOLAR_ID}&hl=en",
    }
    OUTPUT_PATH.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
