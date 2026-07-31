#!/usr/bin/env python3
"""Snapshot the papers Google Scholar says cite Georgios Koukis' work.

Why this exists: OpenAlex and Semantic Scholar together find roughly two thirds
of the citations Scholar reports (70-80 against 105 in July 2026).  The rest come
from documents only Scholar indexes -- MSc/PhD theses, workshop proceedings
without DOIs, self-deposited ResearchGate copies, books.  ``build_literature_map``
therefore treats the snapshot written here as a third source, so the "cite my
work" layer matches the number on the public profile.

Scholar has no API and blocks datacenter IPs (GitHub Actions runners included)
with a CAPTCHA or HTTP 429, so:

  * the citing lists are cached in ``scholar_citations.json`` and committed, and
    the map build only ever *reads* that file -- it never depends on the network;
  * every failure here is soft: the previous snapshot is kept and the script
    exits 0, leaving the scheduled workflow green.  Being cut off partway keeps
    what was read and carries the rest over, so runs accumulate;
  * requests go through one cookie session (Scholar rejects the search endpoint
    without the cookies its profile pages set) and are spaced several seconds
    apart, which keeps the whole run to well under a hundred requests a year.

When this IP is already blocked, borrow a browser that is not: a CAPTCHA solved
by hand leaves an exemption cookie, so sending that browser's cookies lets the
crawl through on the same terms a person gets.  Borrowed cookies are worth a try
(--cookie-prompt, --cookie-from-clipboard, or SCHOLAR_COOKIE), but Google reads
bot-ness off the TLS handshake and header order too, so they often are not
enough.  What always works is letting the browser do the fetching: run
scripts/collect_scholar_pages.js in its console and replay the capture here with
--from-pages.  The parsers and the crawl are the same either way.

Usage:
  python scripts/fetch_scholar_citations.py
  python scripts/fetch_scholar_citations.py --from-pages ~/Downloads/scholar_pages.json
  python scripts/fetch_scholar_citations.py [--cookie-prompt|--cookie-from-clipboard]

Environment overrides (all optional):
  SCHOLAR_ID          profile to snapshot, default E2bGWsUAAAAJ
  SCHOLAR_MAX_PAGES   per-paper page cap, default 6 (20 results a page)
  SCHOLAR_COOKIE      Cookie header copied from a browser that is not blocked
"""

from __future__ import annotations

import getpass
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from html import unescape
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


SCHOLAR_ID = os.environ.get("SCHOLAR_ID", "E2bGWsUAAAAJ")
COOKIE = os.environ.get("SCHOLAR_COOKIE", "").strip()
MAX_PAGES = int(os.environ.get("SCHOLAR_MAX_PAGES", "6"))
PAGE_SIZE = 20  # Scholar caps anonymous result pages at 20.
MAX_RETRIES = 3
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "scholar_citations.json"

PROFILE_URL = "https://scholar.google.com/citations"
SEARCH_URL = "https://scholar.google.com/scholar"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

BLOCK_MARKERS = ("gs_captcha", "/sorry/", "unusual traffic", "not a robot", 'id="captcha"')

# Seconds between requests. Scholar tolerates a slow reader and blocks a fast one.
PAUSE = (3.0, 7.0)


class ScholarUnavailable(Exception):
    """Scholar could not be reached, or is rate-limiting / blocking this IP."""


class ClipboardError(Exception):
    """The clipboard held nothing that looks like a Cookie header."""


# --------------------------------------------------------------------------
# Borrowing a browser's cookies
# --------------------------------------------------------------------------

# Ways to read the clipboard, tried in order. powershell.exe comes first because
# it is what reaches the *Windows* clipboard from both Windows and WSL, where the
# browser doing the copying lives; "powershell" alone is not on WSL's PATH.
PS_READ = ("-NoProfile", "-Command", "Get-Clipboard -Raw")
CLIPBOARD_READERS: tuple[tuple[str, ...], ...] = (
    ("powershell.exe", *PS_READ),
    ("powershell", *PS_READ),
    ("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", *PS_READ),
    ("pbpaste",),
    ("wl-paste", "--no-newline"),
    ("xclip", "-selection", "clipboard", "-o"),
    ("xsel", "--clipboard", "--output"),
)


def read_clipboard() -> str:
    """Return the clipboard's text, whatever this platform reads it with."""
    failures: list[str] = []
    for command in CLIPBOARD_READERS:
        try:
            done = subprocess.run(
                list(command), capture_output=True, text=True, timeout=30, check=True
            )
        except FileNotFoundError:
            continue  # Not this platform's tool; try the next.
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{command[0]}: {exc}")
            continue
        if done.stdout.strip():
            return done.stdout

    detail = "; ".join(failures) if failures else "no clipboard tool found"
    raise ClipboardError(
        f"could not read the clipboard ({detail}) - pass the cookies as "
        "SCHOLAR_COOKIE='name=value; ...' instead"
    )


def cookie_from_clipboard() -> str:
    """Extract a Cookie header from whatever DevTools left on the clipboard.

    "Copy as cURL" is the dependable way to get the header out of a Chromium
    browser -- the Headers pane hides the raw view for requests served from cache
    -- and it arrives as a long command with the cookies quoted inside, in either
    bash (-H 'cookie: ...') or cmd (-H ^"cookie: ...^") flavour.  A bare
    "name=value; name=value" string is accepted too, for anyone who copied just
    that.  Nothing is echoed: the value is a live session.
    """
    clip = read_clipboard()

    # Both cURL flavours keep each -H on its own line, so work line by line and
    # only undo cmd's quote escaping.
    lines = [line.strip() for line in clip.replace('^"', '"').splitlines()]
    for index, line in enumerate(lines):
        # Chromium's copied cURL sends the cookies as -b, not as a header, so
        # that flag is the case that actually fires for a DevTools copy.
        found = re.search(r"(?:^|\s)(?:-b|--cookie)\s+(.+)", line) or re.search(
            r"(?i)\bcookie\b\s*:\s*(.+)", line
        )
        if found:
            cookie = found.group(1)
            break
        # DevTools' parsed Headers pane lists the name and value on two rows.
        if re.fullmatch(r'(?i)"?cookie"?:?', line):
            cookie = next((rest for rest in lines[index + 1:] if rest), "")
            break
    else:
        cookie = clip.strip()  # someone copied just "name=value; name=value"

    # Cookie values never contain a quote, so the quotes around the copied value
    # delimit it: drop the opening one, then cut at the closing one.
    cookie = re.split(r"[\"']", cookie.strip().lstrip("\"'"))[0]
    cookie = cookie.strip().rstrip(";").strip()
    if "=" not in cookie or len(cookie) < 8:
        raise ClipboardError(
            f"the copied cURL command ({len(clip)} characters) carries no cookies "
            "- the request was probably one sent without them; copy a "
            "scholar.google.com page request instead, or use --cookie-prompt"
            if "curl" in clip[:200].lower()
            else f"the clipboard holds {len(clip)} characters with no cookies in "
            "them - anything copied after the request (a command, an error "
            "message) takes its place; --cookie-prompt avoids the race entirely"
        )
    return cookie


def cookie_from_prompt() -> str:
    """Ask for the Cookie header on the terminal, without echoing it.

    The clipboard route depends on what the browser chose to put in its copied
    cURL; this one depends on nothing.  In DevTools' Network tab the value sits
    under Request Headers next to ``cookie`` -- select it, copy, and paste here.
    """
    cookie = getpass.getpass("Paste the Cookie header (hidden), then Enter: ").strip()
    cookie = re.sub(r"(?i)^cookie\s*:\s*", "", cookie).strip().rstrip(";").strip()
    if "=" not in cookie or len(cookie) < 8:
        raise ClipboardError("that is not a Cookie header (expected 'name=value; ...')")
    return cookie


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

class Session:
    """One cookie jar reused for every request.

    The ``?cites=`` search endpoint answers 429 to a cookie-less client even on
    the first request, so the profile page is always fetched first: it sets the
    cookies (and supplies the Referer) that make the rest of the crawl look like
    a person reading the profile.
    """

    def __init__(self) -> None:
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.referer = PROFILE_URL

    def get(self, url: str) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.referer,
        }
        if COOKIE:
            # A hand-solved CAPTCHA leaves an exemption cookie in the browser;
            # sending it here is what lets a blocked IP finish the crawl.
            headers["Cookie"] = COOKIE
        last_error: object = None
        for attempt in range(MAX_RETRIES):
            try:
                with self.opener.open(Request(url, headers=headers), timeout=40) as response:
                    html = response.read().decode("utf-8", errors="replace")
            except HTTPError as exc:
                # 429/503 here is the same decision Scholar makes when it serves
                # a CAPTCHA, so it is equally pointless to retry through.
                raise ScholarUnavailable(f"Scholar returned HTTP {exc.code}") from exc
            except (URLError, TimeoutError) as exc:
                last_error = f"network error: {exc}"
            else:
                if any(marker in html for marker in BLOCK_MARKERS):
                    # Not worth retrying: once Scholar decides this IP is a bot
                    # it keeps deciding that, and every further request extends
                    # the block. Give up and leave the old snapshot in place.
                    raise ScholarUnavailable("CAPTCHA / robot-check page")
                self.referer = url
                return html

            if attempt < MAX_RETRIES - 1:
                time.sleep(4 * 2**attempt + random.uniform(0, 2))

        raise ScholarUnavailable(f"unavailable after {MAX_RETRIES} attempts ({last_error})")

    def pause(self) -> None:
        time.sleep(random.uniform(*PAUSE))


def canonical(url: str) -> str:
    """Same URL, same string: query parameters sorted, so key lookups match."""
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(sorted(parse_qsl(parts.query))), "")
    )


class SavedPages:
    """A capture from scripts/collect_scholar_pages.js, replayed offline.

    Scholar reads bot-ness off the TLS handshake and header order as much as off
    cookies, so these requests can be refused here while the identical ones
    succeed in a browser -- borrowed cookies do not change that.  When they are,
    the browser fetches the pages and this stands in for Session, leaving the
    crawl and the parsers exactly as they are for a live run.
    """

    def __init__(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScholarUnavailable(f"cannot read {path} ({exc})") from exc
        self.pages = {
            canonical(url): html for url, html in (data.get("pages") or {}).items()
        }
        if not self.pages:
            raise ScholarUnavailable(f"{path} holds no captured pages")

    def get(self, url: str) -> str:
        html = self.pages.get(canonical(url))
        if html is None:
            # The capture stopped here; the caller carries the rest over.
            raise ScholarUnavailable("page not in the capture")
        if any(marker in html for marker in BLOCK_MARKERS):
            raise ScholarUnavailable("the capture holds a CAPTCHA page")
        return html

    def pause(self) -> None:
        """Nothing to be polite to: the pages are already on disk."""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def strip_tags(html: str) -> str:
    """Tag soup -> plain text. Tags vanish rather than becoming spaces, because
    Scholar wraps each author name in its own <a>, and a space there would leave
    "A Author , B Author"."""
    return re.sub(r"[\s ]+", " ", unescape(re.sub(r"<[^>]+>", "", html))).strip()


def parse_profile(html: str) -> list[dict]:
    """Every profile row that has citations, with the id of its citing list."""
    papers: list[dict] = []
    for row in re.findall(r'<tr class="gsc_a_tr">(.*?)</tr>', html, re.S):
        title = re.search(r'class="gsc_a_at"[^>]*>(.*?)</a>', row, re.S)
        cites = re.search(r"cites=([\d,]+)", row)
        count = re.search(r'class="gsc_a_ac[^"]*"[^>]*>(.*?)</a>', row, re.S)
        year = re.search(r'class="gsc_a_h[^"]*"[^>]*>(.*?)</span>', row, re.S)
        if not title or not cites:
            continue  # An uncited paper has no citing list to crawl.
        cited_by = strip_tags(count.group(1)) if count else ""
        papers.append(
            {
                "title": strip_tags(title.group(1)),
                "year": int(strip_tags(year.group(1))) if year and strip_tags(year.group(1)).isdigit() else None,
                "cluster": cites.group(1),
                "cited_by": int(cited_by) if cited_by.isdigit() else 0,
            }
        )
    return papers


def parse_authors_line(line: str) -> tuple[str, str, int | None]:
    """"A Author, B Author - Venue, 2024 - publisher.com" -> parts.

    Scholar squeezes authors, venue, year and host into one line with an en dash
    or a hyphen as separator; the venue segment is missing on unpublished items.
    """
    segments = [s.strip() for s in re.split(r"\s+[-–]\s+", line) if s.strip()]
    authors = segments[0] if segments else ""
    middle = segments[1] if len(segments) > 2 else ""
    year_match = re.search(r"\b(19|20|21)\d{2}\b", middle or line)
    year = int(year_match.group(0)) if year_match else None
    venue = re.sub(r",?\s*\b(19|20|21)\d{2}\b\s*$", "", middle).strip(" ,")
    # Scholar elides long venue names with an ellipsis; a bare "…" is not a venue.
    venue = venue.replace("…", "").strip(" ,-")
    return authors, venue, year


def parse_results(html: str) -> list[dict]:
    """Pull one page of citing papers out of a Scholar results page."""
    blocks = re.split(r'(?=<div class="gs_r gs_or gs_scl")', html)
    papers: list[dict] = []
    for block in blocks[1:]:
        cid = re.search(r'data-cid="([^"]+)"', block)
        heading = re.search(r'<h3 class="gs_rt".*?</h3>', block, re.S)
        if not cid or not heading:
            continue
        link = re.search(r'<a[^>]+href="(http[^"]+)"[^>]*>(.*?)</a>', heading.group(0), re.S)
        # A "[CITATION]" result is a reference Scholar knows only from other
        # bibliographies: it still counts as a citation, it just has no page.
        title = strip_tags(link.group(2)) if link else re.sub(
            r"^(?:\s*\[[^\]]+\])+\s*", "", strip_tags(heading.group(0))
        )
        if not title:
            continue
        meta = re.search(r'<div class="gs_a">(.*?)</div>', block, re.S)
        authors, venue, year = parse_authors_line(strip_tags(meta.group(1)) if meta else "")
        # Read the count off the "cited by" link's own text rather than the
        # phrase around it: a borrowed cookie can carry a UI language pref, and
        # "Cited by 24" comes back as "Γίνεται αναφορά σε 24" when it does.
        cited = re.search(r'href="[^"]*[?&;]cites=[^"]*"[^>]*>[^<]*?(\d+)</a>', block)
        papers.append(
            {
                "id": cid.group(1),
                "title": title,
                "authors": authors,
                "venue": venue,
                "year": year,
                "citations": int(cited.group(1)) if cited else 0,
                "url": link.group(1) if link else "",
            }
        )
    return papers


def has_next_page(html: str) -> bool:
    return 'class="gs_ico gs_ico_nav_next"' in html


# --------------------------------------------------------------------------
# Crawl
# --------------------------------------------------------------------------

def fetch_citing(session: Session | SavedPages, paper: dict) -> list[dict]:
    """Page through one paper's citing list, newest Scholar page order kept."""
    found: list[dict] = []
    seen: set[str] = set()
    for page in range(MAX_PAGES):
        query = urlencode(
            {
                "hl": "en",
                "as_sdt": "2005",
                "sciodt": "0,5",
                "cites": paper["cluster"],
                "start": page * PAGE_SIZE,
                "num": PAGE_SIZE,
            }
        )
        html = session.get(f"{SEARCH_URL}?{query}")
        batch = parse_results(html)
        for entry in batch:
            if entry["id"] not in seen:
                seen.add(entry["id"])
                found.append(entry)
        if len(batch) < PAGE_SIZE or not has_next_page(html):
            break
        session.pause()
    return found


def previous_lists() -> dict[str, list[dict]]:
    """The citing list of each paper in the committed snapshot, by cluster id."""
    if not OUTPUT_PATH.exists():
        return {}
    try:
        old = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        paper["cluster"]: paper.get("citations") or []
        for paper in old.get("papers") or []
        if paper.get("cluster")
    }


def build(session: Session | SavedPages) -> tuple[dict, int]:
    """Crawl as much of the profile as Scholar allows this run.

    Being cut off partway is the normal case, not the exception, so a run keeps
    what it managed to read and carries the rest over from the committed
    snapshot. Successive runs therefore make progress instead of each needing to
    complete the whole crawl in one go. Returns the snapshot and how many papers
    were read fresh.
    """
    profile_query = urlencode({"user": SCHOLAR_ID, "hl": "en", "pagesize": "100"})
    profile = session.get(f"{PROFILE_URL}?{profile_query}")

    papers = parse_profile(profile)
    if not papers:
        raise ScholarUnavailable("no cited papers found on the profile")

    carried = previous_lists()
    snapshot: list[dict] = []
    fresh = 0
    blocked: object = None

    for paper in papers:
        citing: list[dict] | None = None
        if blocked is None:
            try:
                session.pause()
                citing = fetch_citing(session, paper)
            except ScholarUnavailable as exc:
                blocked = exc  # Stop asking; nothing after this would succeed.

        if citing is None:
            citing = carried.get(paper["cluster"], [])
        else:
            fresh += 1
        snapshot.append({**paper, "citations": citing})

    if blocked is not None:
        if not fresh:
            raise ScholarUnavailable(str(blocked))
        print(
            f"::warning::The crawl stopped after {fresh} of {len(papers)} papers "
            f"({blocked}); kept the previous lists for the rest.",
            file=sys.stderr,
        )

    return {
        "scholar_id": SCHOLAR_ID,
        "source": f"https://scholar.google.com/citations?user={SCHOLAR_ID}&hl=en",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "papers": snapshot,
    }, fresh


def unchanged(snapshot: dict) -> bool:
    """True if only the fetch timestamp differs from the committed snapshot."""
    if not OUTPUT_PATH.exists():
        return False
    try:
        previous = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return {k: v for k, v in previous.items() if k != "fetched_at"} == {
        k: v for k, v in snapshot.items() if k != "fetched_at"
    }


def main() -> int:
    global COOKIE

    args = sys.argv[1:]
    if {"-h", "--help"} & set(args):
        print(__doc__.strip())
        return 0

    capture = ""
    for index, arg in enumerate(list(args)):
        if arg.startswith("--from-pages="):
            capture = arg.split("=", 1)[1]
            args.remove(arg)
            break
        if arg == "--from-pages":
            if index + 1 >= len(args):
                print("error: --from-pages needs a file.", file=sys.stderr)
                return 1
            capture = args[index + 1]
            del args[index : index + 2]
            break

    for flag, borrow in (
        ("--cookie-from-clipboard", cookie_from_clipboard),
        ("--cookie-prompt", cookie_from_prompt),
    ):
        if flag not in args:
            continue
        args.remove(flag)
        try:
            COOKIE = borrow()
        except ClipboardError as exc:
            print(f"error: {exc}.", file=sys.stderr)
            return 1
        print(f"Borrowing {COOKIE.count('=')} cookies from the browser.")
        break
    if args:
        print(f"error: unexpected argument {args[0]!r}.", file=sys.stderr)
        print(__doc__.split("Usage:", 1)[1].split("Environment")[0].strip(), file=sys.stderr)
        return 1

    session: Session | SavedPages
    if capture:
        # A file the caller named by hand: a typo deserves an error, not the
        # silent "leave the snapshot alone" a blocked crawl gets.
        try:
            session = SavedPages(Path(capture).expanduser())
        except ScholarUnavailable as exc:
            print(f"error: {exc}.", file=sys.stderr)
            return 1
        print(f"Replaying {len(session.pages)} pages captured by the browser.")
    else:
        session = Session()

    try:
        snapshot, fresh = build(session)
    except ScholarUnavailable as exc:
        print(f"::warning::Skipping Scholar citation snapshot - {exc}.", file=sys.stderr)
        return 0

    total = sum(len(p["citations"]) for p in snapshot["papers"])
    claimed = sum(p["cited_by"] for p in snapshot["papers"])
    summary = (
        f"{len(snapshot['papers'])} cited papers ({fresh} read this run), "
        f"{total} citing records of {claimed} Scholar reports"
    )

    if unchanged(snapshot):
        print(f"Scholar citations are unchanged ({summary}) - leaving the snapshot alone.")
        return 0

    OUTPUT_PATH.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"Wrote {OUTPUT_PATH.name}: {summary}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
