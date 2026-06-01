#!/usr/bin/env python3
"""Refresh the public Google Scholar metrics snapshot used by index_test.html.

Google Scholar has no official API and actively blocks datacenter IP ranges
(including GitHub Actions runners) by serving a CAPTCHA / "unusual traffic" page
or HTTP 429 instead of the profile.  This script therefore treats an unreachable
or rate-limited Scholar as a *soft*, non-fatal condition: it logs a warning,
leaves the existing snapshot untouched, and exits 0 so the scheduled workflow
stays green.  Only a genuinely unexpected error (a real bug) surfaces as a
failure -- it propagates as an uncaught traceback and a non-zero exit.

Set the SCHOLAR_ID environment variable to override the profile that is fetched
(handy for testing the soft-skip path with an invalid id).
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SCHOLAR_ID = os.environ.get("SCHOLAR_ID", "E2bGWsUAAAAJ")
PAGE_SIZE = 100
MAX_RETRIES = 4
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "scholar_metrics.json"

# Rotate a few realistic browser User-Agents across retries.
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)

# Markers Scholar embeds in its CAPTCHA / robot-check responses.
BLOCK_MARKERS = ("gs_captcha", "/sorry/", "unusual traffic", "not a robot", 'id="captcha"')


class ScholarUnavailable(Exception):
    """Scholar could not be reached, or is rate-limiting / blocking this IP."""


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


def fetch_profile_html(start: int) -> str:
    """Download one profile page, retrying with backoff when Scholar blocks us.

    Raises ScholarUnavailable if Scholar is unreachable, rate-limited, or serves
    a CAPTCHA page on every attempt.
    """
    query = urlencode(
        {"user": SCHOLAR_ID, "hl": "en", "cstart": start, "pagesize": PAGE_SIZE}
    )
    url = f"https://scholar.google.com/citations?{query}"

    last_error: object = None
    for attempt in range(MAX_RETRIES):
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENTS[attempt % len(USER_AGENTS)],
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                html = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            # 4xx other than rate-limiting won't be fixed by retrying.
            if exc.code not in (429, 403, 503):
                raise ScholarUnavailable(f"Scholar returned HTTP {exc.code}") from exc
        except URLError as exc:
            last_error = f"network error: {exc.reason}"
        else:
            if any(marker in html for marker in BLOCK_MARKERS):
                last_error = "CAPTCHA / robot-check page"
            else:
                return html

        if attempt < MAX_RETRIES - 1:
            # Exponential backoff with jitter: 1-2s, 2-3s, 4-5s ...
            time.sleep(2**attempt + random.uniform(0, 1))

    raise ScholarUnavailable(
        f"Scholar unavailable after {MAX_RETRIES} attempts ({last_error})"
    )


def fetch_profile_page(start: int) -> ScholarProfileParser:
    parser = ScholarProfileParser()
    parser.feed(fetch_profile_html(start))
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
    try:
        first_page = fetch_profile_page(0)

        if len(first_page.metric_values) < 6:
            raise ScholarUnavailable(
                "profile metrics table not found (Scholar likely served a block page)"
            )

        metrics = {
            "scholar_id": SCHOLAR_ID,
            "publications": get_publication_count(first_page),
            "citations": first_page.metric_values[0],
            "h_index": first_page.metric_values[2],
            "i10_index": first_page.metric_values[4],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": f"https://scholar.google.com/citations?user={SCHOLAR_ID}&hl=en",
        }
    except ScholarUnavailable as exc:
        # Soft failure: Scholar is blocking this IP (common on CI runners).
        # Keep the previous snapshot and exit 0 so the workflow stays green.
        # "::warning::" surfaces as a yellow annotation in the Actions UI.
        print(f"::warning::Skipping Scholar update - {exc}.", file=sys.stderr)
        return 0

    OUTPUT_PATH.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
