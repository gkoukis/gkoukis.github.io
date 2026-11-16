# Updating Scholar metrics and the literature map

Run everything from the repo root in PowerShell.

## 1. Headline metrics (publications, citations, h-index)

```powershell
py -3 .\scripts\update_scholar_metrics.py
```

## 2. Capture the Scholar "Cited by" pages in the browser

```powershell
Get-Content -Raw .\scripts\collect_scholar_pages.js | Set-Clipboard
```

1. Open https://scholar.google.com/citations?user=E2bGWsUAAAAJ&hl=en in Chrome and solve any CAPTCHA.
2. Press F12, open Console, type `allow pasting` if Chrome asks, paste with Ctrl+V and press Enter.
3. **When the file picker opens, press Cancel.** That starts a fresh capture. If you pick an
   old `scholar_pages.json`, every page in it is reused, the profile page included, and the
   map stays stuck at the old citation counts.
   Resume from a file only when the previous run *stopped early* (CAPTCHA) a few minutes ago.
4. Wait until the console says `done`. It downloads `scholar_pages.json` (Chrome may name it
   `scholar_pages (1).json`, and the commands below pick the newest file).

## 3. Rebuild the snapshot and the map

```powershell
$capture = Get-ChildItem "$env:USERPROFILE\Downloads\scholar_pages*.json" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
$capture.FullName   # check that this is the file you just downloaded

py -3 .\scripts\fetch_scholar_citations.py --from-pages $capture.FullName
py -3 -u .\scripts\build_literature_map.py
```

`fetch_scholar_citations.py` prints `N citing records of M Scholar reports`, and N should be
close to M. The map build prints how many own papers and citing papers it found.

## 4. Commit and push

```powershell
git add scholar_metrics.json scholar_citations.json literature_map.json index.html
git commit -m "Update Scholar metrics and literature map"
git pull --rebase origin main
git push origin main
```

## Notes

- If one of your papers is missing from the map, OpenAlex probably has it without your
  author id. Add its DOI to `EXTRA_DOIS` in `build_literature_map.py`.
- `import_manual_scholar_pages.py` is a fallback for "Cited by" pages saved by hand as
  `cites-<cluster>-start-<offset>.html`. Run `py -3 .\scripts\import_manual_scholar_pages.py <dir> --dry-run` first.
- Never commit the `scholar_pages*.json` captures. They contain signed-in HTML.
