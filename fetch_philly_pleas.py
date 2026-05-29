#!/usr/bin/env python3
"""Build plea calendar: status conferences for serious violent felonies + all plea hearings.

Reads philly_calendar.json (produced by fetch_philly_calendar.py), downloads
docket sheet PDFs for relevant CP cases, parses charges, and generates
philly_plea_calendar.html.

Target offenses for status conference filtering:
  - Murder / Homicide (18 § 2501-2504)
  - Attempted Murder (18 § 901 + homicide)
  - Rape (18 § 3121)
  - Aggravated Assault (18 § 2702)

Usage:
    python fetch_philly_pleas.py          # build from calendar data
    python fetch_philly_pleas.py --skip-download   # use existing PDFs only
"""

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from html import escape, unescape

import PyPDF2

from auth_gate import inject_auth
from playwright.sync_api import sync_playwright

BASE_URL = "https://ujsportal.pacourts.us"
CALENDAR_JSON = "philly_calendar.json"
OUTPUT_HTML = "philly_plea_calendar.html"

TARGET_SECTIONS = {
    "2501", "2502", "2503", "2504",
    "2702", "2718", "2901",
    "3121", "3123", "3124.1", "3125",
    "6312",
}

TARGET_DESCRIPTIONS = [
    re.compile(r"Murder", re.IGNORECASE),
    re.compile(r"Homicide", re.IGNORECASE),
    re.compile(r"Voluntary Manslaughter", re.IGNORECASE),
    re.compile(r"Involuntary Manslaughter", re.IGNORECASE),
    re.compile(r"Aggravated Assault", re.IGNORECASE),
    re.compile(r"\bRape\b", re.IGNORECASE),
    re.compile(r"Robbery.+Serious Bodily", re.IGNORECASE),
    re.compile(r"Strangulation", re.IGNORECASE),
    re.compile(r"Child Pornography", re.IGNORECASE),
    re.compile(r"\bIDSI\b", re.IGNORECASE),
    re.compile(r"Involuntary Deviate Sexual", re.IGNORECASE),
    re.compile(r"Aggravated Indecent Assault", re.IGNORECASE),
    re.compile(r"Kidnapping", re.IGNORECASE),
]

STATUS_EVENT_TYPES = {"Status", "Status Listing", "Status Hearing", "Initial Status Listing"}

PLEA_EVENT_TYPES = {"Plea", "Video-Plea", "MHC-Neg Plea", "Neg Plea", "Guilty Plea"}


def extract_charges(text):
    """Parse charges from docket sheet text. Returns list of {statute, description, grade}."""
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
            desc_part = line[:match.start()].strip()
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
                "section": section,
            })
            current_desc = ""
        elif line and not re.match(r"^\d", line) and "Printed:" not in line:
            current_desc = (current_desc + " " + line).strip() if current_desc else line

    return charges


def has_target_charge(charges):
    """Check if any charge matches target offenses."""
    for ch in charges:
        section = ch.get("section", "")
        if section in TARGET_SECTIONS:
            return True
        desc = ch.get("description", "")
        for pat in TARGET_DESCRIPTIONS:
            if pat.search(desc):
                return True
    return False


def get_target_charges(charges):
    """Return only the target-offense charges."""
    result = []
    for ch in charges:
        section = ch.get("section", "")
        desc = ch.get("description", "")
        if section in TARGET_SECTIONS:
            result.append(ch)
        else:
            for pat in TARGET_DESCRIPTIONS:
                if pat.search(desc):
                    result.append(ch)
                    break
    return result


def try_download(page, href, tmp_path):
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


def get_fresh_link(page, docket_number):
    """Search for a single docket and get a fresh download link."""
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


def download_docket_pdfs(dockets_to_download):
    """Download docket PDFs in batches. Returns {docket: pdf_path}."""
    os.makedirs("dockets", exist_ok=True)
    results = {}
    to_process = list(dockets_to_download)
    total = len(to_process)

    SESSION_LIMIT = 40
    COOLDOWN = 180

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for batch_start in range(0, total, SESSION_LIMIT):
            batch_end = min(batch_start + SESSION_LIMIT, total)
            batch = to_process[batch_start:batch_end]

            if batch_start > 0:
                print(f"\n  Cooling down {COOLDOWN}s before next batch...", flush=True)
                time.sleep(COOLDOWN)

            print(f"\n  Session for dockets {batch_start+1}-{batch_end}...", flush=True)
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                accept_downloads=True,
            )
            page = ctx.new_page()

            for j, docket in enumerate(batch):
                idx = batch_start + j
                pdf_path = f"dockets/{docket}.pdf"

                if os.path.exists(pdf_path):
                    results[docket] = pdf_path
                    print(f"  [{idx+1}/{total}] {docket}: cached", flush=True)
                    continue

                print(f"  [{idx+1}/{total}] {docket}...", end=" ", flush=True)

                link = get_fresh_link(page, docket)
                if not link:
                    print("no link")
                    continue

                tmp_path = f"/tmp/plea_dl_{idx}.pdf"
                try:
                    try_download(page, link, tmp_path)
                    shutil.copy2(tmp_path, pdf_path)
                    os.remove(tmp_path)
                    results[docket] = pdf_path
                    print("OK")
                    time.sleep(1)
                except Exception as e:
                    print(f"FAILED: {e}")

            ctx.close()

        browser.close()

    return results


def parse_charges_from_pdfs(pdf_map):
    """Parse charges from downloaded PDFs. Returns {docket: [charges]}."""
    charges_map = {}
    for docket, pdf_path in pdf_map.items():
        try:
            reader = PyPDF2.PdfReader(pdf_path)
            text = ""
            for pg in reader.pages:
                text += pg.extract_text() or ""
            charges_map[docket] = extract_charges(text)
        except Exception:
            charges_map[docket] = []
    return charges_map


def build_plea_calendar_html(status_events, plea_events, charges_map, pdf_map):
    """Build the plea calendar HTML page."""
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    target_status = []
    for e in status_events:
        docket = e["docket"]
        charges = charges_map.get(docket, [])
        if has_target_charge(charges):
            e["_target_charges"] = get_target_charges(charges)
            target_status.append(e)

    by_date_status = {}
    for e in target_status:
        dt = e["event_datetime"].split(" ")[0]
        by_date_status.setdefault(dt, []).append(e)

    by_date_pleas = {}
    for e in plea_events:
        dt = e["event_datetime"].split(" ")[0]
        by_date_pleas.setdefault(dt, []).append(e)

    all_dates = sorted(set(list(by_date_status.keys()) + list(by_date_pleas.keys())))

    nav_bar = """<div style="background:#1a1a2e;padding:10px 30px;">
    <a href="index.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">Home</a>
    <a href="philly_calendar.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">Court Calendar</a>
    <a href="philly_felonies_dashboard.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">New Felony Filings</a>
    <a href="philly_plea_calendar.html" style="color:white;text-decoration:none;font-size:13px;margin-right:20px;font-weight:600;">Plea Calendar</a>
    <a href="philly_new_pleas.html" style="color:#aaa;text-decoration:none;font-size:13px;">Guilty Plea Watch</a>
</div>
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Plea Calendar — Philadelphia Courts</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }}
    .header {{ background: #8e1600; color: white; padding: 20px 30px; }}
    .header h1 {{ font-size: 24px; margin-bottom: 5px; }}
    .header .meta {{ color: #ddd; font-size: 13px; }}
    .stats {{ display: flex; gap: 15px; padding: 20px 30px; flex-wrap: wrap; }}
    .stat {{ background: white; border-radius: 8px; padding: 15px 20px; min-width: 150px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .stat .number {{ font-size: 28px; font-weight: 700; color: #8e1600; }}
    .stat .label {{ font-size: 12px; color: #666; text-transform: uppercase; }}
    .explainer {{ background: #fff3cd; padding: 15px 20px; border-radius: 8px; margin: 0 30px; font-size: 14px; line-height: 1.6; }}
    .explainer strong {{ color: #8e1600; }}
    .tab-bar {{ display: flex; padding: 0 30px; background: #e8e8e8; border-bottom: 2px solid #ccc; margin-top: 20px; }}
    .tab {{ padding: 12px 24px; font-size: 15px; font-weight: 600; cursor: pointer; border: none; background: none; color: #666; border-bottom: 3px solid transparent; margin-bottom: -2px; }}
    .tab:hover {{ color: #333; }}
    .tab.active {{ color: #1a1a2e; border-bottom-color: #8e1600; background: #f5f5f5; }}
    .tab .tab-count {{ background: #8e1600; color: white; font-size: 11px; padding: 1px 7px; border-radius: 10px; margin-left: 6px; }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}
    .content {{ padding: 0 30px 30px; }}
    .controls {{ padding: 15px 30px; display: flex; gap: 15px; }}
    .controls input {{ padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; width: 300px; font-size: 14px; }}
    .day-section {{ margin-top: 25px; }}
    .day-header {{ font-size: 20px; font-weight: 700; color: #1a1a2e; padding: 10px 0; border-bottom: 3px solid #8e1600; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: baseline; }}
    .day-header .count {{ font-size: 14px; color: #666; font-weight: 400; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px; }}
    th {{ background: #8e1600; color: white; text-align: left; padding: 10px 12px; font-size: 12px; text-transform: uppercase; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }}
    tr:hover {{ background: #fff8f5; }}
    a {{ color: #2980b9; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .charge-tag {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; margin: 1px 2px; }}
    .charge-murder {{ background: #2c0000; color: white; }}
    .charge-assault {{ background: #fde8e8; color: #8e1600; }}
    .charge-rape {{ background: #4a0030; color: white; }}
    .badge-plea {{ background: #fef9e7; color: #b7950b; display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; }}
    .badge-status {{ background: #e8f0fd; color: #2c3e80; display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; }}
    .no-events {{ padding: 20px; text-align: center; color: #999; background: white; border-radius: 8px; }}
    .docket-num {{ color: #2980b9; cursor: pointer; border-bottom: 1px dashed #2980b9; }}
    .docket-num:hover {{ background: #e8f4fd; }}
</style>
</head>
<body>
<div style="position:fixed;right:0;top:50%;transform:translateY(-50%);background:#c0392b;color:white;padding:12px 8px;font-size:11px;font-weight:700;letter-spacing:1px;writing-mode:vertical-rl;text-orientation:mixed;z-index:9999;border-radius:4px 0 0 4px;box-shadow:-2px 0 8px rgba(0,0,0,0.2);">DO NOT CITE DIRECTLY — ALWAYS CHECK THE DOCKET</div>

{nav_bar}

<div class="header">
    <h1>Plea Calendar</h1>
    <div class="meta">Updated {now}</div>
</div>

<div class="stats">
    <div class="stat"><div class="number">{len(target_status)}</div><div class="label">Target Offense Status Hearings</div></div>
    <div class="stat"><div class="number">{len(plea_events)}</div><div class="label">Scheduled Plea Hearings</div></div>
    <div class="stat"><div class="number">{len(all_dates)}</div><div class="label">Court Days</div></div>
</div>

<div class="explainer">
    <strong>What this page shows:</strong><br>
    <strong>Tab 1 — Status Conferences:</strong> Upcoming status hearings for cases charged with
    murder (all killing offenses: 18 &sect; 2501-2504), attempted murder, rape (18 &sect; 3121),
    or aggravated assault (18 &sect; 2702). These are the hearings where plea deals are often discussed.<br>
    <strong>Tab 2 — Plea Hearings:</strong> All scheduled guilty plea hearings regardless of charge type.
    These are cases where a plea is formally expected to be entered.
</div>

<div class="tab-bar">
    <button class="tab active" onclick="switchTab('status')">Status — Serious Felonies<span class="tab-count">{len(target_status)}</span></button>
    <button class="tab" onclick="switchTab('pleas')">Plea Hearings<span class="tab-count">{len(plea_events)}</span></button>
</div>
"""

    # === STATUS CONFERENCES TAB ===
    html += """<div id="panel-status" class="tab-panel active">
<div class="controls">
    <input type="text" id="search-status" placeholder="Search name, docket, charge..." oninput="filterPanel('status')">
</div>
<div class="content">
"""

    for date_key in all_dates:
        events = by_date_status.get(date_key, [])
        if not events:
            continue
        events.sort(key=lambda e: (e["event_datetime"], e["docket"]))

        try:
            display = datetime.strptime(date_key, "%m/%d/%Y").strftime("%A, %B %d, %Y")
        except ValueError:
            display = date_key

        html += f"""
<div class="day-section">
    <div class="day-header">{display}<span class="count">{len(events)} events</span></div>
    <table><thead><tr>
        <th>Time</th><th>Room</th><th>Docket</th><th>Defendant</th><th>Target Charges</th>
    </tr></thead><tbody>
"""
        for e in events:
            dt = e["event_datetime"]
            time_str = dt.split(" ", 1)[1] if " " in dt else dt
            docket = e["docket"]
            pdf_path = pdf_map.get(docket, "")

            if pdf_path:
                docket_cell = f'<a href="{escape(pdf_path)}" target="_blank">{escape(docket)}</a>'
            else:
                docket_cell = f'<span class="docket-num" onclick="navigator.clipboard.writeText(\'{docket}\')" title="Click to copy">{escape(docket)}</span>'

            charges_html = ""
            for ch in e.get("_target_charges", []):
                section = ch.get("section", "")
                desc = ch.get("description", ch.get("statute", ""))
                if section in ("2501", "2502", "2503", "2504"):
                    css = "charge-murder"
                elif section == "3121":
                    css = "charge-rape"
                else:
                    css = "charge-assault"
                charges_html += f'<span class="charge-tag {css}">{escape(desc[:60])}</span> '

            html += f"""        <tr>
            <td style="font-weight:600;white-space:nowrap">{escape(time_str)}</td>
            <td style="white-space:nowrap">{escape(e.get('courtroom', ''))}</td>
            <td>{docket_cell}</td>
            <td>{escape(e.get('caption', ''))}</td>
            <td>{charges_html}</td>
        </tr>
"""
        html += "    </tbody></table>\n</div>\n"

    if not any(by_date_status.get(d) for d in all_dates):
        html += '<div class="no-events">No status conferences for target offenses found on the calendar.</div>\n'

    html += """</div>
</div>
"""

    # === PLEA HEARINGS TAB ===
    html += """<div id="panel-pleas" class="tab-panel">
<div class="controls">
    <input type="text" id="search-pleas" placeholder="Search name, docket..." oninput="filterPanel('pleas')">
</div>
<div class="content">
"""

    for date_key in all_dates:
        events = by_date_pleas.get(date_key, [])
        if not events:
            continue
        events.sort(key=lambda e: (e["event_datetime"], e["docket"]))

        try:
            display = datetime.strptime(date_key, "%m/%d/%Y").strftime("%A, %B %d, %Y")
        except ValueError:
            display = date_key

        html += f"""
<div class="day-section">
    <div class="day-header">{display}<span class="count">{len(events)} events</span></div>
    <table><thead><tr>
        <th>Time</th><th>Room</th><th>Event</th><th>Docket</th><th>Defendant</th><th>Court</th>
    </tr></thead><tbody>
"""
        for e in events:
            dt = e["event_datetime"]
            time_str = dt.split(" ", 1)[1] if " " in dt else dt
            docket = e["docket"]
            pdf_path = pdf_map.get(docket, "")
            court_label = "CP" if e.get("is_cp") else "MC"

            if pdf_path:
                docket_cell = f'<a href="{escape(pdf_path)}" target="_blank">{escape(docket)}</a>'
            else:
                docket_cell = f'<span class="docket-num" onclick="navigator.clipboard.writeText(\'{docket}\')" title="Click to copy">{escape(docket)}</span>'

            html += f"""        <tr>
            <td style="font-weight:600;white-space:nowrap">{escape(time_str)}</td>
            <td style="white-space:nowrap">{escape(e.get('courtroom', ''))}</td>
            <td><span class="badge-plea">{escape(e['event_type'])}</span></td>
            <td>{docket_cell}</td>
            <td>{escape(e.get('caption', ''))}</td>
            <td>{court_label}</td>
        </tr>
"""
        html += "    </tbody></table>\n</div>\n"

    if not any(by_date_pleas.get(d) for d in all_dates):
        html += '<div class="no-events">No plea hearings found on the calendar.</div>\n'

    html += """</div>
</div>

<script>
function switchTab(tab) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    event.target.closest('.tab').classList.add('active');
    document.getElementById('panel-' + tab).classList.add('active');
}

function filterPanel(panel) {
    const q = document.getElementById('search-' + panel).value.toLowerCase();
    document.querySelectorAll('#panel-' + panel + ' table tbody tr').forEach(row => {
        row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
}
</script>
</body>
</html>"""

    return html


def main():
    skip_download = "--skip-download" in sys.argv

    if not os.path.exists(CALENDAR_JSON):
        print(f"ERROR: {CALENDAR_JSON} not found. Run fetch_philly_calendar.py first.")
        sys.exit(1)

    with open(CALENDAR_JSON) as f:
        calendar_data = json.load(f)

    all_events = []
    for date_str, day_data in calendar_data.items():
        all_events.extend(day_data["events"])

    status_events = [
        e for e in all_events
        if e["event_type"] in STATUS_EVENT_TYPES and e["docket"].startswith("CP-51-CR")
    ]
    plea_events = [
        e for e in all_events
        if e["event_type"] in PLEA_EVENT_TYPES
    ]

    status_dockets = set(e["docket"] for e in status_events)
    plea_cp_dockets = set(e["docket"] for e in plea_events if e["docket"].startswith("CP-51-CR"))
    all_cp_dockets = status_dockets | plea_cp_dockets

    print(f"Calendar: {len(all_events)} total events")
    print(f"  {len(status_events)} status conference events ({len(status_dockets)} unique CP dockets)")
    print(f"  {len(plea_events)} plea hearing events ({len(plea_cp_dockets)} CP dockets)")
    print(f"  {len(all_cp_dockets)} unique CP dockets to process")

    already_have = set()
    for d in all_cp_dockets:
        if os.path.exists(f"dockets/{d}.pdf"):
            already_have.add(d)
    need_download = all_cp_dockets - already_have
    print(f"  {len(already_have)} already have PDFs, {len(need_download)} to download")

    pdf_map = {d: f"dockets/{d}.pdf" for d in already_have}

    if need_download and not skip_download:
        print(f"\nDownloading {len(need_download)} docket PDFs...")
        new_pdfs = download_docket_pdfs(need_download)
        pdf_map.update(new_pdfs)
        print(f"  Downloaded {len(new_pdfs)} PDFs")
    elif need_download:
        print(f"  Skipping download (--skip-download)")

    print(f"\nParsing charges from {len(pdf_map)} PDFs...")
    charges_map = parse_charges_from_pdfs(pdf_map)

    target_count = sum(1 for d in status_dockets if has_target_charge(charges_map.get(d, [])))
    print(f"  {target_count} status dockets have target charges")

    print(f"\nBuilding plea calendar HTML...")
    html = inject_auth(build_plea_calendar_html(status_events, plea_events, charges_map, pdf_map))
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"Saved to {OUTPUT_HTML}")

    print(f"\n{'='*50}")
    print(f"SUMMARY:")
    print(f"  Status conferences with target offenses: {target_count}")
    print(f"  Plea hearings: {len(plea_events)}")
    print(f"  PDFs downloaded: {len(pdf_map)}")


if __name__ == "__main__":
    main()
