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
 *   3. Watch the log; it fetches one page every few seconds and takes ~3 min.
 *      It stops early if Scholar starts serving check pages, keeping what it got.
 *   4. It downloads scholar_pages.json.  Point the crawl at it:
 *      python3 scripts/fetch_scholar_citations.py --from-pages ~/Downloads/scholar_pages.json
 *
 * Nothing leaves the browser: the fetches are same-origin and the file is
 * written by the browser's own download.  The capture is signed-in HTML, so it
 * can contain account chrome (your name, the signed-in email) next to the
 * results: it is gitignored, and it is an input to keep local, never to commit or
 * share.  Only the parsed public fields end up in scholar_citations.json.
 */
(async () => {
    const MAX_PAGES = 6;          // Matches the Python crawl's per-paper cap.
    const PAGE_SIZE = 20;         // Scholar caps result pages at 20.
    const PAUSE = [2500, 6000];   // Scholar tolerates a slow reader.
    const BLOCKED = /gs_captcha|\/sorry\/|unusual traffic|not a robot|id="captcha"/i;

    const id = new URLSearchParams(location.search).get('user') || 'E2bGWsUAAAAJ';
    const pages = {};
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
    const breathe = () => sleep(PAUSE[0] + Math.random() * (PAUSE[1] - PAUSE[0]));

    async function grab(path) {
        const url = new URL(path, 'https://scholar.google.com').toString();
        const response = await fetch(url, { credentials: 'include' });
        const html = await response.text();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        if (BLOCKED.test(html)) throw new Error('Scholar served a robot check');
        pages[url] = html;
        return html;
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
        const profile = await grab(`/citations?user=${id}&hl=en&pagesize=100`);
        // Only the cluster ids are needed here; Python does the real parsing.
        const clusters = [...new Set([...profile.matchAll(/cites=([\d,]+)/g)]
            .map(match => match[1]))];
        console.log(`profile: ${clusters.length} cited papers`);

        for (const [index, cluster] of clusters.entries()) {
            for (let page = 0; page < MAX_PAGES; page++) {
                const query = `hl=en&as_sdt=2005&sciodt=0%2C5&cites=${cluster}` +
                    `&start=${page * PAGE_SIZE}&num=${PAGE_SIZE}`;
                const html = await grab(`/scholar?${query}`);
                const results = (html.match(/data-cid="/g) || []).length;
                console.log(`paper ${index + 1}/${clusters.length}, ` +
                    `page ${page + 1}: ${results} results`);
                if (results < PAGE_SIZE || !html.includes('gs_ico_nav_next')) break;
                await breathe();
            }
            await breathe();
        }
    } catch (error) {
        stopped = error;   // Keep every page fetched before this point.
    }

    save();
    const count = Object.keys(pages).length;
    console.log(stopped
        ? `stopped early (${stopped.message}) - saved ${count} pages; re-run later to finish`
        : `done - saved ${count} pages to scholar_pages.json`);
})();
