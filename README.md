# av-tools

Internal reporting tools for The Philadelphia Inquirer. Password-protected dashboards and automated data pipelines for court monitoring.

**Live site:** https://abgutman.github.io/av-tools/ (password-protected)

## What's here

### Dashboards (HTML, served via GitHub Pages)

| Page | File | Description |
|------|------|-------------|
| Homepage | `index.html` | Hub linking all tools, organized by section |
| Court Calendar | `philly_calendar.html` | 7-day Philly criminal court calendar (CCP + Municipal) |
| Felony Filings | `philly_felonies_dashboard.html` | New felony dockets filed in past 24 hours |
| Plea Calendar | `philly_plea_calendar.html` | Status conferences for serious crimes + plea hearings |
| Guilty Plea Watch | `philly_new_pleas.html` | Tracks ~376 violent felony dockets for new guilty pleas |
| DOJ Civil Cases | `civil_edpa_dashboard.html` | Federal civil cases (E.D. Pa.) involving the U.S. government |
| Habeas Corpus | `habeas_edpa.html` | Habeas petitions filed in E.D. Pa. |
| Lower Merion | `montco_lm_dashboard.html` | Civil cases in Montco CCP with Lower Merion addresses |
| Greater Media | `delco_media_dashboard.html` | Civil cases in Delco CCP with Media/Swarthmore/Wallingford addresses |

### Scrapers (Python)

| Script | Court/Source | Runs on GitHub Actions? |
|--------|-------------|------------------------|
| `fetch_philly_calendar.py` | PA UJS Portal (Philly criminal) | No (UJS blocks cloud IPs) |
| `fetch_philly_felonies.py` | PA UJS Portal | No |
| `fetch_philly_pleas.py` | PA UJS Portal | No |
| `fetch_plea_watch.py` | PA UJS Portal | No |
| `fetch_montco_lm.py` | Montco Prothonotary (courtsapp.montcopa.org) | Yes, daily at 5 AM ET |
| `fetch_delco_media.py` | Delco C-Track (delcopublicaccess.co.delaware.pa.us) | Yes, daily at 5 AM ET |
| `send_local_digest.py` | Reads state files, sends email digest | Yes, Mondays at 5 AM ET |

### Other files

- `auth_gate.py` — Shared password gate injected into all pages (SHA-256 client-side check)
- `robots.txt` — Blocks all crawlers
- `dockets/` — Cached PDF docket sheets for Philly criminal cases
- State files (`*.json`) — Track previously seen cases to avoid re-fetching

## Automation

### GitHub Actions (`daily-scrape.yml`)
- **Schedule:** Every day at 5 AM ET (9:00 UTC)
- **Runs:** Montco + Delco scrapers, commits updated dashboards
- **Weekly digest:** Mondays only, emails Lower Merion + Greater Media summary
- **Secrets needed:** `GMAIL_USER`, `GMAIL_APP_PASSWORD`
- **Recipient:** Hardcoded in `send_local_digest.py` (dsagner@inquirer.com, cc agutman@inquirer.com)

### Local deploy (`deploy_dashboard.sh`)
- Runs all 6 scrapers (including Philly ones that only work locally)
- Copies output to this repo directory, commits and pushes
- Must be run from the working directory (`~/Desktop/claude_sandbox/`)

## Password

All pages are behind a client-side SHA-256 password gate. The hash is in `auth_gate.py`. The `inject_auth()` function adds the gate + noindex meta + home link to any HTML string.

## Related repos

| Repo | Purpose | Connection to av-tools |
|------|---------|----------------------|
| `court-feed` | PA + federal appellate court opinions/filings/calendars | Linked from av-tools homepage |
| `pa-appeals-alert` | Email alerts for new PA appellate filings | Independent (Claude cron) |
| `page_watcher` | Monitors web pages for changes (NPS President's House) | Independent |
| `ccp-case-watcher` | Philly docket change detection (shelved, reCAPTCHA) | Independent |
| `habeas-tracker` | Standalone habeas corpus database page | Superseded by `habeas_edpa.html` in av-tools |
| `pa-appeals-digest` | PA appeals opinion agent | Related to court-feed |
