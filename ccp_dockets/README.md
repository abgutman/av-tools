# ccp_dockets

Philadelphia Court of Common Pleas civil docket monitor. Two tools:

1. **New complaints** — daily scan of all new civil complaints filed since the last run, filtered to exclude liens, parking, MJ appeals, and other noise. Emails a table digest.
2. **Watchlist** — daily check on a list of specific case IDs. Emails change alerts when the docket is updated.

Dashboard deployed to [av-tools](https://abgutman.github.io/av-tools/ccp_dockets_dashboard.html) (password-gated).

---

## How it works

Source: FJD public e-filing system at `fjdefile.phila.gov/efsfjd/`. No CAPTCHA on calendar/docket endpoints; cloud IPs are allowed (GitHub Actions compatible). Oracle mod_plsql, server-rendered HTML.

`case_id` format: `YYMM` + 5-digit zero-padded sequence (e.g. `260600784` = June 2026, seq 784). Sequences increase monotonically with filing date. One generic session token (harvested from the public calendar) unlocks any case_id — no per-case auth needed.

The scraper enumerates `case_id`s from the last known sequence upward, stopping after 8 consecutive missing cases (end-of-stack sentinel: ~2,991-byte "does not exist" page).

See [`fjd_docket.py`](./fjd_docket.py) for the full engine and reverse-engineering notes.

---

## Files

| File | Purpose |
|------|---------|
| `fjd_docket.py` | Core engine: session minting, fetch, parse, change detection |
| `scrape_new_complaints.py` | Tab 1: enumerate new cases, filter, write JSON, email digest |
| `scrape_watchlist.py` | Tab 2: check watched cases, detect changes, email alerts |
| `generate_dashboard.py` | Build `dashboard.html` + `dockets/<case_id>.html` replicas |
| `deploy_prep.py` | Apply password gate; produce `ccp_dockets_dashboard.html` |
| `watchlist.json` | List of `{case_id, note}` to watch (edit here to add/remove) |
| `ccp-dockets.yml` | GitHub Actions workflow (copy to `.github/workflows/`) |

### Data files (committed to repo — Actions state persistence)

| File | Contents |
|------|---------|
| `data/state_complaints.json` | `{last_seq_by_month: {"2606": N}}` — scan pointer |
| `data/state_watchlist.json` | Per-case snapshot: sig, entry_count, full parsed docket |
| `data/complaints.json` | Full parsed dockets from latest scan (replaced each run) |
| `data/watchlist_view.json` | Summary rows for the dashboard watchlist tab |

---

## Exclude filter (Tab 1)

Cases are **included by default** (unknown types pass through). Excluded if `case_type`:
- Contains: `LIEN`, `PENNDOT`, `PARKING`, `CERTIFIED`
- Starts with: `MC -`
- Ends with: `-MR`
- Exact match: `SELF ASSESSED TAXES`, `CREDIT CARD COLLECTION`, `AUCTION MOTOR VEHICLE`, `EJECTMENT`, `QUIET TITLE`

---

## Adding cases to the watchlist

Edit `watchlist.json` directly (GitHub web editor or local):
```json
[
  {"case_id": "260303606", "note": "BOHM v. BOHM — alleged fraud, Commerce Program"},
  {"case_id": "260601234", "note": "New case — brief description here"}
]
```

The next run will pick it up, snapshot the current docket state, and begin alerting on future changes. No alert is sent on first add.

---

## Local usage

```bash
# Use the ccp_civil venv (has all deps) or install fresh:
pip install -r requirements.txt

# Scan new complaints (dry run — no email, no state advance):
python scrape_new_complaints.py

# Same but send email + advance state pointer:
python scrape_new_complaints.py --live

# Check watchlist (no email):
python scrape_watchlist.py

# Then build dashboard + replicas:
python generate_dashboard.py

# Apply gate (produces ccp_dockets_dashboard.html):
python deploy_prep.py
```

---

## Deployment

The workflow file `ccp-dockets.yml` goes in `.github/workflows/` of the `av-tools` repo. All script files go in `ccp_dockets/` at the repo root. Outputs staged to repo root:
- `ccp_dockets_dashboard.html` — main dashboard
- `dockets/<case_id>.html` — per-case replica pages

GitHub Actions commits the state JSONs + output HTML back to the repo after each run so state persists across runs.

---

## Source notes

- Data: First Judicial District of Pennsylvania public e-filing system
- URL: https://fjdefile.phila.gov/efsfjd/
- Scope: Philadelphia Court of Common Pleas civil cases
- Site disclaimer: "commercial use of data obtained using this site is strictly prohibited." Use here is journalistic / non-commercial. Documents are never purchased or accessed.
- Party addresses are public court record and are included in docket replicas (behind the password gate).
