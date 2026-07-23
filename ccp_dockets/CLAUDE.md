# ccp_dockets — CLAUDE.md

CCP civil docket monitor. Three tabs: daily new-complaint digest + watchlist alerter +
party-name watch.

## Party watch (Tab 3)

`fjd_party_search.py` + `scrape_name_watch.py` + `name_watch.json`. Monitors new cases
filed **by or against** named entities via the FJD participant-name index
(`zk_fjd_public_qry_01`), NOT case-id enumeration. Same CAPTCHA-free tricks as the docket
engine: blank `hash_code`, results in the **302 body** (`allow_redirects=False`). Search
is a **case-insensitive full-string prefix** match **capped at 50 rows**; `scrape_name_watch`
subdivides the date window on truncation. Each `name_watch.json` entry has `queries`
(prefixes POSTed) + a filter (`pattern` regex / `must_contain` any / `must_contain_all`).
Do NOT modify `fjd_party_search.py` without re-verifying live (undocumented FJD quirks).

Workflow `ccp-namewatch.yml`: every 15 min daytime, hourly overnight. Commits only
`ccp_dockets_dashboard.html` + `data/name_watch_view.json` + `data/state_name_watch.json`
(NOT the replica tree), and uses `git diff -I generated_at` so idle runs don't commit
pure-timestamp churn. Reuses the `GMAIL_USER`/`GMAIL_APP_PASSWORD` secrets. Email to Av only.

## Trial dispositions (daily email)

`scrape_trial_dispositions.py` — adds a **Trial Dispositions** section to the daily
`scrape_new_complaints.py` email: how trial-listed cases concluded (verdict/finding,
settlement, discontinuance, judgment, non pros, default). No case-id guessing — the source
is the **Trial Dates Certain (MJ)** calendar, whose every row carries a real `case_id`.

- **Tracked pool** (`data/state_trial_dispositions.json`): when a case first appears on the
  MJ calendar it's recorded (as its non-terminal `LISTED FOR TRIAL` status); we poll its
  docket daily once the trial date arrives, until the case-level **Status** field turns
  terminal — reported once, then retired. Cases that never resolve age out after
  `AGING_DAYS` (45). A case that is ALREADY terminal the first time we poll it is baselined
  **silently** (can't attribute it to "today") — same rule as the watchlist "no alert on
  first add".
- **Signal = case Status field, not entry types.** A concluded docket's Status reads e.g.
  `SETTLED PRIOR TO ASSGN TRL JUD` / `FINDING FOR PLAINTIFF` / `JUDGMENT ENTERED`, while its
  newest *entry* is usually a procedural `NOTICE GIVEN UNDER RULE 236`. `STATUS_MAP` +
  `NONTERMINAL_EXACT` were built from a **live sweep of all ~134 then-current trial-listed
  dockets (July 2026)**. Unknown statuses hit a conservative keyword fallback; anything
  truly unrecognized is kept + logged, and surfaced as "Unclassified — verify" if it ages
  out, so nothing is dropped silently. Extend `STATUS_MAP` from run logs when new strings
  appear.
- **Reused, not duplicated:** `run_scan(session, live)` takes the SAME `FjdSession` the
  complaints scan already minted (no second token). Called from `scrape_new_complaints.py`.
  The daily email now sends when there are new complaints **OR** new dispositions.
- **Order wording — EMAIL ONLY:** `extract_order_text()` pulls the disposition's order
  language from the docket's single-cell entry rows (e.g. "IT IS HEREBY ORDERED THAT
  JUDGMENT OF POSSESSION …", or "AWARDS PLTFS DAMAGES IN THE TOTAL AMOUNT OF $75,803.76") —
  the wording on the docket, NOT the sealed/purchasable PDF. Scheduling/case-management
  orders ("ASSIGNED TO THE … POOL") are filtered out; the newest category-matching order
  wins. It is shown **only in the (private, Av-only) email**. `append_to_log()`
  DELIBERATELY STRIPS `order_text` before writing `data/dispositions_log.json`, because the
  order text can carry addresses / lockout terms / party detail we keep out of the
  committed, dashboard-facing record. **The clean `award` dollar figure IS retained** in the
  log (via `extract_award()`, plaintiff-favorable outcomes only) — a verdict amount is public
  record, not PII, and the Inky digest surfaces it in its Trial Dispositions section. **If you
  ever build the Dispositions dashboard tab, read it from `dispositions_log.json` — which has
  the `award` figure but no order text by design.**
- **Layout:** the email section is grouped by **case type** (EJECTMENT, MED MAL, CONTRACTS,
  …), not by outcome; each row shows the category label + raw status, with the order text
  as an italic sub-row beneath.
- **Integrity:** every row shows the raw FJD status + our category label + (email only) the
  order text; a trial *listing* is never presented as a trial that was held.
  `disposition_date` and the order excerpt are best-effort — verify against the docket
  before publishing.

State file `data/state_trial_dispositions.json` (the pool) and `data/dispositions_log.json`
(the rolling record — metadata only, order text excluded) are both committed by
`ccp-dockets.yml` (added to the `git add` list) so Actions persists them across runs.

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
Cron: `0 0 * * *` UTC = 8pm EDT / 7pm EST (cron is UTC-fixed). Runs in the
evening because FJD issues the day's sequence numbers through business hours; a
6am scan hit the "does not exist" frontier wall and returned 0.

`complaints.json` is a **rolling 30-day window** (merge + dedup by case_id, drop
stale), not just the latest scan — so a barren scan never blanks the dashboard.
`--max-misses` default is 20 to survive gaps of reserved-but-unfiled seqs.

## PII

Party addresses are public court record and appear in docket replicas behind the password gate. `REDACT_PII = False` toggle at top of `generate_dashboard.py` strips addresses if set to True.

## Email

`to=["agutman@inquirer.com"]` — Av only. Change here and in both scraper files if adding recipients.
