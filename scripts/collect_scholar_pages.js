/* Collect the Scholar pages the citation crawl needs, from inside the browser.
 *
 * Google decides what is a bot from the TLS handshake and header order as much
 * as from cookies, so scripts/fetch_scholar_citations.py can be refused while
 * the very same requests succeed in a browser that is reading the profile.  This
 * snippet does the fetching there and saves the raw HTML; the Python script then
 * parses it with --from-pages, so both routes share one set of parsers.
 *
 * How to use it:
 *   1. Open https://scholar.google.com/citations?user=E2bGWsUAAAAJ&hl=en
 *      and solve the CAPTCHA if there is one.
 *   2. F12 -> Console.  On the first paste Edge/Chrome refuses and asks you to
 *      type "allow pasting" -- do that, then paste this whole file and press
 *      Enter.
 *   3. It immediately opens a file picker (there is no argv to put a flag on,
 *      since this runs by pasting into the console, so this stands in for
 *      one) -- pick the scholar_pages.json you downloaded last time and every
 *      page already in it is reused instead of re-fetched, so a run that got
 *      blocked partway picks up where it stopped almost instantly. Cancel to
 *      start fresh. It has to be the very first thing the script does: the
 *      picker needs the activation from pasting + pressing Enter, and that is
 *      spent the moment anything else -- even a confirm() dialog -- uses it
 *      first.  If a resumed capture has anything in it, it then asks which
 *      papers, if any, to re-fetch from scratch even though they are cached
 *      (the "paper N/9" numbers from the log) -- leave that blank to trust
 *      the cache for all of them.
 *   4. Watch the log; it fetches one page every few seconds and takes ~3 min,
 *      minus whatever got skipped from cache.  It stops early if Scholar
 *      starts serving check pages, keeping what it has and downloading that --
 *      re-run and resume from the download to pick up where it left off.
 *   5. It downloads scholar_pages.json.  Point the crawl at it:
 *      python3 scripts/fetch_scholar_citations.py --from-pages ~/Downloads/scholar_pages.json
 *
 * python3 scripts/fetch_scholar_citations.py --from-pages /mnt/c/XXX/scholar_pages.json
 * python3 scripts/build_literature_map.py
 * 
 * Nothing leaves the browser: the fetches are same-origin, a resume file is
 * read locally through a file picker, and the output is written by the
 * browser's own download.  The capture is signed-in HTML, so it can contain
 * account chrome (your name, the signed-in email) next to the results: it is
 * gitignored, and it is an input to keep local, never to commit or share.
 * Only the parsed public fields end up in scholar_citations.json.
 */
(async () => {
    const MAX_PAGES = 6;          // Matches the Python crawl's per-paper cap.
    const PAGE_SIZE = 20;         // Scholar caps result pages at 20.
    const PAUSE = [2500, 6000];   // Scholar tolerates a slow reader.
    const BLOCKED = /gs_captcha|\/sorry\/|unusual traffic|not a robot|id="captcha"/i;

    const id = new URLSearchParams(location.search).get('user') || 'E2bGWsUAAAAJ';
    const pages = {};
    const stats = { cached: 0, fetched: 0 };
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const breathe = () => sleep(PAUSE[0] + Math.random() * (PAUSE[1] - PAUSE[0]));

    function pickFile() {
        // A plain file input, not the File System Access API: DevTools grants
        // console-evaluated code the user activation a click needs, and this
        // works everywhere that API doesn't have to.  input.click() can still
        // throw synchronously if that activation is already gone -- a thrown
        // Promise executor rejects the promise, so the caller sees it either way.
        return new Promise((resolve, reject) => {
            const input = document.createElement('input');
            input.type = 'file';
            input.accept = 'application/json';
            const finish = file => { input.remove(); resolve(file); };
            input.addEventListener('change', () => finish(input.files[0] || null), { once: true });
            input.addEventListener('cancel', () => finish(null), { once: true });
            document.body.appendChild(input);
            try {
                input.click();
            } catch (error) {
                input.remove();
                reject(error);
            }
        });
    }

    // --- resume: reuse whatever a previous, cut-off run already captured ----
    // This has to run before anything else touches the page's activation --
    // a confirm() dialog included -- or the picker below is refused.  Either
    // way it can't take the run down: a cancelled or failed picker just means
    // starting fresh, same as if this block were never here.
    let resume = {};
    console.log('Pick the scholar_pages.json from last time to resume, or Cancel to start fresh...');
    try {
        const file = await pickFile();
        if (file) {
            resume = JSON.parse(await file.text()).pages || {};
            console.log(`Resuming: ${Object.keys(resume).length} pages loaded from ${file.name}.`);
        } else {
            console.log('Starting fresh (no file chosen).');
        }
    } catch (error) {
        console.warn(`Could not load a previous capture (${error.message}) -- starting fresh. ` +
            'If this keeps happening, click anywhere on the page once before pasting the script.');
    }

    // --- optionally force specific papers to be re-fetched anyway -----------
    let redo = new Set();
    if (Object.keys(resume).length) {
        const answer = prompt('Re-fetch specific papers from scratch even though a ' +
            'capture was loaded? Comma-separated paper numbers (the "paper N/9" from ' +
            'the log), or leave blank to trust the cache for all of them.', '');
        redo = new Set((answer || '').split(',')
            .map(entry => parseInt(entry.trim(), 10))
            .filter(n => !Number.isNaN(n)));
        if (redo.size) console.log(`Will re-fetch paper(s): ${[...redo].sort((a, b) => a - b).join(', ')}.`);
    }

    async function grab(path) {
        const url = new URL(path, 'https://scholar.google.com').toString();
        if (resume[url] !== undefined) {
            pages[url] = resume[url];
            stats.cached++;
            return { html: resume[url], cached: true };
        }
        const response = await fetch(url, { credentials: 'include' });
        const html = await response.text();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        if (BLOCKED.test(html)) throw new Error('Scholar served a robot check');
        pages[url] = html;
        stats.fetched++;
        return { html, cached: false };
    }

    function save() {
        const blob = new Blob([JSON.stringify({ scholar_id: id, pages })],
            { type: 'application/json' });
        const link = document.createElement('a');
        link.href = URL.createObjectURL(blob);
        link.download = 'scholar_pages.json';
        link.click();
        URL.revokeObjectURL(link.href);
    }

    let stopped = null;
    try {
        const { html: profile, cached: profileCached } =
            await grab(`/citations?user=${id}&hl=en&pagesize=100`);
        // Only the cluster ids are needed here; Python does the real parsing.
        const clusters = [...new Set([...profile.matchAll(/cites=([\d,]+)/g)]
            .map(match => match[1]))];
        console.log(`profile: ${clusters.length} cited papers${profileCached ? ' (cached)' : ''}`);

        if (redo.size) {
            // Drop only the pages that belong to the requested papers, so the
            // rest of the resume file still short-circuits the network calls.
            for (const url of Object.keys(resume)) {
                const cites = url.match(/[?&]cites=([\d,]+)/);
                const clusterIndex = cites ? clusters.indexOf(cites[1]) : -1;
                if (clusterIndex !== -1 && redo.has(clusterIndex + 1)) delete resume[url];
            }
        }

        for (const [index, cluster] of clusters.entries()) {
            const fetchedBefore = stats.fetched;
            for (let page = 0; page < MAX_PAGES; page++) {
                const query = `hl=en&as_sdt=2005&sciodt=0%2C5&cites=${cluster}` +
                    `&start=${page * PAGE_SIZE}&num=${PAGE_SIZE}`;
                const { html, cached } = await grab(`/scholar?${query}`);
                const results = (html.match(/data-cid="/g) || []).length;
                console.log(`paper ${index + 1}/${clusters.length}, page ${page + 1}: ` +
                    `${results} results${cached ? ' (cached)' : ''}`);
                if (results < PAGE_SIZE || !html.includes('gs_ico_nav_next')) break;
                if (!cached) await breathe();
            }
            // Only worth being polite to Scholar about papers actually asked for.
            if (stats.fetched > fetchedBefore) await breathe();
        }
    } catch (error) {
        stopped = error;   // Keep every page fetched or reused before this point.
    }

    save();
    const total = Object.keys(pages).length;
    const tally = `${stats.cached} reused, ${stats.fetched} new`;
    console.log(stopped
        ? `stopped early (${stopped.message}) - saved ${total} pages (${tally}); ` +
          `re-run and resume from this download to continue`
        : `done - saved ${total} pages (${tally}) to scholar_pages.json`);
})();
