# Fortification Program Dashboard (data.json architecture)

This dashboard separates the static HTML/JS shell from the data:

- `index.html` — the dashboard shell (HTML, CSS, JS). **Never changes automatically.**
- `data.json` — all the program data. **Rebuilt daily by GitHub Actions.**
- `scripts/rebuild_dashboard.py` — fetches the Google Sheet and writes `data.json`.
- `.github/workflows/refresh-dashboard.yml` — runs the script daily at 6:00 AM IST.

## Why this structure

The old version embedded all data directly inside `index.html`, so every daily
refresh rewrote the entire 1MB+ file. Now only `data.json` is rewritten —
`index.html` stays untouched unless you deliberately update the dashboard's
design or features.

## One-time setup

1. Add your Apps Script URL as a repository secret:
   **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `APPS_SCRIPT_URL`
   - Value: your Apps Script Web App URL

2. Enable GitHub Pages:
   **Settings → Pages → Source: Deploy from a branch → Branch: main, folder: / (root)**

3. Run the workflow once manually to generate the first `data.json`:
   **Actions → Refresh Dashboard Data → Run workflow**

## Updating the dashboard's design/features

Edit `index.html` directly and push it. `data.json` is untouched by this,
so your data stays intact.

## Updating the data logic (e.g. new calculation, new filter)

Edit `scripts/rebuild_dashboard.py`. Next scheduled run (or manual trigger)
will regenerate `data.json` with the new logic.

## Important: this requires a web server

Because `index.html` fetches `data.json` via `fetch()`, opening `index.html`
directly as a local file (`file://...`) will NOT work due to browser CORS
restrictions. It only works when served over HTTP — i.e. GitHub Pages, or
any other web server.
