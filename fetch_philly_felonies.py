#!/usr/bin/env python3
"""Fetch new criminal dockets filed in Philadelphia and extract charges.

Uses Playwright to automate the UJS portal search, then downloads docket
sheet PDFs and parses charges from them. Outputs an HTML dashboard.

Usage:
    python fetch_philly_felonies.py              # yesterday + today
    python fetch_philly_felonies.py 05/20/2026   # specific date
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from html import unescape

import PyPDF2
from io import BytesIO

from auth_gate import inject_auth
from playwright.sync_api import sync_playwright

BASE_URL = "https://ujsportal.pacourts.us"
OUTPUT_JSON = "philly_felonies.json"
OUTPUT_HTML = "philly_felonies_dashboard.html"

PA_FELONY_STATUTES = {
    "2501": "Criminal Homicide",
    "2502": "Murder",
    "2503": "Voluntary Manslaughter",
    "2504": "Involuntary Manslaughter",
    "2506": "Drug Delivery Resulting in Death",
    "2702": "Aggravated Assault",
    "2901": "Kidnapping",
    "3011": "Trafficking in Individuals",
    "3121": "Rape",
    "3123": "Involuntary Deviate Sexual Intercourse",
    "3124.1": "Sexual Assault",
    "3125": "Aggravated Indecent Assault",
    "3301": "Arson",
    "3502": "Burglary",
    "3701": "Robbery",
    "3921": "Theft by Unlawful Taking (felony amounts)",
    "3922": "Theft by Deception",
    "3925": "Receiving Stolen Property",
    "4302": "Incest",
    "6105": "Persons Not to Possess Firearms",
    "6106": "Firearms Not to Be Carried Without License",
    "6108": "Carrying Firearms on Public Streets in Philadelphia",
    "6110.2": "Possession of Firearm by Minor",
    "7508": "Drug Trafficking Mandatory Minimum",
    "780-113": "Drug Act violations",
}


def search_cases(page, start_date, end_date):
    """Search UJS for criminal cases filed in Philadelphia."""
    page.goto(f"{BASE_URL}/CaseSearch", wait_until="networkidle")
    page.select_option('select[title="Search By"]', label="Date Filed")
    time.sleep(1)
    # HTML date inputs need YYYY-MM-DD format
    sd = datetime.strptime(start_date, "%m/%d/%Y").strftime("%Y-%m-%d")
    ed = datetime.strptime(end_date, "%m/%d/%Y").strftime("%Y-%m-%d")
    page.fill('input[name="FiledStartDate"]', sd)
    page.fill('input[name="FiledEndDate"]', ed)

    adv = page.query_selector('input[name="AdvanceSearch"]')
    if adv:
        adv.check()
        time.sleep(1)

    page.select_option('select[title="County"]', label="Philadelphia")
    time.sleep(0.5)
    page.click("#btnSearch")
    page.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(5)

    content = page.content()

    cases = {}
    rows = page.query_selector_all("tr")
    for row in rows[1:]:
        cells = row.query_selector_all("td")
        if len(cells) < 12:
            continue
        docket = cells[2].inner_text().strip()
        if not docket.startswith("CP-51-CR-"):
            continue
        if docket in cases:
            continue

        links = row.query_selector_all('a[href*="CpDocketSheet"]')
        ds_link = ""
        if links:
            href = links[0].get_attribute("href") or ""
            ds_link = unescape(href)

        cases[docket] = {
            "docket": docket,
            "caption": cells[4].inner_text().strip(),
            "status": cells[5].inner_text().strip(),
            "filed": cells[6].inner_text().strip(),
            "participant": cells[7].inner_text().strip(),
            "dob": cells[8].inner_text().strip(),
            "otn": cells[11].inner_text().strip(),
            "ds_link": ds_link,
            "charges": [],
            "bail": "",
            "arresting_agency": "",
            "next_event": "",
        }

    return cases


def extract_charges_from_text(text):
    """Parse charges from docket sheet text."""
    charges = []
    idx = text.find("CHARGES")
    if idx < 0:
        return charges

    charges_text = text[idx:]
    disp_idx = charges_text.find("DISPOSITION")
    if disp_idx > 0:
        charges_text = charges_text[:disp_idx]

    statute_pattern = re.compile(r"(\d+)\s*§\s*([\d.]+(?:\.\d+)?)")
    lines = charges_text.split("\n")
    current_desc = ""

    for line in lines:
        line = line.strip()
        if not line or line.startswith("Seq.") or line.startswith("CHARGES"):
            continue
        match = statute_pattern.search(line)
        if match:
            title = match.group(1)
            section = match.group(2)
            desc_part = line[: match.start()].strip()
            if current_desc:
                desc_part = current_desc + " " + desc_part
            desc_part = re.sub(r"[UF]\d?\s+\d{6,}-\d.*", "", desc_part).strip()
            desc_part = re.sub(r"\s+\d+\s+\d+\s*$", "", desc_part).strip()
            grade = ""
            grade_match = re.search(r"\b(F1|F2|F3|M1|M2|M3|S|U)\b", line[:match.start()])
            if grade_match:
                grade = grade_match.group(1)
            charges.append({
                "statute": f"{title} § {section}",
                "description": desc_part,
                "grade": grade,
                "is_felony": grade.startswith("F") or section in PA_FELONY_STATUTES,
            })
            current_desc = ""
        elif line and not re.match(r"^\d", line) and "Printed:" not in line:
            current_desc = (current_desc + " " + line).strip() if current_desc else line

    return charges


def extract_bail_from_text(text):
    """Extract bail info from docket sheet."""
    idx = text.find("BAIL INFORMATION")
    if idx < 0:
        return ""
    bail_text = text[idx:idx + 500]
    amounts = re.findall(r"\$[\d,]+\.\d+", bail_text)
    bail_types = re.findall(r"(Monetary|Unsecured|ROR|Denied|Nominal)", bail_text, re.I)
    if amounts and bail_types:
        return f"{bail_types[-1]} {amounts[-1]}"
    elif amounts:
        return amounts[-1]
    return ""


def extract_next_event(text):
    """Extract next calendar event."""
    idx = text.find("CALENDAR EVENTS")
    if idx < 0:
        return ""
    cal_text = text[idx:idx + 500]
    event_match = re.search(
        r"(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2}\s*[ap]m)\s+(\S+)\s+Scheduled\s+(.*?)(?:\n|$)",
        cal_text,
    )
    if event_match:
        return f"{event_match.group(4).strip()} - {event_match.group(1)} {event_match.group(2)}"
    return ""


def extract_arresting_agency(text):
    """Extract arresting agency."""
    match = re.search(r"Arresting Agency\s*:\s*(.+?)(?:\s{2,}|Arresting Officer)", text)
    if match:
        return match.group(1).strip()
    return ""


def get_fresh_link(page, docket_number):
    """Search for a single docket number and get a fresh download link."""
    page.goto(f"{BASE_URL}/CaseSearch", wait_until="networkidle")
    page.select_option('select[title="Search By"]', label="Docket Number")
    time.sleep(1)
    page.fill('input[name="DocketNumber"]', docket_number)
    page.click("#btnSearch")
    page.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(3)

    content = page.content()
    m = re.search(
        r'href="(/Report/CpDocketSheet\?docketNumber=' + re.escape(docket_number) + r'[^"]+)"',
        content,
    )
    if m:
        return unescape(m.group(1))
    return ""


def refresh_search(page, start_date, end_date):
    """Re-run the date search to get fresh dnh tokens."""
    page.goto(f"{BASE_URL}/CaseSearch", wait_until="networkidle")
    page.select_option('select[title="Search By"]', label="Date Filed")
    time.sleep(1)
    sd = datetime.strptime(start_date, "%m/%d/%Y").strftime("%Y-%m-%d")
    ed = datetime.strptime(end_date, "%m/%d/%Y").strftime("%Y-%m-%d")
    page.fill('input[name="FiledStartDate"]', sd)
    page.fill('input[name="FiledEndDate"]', ed)
    adv = page.query_selector('input[name="AdvanceSearch"]')
    if adv:
        adv.check()
        time.sleep(1)
    page.select_option('select[title="County"]', label="Philadelphia")
    time.sleep(0.5)
    page.click("#btnSearch")
    page.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(5)

    content = page.content()
    link_map = {}
    for m in re.finditer(
        r'href="(/Report/CpDocketSheet\?docketNumber=(CP-51-CR-[^&]+)[^"]+)"', content
    ):
        docket = m.group(2)
        link_map[docket] = unescape(m.group(1))
    return link_map


def try_download(page, href, tmp_path):
    """Attempt to download a docket sheet PDF. Returns True on success."""
    with page.expect_download(timeout=30000) as dl_info:
        page.evaluate(
            """(href) => {
                const a = document.createElement('a');
                a.href = href;
                a.style.display = 'none';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            }""",
            href,
        )
    dl_info.value.save_as(tmp_path)
    return True


def download_docket_sheets(browser, cases, start_date, end_date, max_cases=None):
    """Download and parse docket sheet PDFs in batches with fresh browser contexts."""
    to_process = [c for c in cases.values() if c["ds_link"]]
    if max_cases:
        to_process = to_process[:max_cases]

    total = len(to_process)
    print(f"\nDownloading {total} docket sheets...")

    SESSION_LIMIT = 40
    COOLDOWN = 180

    for batch_start in range(0, total, SESSION_LIMIT):
        batch_end = min(batch_start + SESSION_LIMIT, total)
        batch = to_process[batch_start:batch_end]

        if batch_start > 0:
            print(f"\n  Cooling down {COOLDOWN}s before next batch...", flush=True)
            time.sleep(COOLDOWN)
            print(f"  New browser session for cases {batch_start+1}-{batch_end}...")

        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            accept_downloads=True,
        )
        page = ctx.new_page()

        page.goto(f"{BASE_URL}/CaseSearch", wait_until="networkidle")
        page.select_option('select[title="Search By"]', label="Date Filed")
        time.sleep(1)
        sd = datetime.strptime(start_date, "%m/%d/%Y").strftime("%Y-%m-%d")
        ed = datetime.strptime(end_date, "%m/%d/%Y").strftime("%Y-%m-%d")
        page.fill('input[name="FiledStartDate"]', sd)
        page.fill('input[name="FiledEndDate"]', ed)
        adv = page.query_selector('input[name="AdvanceSearch"]')
        if adv:
            adv.check()
            time.sleep(1)
        page.select_option('select[title="County"]', label="Philadelphia")
        time.sleep(0.5)
        page.click("#btnSearch")
        page.wait_for_load_state("networkidle", timeout=30000)
        time.sleep(5)

        content = page.content()
        link_map = {}
        for m in re.finditer(
            r'href="(/Report/CpDocketSheet\?docketNumber=(CP-51-CR-[^&]+)[^"]+)"', content
        ):
            link_map[m.group(2)] = unescape(m.group(1))

        for case in batch:
            if case["docket"] in link_map:
                case["ds_link"] = link_map[case["docket"]]

        for j, case in enumerate(batch):
            idx = batch_start + j
            docket = case["docket"]
            print(f"  [{idx+1}/{total}] {docket}: {case['caption'][:40]}...", end=" ", flush=True)

            os.makedirs("dockets", exist_ok=True)
            pdf_name = f"dockets/{docket}.pdf"
            tmp_path = f"/tmp/ujs_ds_{idx}.pdf"
            downloaded = False

            try:
                try_download(page, case["ds_link"], tmp_path)
                downloaded = True
            except Exception:
                print(f"retry...", end=" ", flush=True)
                fresh = get_fresh_link(page, docket)
                if fresh:
                    try:
                        try_download(page, fresh, tmp_path)
                        downloaded = True
                    except Exception:
                        pass

            if not downloaded:
                print("FAILED")
                continue

            try:
                import shutil
                shutil.copy2(tmp_path, pdf_name)
                case["pdf_path"] = pdf_name

                reader = PyPDF2.PdfReader(tmp_path)
                full_text = ""
                for pg in reader.pages:
                    full_text += pg.extract_text() or ""

                case["charges"] = extract_charges_from_text(full_text)
                case["bail"] = extract_bail_from_text(full_text)
                case["arresting_agency"] = extract_arresting_agency(full_text)
                case["next_event"] = extract_next_event(full_text)

                felony_count = sum(1 for c in case["charges"] if c["is_felony"])
                print(f"{len(case['charges'])} charges ({felony_count} felony)")
                os.remove(tmp_path)
                time.sleep(1)

            except Exception as e:
                print(f"PARSE ERROR: {e}")
                continue

        ctx.close()

    return cases


def build_dashboard(cases, start_date, end_date):
    """Generate HTML dashboard."""
    all_cases = list(cases.values())
    felony_cases = [c for c in all_cases if any(ch["is_felony"] for ch in c["charges"])]
    all_with_charges = [c for c in all_cases if c["charges"]]

    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    charge_counts = {}
    for c in all_cases:
        for ch in c["charges"]:
            key = ch["statute"]
            charge_counts[key] = charge_counts.get(key, 0) + 1
    top_charges = sorted(charge_counts.items(), key=lambda x: -x[1])[:15]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Philadelphia Felony Dashboard</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }}
    .header {{ background: #1a1a2e; color: white; padding: 20px 30px; }}
    .header h1 {{ font-size: 24px; margin-bottom: 5px; }}
    .header .meta {{ color: #aaa; font-size: 13px; }}
    .stats {{ display: flex; gap: 15px; padding: 20px 30px; flex-wrap: wrap; }}
    .stat {{ background: white; border-radius: 8px; padding: 15px 20px; min-width: 160px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .stat .number {{ font-size: 28px; font-weight: 700; color: #1a1a2e; }}
    .stat .label {{ font-size: 12px; color: #666; text-transform: uppercase; }}
    .content {{ padding: 0 30px 30px; }}
    .section {{ margin-top: 20px; }}
    .section h2 {{ font-size: 18px; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 2px solid #e0e0e0; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    th {{ background: #1a1a2e; color: white; text-align: left; padding: 10px 12px; font-size: 12px; text-transform: uppercase; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }}
    tr:hover {{ background: #f8f8ff; }}
    .felony {{ color: #c0392b; font-weight: 600; }}
    .misdemeanor {{ color: #7f8c8d; }}
    .charge-list {{ list-style: none; padding: 0; }}
    .charge-list li {{ margin-bottom: 3px; }}
    .charge-list li.felony-charge {{ color: #c0392b; font-weight: 500; }}
    a {{ color: #2980b9; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .badge {{ display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 11px; font-weight: 600; }}
    .badge-felony {{ background: #fde8e8; color: #c0392b; }}
    .badge-misd {{ background: #eef; color: #555; }}
    .top-charges {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .top-charge {{ background: white; padding: 8px 12px; border-radius: 6px; font-size: 13px; box-shadow: 0 1px 2px rgba(0,0,0,0.1); }}
    .top-charge .count {{ font-weight: 700; color: #1a1a2e; }}
    .note {{ background: #fff3cd; padding: 10px 15px; border-radius: 6px; font-size: 13px; margin-top: 15px; }}
    .filter-bar {{ padding: 15px 30px; background: white; border-bottom: 1px solid #ddd; }}
    .filter-bar input {{ padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; width: 300px; font-size: 14px; }}
</style>
</head>
<body>
<div style="position:fixed;right:0;top:50%;transform:translateY(-50%);background:#c0392b;color:white;padding:12px 8px;font-size:11px;font-weight:700;letter-spacing:1px;writing-mode:vertical-rl;text-orientation:mixed;z-index:9999;border-radius:4px 0 0 4px;box-shadow:-2px 0 8px rgba(0,0,0,0.2);">DO NOT CITE DIRECTLY — ALWAYS CHECK THE DOCKET</div>
<div style="background:#1a1a2e;padding:10px 30px;">
    <a href="index.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">Home</a>
    <a href="philly_calendar.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">Court Calendar</a>
    <a href="philly_felonies_dashboard.html" style="color:white;text-decoration:none;font-size:13px;margin-right:20px;font-weight:600;">New Felony Filings</a>
    <a href="philly_plea_calendar.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">Plea Calendar</a>
    <a href="philly_new_pleas.html" style="color:#aaa;text-decoration:none;font-size:13px;">Guilty Plea Watch</a>
</div>
<div class="header">
    <h1>Philadelphia Criminal Docket Dashboard</h1>
    <div class="meta">Cases filed {start_date} to {end_date} | Generated {now}</div>
</div>

<div class="stats">
    <div class="stat"><div class="number">{len(all_cases)}</div><div class="label">Total CP Cases</div></div>
    <div class="stat"><div class="number">{len(all_with_charges)}</div><div class="label">With Charges Parsed</div></div>
    <div class="stat"><div class="number felony">{len(felony_cases)}</div><div class="label">With Felony Charges</div></div>
    <div class="stat"><div class="number">{sum(len(c['charges']) for c in all_cases)}</div><div class="label">Total Charges</div></div>
</div>

<div class="filter-bar">
    <input type="text" id="search" placeholder="Filter by name, docket, charge..." oninput="filterTable()">
</div>

<div class="content">

<div class="section">
    <h2>Most Common Charges</h2>
    <div class="top-charges">
"""
    for statute, count in top_charges:
        html += f'        <div class="top-charge"><span class="count">{count}</span> {statute}</div>\n'

    html += """    </div>
</div>

<div class="section">
    <h2>All Cases</h2>
    <table id="cases-table">
    <thead>
    <tr>
        <th>Docket</th>
        <th>Defendant</th>
        <th>Filed</th>
        <th>Charges</th>
        <th>Bail</th>
        <th>Next Event</th>
    </tr>
    </thead>
    <tbody>
"""

    sorted_cases = sorted(all_cases, key=lambda c: (
        -sum(1 for ch in c["charges"] if ch["is_felony"]),
        c["docket"],
    ))

    for c in sorted_cases:
        pdf_path = c.get("pdf_path", "")
        docket_url = pdf_path if pdf_path else "#"
        has_felony = any(ch["is_felony"] for ch in c["charges"])
        row_class = ""

        charges_html = '<ul class="charge-list">'
        for ch in c["charges"]:
            cls = "felony-charge" if ch["is_felony"] else ""
            badge = '<span class="badge badge-felony">F</span> ' if ch["is_felony"] else ""
            charges_html += f'<li class="{cls}">{badge}{ch["description"]} ({ch["statute"]})</li>'
        if not c["charges"]:
            charges_html += "<li><em>Pending download</em></li>"
        charges_html += "</ul>"

        html += f"""    <tr class="{row_class}">
        <td><a href="{docket_url}" target="_blank">{c['docket']}</a></td>
        <td>{c['participant']}</td>
        <td>{c['filed']}</td>
        <td>{charges_html}</td>
        <td>{c['bail']}</td>
        <td>{c['next_event']}</td>
    </tr>
"""

    html += """    </tbody>
    </table>
</div>

<div class="note">
    <strong>Note:</strong> This dashboard shows Common Pleas (CP) criminal cases filed in Philadelphia.
    CP-51-CR cases are typically bound over from Municipal Court after a preliminary hearing, indicating
    the judge found probable cause. Charge grades marked "U" mean the grade was not specified in the CP docket.
    Cases are sorted with felony charges first.
</div>

</div>

<script>
function filterTable() {
    const q = document.getElementById('search').value.toLowerCase();
    const rows = document.querySelectorAll('#cases-table tbody tr');
    rows.forEach(row => {
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(q) ? '' : 'none';
    });
}
</script>
</body>
</html>"""

    return html


def main():
    if len(sys.argv) >= 2:
        start_date = sys.argv[1]
        end_date = sys.argv[2] if len(sys.argv) > 2 else start_date
    else:
        yesterday = datetime.now() - timedelta(days=1)
        today = datetime.now()
        start_date = yesterday.strftime("%m/%d/%Y")
        end_date = today.strftime("%m/%d/%Y")

    print(f"Philadelphia Criminal Docket Fetcher")
    print(f"Date range: {start_date} to {end_date}")
    print(f"{'='*50}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            accept_downloads=True,
        )
        page = ctx.new_page()

        print("Step 1: Searching UJS portal...")
        cases = search_cases(page, start_date, end_date)
        print(f"Found {len(cases)} unique CP-51-CR cases\n")
        ctx.close()

        print("Step 2: Downloading docket sheets...")
        cases = download_docket_sheets(browser, cases, start_date, end_date)

        browser.close()

    with open(OUTPUT_JSON, "w") as f:
        json.dump(list(cases.values()), f, indent=2)
    print(f"\nSaved case data to {OUTPUT_JSON}")

    html = inject_auth(build_dashboard(cases, start_date, end_date))
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"Dashboard saved to {OUTPUT_HTML}")

    felony_cases = [c for c in cases.values() if any(ch["is_felony"] for ch in c["charges"])]
    print(f"\n{'='*50}")
    print(f"SUMMARY: {len(cases)} cases, {len(felony_cases)} with felony charges")
    total_charges = sum(len(c["charges"]) for c in cases.values())
    felony_charges = sum(
        sum(1 for ch in c["charges"] if ch["is_felony"]) for c in cases.values()
    )
    print(f"Total charges: {total_charges} ({felony_charges} felony)")


if __name__ == "__main__":
    main()
