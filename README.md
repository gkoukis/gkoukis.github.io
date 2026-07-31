# Bio page in GitHub

Static personal site, served from `index.html`. Two pieces of research data are
refreshed on a schedule rather than edited by hand.

## Google Scholar metrics

`scholar_metrics.json` holds the publication / citation / h-index / i10 numbers.

- `scripts/update_scholar_metrics.py` scrapes the public profile.
- `scripts/sync_index_metrics.py` writes the numbers into the hard-coded
  fallbacks in `index.html`.
- `scripts/update-scholar.sh` runs both locally (Scholar usually blocks GitHub
  Actions runners, so the scheduled job often no-ops).
- `.github/workflows/update-scholar-metrics.yml` — weekly, Mondays 04:17 UTC.

## Literature map

`literature_map.json` holds the citation network drawn in the "Literature Map"
section. It has three layers of nodes — the author's own papers, papers citing
them, and papers they cite — plus every citation edge between nodes that are
both in the graph. Nodes are also grouped into labelled topic clusters, which
the page draws as regions.

- `scripts/build_literature_map.py` rebuilds it by merging
  [OpenAlex](https://openalex.org),
  [Semantic Scholar](https://www.semanticscholar.org/product/api) (both free,
  no API key required) and the Google Scholar snapshot below.
- `scripts/fetch_scholar_citations.py` refreshes that snapshot
  (`scholar_citations.json`): the citing papers listed behind each "Cited by"
  link on the public profile. Run it **locally** — Scholar CAPTCHAs CI runners,
  and often a home IP too once it has seen a few requests. The file is committed,
  and the map build only ever reads it, so a blocked run changes nothing.
- `.github/workflows/update-literature-map.yml` — weekly, Mondays 05:41 UTC.
  Deliberately separate from the Scholar job so one source failing cannot block
  the other.

Rebuild locally with:

```sh
python scripts/fetch_scholar_citations.py    # ~2 min, paces itself; may be blocked
python scripts/build_literature_map.py       # ~2 min, S2 is rate-limited
SKIP_S2=1 SKIP_SCHOLAR=1 python scripts/build_literature_map.py   # OpenAlex only, ~30s
```

Both scripts only rewrite their file when the data actually changes, so a run
that finds nothing new leaves the snapshot (and git history) untouched. If
Scholar blocks the fetch, wait an hour or so and try again — retrying
immediately just extends the block. A blocked run keeps whatever it managed to
read, so repeating it eventually completes the crawl.

To get through a block instead of waiting, let the browser do the fetching. Google
reads bot-ness off the TLS handshake and header order as much as off cookies, so
requests from a browser that is happily showing you the profile still succeed
when the script's own are refused:

1. Open <https://scholar.google.com/citations?user=E2bGWsUAAAAJ&hl=en> and solve
   the CAPTCHA if there is one.
2. F12 → Console. The first paste is refused until you type `allow pasting`; then
   paste all of [`scripts/collect_scholar_pages.js`](scripts/collect_scholar_pages.js)
   and press Enter. It fetches a page every few seconds (~3 min), logs progress,
   and downloads `scholar_pages.json`.
3. ```sh
   python scripts/fetch_scholar_citations.py --from-pages ~/Downloads/scholar_pages.json
   ```

The capture is raw HTML, parsed by exactly the same code as a live crawl, so the
snapshot comes out identical. A capture that stopped early is fine: the papers it
did reach are updated and the rest keep their previous lists.

`scholar_pages.json` is pages fetched from a signed-in session, so it can contain
account chrome (your name, the signed-in email) alongside the results. It is
gitignored and only ever an input — keep it local, and there is no reason to
share or commit it. Only the parsed `scholar_citations.json`, which holds public
bibliographic fields, is committed.

Borrowed cookies are the lighter-weight thing to try first, and sometimes enough:
copy a `scholar.google.com` request as cURL (Network tab → right-click → Copy →
Copy as cURL) and run `--cookie-from-clipboard`, which lifts them out of the
copied command without printing them — Chromium passes them as `-b`, other tools
as a `cookie:` header, and both are handled, including from WSL via
`powershell.exe`. Since whatever you copy next replaces them, `--cookie-prompt`
asks for the value instead and hides the paste, and `SCHOLAR_COOKIE` takes it
directly for unattended runs.

Those cookies are a live Google session. They are only ever sent to
`scholar.google.com`, never written to disk, and never printed — the script
reports a count, not values. Two habits worth keeping anyway: prefer
`--cookie-prompt` over an inline `SCHOLAR_COOKIE='...' python ...`, which would
otherwise sit in your shell history in plain text, and clear the clipboard when
you are done.

## Previewing locally

The page fetches its JSON, so opening `index.html` from disk fails on CORS.
Serve the directory instead:

```sh
python -m http.server 8000
```

then open <http://localhost:8000/#litmap>.

### Why three sources

No open source on its own matches Google Scholar. Measured over these papers in
July 2026: OpenAlex finds 56 citations, Semantic Scholar 70, Google Scholar 105.
The papers Scholar sees and the others do not are theses, books, workshop
proceedings without DOIs and self-deposited copies, which is why the third
source is worth the manual step.

Scholar cannot be automated — no API, and it CAPTCHAs CI runners — so it is
crawled by hand into `scholar_citations.json` and read from there. Matching is by
normalized title, since Scholar publishes no identifiers; papers all three
sources know stay one node.

Both top-up sources are strictly additive: with Semantic Scholar down and the
snapshot missing, the build still succeeds on OpenAlex alone.

One residual gap is by definition, not coverage: Scholar counts citation
*instances* while the map counts distinct citing *papers*, so one paper citing
three of his works is three in Scholar and one node here.

### How the clusters are formed

Topic labels are too noisy and too long-tailed to cluster on directly — 74 of
the topics here cover one or two papers each, and OpenAlex files an edge-cloud
framework under "Air Quality Monitoring". So topics only *name and seed* the
clusters (the most common ones become the groups), and every other paper then
joins whichever cluster its own citation neighbourhood mostly belongs to. That
makes the grouping reference-based, and it repairs mislabelled papers because
their neighbours outvote their bad label.

### Things worth knowing

- **Author disambiguation.** OpenAlex has merged this ORCID with an unrelated,
  much older "G. Koukis" (an engineering geologist publishing since the 1980s).
  `FROM_DATE` in the script cuts everything before 2019, which removes the other
  author entirely. Revisit it if OpenAlex ever splits the records.
- **Preprints.** An arXiv/Preprints.org version and its published version are
  collapsed into one node, keeping the union of their references and the higher
  citation count, since OpenAlex splits those across the two records.
- **Missing works.** Anything OpenAlex has not indexed cannot appear — currently
  the IETF BMWG draft and the BalkanCom 2025 paper. Add their DOIs to
  `EXTRA_DOIS` once OpenAlex picks them up; `EXCLUDE_DOIS` drops anything
  misattributed.
- **Unconnected nodes.** Very recent papers show as isolated until the sources
  index their reference lists. That is expected, not a bug.
- **Links out.** Clicking a node opens its DOI. Papers known only from Google
  Scholar have no DOI, so they carry the result URL Scholar links to instead
  (`url` in the JSON); a node with neither is not clickable.
- **Colour vs clusters.** Node colour encodes the *layer* (his papers / citing /
  cited) using three colourblind-validated hues. Topic is carried by the
  labelled region, not by hue — eight categorical colours cannot pass the
  contrast and CVD gates in a graph where any two nodes can end up adjacent.
