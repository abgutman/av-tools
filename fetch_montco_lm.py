#!/usr/bin/env python3
"""
Montgomery County Court of Common Pleas — Lower Merion Civil Case Tracker

Fetches new civil cases, checks each for Lower Merion zip codes in party
addresses or parcel info, and builds an HTML dashboard showing:
  - New lawsuits filed
  - New judgments
  - Upcoming hearings / scheduled events

Data source: https://courtsapp.montcopa.org/psi/v/search/case
"""

import re
import json
import os
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode, quote
import requests

try:
    from auth_gate import inject_auth
except ImportError:
    def inject_auth(html):
        return html

BASE = "https://courtsapp.montcopa.org"
STATE_FILE = "montco_lm_state.json"
OUTPUT_HTML = "montco_lm_dashboard.html"
LOOKBACK_DAYS = 7

LOWER_MERION_ZIPS = {
    "19003", "19004", "19010", "19035", "19041",
    "19066", "19072", "19083", "19085", "19096",
}
ZIP_NAMES = {
    "19003": "Ardmore",
    "19004": "Bala Cynwyd",
    "19010": "Bryn Mawr",
    "19035": "Gladwyne",
    "19041": "Haverford",
    "19066": "Merion Station",
    "19072": "Narberth",
    "19083": "Havertown",
    "19085": "Villanova",
    "19096": "Wynnewood",
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"cases": {}, "last_run": None}


def save_state(state):
    state["last_run"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def build_search_url(date_from, date_to, court="C", count=50, skip=0):
    params = {
        "Q": "", "IncludeSoundsLike": "false", "Count": str(count),
        "fromAdv": "1", "CaseNumber": "", "ParcelNumber": "",
        "CaseType": "", "DateCommencedFrom": date_from,
        "DateCommencedTo": date_to,
        "IncludeInitialFilings": "false", "IncludeInitialEFilings": "false",
        "FilingType": "", "FilingDateFrom": "", "FilingDateTo": "",
        "IncludeSubsequentFilings": "false", "IncludeSubsequentEFilings": "false",
        "Court": court, "JudgeID": "", "Attorney": "", "AttorneyID": "",
        "Grid": "true", "Sort": "DateCommenced desc",
    }
    if skip:
        params["Skip"] = str(skip)
    return f"{BASE}/psi/v/search/case?" + urlencode(params, quote_via=quote)


def fetch_case_list(session, date_from, date_to):
    """Paginate through the Grid endpoint to get all civil cases with internal IDs."""
    all_cases = []
    skip = 0
    page_size = 50

    while True:
        url = build_search_url(date_from, date_to, count=page_size, skip=skip)
        resp = session.get(url)

        rows = re.findall(
            r"href='?/psi/v/detail/Case/(\d+)'?>Select</a></td>\s*<td>(20\d{2}-\d+)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>\s*<td>([^<]*)</td>",
            resp.text
        )
        if not rows:
            break

        for row in rows:
            all_cases.append({
                "internal_id": row[0],
                "case_number": row[1],
                "commenced": row[2],
                "case_type": row[3],
                "plaintiff": row[4],
                "defendant": row[5],
            })

        print(f"  Page {skip // page_size + 1}: {len(rows)} cases (total: {len(all_cases)})")
        if len(rows) < page_size:
            break
        skip += page_size
        time.sleep(0.2)

    return all_cases


def fetch_case_detail(session, internal_id):
    """POST to the detail data API to get full case info including addresses."""
    url = f"{BASE}/psi/v/detail/Case/{internal_id}/data"
    try:
        resp = session.post(url, json={"DocketRange": "100"}, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"    Error fetching {internal_id}: {e}")
    return None


def extract_parties(data):
    """Parse party names and addresses from the Relates HTML fragments."""
    parties = []
    for html in data.get("Relates", []):
        if "Plaintiffs" in html[:300]:
            role = "Plaintiff"
        elif "Defendants" in html[:300]:
            role = "Defendant"
        else:
            continue

        for row_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row_match.group(1), re.DOTALL)
            if len(cells) < 4:
                continue

            name_idx = None
            for i, cell in enumerate(cells):
                if 'Select' in cell or 'noprint' in cell:
                    continue
                name_idx = i
                break

            if name_idx is None or name_idx + 1 >= len(cells):
                continue

            name = re.sub(r'<[^>]+>', '', cells[name_idx]).strip()
            addr_raw = cells[name_idx + 1]
            addr = re.sub(r'<br\s*/?>', ', ', addr_raw)
            addr = re.sub(r'<[^>]+>', '', addr).strip()
            addr = re.sub(r'\s+', ' ', addr)

            if name and name != 'Name':
                parties.append({"name": name, "role": role, "address": addr})
    return parties


def extract_docket_entries(data):
    """Parse docket entries from the Relates HTML to find judgments and hearings."""
    entries = []
    for html in data.get("Relates", []):
        if "Docket" not in html[:300] and "Filing" not in html[:500]:
            continue
        if "Plaintiffs" in html[:300] or "Defendants" in html[:300]:
            continue

        for row_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row_match.group(1), re.DOTALL)
            if len(cells) < 2:
                continue

            texts = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
            texts = [t for t in texts if t and t != 'Select']

            if not texts:
                continue

            date_match = re.match(r'(\d{1,2}/\d{1,2}/\d{4})', texts[0])
            if date_match:
                entry_date = date_match.group(1)
                description = ' | '.join(texts[1:])
                entries.append({"date": entry_date, "description": description})

    return entries


def extract_case_detail_header(data):
    """Parse the Detail HTML for judge, status, etc."""
    detail = data.get("Detail", "")
    info = {}

    judge_m = re.search(r'Judge[:\s]*</[^>]+>\s*([^<]+)', detail)
    if judge_m:
        info["judge"] = judge_m.group(1).strip()

    status_m = re.search(r'Status[:\s]*</[^>]+>\s*([^<]+)', detail)
    if status_m:
        info["status"] = status_m.group(1).strip()

    return info


def check_lower_merion(parties, raw_json_text):
    """Check if any party address or the raw data contains a Lower Merion zip."""
    found_zips = set()

    all_pa_zips = set(re.findall(r'PA\s+(\d{5})', raw_json_text))
    found_zips = all_pa_zips & LOWER_MERION_ZIPS

    lm_parties = []
    for p in parties:
        zip_m = re.search(r'PA\s+(\d{5})', p["address"])
        if zip_m and zip_m.group(1) in LOWER_MERION_ZIPS:
            p["lm_zip"] = zip_m.group(1)
            lm_parties.append(p)

    return found_zips, lm_parties


def classify_case(case_type, docket_entries):
    """Classify events: new filing, judgment, hearing."""
    events = []

    events.append("filing")

    judgment_keywords = ["judgment", "verdict", "order", "decree", "decision", "adjudication"]
    hearing_keywords = ["hearing", "conference", "trial", "argument", "arbitration",
                        "mediation", "call of", "scheduling", "pretrial"]

    for entry in docket_entries:
        desc_lower = entry["description"].lower()
        if any(kw in desc_lower for kw in judgment_keywords):
            events.append("judgment")
        if any(kw in desc_lower for kw in hearing_keywords):
            events.append("hearing")

    return list(set(events))


def fetch_and_filter(session, date_from, date_to, state):
    """Main pipeline: fetch cases, check LM zips, return matches."""
    print(f"\nFetching civil cases from {date_from} to {date_to}...")
    cases = fetch_case_list(session, date_from, date_to)
    print(f"  Found {len(cases)} civil cases total")

    new_cases = [c for c in cases if c["case_number"] not in state["cases"]]
    existing_cases = [c for c in cases if c["case_number"] in state["cases"]]

    print(f"  New cases to check: {len(new_cases)}")
    print(f"  Previously seen: {len(existing_cases)}")

    lm_cases = []
    checked = 0

    for case in new_cases:
        data = fetch_case_detail(session, case["internal_id"])
        checked += 1

        if not data:
            continue

        raw_text = json.dumps(data)
        parties = extract_parties(data)
        found_zips, lm_parties = check_lower_merion(parties, raw_text)

        if found_zips:
            docket_entries = extract_docket_entries(data)
            header_info = extract_case_detail_header(data)
            events = classify_case(case["case_type"], docket_entries)

            case_record = {
                **case,
                "parties": parties,
                "lm_parties": lm_parties,
                "lm_zips": list(found_zips),
                "docket_entries": docket_entries[-10:],
                "judge": header_info.get("judge", ""),
                "status": header_info.get("status", ""),
                "events": events,
                "first_seen": datetime.now().isoformat(),
                "detail_url": f"{BASE}/psi/v/detail/Case/{case['internal_id']}",
            }
            lm_cases.append(case_record)

            state["cases"][case["case_number"]] = case_record
            print(f"  [{checked}] ** LM ** {case['case_number']} ({case['case_type']})")
            print(f"       {case['plaintiff']} v. {case['defendant']}")
            for p in lm_parties:
                area = ZIP_NAMES.get(p.get("lm_zip", ""), "")
                print(f"       -> {p['role']}: {p['name']} ({area})")
        else:
            state["cases"][case["case_number"]] = {
                **case, "lm_match": False, "first_seen": datetime.now().isoformat()
            }

        if checked % 25 == 0:
            print(f"  ... checked {checked}/{len(new_cases)}, found {len(lm_cases)} LM matches")
        time.sleep(0.1)

    print(f"\n  Checked {checked} new cases, found {len(lm_cases)} Lower Merion matches")

    for cn, record in state["cases"].items():
        if record.get("lm_match") is not False and "lm_zips" in record:
            if record not in lm_cases:
                lm_cases.append(record)

    return lm_cases


def build_dashboard(lm_cases):
    """Build the HTML dashboard."""
    now = datetime.now()

    filings = []
    judgments = []
    hearings = []

    for c in lm_cases:
        if "events" not in c:
            continue
        if "filing" in c["events"]:
            filings.append(c)
        if "judgment" in c["events"]:
            judgments.append(c)
        if "hearing" in c["events"]:
            hearings.append(c)

    filings.sort(key=lambda x: x.get("commenced", ""), reverse=True)
    judgments.sort(key=lambda x: x.get("commenced", ""), reverse=True)
    hearings.sort(key=lambda x: x.get("commenced", ""), reverse=True)

    def case_row(c):
        zips = c.get("lm_zips", [])
        zip_badges = ""
        for z in sorted(zips):
            name = ZIP_NAMES.get(z, z)
            zip_badges += f'<span class="zip-badge">{name} ({z})</span> '

        lm_party_lines = ""
        for p in c.get("lm_parties", []):
            area = ZIP_NAMES.get(p.get("lm_zip", ""), "")
            lm_party_lines += f'<div class="lm-party"><strong>{p["role"]}:</strong> {esc(p["name"])} <span class="addr">{esc(p["address"])}</span></div>'

        docket_rows = ""
        for entry in c.get("docket_entries", [])[-5:]:
            docket_rows += f'<tr><td class="docket-date">{esc(entry["date"])}</td><td>{esc(entry["description"])}</td></tr>'

        docket_table = ""
        if docket_rows:
            docket_table = f'''<details><summary>Recent docket entries</summary>
            <table class="docket-table"><thead><tr><th>Date</th><th>Entry</th></tr></thead>
            <tbody>{docket_rows}</tbody></table></details>'''

        url = c.get("detail_url", "#")
        judge = c.get("judge", "")
        judge_line = f' | Judge: {esc(judge)}' if judge else ""

        return f'''<div class="case-card">
            <div class="case-header">
                <a href="{url}" target="_blank" class="case-number">{esc(c["case_number"])}</a>
                <span class="case-type">{esc(c["case_type"])}</span>
                <a href="{url}" target="_blank" class="docket-link">View Docket &rarr;</a>
            </div>
            <div class="caption">{esc(c["plaintiff"])} v. {esc(c["defendant"])}</div>
            <div class="case-meta">Filed: {esc(c["commenced"])}{judge_line}</div>
            <div class="zip-list">{zip_badges}</div>
            {lm_party_lines}
            {docket_table}
        </div>'''

    def section(title, icon, cases, section_id):
        if not cases:
            cards = '<div class="empty">No cases found in this category.</div>'
        else:
            cards = '\n'.join(case_row(c) for c in cases)
        return f'''<div class="section" id="{section_id}">
            <h2>{icon} {title} <span class="count">({len(cases)})</span></h2>
            {cards}
        </div>'''

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Lower Merion Civil Cases — Montgomery County CCP</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }}
.header {{ background: #1b3a4b; color: white; padding: 30px; text-align: center; }}
.header h1 {{ font-size: 26px; margin-bottom: 6px; }}
.header .meta {{ color: #aaa; font-size: 13px; }}
.tabs {{ display: flex; justify-content: center; gap: 10px; padding: 20px; background: #fff; border-bottom: 1px solid #ddd; flex-wrap: wrap; }}
.tab {{ padding: 8px 18px; border-radius: 20px; cursor: pointer; font-size: 14px; font-weight: 600; border: 2px solid #ddd; background: white; }}
.tab.active {{ background: #1b3a4b; color: white; border-color: #1b3a4b; }}
.tab:hover {{ border-color: #1b3a4b; }}
.content {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
.section {{ display: none; }}
.section.active {{ display: block; }}
.section h2 {{ font-size: 20px; margin-bottom: 15px; padding-bottom: 8px; border-bottom: 2px solid #1b3a4b; }}
.count {{ color: #888; font-weight: normal; font-size: 16px; }}
.case-card {{ background: white; border-radius: 8px; padding: 18px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border-left: 4px solid #1b3a4b; }}
.case-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 6px; flex-wrap: wrap; }}
.case-number {{ font-weight: 700; color: #1b3a4b; text-decoration: none; font-size: 15px; }}
.case-number:hover {{ text-decoration: underline; }}
.docket-link {{ margin-left: auto; font-size: 12px; color: white; background: #1b3a4b; padding: 4px 12px; border-radius: 4px; text-decoration: none; font-weight: 600; white-space: nowrap; }}
.docket-link:hover {{ background: #2c5f7a; }}
.case-type {{ background: #e8f0fe; color: #1a56db; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
.caption {{ font-size: 15px; margin-bottom: 4px; }}
.case-meta {{ font-size: 13px; color: #666; margin-bottom: 8px; }}
.zip-list {{ margin-bottom: 6px; }}
.zip-badge {{ display: inline-block; background: #27ae60; color: white; padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; margin: 2px 2px; }}
.lm-party {{ font-size: 13px; margin: 3px 0; padding: 4px 8px; background: #f0fdf4; border-radius: 4px; }}
.addr {{ color: #666; }}
details {{ margin-top: 8px; }}
summary {{ cursor: pointer; font-size: 13px; color: #1b3a4b; font-weight: 600; }}
.docket-table {{ width: 100%; border-collapse: collapse; margin-top: 6px; font-size: 12px; }}
.docket-table th {{ background: #f5f5f5; padding: 6px 8px; text-align: left; border-bottom: 1px solid #ddd; }}
.docket-table td {{ padding: 5px 8px; border-bottom: 1px solid #eee; }}
.docket-date {{ white-space: nowrap; color: #666; }}
.empty {{ color: #999; font-style: italic; padding: 20px; text-align: center; }}
.footer {{ text-align: center; padding: 30px; color: #999; font-size: 12px; }}
</style>
</head>
<body>

<div class="header">
    <h1>Lower Merion Civil Cases</h1>
    <div class="meta">Montgomery County Court of Common Pleas | Updated {now.strftime("%B %d, %Y at %I:%M %p")} | Last {LOOKBACK_DAYS} days</div>
</div>

<div class="tabs">
    <div class="tab active" onclick="showTab('filings')">New Lawsuits ({len(filings)})</div>
    <div class="tab" onclick="showTab('judgments')">Judgments ({len(judgments)})</div>
    <div class="tab" onclick="showTab('hearings')">Hearings ({len(hearings)})</div>
    <div class="tab" onclick="showTab('all')">All Cases ({len(lm_cases)})</div>
</div>

<div class="content">
    {section("New Lawsuits Filed", "&#x1f4c4;", filings, "filings")}
    {section("Judgments", "&#x2696;&#xfe0f;", judgments, "judgments")}
    {section("Hearings &amp; Scheduled Events", "&#x1f4c5;", hearings, "hearings")}
    {section("All Lower Merion Cases", "&#x1f4cb;", lm_cases, "all")}
</div>

<div class="footer">
    Data from Montgomery County Prothonotary | Filtered for Lower Merion Township zip codes
</div>

<script>
function showTab(id) {{
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    event.target.classList.add('active');
}}
document.getElementById('filings').classList.add('active');
</script>

</body>
</html>'''
    return html


def esc(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def main():
    daily = "--daily" in sys.argv
    lookback = 3 if daily else LOOKBACK_DAYS

    state = load_state()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/html, */*",
    })

    print("Initializing session...")
    session.get(f"{BASE}/psi/v/search/case")

    end_date = datetime.now()
    start_date = end_date - timedelta(days=lookback)
    date_from = start_date.strftime("%m/%d/%Y")
    date_to = end_date.strftime("%m/%d/%Y")

    lm_cases = fetch_and_filter(session, date_from, date_to, state)

    LIEN_TYPES = {"lien commonwealth of pa volume", "lien commonwealth of pa",
                   "municipal lien govt", "municipal lien", "lien"}
    lm_active = [c for c in lm_cases
                 if "lm_zips" in c and c.get("case_type", "").lower() not in LIEN_TYPES]
    print(f"\nTotal Lower Merion cases for dashboard: {len(lm_active)}")

    html = build_dashboard(lm_active)
    html = inject_auth(html)
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"Dashboard written to {OUTPUT_HTML}")

    save_state(state)
    print("State saved.")


if __name__ == "__main__":
    main()
