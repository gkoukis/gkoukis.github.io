#!/usr/bin/env python3
"""Build the citation graph shown by the "Literature Map" section of index.html.

The graph has three layers of nodes:

  core      -- Georgios Koukis' own papers (deduplicated so a preprint and its
               published version collapse into one node)
  citing    -- papers that cite at least one core paper ("impact")
  reference -- papers cited by at least one core paper ("lineage")

and one kind of edge: ``source`` cites ``target``.  Nodes are additionally
assigned to a *cluster* (research topic) which the page draws as a labelled
region, so the map reads as thematic neighbourhoods rather than one hairball.

Three sources are merged, because no two of them match Google Scholar's numbers:

  OpenAlex          -- authoritative for *which papers are his* (it knows the
                       ORCID) and supplies topics, references and citations.
  Semantic Scholar  -- consistently finds citing papers OpenAlex misses
                       (workshops, arXiv, venues without clean DOIs).
  Google Scholar    -- read from the committed ``scholar_citations.json``
                       snapshot rather than the network, and the only source
                       that sees theses, books and DOI-less proceedings.  In
                       July 2026 the first two found 70 citing papers against
                       the 105 citations Scholar reports; this closes that gap.

Results are unioned on DOI, then on normalized title for the sources that have
no DOI to offer.  Both top-up sources are strictly additive: a missing or stale
snapshot, or an unreachable Semantic Scholar, still builds a map from OpenAlex
alone.  An unreachable OpenAlex is a *soft* failure -- the previous
literature_map.json is left untouched and the script exits 0 so the scheduled
workflow stays green.

Environment overrides (all optional):
  OPENALEX_AUTHOR_ID  OpenAlex author id, e.g. A5044837543
  OPENALEX_MAILTO     contact address for OpenAlex' polite pool
  S2_API_KEY          Semantic Scholar key (lifts the shared rate limit)
  SKIP_S2             set to 1 to build from OpenAlex only
  SKIP_SCHOLAR        set to 1 to ignore the Google Scholar snapshot
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


AUTHOR_ID = os.environ.get("OPENALEX_AUTHOR_ID", "A5044837543")
MAILTO = os.environ.get("OPENALEX_MAILTO", "george.koukis@athenarc.gr")
S2_API_KEY = os.environ.get("S2_API_KEY", "")
SKIP_S2 = os.environ.get("SKIP_S2") == "1"
SKIP_SCHOLAR = os.environ.get("SKIP_SCHOLAR") == "1"

# OpenAlex has merged this author with an unrelated, much older "G. Koukis"
# (an engineering geologist at the University of Patras publishing since the
# 1980s).  Everything from this date onwards is genuinely ours, so the cutoff
# doubles as author disambiguation.  Raise it only if the merge is ever fixed.
FROM_DATE = "2019-01-01"

# Works to force out of / into the core layer, by DOI (lowercase, no prefix).
EXCLUDE_DOIS: set[str] = set()
EXTRA_DOIS: set[str] = set()

# Records that are the same piece of work but whose titles differ too much for
# the automatic preprint/published merge to catch. Each group collapses into one
# node; the published version wins as the representative and the merged node
# keeps the union of the references and the highest citation count.
MERGE_GROUPS: tuple[frozenset[str], ...] = (
    # "Satellite assisted disrupted communications in OMNeT++" (arXiv, 2022)
    # became "Satellite-Assisted Disrupted Communications: IoT Case Study"
    # (Electronics, 2023).
    frozenset({"10.48550/arxiv.2207.01436", "10.3390/electronics13010027"}),
)

# Reference lists that no indexer has: self-deposited items (ResearchGate,
# Zenodo) are never parsed, so a paper published only that way shows up with no
# outgoing links at all. Map such a paper's DOI to the DOIs it cites and they
# get wired up like any other reference. Both sources are still tried first --
# this is only for what they genuinely do not hold.
MANUAL_REFERENCES: dict[str, tuple[str, ...]] = {
    # "10.13140/rg.2.2.10470.79684": ("10.1109/...", "10.3390/..."),
    # "10.5281/zenodo.20702588": ("10.1109/...",),
}

# How many named topic clusters to draw before folding the tail into "Other".
MAX_CLUSTERS = 7

OPENALEX = "https://api.openalex.org"
S2 = "https://api.semanticscholar.org/graph/v1"
PER_PAGE = 200
BATCH_SIZE = 50
MAX_RETRIES = 4
REQUEST_PAUSE = 0.2
# Semantic Scholar's unauthenticated pool is roughly one request per second.
S2_PAUSE = 0.4 if S2_API_KEY else 1.3

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "literature_map.json"
# Written by scripts/fetch_scholar_citations.py, committed, and only read here:
# Scholar blocks CI runners, so the build must never depend on reaching it.
SCHOLAR_PATH = Path(__file__).resolve().parents[1] / "scholar_citations.json"

WORK_FIELDS = ",".join(
    (
        "id",
        "doi",
        "title",
        "publication_year",
        "cited_by_count",
        "referenced_works",
        "primary_topic",
        "primary_location",
        "authorships",
        "type",
    )
)

S2_FIELDS = "title,year,venue,citationCount,externalIds,authors"

# DOI prefixes that mark a preprint / deposit rather than a version of record.
PREPRINT_PREFIXES = (
    "10.48550/",  # arXiv
    "10.20944/",  # Preprints.org
    "10.13140/",  # ResearchGate
    "10.36227/",  # TechRxiv
)


class OpenAlexUnavailable(Exception):
    """OpenAlex could not be reached, or is rate-limiting this IP."""


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def _get(url: str, headers: dict[str, str], retries: int) -> dict:
    last_error: object = None
    for attempt in range(retries):
        try:
            with urlopen(Request(url, headers=headers), timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (URLError, TimeoutError) as exc:
            last_error = f"network error: {exc}"
        except json.JSONDecodeError as exc:
            last_error = f"malformed JSON: {exc}"

        if attempt < retries - 1:
            time.sleep(2**attempt)

    raise OpenAlexUnavailable(f"unavailable after {retries} attempts ({last_error})")


def fetch(path: str, params: dict[str, str]) -> dict:
    """GET one OpenAlex page, retrying with backoff on transient failures."""
    url = f"{OPENALEX}/{path}?{urlencode({**params, 'mailto': MAILTO})}"
    try:
        return _get(url, {"User-Agent": f"gkoukis.github.io ({MAILTO})"}, MAX_RETRIES)
    except HTTPError as exc:
        raise OpenAlexUnavailable(f"OpenAlex returned HTTP {exc.code}") from exc


def fetch_all(path: str, params: dict[str, str]) -> list[dict]:
    """Follow OpenAlex cursor pagination and return every result."""
    results: list[dict] = []
    cursor = "*"
    while cursor:
        page = fetch(path, {**params, "per-page": str(PER_PAGE), "cursor": cursor})
        results.extend(page.get("results") or [])
        cursor = (page.get("meta") or {}).get("next_cursor")
        if cursor:
            time.sleep(REQUEST_PAUSE)
    return results


def s2_get(path: str, params: dict[str, str]) -> dict | None:
    """GET one Semantic Scholar page. Returns None instead of raising.

    Semantic Scholar is a bonus source, never a build dependency, so every
    failure here degrades to "no extra papers from S2" rather than a failed run.
    """
    url = f"{S2}/{path}?{urlencode(params)}"
    headers = {"User-Agent": f"gkoukis.github.io ({MAILTO})"}
    if S2_API_KEY:
        headers["x-api-key"] = S2_API_KEY
    try:
        result = _get(url, headers, 3)
    except (OpenAlexUnavailable, HTTPError, URLError):
        return None
    finally:
        time.sleep(S2_PAUSE)
    return result


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def short_id(openalex_id: str) -> str:
    """https://openalex.org/W123 -> W123"""
    return openalex_id.rsplit("/", 1)[-1]


def norm_doi(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("https://doi.org/", "").replace("http://doi.org/", "").strip().lower()


def clean_doi(work: dict) -> str:
    return norm_doi(work.get("doi"))


def title_key(title: str) -> str:
    """Normalized title used to collapse duplicates across sources."""
    return re.sub(r"[^a-z0-9]+", " ", unescape(title or "").lower()).strip()


def is_preprint(work: dict) -> bool:
    doi = clean_doi(work)
    return work.get("type") == "preprint" or doi.startswith(PREPRINT_PREFIXES)


def first_author(work: dict) -> str:
    authorships = work.get("authorships") or []
    if not authorships:
        return ""
    lead = (authorships[0].get("author") or {}).get("display_name") or ""
    return f"{lead} et al." if len(authorships) > 1 else lead


def venue(work: dict) -> str:
    source = (work.get("primary_location") or {}).get("source") or {}
    return (source.get("display_name") or "")[:80]


def topic_of(work: dict) -> str:
    return ((work.get("primary_topic") or {}).get("display_name")) or ""


# --------------------------------------------------------------------------
# Node construction
# --------------------------------------------------------------------------

def node_from_openalex(work: dict, group: str) -> dict:
    return {
        "id": short_id(work["id"]),
        "group": group,
        "title": unescape(work.get("title") or ""),
        "author": first_author(work),
        "year": work.get("publication_year"),
        "venue": venue(work),
        "citations": work.get("cited_by_count") or 0,
        "topic": topic_of(work),
        "doi": clean_doi(work),
    }


def node_from_s2(paper: dict, group: str) -> dict:
    authors = paper.get("authors") or []
    lead = (authors[0].get("name") if authors else "") or ""
    return {
        "id": "S2" + str(paper.get("paperId")),
        "group": group,
        "title": unescape(paper.get("title") or ""),
        "author": f"{lead} et al." if len(authors) > 1 else lead,
        "year": paper.get("year"),
        "venue": (paper.get("venue") or "")[:80],
        "citations": paper.get("citationCount") or 0,
        "topic": "",
        "doi": norm_doi((paper.get("externalIds") or {}).get("DOI")),
    }


def node_from_scholar(entry: dict, group: str) -> dict:
    """Node for a paper only Google Scholar knows about.

    Scholar publishes no identifiers, so the node carries the result URL instead
    of a DOI and the page links to that; the citing count is Scholar's own.
    """
    authors = [a.strip() for a in (entry.get("authors") or "").split(",") if a.strip()]
    lead = authors[0] if authors else ""
    return {
        "id": "GS" + str(entry.get("id")),
        "group": group,
        "title": unescape(entry.get("title") or ""),
        "author": f"{lead} et al." if len(authors) > 1 else lead,
        "year": entry.get("year"),
        "venue": (entry.get("venue") or "")[:80],
        "citations": entry.get("citations") or 0,
        "topic": "",
        "doi": "",
        "url": entry.get("url") or "",
    }


def merge_key(work: dict) -> str:
    """Key that decides which works collapse into one node.

    Normally the normalized title, so a preprint and its published version
    merge. MERGE_GROUPS overrides it for pairs that were retitled on
    publication and would otherwise stay split.
    """
    doi = clean_doi(work)
    for index, group in enumerate(MERGE_GROUPS):
        if doi in group:
            return f"merge:{index}"
    return title_key(work.get("title") or work["id"])


def dedupe_core(works: list[dict]) -> list[dict]:
    """Collapse each preprint/published pair into a single richer work."""
    groups: dict[str, list[dict]] = {}
    for work in works:
        groups.setdefault(merge_key(work), []).append(work)

    merged: list[dict] = []
    for versions in groups.values():
        versions.sort(
            key=lambda w: (
                is_preprint(w),
                -len(w.get("referenced_works") or []),
                -(w.get("cited_by_count") or 0),
            )
        )
        best, *rest = versions
        if rest:
            references = list(best.get("referenced_works") or [])
            seen = set(references)
            for other in rest:
                for ref in other.get("referenced_works") or []:
                    if ref not in seen:
                        seen.add(ref)
                        references.append(ref)
            best = {
                **best,
                "referenced_works": references,
                "cited_by_count": max(w.get("cited_by_count") or 0 for w in versions),
                "_versions": [short_id(w["id"]) for w in versions],
                "_dois": [clean_doi(w) for w in versions if clean_doi(w)],
                # Titles of the merged-away versions, so a source that lists the
                # preprint separately (Scholar does) still finds this node.
                "_titles": [w.get("title") or "" for w in versions],
            }
        else:
            best = {
                **best,
                "_dois": [clean_doi(best)] if clean_doi(best) else [],
                "_titles": [best.get("title") or ""],
            }
        merged.append(best)

    merged.sort(key=lambda w: (-(w.get("publication_year") or 0), w.get("title") or ""))
    return merged


def fetch_works_by_id(ids: list[str]) -> list[dict]:
    """Look up works in batches; OpenAlex caps OR-filters at 50 values."""
    works: list[dict] = []
    for start in range(0, len(ids), BATCH_SIZE):
        page = fetch(
            "works",
            {
                "filter": "openalex_id:" + "|".join(ids[start : start + BATCH_SIZE]),
                "per-page": str(BATCH_SIZE),
                "select": WORK_FIELDS,
            },
        )
        works.extend(page.get("results") or [])
        time.sleep(REQUEST_PAUSE)
    return works


def fetch_topics_by_doi(dois: list[str]) -> dict[str, str]:
    """Best-effort topic lookup for papers we only know from Semantic Scholar."""
    topics: dict[str, str] = {}
    for start in range(0, len(dois), BATCH_SIZE):
        batch = dois[start : start + BATCH_SIZE]
        try:
            page = fetch(
                "works",
                {
                    "filter": "doi:" + "|".join(batch),
                    "per-page": str(BATCH_SIZE),
                    "select": "id,doi,primary_topic",
                },
            )
        except OpenAlexUnavailable:
            break
        for work in page.get("results") or []:
            topic = topic_of(work)
            if topic:
                topics[clean_doi(work)] = topic
        time.sleep(REQUEST_PAUSE)
    return topics


# --------------------------------------------------------------------------
# Graph assembly
# --------------------------------------------------------------------------

def assign_clusters(nodes: list[dict], edges: set[tuple[str, str]]) -> list[str]:
    """Group papers into labelled topic clusters, using citations to place them.

    OpenAlex' per-paper topic label is too noisy and too long-tailed to cluster
    on directly: 74 of the topics here cover one or two papers each, and it
    files an edge-cloud framework under "Air Quality Monitoring".  So topics are
    used only to *name and seed* the clusters -- the most common ones become the
    real groups -- and every other paper then joins whichever cluster its own
    citation neighbourhood mostly belongs to (label propagation).

    That is what makes the clusters reference-based rather than label-based, and
    it repairs mislabelled papers, whose neighbours outvote their own bad label.
    """
    ranked = Counter(n["topic"] for n in nodes if n["topic"])
    named = [name for name, _ in ranked.most_common(MAX_CLUSTERS)]
    seed = {name: i for i, name in enumerate(named)}

    assigned: dict[str, int | None] = {
        n["id"]: seed.get(n["topic"]) for n in nodes
    }

    neighbours: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for source, target in edges:
        neighbours[source].append(target)
        neighbours[target].append(source)

    # Synchronous passes, so the result does not depend on iteration order.
    for _ in range(8):
        updates: dict[str, int] = {}
        for node in nodes:
            if assigned[node["id"]] is not None:
                continue
            votes = Counter(
                assigned[m] for m in neighbours[node["id"]] if assigned[m] is not None
            )
            if votes:
                # Ties break toward the larger cluster, which is deterministic
                # because `named` is ordered by frequency.
                updates[node["id"]] = min(
                    votes, key=lambda c: (-votes[c], c)
                )
        if not updates:
            break
        assigned.update(updates)

    other = len(named)
    for node in nodes:
        node["cluster"] = assigned[node["id"]] if assigned[node["id"]] is not None else other

    # Propagation usually empties "Other" entirely; drop any cluster that ended
    # up unused and renumber, so the page never draws an empty region.
    labels = named + ["Other"]
    used = sorted({n["cluster"] for n in nodes})
    remap = {old: new for new, old in enumerate(used)}
    for node in nodes:
        node["cluster"] = remap[node["cluster"]]
    return [labels[i] for i in used]


class Graph:
    """Accumulates nodes keyed so the same paper from either source merges."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.by_doi: dict[str, str] = {}
        self.by_title: dict[str, str] = {}
        self.edges: set[tuple[str, str]] = set()

    def resolve(self, *, node_id: str = "", doi: str = "", title: str = "") -> str | None:
        if node_id and node_id in self.nodes:
            return node_id
        if doi and doi in self.by_doi:
            return self.by_doi[doi]
        if title and title_key(title) in self.by_title:
            return self.by_title[title_key(title)]
        return None

    def add(self, node: dict) -> str:
        """Insert a node, merging it into an existing one when it is the same paper."""
        existing = self.resolve(node_id=node["id"], doi=node["doi"], title=node["title"])
        if existing:
            current = self.nodes[existing]
            # Keep the richest values; core always outranks the other layers.
            if node["group"] == "core":
                current["group"] = "core"
            current["citations"] = max(current["citations"], node["citations"])
            for field in ("author", "venue", "year", "topic", "doi", "url"):
                if not current.get(field) and node.get(field):
                    current[field] = node[field]
            return existing

        self.nodes[node["id"]] = node
        if node["doi"]:
            self.by_doi[node["doi"]] = node["id"]
        self.by_title.setdefault(title_key(node["title"]), node["id"])
        return node["id"]

    def alias(self, node_id: str, titles: list[str]) -> None:
        """Also recognise `node_id` under these titles (e.g. its preprint's)."""
        for title in titles:
            if title:
                self.by_title.setdefault(title_key(title), node_id)

    def link(self, source: str, target: str) -> None:
        if source and target and source != target:
            self.edges.add((source, target))


def load_scholar() -> list[dict]:
    """Read the committed Google Scholar snapshot, or nothing if it is absent."""
    if SKIP_SCHOLAR or not SCHOLAR_PATH.exists():
        return []
    try:
        return json.loads(SCHOLAR_PATH.read_text(encoding="utf-8")).get("papers") or []
    except (json.JSONDecodeError, AttributeError):
        print(f"::warning::Ignoring unreadable {SCHOLAR_PATH.name}.", file=sys.stderr)
        return []


def add_scholar(graph: Graph, papers: list[dict]) -> int:
    """Fold the Scholar snapshot in. Returns how many nodes it added.

    Scholar entries have no identifiers, so they are matched to what is already
    on the map by normalized title -- that is what keeps a paper found by all
    three sources one node.  Anything left over is a citation only Scholar sees,
    and becomes a new node in the citing layer.
    """
    added = 0
    for paper in papers:
        anchor = graph.resolve(title=paper.get("title") or "")
        if not anchor:
            # A profile entry with no counterpart in OpenAlex: a deliverable, a
            # demo, or a paper indexed nowhere else. Nothing to attach to.
            continue

        # Scholar's own per-paper count is the most complete one there is.
        core_node = graph.nodes[anchor]
        core_node["citations"] = max(core_node["citations"], paper.get("cited_by") or 0)

        for entry in paper.get("citations") or []:
            known = len(graph.nodes)
            node_id = graph.add(node_from_scholar(entry, "citing"))
            if len(graph.nodes) > known:
                added += 1
            # It may have merged into one of his own papers (a self-citation).
            if graph.nodes[node_id]["group"] == "core":
                continue
            graph.link(node_id, anchor)
    return added


def build() -> tuple[dict, int]:
    """Returns the graph plus the node count before Semantic Scholar topped it up."""
    graph = Graph()

    # --- the author's own papers ------------------------------------------
    core_filter = f"author.id:{AUTHOR_ID},from_publication_date:{FROM_DATE}"
    raw_core = fetch_all("works", {"filter": core_filter, "select": WORK_FIELDS})

    if EXTRA_DOIS:
        raw_core += fetch_all(
            "works",
            {"filter": "doi:" + "|".join(sorted(EXTRA_DOIS)), "select": WORK_FIELDS},
        )
    if EXCLUDE_DOIS:
        raw_core = [w for w in raw_core if clean_doi(w) not in EXCLUDE_DOIS]
    if not raw_core:
        raise OpenAlexUnavailable(f"no works found for author {AUTHOR_ID}")

    core = dedupe_core(raw_core)
    for work in core:
        graph.alias(graph.add(node_from_openalex(work, "core")), work.get("_titles") or [])

    # Every OpenAlex id that maps to a core node, including merged-away
    # preprint versions -- citations land on either record.
    core_alias: dict[str, str] = {}
    for work in core:
        canonical = short_id(work["id"])
        for alias in work.get("_versions") or [canonical]:
            core_alias[alias] = canonical

    # --- papers citing him, per core paper --------------------------------
    # Asking per paper (rather than one OR-query) means the cites: index tells
    # us *which* paper each citation points at, so a citing work still gets
    # wired up when its own reference list is incomplete -- which happens.
    for work in core:
        canonical = short_id(work["id"])
        for alias in work.get("_versions") or [canonical]:
            for citing in fetch_all(
                "works", {"filter": f"cites:{alias}", "select": WORK_FIELDS}
            ):
                if short_id(citing["id"]) in core_alias:
                    continue
                graph.link(graph.add(node_from_openalex(citing, "citing")), canonical)

    # --- papers he cites ---------------------------------------------------
    reference_ids = sorted(
        {
            short_id(ref)
            for work in core
            for ref in work.get("referenced_works") or []
            if short_id(ref) not in core_alias
        }
    )
    for reference in fetch_works_by_id(reference_ids):
        graph.add(node_from_openalex(reference, "reference"))

    openalex_only = len(graph.nodes)

    # --- Semantic Scholar top-up ------------------------------------------
    s2_added = 0
    if not SKIP_S2:
        for work in core:
            canonical = short_id(work["id"])
            for doi in work.get("_dois") or []:
                for path, wrapper, group, forward in (
                    ("citations", "citingPaper", "citing", False),
                    ("references", "citedPaper", "reference", True),
                ):
                    page = s2_get(
                        f"paper/DOI:{doi}/{path}",
                        {"fields": S2_FIELDS, "limit": "1000"},
                    )
                    if not page:
                        continue
                    for entry in page.get("data") or []:
                        paper = entry.get(wrapper) or {}
                        if not paper.get("paperId") or not paper.get("title"):
                            continue
                        known = len(graph.nodes)
                        node_id = graph.add(node_from_s2(paper, group))
                        if len(graph.nodes) > known:
                            s2_added += 1
                        # It may have merged into one of his own papers.
                        if graph.nodes[node_id]["group"] == "core":
                            continue
                        graph.link(*((canonical, node_id) if forward else (node_id, canonical)))

    # --- Google Scholar top-up --------------------------------------------
    # Offline, from the committed snapshot, and last of the three so a paper the
    # other two do know keeps its DOI, topic and reference list.
    scholar_added = add_scholar(graph, load_scholar())

    # --- hand-supplied reference lists ------------------------------------
    for core_doi, cited_dois in MANUAL_REFERENCES.items():
        anchor = graph.resolve(doi=norm_doi(core_doi))
        if not anchor or not cited_dois:
            continue
        wanted = [norm_doi(d) for d in cited_dois if d]
        for start in range(0, len(wanted), BATCH_SIZE):
            for work in fetch_all(
                "works",
                {
                    "filter": "doi:" + "|".join(wanted[start : start + BATCH_SIZE]),
                    "select": WORK_FIELDS,
                },
            ):
                graph.link(anchor, graph.add(node_from_openalex(work, "reference")))

    # --- remaining citation edges between known nodes ---------------------
    # Every OpenAlex work lists its references, so wire up any pair where both
    # ends are already on the map. References and citing papers cross-link each
    # other too; that wiring is what makes the map a web rather than a star.
    for work in fetch_works_by_id(sorted(n for n in graph.nodes if n.startswith("W"))):
        source = graph.resolve(node_id=core_alias.get(short_id(work["id"]), short_id(work["id"])))
        if not source:
            continue
        for ref in work.get("referenced_works") or []:
            target = graph.resolve(node_id=core_alias.get(short_id(ref), short_id(ref)))
            if target:
                graph.link(source, target)

    # --- topics for Semantic-Scholar-only papers ---------------------------
    missing = sorted(
        {n["doi"] for n in graph.nodes.values() if not n["topic"] and n["doi"]}
    )
    if missing:
        found = fetch_topics_by_doi(missing)
        for node in graph.nodes.values():
            if not node["topic"]:
                node["topic"] = found.get(node["doi"], "")

    # Anything still without a topic inherits from the core paper it touches,
    # which is a better guess than "Other" for a paper that cites only one.
    neighbours: dict[str, list[str]] = {}
    for source, target in graph.edges:
        neighbours.setdefault(source, []).append(target)
        neighbours.setdefault(target, []).append(source)
    for node in graph.nodes.values():
        if node["topic"]:
            continue
        nearby = Counter(
            graph.nodes[n]["topic"]
            for n in neighbours.get(node["id"], [])
            if graph.nodes.get(n, {}).get("topic")
        )
        node["topic"] = nearby.most_common(1)[0][0] if nearby else ""

    # --- reconcile citation counts ----------------------------------------
    # A source's own count can lag the papers we actually found citing a work
    # (especially after a merge, where each version reported its share). Take
    # whichever is larger so the tooltip never claims fewer citations than the
    # map itself displays, and node size matches what is drawn.
    incoming = Counter(target for _, target in graph.edges)
    for node in graph.nodes.values():
        node["citations"] = max(node["citations"], incoming.get(node["id"], 0))

    # --- drop stragglers ---------------------------------------------------
    # Titleless records exist: OpenAlex carries repository stubs with an empty
    # title, no DOI and no year, and a node nobody can identify is worse on the
    # map than one fewer node. His own papers are never dropped.
    connected = {n for edge in graph.edges for n in edge}
    nodes = [
        n for n in graph.nodes.values()
        if n["group"] == "core" or (n["id"] in connected and n["title"].strip())
    ]
    kept = {n["id"] for n in nodes}
    edges = {e for e in graph.edges if e[0] in kept and e[1] in kept}

    # --- cluster assignment ------------------------------------------------
    clusters = assign_clusters(nodes, edges)
    for node in nodes:
        for field in ("doi", "url"):
            if not node.get(field):
                node.pop(field, None)

    counts = {
        group: sum(1 for n in nodes if n["group"] == group)
        for group in ("core", "citing", "reference")
    }

    return {
        "author": {
            "openalex_id": AUTHOR_ID,
            "name": "Georgios Koukis",
            "orcid": "0009-0005-0116-7296",
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": (
            ["https://openalex.org"]
            + ([] if SKIP_S2 else ["https://semanticscholar.org"])
            + ([] if not scholar_added else ["https://scholar.google.com"])
        ),
        "clusters": clusters,
        "counts": {
            **counts,
            "edges": len(edges),
            "from_semantic_scholar": s2_added,
            "from_google_scholar": scholar_added,
        },
        "nodes": sorted(nodes, key=lambda n: (n["group"] != "core", -n["citations"])),
        "edges": [{"s": s, "t": t} for s, t in sorted(edges)],
    }, openalex_only


def unchanged(graph: dict) -> bool:
    """True if only the build timestamp differs from the committed snapshot."""
    if not OUTPUT_PATH.exists():
        return False
    try:
        previous = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return {k: v for k, v in previous.items() if k != "generated_at"} == {
        k: v for k, v in graph.items() if k != "generated_at"
    }


def main() -> int:
    try:
        graph, openalex_only = build()
    except OpenAlexUnavailable as exc:
        print(f"::warning::Skipping literature map update - {exc}.", file=sys.stderr)
        return 0

    counts = graph["counts"]
    summary = (
        f"{counts['core']} own papers, {counts['citing']} citing, "
        f"{counts['reference']} referenced, {counts['edges']} edges, "
        f"{len(graph['clusters'])} clusters "
        f"(+{counts['from_semantic_scholar']} papers from Semantic Scholar, "
        f"+{counts['from_google_scholar']} from Google Scholar)"
    )

    if unchanged(graph):
        print(f"Literature map is unchanged ({summary}) - leaving the snapshot alone.")
        return 0

    OUTPUT_PATH.write_text(
        json.dumps(graph, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT_PATH.name}: {summary}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
