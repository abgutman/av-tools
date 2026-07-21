# ccp_dockets

Philadelphia Court of Common Pleas civil docket monitor. Four tools:

1. **New complaints** — daily scan of all new civil complaints filed since the last run, filtered to exclude liens, parking, MJ appeals, and other noise. Emails a table digest.
2. **Watchlist** — daily check on a list of specific case IDs. Emails change alerts when the docket is updated.
3. **Party watch** — every-15-minutes check for new cases filed **by or against** named entities (e.g. PECO, the Philadelphia Sheriff). Emails a digest of new matches. Uses the FJD participant-name index, not case-ID enumeration.
4. **Trial dispositions** — a section in the daily email (tool 1) reporting how trial-listed cases concluded — verdict/finding, settlement, discontinuance, judgment, non pros, default. Watches the specific cases on the **Trial Dates Certain** calendar; no case-ID guessing. Email-only (not a dashboard tab).

Dashboard deployed to [av-tools](https://abgutman.github.io/av-tools/ccp_dockets_dashboard.html) (password-gated), with a tab per tool.

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
| `fjd_docket.py` | Docket-report engine: session minting, fetch, parse, change detection |
| `fjd_party_search.py` | Participant-name-search engine (Tab 3): prefix search, 302-body parse, 50-row-cap detection |
| `scrape_new_complaints.py` | Tab 1: enumerate new cases, filter, write JSON, email digest (also drives the trial-dispositions section) |
| `scrape_trial_dispositions.py` | Tool 4: watch Trial Dates Certain cases, detect terminal dispositions, build the email section |
| `scrape_watchlist.py` | Tab 2: check watched cases, detect changes, email alerts |
| `scrape_name_watch.py` | Tab 3: search watched names, dedup, email new-filing digest |
| `generate_dashboard.py` | Build `dashboard.html` (3 tabs) + `dockets/<case_id>.html` replicas |
| `deploy_prep.py` | Apply password gate; produce `ccp_dockets_dashboard.html` |
| `watchlist.json` | List of `{case_id, note}` to watch (edit here to add/remove) |
| `name_watch.json` | List of watched names for Tab 3 (edit here to add/remove) |
| `ccp-dockets.yml` | Daily GitHub Actions workflow (copy to `.github/workflows/`) |
| `ccp-namewatch.yml` | Every-15-min party-watch workflow (copy to `.github/workflows/`) |

### Data files (committed to repo — Actions state persistence)

| File | Contents |
|------|---------|
| `data/state_complaints.json` | `{last_seq_by_month: {"2606": N}}` — scan pointer |
| `data/state_watchlist.json` | Per-case snapshot: sig, entry_count, full parsed docket |
| `data/complaints.json` | Full parsed dockets from latest scan (replaced each run) |
| `data/watchlist_view.json` | Summary rows for the dashboard watchlist tab |
| `data/state_trial_dispositions.json` | Tool 4 watched pool: per-case trial date, status, reported/retired flags |
| `data/dispositions_log.json` | Tool 4 rolling record (180 days) of reported dispositions — metadata only, **no order text** (PII) |

---

## Trial dispositions (Tool 4)

Universe = the **Trial Dates Certain (MJ)** calendar; every row carries a real `case_id`, so
no enumeration. A **tracked pool** records each case when it first appears (as its
non-terminal `LISTED FOR TRIAL` status), then polls its docket daily once the trial date
arrives until the case-level **Status** field turns terminal. Reported once, then retired;
unresolved cases age out after 45 days. A case already terminal the first time it's polled
is baselined silently (can't attribute it to "today").

**Why the Status field, not entry types:** a concluded docket's Status is the disposition
(`SETTLED PRIOR TO ASSGN TRL JUD`, `FINDING FOR PLAINTIFF`, `JUDGMENT ENTERED`), whereas its
newest *entry* is usually a procedural `NOTICE GIVEN UNDER RULE 236`. The map below was built
from a live sweep of all ~134 then-current trial-listed dockets (July 2026):

| Category | Case Status strings |
|----------|---------------------|
| Finding / verdict | `FINDING FOR PLAINTIFF`, `FINDING FOR DEFENDANT`, `COURT FINDING` |
| Damages assessed | `DAMAGES ASSESSED` |
| Settlement | `SETTLED PRIOR TO ASSGN TRL JUD`, `SETTLED AFTER ASSGN TRIAL JUDG`, `SETTLED BY STIPULATION`, `STIPULATION - FINAL DISPO`, `PRAEC/SETTLE DISCONTINUE END` |
| Discontinuance | `PRAECIPE TO DISCONTINUE` |
| Judgment | `JUDGMENT ENTERED`, `JUDGMENT ENTERED BY AGREEMENT`, `JUDGMENT ON COURT'S FINDING`, `JUDGMENT ON COURT'S ORDER`, `JUDGMENT-COURT ORDER FINAL DIS`, `ORDER ENTERED - FINAL DISPOS` |
| Non pros (dismissed) | `JUDGMENT OF NON PROS ENTERED` |
| Default judgment | `COURT ORDERED DEFAULT JUDGMENT`, `JUDGMENT ENTERED BY DEFAULT` |
| Transferred out | `TRANSFER TO ORPHANS COURT` |
| *(keep watching)* | `LISTED …` (all conf variants), `HELD UNDER ADVISEMENT`, `STAYED BY ORDER OF COURT`, `DEFERRED - BANKRUPTCY` |

Unknown statuses hit a conservative keyword fallback (`SETTLED`/`DISCONTINU`/`NON PROS`/
`DEFAULT`/`FINDING FOR`/`VERDICT`/`DISMISS`/`JUDGMENT`); anything still unrecognized is kept
+ logged and surfaced as "Unclassified — verify" if it ages out, so nothing is dropped
silently. **Extend `STATUS_MAP` in `scrape_trial_dispositions.py` when run logs flag a new
string.** `disposition_date` is best-effort (newest matching docket entry) — always verify
against the docket before publishing.

**Email layout:** the section is grouped by **case type** (ejectment, med mal, contracts,
etc.), not by outcome. Each disposition row shows the category label + raw status + dates,
with the order text as an italic sub-row beneath.

**Order wording — email only:** `extract_order_text()` pulls the **wording of the order on
the docket** (not the sealed PDF), e.g. "IT IS HEREBY ORDERED THAT JUDGMENT OF POSSESSION IS
HEREBY ENTERED…" or "AWARDS PLTFS DAMAGES IN THE TOTAL AMOUNT OF $75,803.76." It appears
**only in the private, Av-only email**. Scheduling / case-management orders are filtered out;
the excerpt caps at 800 chars. `data/dispositions_log.json` (the rolling 180-day record that
would feed a future dashboard tab) **deliberately omits the order text** — it can carry
addresses / lockout terms / party detail we keep out of the committed, dashboard-facing
record. The log retains disposition metadata (case type, category, raw status, dates,
parties) only.

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

## Adding names to party watch (Tab 3)

Edit `name_watch.json`. Each entry decouples the **prefix(es) sent to FJD** from a
**client-side filter**, because the FJD name search is a case-insensitive *full-string
prefix* match capped at 50 rows (so a broad word like `PHILADELPHIA` truncates, and an
entity indexed under two orderings needs two prefixes):

```json
[
  { "label": "PECO", "queries": ["PECO"], "pattern": "\\bpeco\\b" },
  { "label": "Philadelphia Sheriff",
    "queries": ["SHERIFF", "PHILADELPHIA SH"],
    "must_contain_all": ["sheriff", "philadelphia"] }
]
```

- `queries` — prefix strings POSTed to FJD; results from all are merged + deduped by case_id.
- `pattern` — a case-insensitive regex kept if it matches the party name. `\bpeco\b` keeps
  PECO / PECO ENERGY COMPANY but drops PECOLA / PECORA.
- `must_contain` — keep if the name contains **any** of these substrings.
- `must_contain_all` — keep if the name contains **all** of these substrings.

To find the right prefixes for a new name, search it manually at `fjdefile.phila.gov`
and note how the party is actually stored (orderings, "DEPT" vs "OFFICE", etc.). First
run per label seeds silently (no email); later new cases trigger a digest.

## Local usage

```bash
# Use the ccp_civil venv (has all deps) or install fresh:
pip install -r requirements.txt

# Scan new complaints (dry run — no email, no state advance):
python scrape_new_complaints.py

# Same but send email + advance state pointer:
python scrape_new_complaints.py --live

# Trial dispositions alone (dry run — no email, pool state not saved):
python scrape_trial_dispositions.py
# (the daily --live run of scrape_new_complaints.py runs this automatically)

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
