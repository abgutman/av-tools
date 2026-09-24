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

## Case-type watch (intraday email — separate from the party watch)

`scrape_type_watch.py` + `type_watch.json` + workflow `ccp-typewatch.yml`. Alerts on new
filings matching a **case type** rule — something the FJD name index cannot search. Runs
its own incremental case-id enumeration (same `fjd_docket` engine as the daily complaints
scan) with its **own pointer** (`data/state_type_watch.json`) so the daily scan's state is
untouched; the two pointers advance independently and the daily digest remains the
comprehensive backstop. Every 30 min daytime (`:07/:37`, offset from the namewatch's
quarter-hour crons), hourly overnight.

Rules (`type_watch.json`): `case_type_startswith` (any-of, case-insensitive) +
`exclude_plaintiff_pattern` (regex; drop if ANY plaintiff matches). Current rule: case type
starts with `EQUITY - NO REAL ESTATE` (also catches the `(TRO)` variant) and plaintiff is
NOT the City (`\bcity of phila`) — the City files these constantly for code enforcement
(19 of 25 in a sample month). Expected volume ≈ 5–6 alerts/month. A case landing in
multiple rules is reported under the first matching label only.

**Seeding:** `state_type_watch.json` must be seeded at the current frontier before first
deploy (copy the month's pointer from `data/state_complaints.json`) or the first run walks
the entire month's backlog. New months self-seed (seq 1 IS the frontier). First qualifying
case emails immediately — there is no per-label silent-seed like the name watch, the
pointer is the dedup. Email to Av only.

## Philadelphia-in-court alert (daily email — separate from the party watch)

`phila_party_alert.py` + `phila_gov_watch.json` + `phila_officials.json` + workflow
`ccp-phila-alert.yml`. Daily morning digest to **Av + swalsh@inquirer.com** of NEW
**civil** cases (last 7 days, rolling + deduped) where a party is a Philadelphia
government/public body (name contains "Philadelphia") OR a Philadelphia elected /
top-cabinet official. Two courts:
- **PCCP** — reuses `PartySearchSession` + `name_matches`/`_search_window` from the
  party watch (FJD participant-name index). Same 50-row cap + date-bisection.
- **EDPA** — CourtListener `/search/?type=d&court=paed` (curl, token optional but
  raises the rate limit). Matches caption **and** the structured `party[]` array;
  civil-only via the `cv` docket-number token.

Key design points:
- **Tax liens excluded by default.** FJD "T"-division case IDs (`2607T...`) are
  Revenue Dept tax liens — ~34/day for the City alone, routine and non-newsworthy,
  and their volume blows past the 50-row/day search cap (silent truncation of real
  cases). `TAX_LIEN_RE` drops them; `--include-tax-liens` keeps them.
- **Routine case types excluded** (after enrichment): `self assessed taxes`,
  `real estate tax claim/lien`, `auction motor vehicle` — automated collection/disposal
  matters, not litigation. `EXCLUDE_CASE_TYPES` in `phila_party_alert.py`, matched on a
  normalized (alphanumeric-only) substring so punctuation/spacing variants still hit.
  This is separate from the T-division `TAX_LIEN_RE` drop (that keys on the case ID; this
  keys on the enriched Case Type field, catching tax/auction matters with a normal ID).
- **Officials matched on FULL name** (surname prefix query + both name parts required).
  A personal-name match is POSSIBLE, not confirmed — could be a namesake — so every
  officials hit is flagged "verify identity" in the email; common surnames get an
  extra "higher false-match risk" flag (`common_name: true`).
- **Email layout extras:** a top callout links to the public CCP **Civil Docket Search**
  (`CCP_SEARCH_URL` = the bare `zk_fjd_public_qry_03.zp_dktrpt_setup_idx`, which 302s to a
  fresh token — do NOT hardcode a tokened URL, the `uid`/`o` expire). Bottom of the email
  has (1) a **"What this digest leaves out"** block (data-driven from `EXCLUDE_CASE_TYPES`
  + T-division tax liens + criminal/non-civil) and (2) a **"Officials tracked (N)"** roster
  of every `phila_officials.json` label with a note to **Slack Av** to add/change anyone.
- **First run seeds silently** (per (court, label)), like the watchlist — no backlog blast.
- **State:** `data/state_phila_alert.json` (`{seen: {pccp|edpa: {label: {id: iso}}}}`),
  committed by the workflow. **Officials roster is journalist-editable** and carries
  `source` + `review` notes (L&I leadership + a few deputy-mayor seats are flagged
  "verify" — compiled July 2026, needs periodic refresh as officials change).
- **Residual limitation:** high-volume filers (City, School District/Board of Ed) can
  still exceed the 50-row cap on batch-filing days; the daily new-complaints scan is
  the comprehensive backstop for numeric CP complaints.

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
