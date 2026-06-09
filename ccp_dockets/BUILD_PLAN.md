# ccp_dockets — build plan / handoff spec

Status: **engine done & verified** (`fjd_docket.py`). Remaining: two scrapers,
dashboard generator, deploy prep, GitHub Actions workflow, docs.

## What this is
Two-tab dashboard on av-tools (password-gated, noindexed), daily GitHub Actions
run at **6am ET** (`0 10 * * *` UTC during EDT). Source: FJD docket reports
scraped by enumerating `case_id`s. State-court public record; non-commercial use.

## Engine API (`fjd_docket.py`) — DONE, verified live 2026-06-09
- `FjdSession()` — mints a generic docket token (uid,o) on init; reuse across fetches.
- `sess.fetch_docket(case_id)` -> `(status, html)`, status in `OK|MISSING|BOUNCE`.
  Auto re-mints on bounce. `MISSING` = end-of-stack ("does not exist").
- `parse_docket(html, case_id)` -> dict: `case_id, caption, filing_date` (ISO),
  `filing_date_raw, court, case_type, jury, status, plaintiffs[], defendants[],
  parties[{seq,type,name,address}], events[], entries[{date,time,type,party}],
  last_entry, entry_count`.
- `docket_signature(parsed)` -> change-detection key (entry_count|date|type).
- `current_yymm()`, `make_case_id(yymm, seq)`.
- Constants: `OK, MISSING, BOUNCE`. Throttle = 0.6s built into fetch_docket.

## Confirmed spec

### Tab 1 — New Complaints (daily digest + email)
- Each run: scan `case_id` stream from `last_seq+1` upward (current YYMM) until
  ~8 consecutive `MISSING` = end of stack. Parse each new case.
- **EXCLUDE filter** (drop these case_types; keep everything else incl. commercial
  foreclosure). Match rules (case-insensitive on `case_type`):
  - contains `LIEN`
  - contains `PENNDOT`
  - contains `PARKING`
  - contains `CERTIFIED`
  - startswith `MC -`
  - endswith `-MR`
  - exact: `SELF ASSESSED TAXES`, `CREDIT CARD COLLECTION`, `AUCTION MOTOR VEHICLE`,
    `EJECTMENT`, `QUIET TITLE`
- Keep new cases (since last_seq) passing the filter; these are "filed since you
  last looked" (≈ last 24h on a daily run; Monday run catches Fri–Sun via last_seq).
- State: `data/state_complaints.json` = `{"last_seq_by_month": {"2606": N}, ...}`.
  Handle month rollover (new YYMM starts scan at seq 1).
- Output: rows -> `data/complaints.json`; build docket replica page per case;
  email digest (table) via `email_utils.send_email`, only with `--live`.

### Tab 2 — Watchlist (daily change check + email)
- `watchlist.json` = list of `{case_id, note}` (seeded with 260303606 / BOHM note).
  User adds by editing this file in GitHub web editor + commit (option 1), or tells
  Av (option 3). The `note` shows as its own column.
- Each run: fetch each watched case, compute `docket_signature`. Compare to stored
  snapshot in `data/state_watchlist.json`. On change -> email alert with the new
  entries (diff), update snapshot.
- Dashboard table columns: Case ID, Note, Type, Parties (plaintiffs v defendants),
  Last filing (last_entry.type + date). Row click -> docket replica page.
- Output: `data/watchlist_view.json` + replica pages.

### Docket replica pages (`dockets/<case_id>.html`)
- Faithful replica of the docket: header, parties, events, docket entries.
- DEFAULT = full (includes party addresses; it's public record behind the gate).
  Add `REDACT_PII = False` toggle near top; if True, strip party address/phone/email.
- Generated for all watchlist cases + all digest cases each run.
- Document/PDF links are purchase-only — show entry text, note "documents available
  on the official docket (paid)"; NEVER touch the cart/purchase flow.

## Files to build
- `scrape_new_complaints.py` — Tab 1 scan+filter+state+json+email (`--live`, `--max-misses`).
- `scrape_watchlist.py` — Tab 2 check+diff+state+json+email (`--live`).
- `generate_dashboard.py` — reads complaints.json + watchlist_view.json -> builds
  `dashboard.html` (2 tabs, vanilla JS like ccp_civil) + `dockets/*.html` replicas.
  Footer source line + "always confirm against the official docket" reminder.
- `deploy_prep.py` — `from auth_gate import inject_auth`; gate dashboard + replicas
  -> `ccp_dockets_dashboard.html`. (sys.path.insert parent dir; see ccp_civil/deploy_prep.py)
- `requirements.txt` — requests, beautifulsoup4, lxml
- `README.md` (provenance + replication), folder `CLAUDE.md`.
- Workflow `ccp-dockets.yml` (mirror ccp-civil.yml; cron `0 10 * * *`; py 3.12;
  pip install ccp_dockets/requirements.txt; run both scrapers --live + generate +
  deploy_prep; commit state json + html + dockets/; GMAIL_USER/GMAIL_APP_PASSWORD secrets).

## Conventions (from existing workspace)
- `auth_gate.inject_auth(html)` adds noindex + password gate + home link. Password `avstools2026`.
- `email_utils.send_email(subject, body, log_fn=None, to=None)`; default EMAIL_TO has 3 people
  — **pass `to=["agutman@inquirer.com"]`** (Av only) for now.
- Reuse `ccp_civil/.venv` for local runs (has requests/bs4/lxml). Py 3.14 local, 3.12 Actions.
- Deploy clone (av-tools, persistent — NOT /tmp): `~/Desktop/claude_sandbox/.deploy/court-dashboard/`.
  Scripts copied under `ccp_dockets/`; output html + `dockets/` at repo root.
- HTML data embed pattern: `<script id="payload" type="application/json">__DATA__</script>` + vanilla JS tabs.

## Add av-tools homepage link
Homepage is generated by `criminal/fetch_philly_calendar.py` `build_index_html()` — add a
card linking `ccp_dockets_dashboard.html` (separate from the existing ccp_civil calendar card).
