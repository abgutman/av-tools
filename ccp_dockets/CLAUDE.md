# ccp_dockets — CLAUDE.md

CCP civil docket monitor. Two tabs: daily new-complaint digest + watchlist alerter.

## Engine

`fjd_docket.py` — DO NOT modify without re-verifying live. Critical quirks:
- Docket HTML is in the **302 body** (`allow_redirects=False`). Following the redirect drops the docket.
- `uid`/`o` tokens CANNOT be blank. Must harvest fresh from a calendar results page.
- Parser uses **lxml** (not html.parser) — FJD's unclosed `<td>` tags break html.parser.
- Tables identified by **column header labels** (`_find_table()`), not by `<h3>` heading proximity.

## State files

Both state JSONs must be committed to the repo — GitHub Actions reads and writes them to persist state across runs.
- `data/state_complaints.json` — last_seq_by_month scan pointer
- `data/state_watchlist.json` — per-case snapshot + full docket for replica generation

Seed: `state_complaints.json` initialized at `{"last_seq_by_month": {"2606": 783}}` so first run starts at seq 784 (June 4, 2026).

## Exclude filter

`scrape_new_complaints.py` — filter is **exclude-list** (new types default to INCLUDED). See README for the full list. Do not add overly broad rules.

## Watchlist

`watchlist.json` — user edits this directly in GitHub web editor to add/remove cases. No alert is sent on first add (baseline snapshot taken). Subsequent docket changes trigger email.

## Deploy

Scripts live in `ccp_dockets/` in the av-tools repo. Outputs at repo root:
- `ccp_dockets_dashboard.html` — gated dashboard
- `dockets/<case_id>.html` — gated docket replicas

Workflow: `ccp-dockets.yml` -> `.github/workflows/` in av-tools repo.
Cron: `0 10 * * *` UTC (6am EDT / 5am EST — cron is UTC-fixed).

## PII

Party addresses are public court record and appear in docket replicas behind the password gate. `REDACT_PII = False` toggle at top of `generate_dashboard.py` strips addresses if set to True.

## Email

`to=["agutman@inquirer.com"]` — Av only. Change here and in both scraper files if adding recipients.
