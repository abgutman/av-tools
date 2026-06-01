#!/usr/bin/env python3
"""
Delaware County Court of Common Pleas — Greater Media Civil Case Tracker

Fetches new civil cases, checks party addresses for Greater Media area,
and builds an HTML dashboard showing new lawsuits, judgments, and hearings.

Data source: https://delcopublicaccess.co.delaware.pa.us/
API: https://delcopublicaccessapi.co.delaware.pa.us/api/v1
"""

import re
import json
import os
import sys
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode
import requests

try:
    from auth_gate import inject_auth
except ImportError:
    def inject_auth(html):
        return html

API_BASE = "https://delcopublicaccessapi.co.delaware.pa.us/api/v1"
STATE_FILE = "delco_media_state.json"
OUTPUT_HTML = "delco_media_dashboard.html"
LOOKBACK_DAYS = 7

MEDIA_ZIPS = {"19063", "19065", "19081", "19086", "19091"}
ZIP_NAMES = {
    "19063": "Media",
    "19065": "Media (east)",
    "19081": "Swarthmore",
    "19086": "Wallingford",
    "19091": "Media (PO Box)",
}
MEDIA_CITIES = {"media", "swarthmore", "wallingford"}

SKIP_CASE_TYPES = {"Municipal Lien", "Lien", "Non-Reportable"}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"cases": {}, "last_run": None}


def save_state(state):
    state["last_run"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def search_cases(session, date_from, date_to, page=1):
    """Search for civil cases filed in a date range."""
    params = {
        "queryString": "true",
        "searchFields[0].searchType": "",
        "searchFields[0].operation": ">=",
        "searchFields[0].values[0]": date_from,
        "searchFields[0].indexFieldName": "filedDate",
        "searchFields[1].searchType": "",
        "searchFields[1].operation": "<=",
        "searchFields[1].values[0]": date_to,
        "searchFields[1].indexFieldName": "filedDate",
        "page": str(page),
        "pageSize": "20",
        "sortField": "filedDate",
        "sortDirection": "desc",
    }

    url = f"{API_BASE}/cases/search?{urlencode(params)}"
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=20)
            break
        except Exception as e:
            if attempt < 2:
                print(f"  Search retry {attempt + 1}: {e}")
                time.sleep(3)
            else:
                print(f"  Search failed after 3 attempts: {e}")
                return [], False, 0

    if resp.status_code != 200:
        print(f"  Search error: {resp.status_code}")
        return [], False, 0

    data = resp.json()
    items = data.get("resultItems", [])
    more = resp.headers.get("X-CTrack-Paging-MoreResults", "false") == "true"
    total = int(resp.headers.get("X-CTrack-Paging-TotalCount", "0"))

    cases = []
    for item in items:
        row = item.get("rowMap", {})
        cases.append({
            "case_id": row.get("caseID", ""),
            "case_number": row.get("caseNumber", ""),
            "title": row.get("shortTitle", ""),
            "case_type": row.get("caseType", ""),
            "classification": row.get("caseClassification", ""),
            "filed_date": row.get("filedDate", "")[:10] if row.get("filedDate") else "",
            "closed": row.get("closed", False),
        })

    return cases, more, total


def fetch_parties(session, case_id):
    """Get party details including addresses for a case."""
    url = f"{API_BASE}/cases/{case_id}/parties"
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"    Error fetching parties for {case_id}: {e}")
    return []


def check_media(parties_data):
    """Check if any party has a Media address."""
    media_parties = []

    for party_group in parties_data:
        if isinstance(party_group, dict):
            groups = [party_group]
        elif isinstance(party_group, list):
            groups = party_group
        else:
            continue

        for p in groups:
            name_info = p.get("partyName", {})
            if not name_info:
                continue
            addr_info = p.get("address", {}) or {}
            postal = (addr_info.get("postalCode") or "").strip()
            city = (addr_info.get("city") or "").strip().lower()

            is_media = any(postal.startswith(z) for z in MEDIA_ZIPS) or city in MEDIA_CITIES

            if is_media:
                display_name = name_info.get("displayName", "")
                role = name_info.get("role", "")
                formatted = addr_info.get("formattedAddress", "")
                if not formatted:
                    parts = [addr_info.get("line1", ""), addr_info.get("city", ""),
                             addr_info.get("state", ""), postal]
                    formatted = ", ".join(p for p in parts if p)

                media_parties.append({
                    "name": display_name,
                    "role": role,
                    "address": formatted.replace("\n", ", "),
                    "zip": postal,
                })

    return media_parties


def fetch_case_detail(session, case_id):
    """Get full case detail."""
    url = f"{API_BASE}/cases/{case_id}"
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"    Error fetching detail for {case_id}: {e}")
    return {}


def fetch_and_filter(session, date_from, date_to, state):
    """Main pipeline: search cases, check for Media addresses."""
    print(f"\nFetching civil cases from {date_from} to {date_to}...")

    all_cases = []
    page = 1
    total_expected = None
    while True:
        cases, more, total = search_cases(session, date_from, date_to, page)
        if not cases:
            break
        if total_expected is None:
            total_expected = total
        all_cases.extend(cases)
        print(f"  Page {page}: {len(cases)} cases (total so far: {len(all_cases)}/{total_expected})")
        if len(all_cases) >= total_expected:
            break
        page += 1
        time.sleep(0.5)

    all_cases = [c for c in all_cases if c["case_type"] not in SKIP_CASE_TYPES]
    print(f"  After filtering liens: {len(all_cases)} cases")

    new_cases = [c for c in all_cases if c["case_number"] not in state["cases"]]
    print(f"  New cases to check: {len(new_cases)}")

    media_cases = []
    checked = 0

    for case in new_cases:
        parties_data = fetch_parties(session, case["case_id"])
        checked += 1

        media_parties = check_media(parties_data)

        if media_parties:
            detail = fetch_case_detail(session, case["case_id"])

            judge = ""
            status = ""
            if detail:
                judge = detail.get("judicialOfficer", {}).get("displayName", "") if detail.get("judicialOfficer") else ""
                status = detail.get("caseStatus", "")

            case_record = {
                **case,
                "media_parties": media_parties,
                "judge": judge,
                "status": status,
                "first_seen": datetime.now().isoformat(),
                "detail_url": f"https://delcopublicaccess.co.delaware.pa.us/#/cases/{case['case_id']}",
            }
            media_cases.append(case_record)
            state["cases"][case["case_number"]] = case_record

            print(f"  [{checked}] ** MEDIA ** {case['case_number']} ({case['case_type']})")
            print(f"       {case['title']}")
            for p in media_parties:
                print(f"       -> {p['role']}: {p['name']} ({p['address']})")
        else:
            state["cases"][case["case_number"]] = {
                **case, "media_match": False, "first_seen": datetime.now().isoformat()
            }

        if checked % 25 == 0:
            print(f"  ... checked {checked}/{len(new_cases)}, found {len(media_cases)} Media matches")
        time.sleep(0.3)

    print(f"\n  Checked {checked} new cases, found {len(media_cases)} Media matches")

    for cn, record in state["cases"].items():
        if record.get("media_match") is not False and "media_parties" in record:
            if record not in media_cases:
                media_cases.append(record)

    return media_cases


def esc(text):
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


FORECLOSURE_KEYWORDS = {"mortgage foreclosure", "foreclosure", "mortgage", "ejectment"}


def is_foreclosure(case):
    ct = (case.get("case_type") or "").lower()
    cl = (case.get("classification") or "").lower()
    return any(kw in ct or kw in cl for kw in FORECLOSURE_KEYWORDS)


def build_dashboard(media_cases):
    """Build the HTML dashboard with tabs."""
    now = datetime.now()

    foreclosures = [c for c in media_cases if is_foreclosure(c)]
    other = [c for c in media_cases if not is_foreclosure(c)]

    for lst in (media_cases, foreclosures, other):
        lst.sort(key=lambda x: x.get("filed_date", ""), reverse=True)

    def case_row(c):
        party_lines = ""
        for p in c.get("media_parties", []):
            party_lines += f'<div class="media-party"><strong>{esc(p["role"])}:</strong> {esc(p["name"])} <span class="addr">{esc(p["address"])}</span></div>'

        url = c.get("detail_url", "#")
        judge = c.get("judge", "")
        judge_line = f' | Judge: {esc(judge)}' if judge else ""
        status = c.get("status", "")
        status_line = f' | Status: {esc(status)}' if status else ""
        classification = c.get("classification", "")

        return f'''<div class="case-card">
            <div class="case-header">
                <a href="{url}" target="_blank" class="case-number">{esc(c["case_number"])}</a>
                <span class="case-type">{esc(c["case_type"])}</span>
                <a href="{url}" target="_blank" class="docket-link">View Docket &rarr;</a>
            </div>
            <div class="caption">{esc(c["title"])}</div>
            <div class="case-meta">Filed: {esc(c["filed_date"])}{judge_line}{status_line}</div>
            {f'<div class="classification">{esc(classification)}</div>' if classification else ''}
            {party_lines}
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
<title>Greater Media Civil Cases — Delaware County CCP</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }}
.header {{ background: #2c3e50; color: white; padding: 30px; text-align: center; }}
.header h1 {{ font-size: 26px; margin-bottom: 6px; }}
.header .meta {{ color: #aaa; font-size: 13px; }}
.tabs {{ display: flex; justify-content: center; gap: 10px; padding: 20px; background: #fff; border-bottom: 1px solid #ddd; flex-wrap: wrap; }}
.tab {{ padding: 8px 18px; border-radius: 20px; cursor: pointer; font-size: 14px; font-weight: 600; border: 2px solid #ddd; background: white; }}
.tab.active {{ background: #2c3e50; color: white; border-color: #2c3e50; }}
.tab:hover {{ border-color: #2c3e50; }}
.content {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
.section {{ display: none; }}
.section.active {{ display: block; }}
.section h2 {{ font-size: 20px; margin-bottom: 15px; padding-bottom: 8px; border-bottom: 2px solid #2c3e50; }}
.count {{ color: #888; font-weight: normal; font-size: 16px; }}
.case-card {{ background: white; border-radius: 8px; padding: 18px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border-left: 4px solid #2c3e50; }}
.case-header {{ display: flex; align-items: center; gap: 12px; margin-bottom: 6px; flex-wrap: wrap; }}
.case-number {{ font-weight: 700; color: #2c3e50; text-decoration: none; font-size: 15px; }}
.case-number:hover {{ text-decoration: underline; }}
.docket-link {{ margin-left: auto; font-size: 12px; color: white; background: #2c3e50; padding: 4px 12px; border-radius: 4px; text-decoration: none; font-weight: 600; white-space: nowrap; }}
.docket-link:hover {{ background: #3d556e; }}
.case-type {{ background: #e8f0fe; color: #1a56db; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
.caption {{ font-size: 15px; margin-bottom: 4px; }}
.case-meta {{ font-size: 13px; color: #666; margin-bottom: 8px; }}
.classification {{ font-size: 12px; color: #888; margin-bottom: 6px; font-style: italic; }}
.media-party {{ font-size: 13px; margin: 3px 0; padding: 4px 8px; background: #f0fdf4; border-radius: 4px; }}
.addr {{ color: #666; }}
.empty {{ color: #999; font-style: italic; padding: 20px; text-align: center; }}
.footer {{ text-align: center; padding: 30px; color: #999; font-size: 12px; }}
</style>
</head>
<body>

<div class="header">
    <h1>Greater Media Civil Cases</h1>
    <div class="meta">Delaware County Court of Common Pleas | Updated {now.strftime("%B %d, %Y at %I:%M %p")} | Last {LOOKBACK_DAYS} days</div>
</div>

<div class="tabs">
    <div class="tab active" onclick="showTab('filings')">New Filings ({len(other)})</div>
    <div class="tab" onclick="showTab('foreclosures')">Foreclosures ({len(foreclosures)})</div>
    <div class="tab" onclick="showTab('all')">All Cases ({len(media_cases)})</div>
</div>

<div class="content">
    {section("New Filings", "&#x1f4c4;", other, "filings")}
    {section("Foreclosures", "&#x1f3e0;", foreclosures, "foreclosures")}
    {section("All Greater Media Cases", "&#x1f4cb;", media_cases, "all")}
</div>

<div class="footer">
    Data from Delaware County C-Track Public Access | Filtered for Greater Media area (19063, 19065, 19081, 19086, 19091)
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


def main():
    daily = "--daily" in sys.argv
    lookback = 3 if daily else LOOKBACK_DAYS

    state = load_state()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
    })

    end_date = datetime.now()
    start_date = end_date - timedelta(days=lookback)
    date_from = start_date.strftime("%Y-%m-%d")
    date_to = end_date.strftime("%Y-%m-%d")

    media_cases = fetch_and_filter(session, date_from, date_to, state)

    active = [c for c in media_cases if "media_parties" in c]
    print(f"\nTotal Media cases for dashboard: {len(active)}")

    html = build_dashboard(active)
    html = inject_auth(html)
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"Dashboard written to {OUTPUT_HTML}")

    save_state(state)
    print("State saved.")


if __name__ == "__main__":
    main()
