#!/usr/bin/env python3
"""Fetch 7-day criminal court calendar for Philadelphia from UJS portal.

Searches the Calendar Event search for CP Criminal and MC Criminal court
offices in Philadelphia, collects all scheduled events, and generates an
HTML dashboard grouped by day.

Usage:
    python fetch_philly_calendar.py          # next 7 days
    python fetch_philly_calendar.py 3        # next 3 days
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from html import escape

from auth_gate import inject_auth

from playwright.sync_api import sync_playwright

BASE_URL = "https://ujsportal.pacourts.us"
OUTPUT_JSON = "philly_calendar.json"
OUTPUT_HTML = "philly_calendar.html"

NEWSWORTHY_TYPES = {
    "Preliminary Hearing",
    "Preliminary Hearing Refile",
    "Certification Preliminary Hearing",
    "Arraignment Preliminary Hearing",
    "Sentencing",
    "Penalty Phase Hearing",
    "JLSWOP Resentencing",
    "Waiver Trial",
    "Jury Trial",
    "Trial",
    "Status",
    "Status Hearing",
    "Status Listing",
    "Initial Status Listing",
}


COURT_OFFICES = [
    ("CP-01-51-Crim", "Court of Common Pleas - Criminal"),
    ("MC-01-51-Crim", "Municipal Court - Criminal"),
]


def fetch_calendar_day(page, date_str, court_office_label):
    """Fetch all calendar events for a given date and court office."""
    page.goto(f"{BASE_URL}/CaseSearch", wait_until="networkidle")
    time.sleep(1)

    page.select_option('select[title="Search By"]', label="Calendar Event")
    time.sleep(1)

    page.select_option('select[title="Judicial District"]', value="01")
    time.sleep(1)

    page.select_option('select[title="Court Office"]', label=court_office_label)
    time.sleep(0.5)

    iso_date = date_str
    page.fill('input[name="CalendarEventStartDate"]', iso_date)
    page.fill('input[name="CalendarEventEndDate"]', iso_date)

    sched = page.query_selector('input[name="ScheduledEventsOnly"]')
    if sched and not sched.is_checked():
        sched.check()

    page.click("#btnSearch")
    page.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(5)

    rows = page.query_selector_all("tr")
    events = []
    for row in rows[1:]:
        cells = row.query_selector_all("td")
        if len(cells) < 18:
            continue
        texts = [c.inner_text().strip() for c in cells]

        docket = texts[2]
        if not docket.startswith(("CP-51-CR", "MC-51-CR")):
            continue

        event_type = texts[14]
        event_status = texts[15]
        if event_status != "Scheduled":
            continue

        events.append({
            "docket": docket,
            "caption": texts[4],
            "case_status": texts[5],
            "filed": texts[6],
            "participant": texts[7],
            "dob": texts[8],
            "court_office": texts[10],
            "otn": texts[11],
            "event_type": event_type,
            "event_status": event_status,
            "event_datetime": texts[16],
            "courtroom": texts[17],
            "is_newsworthy": event_type in NEWSWORTHY_TYPES,
            "is_cp": docket.startswith("CP-51-CR"),
        })

    return events


def fetch_all_days(num_days=7):
    """Fetch calendar events for the next N days."""
    all_events = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for day_offset in range(num_days):
            target = datetime.now() + timedelta(days=day_offset)
            if target.weekday() >= 5:
                continue
            date_str = target.strftime("%Y-%m-%d")
            date_display = target.strftime("%A, %B %d, %Y")
            all_events[date_str] = {"display": date_display, "events": []}

            for office_label, office_name in COURT_OFFICES:
                ctx = browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                )
                page = ctx.new_page()

                print(f"Fetching {date_display} - {office_name}...", end=" ", flush=True)
                try:
                    events = fetch_calendar_day(page, date_str, office_label)
                    all_events[date_str]["events"].extend(events)
                    print(f"{len(events)} events")
                except Exception as e:
                    print(f"ERROR: {e}")

                ctx.close()
                time.sleep(3)

        browser.close()

    return all_events


def badge_class_for(etype):
    if etype in ("Jury Trial", "Waiver Trial", "Trial"):
        return "badge-trial"
    if "Sentencing" in etype or etype == "Penalty Phase Hearing" or etype == "JLSWOP Resentencing":
        return "badge-sentencing"
    if "Preliminary Hearing" in etype:
        return "badge-prelim"
    if "Arraignment" in etype:
        return "badge-arraignment"
    if "Violation" in etype or "VOP" in etype or "Gagnon" in etype:
        return "badge-vop"
    if "Motion" in etype or "PCRA" in etype:
        return "badge-motion"
    if "Plea" in etype or "Guilty" in etype:
        return "badge-plea"
    return "badge-other"


def build_event_row(e):
    dt = e["event_datetime"]
    time_str = dt.split(" ", 1)[1] if " " in dt else dt
    etype = e["event_type"]
    bc = badge_class_for(etype)
    court_class = "cp" if e["is_cp"] else "mc"
    court_label = "CP" if e["is_cp"] else "MC"
    nw = "1" if e["is_newsworthy"] else "0"
    docket = e["docket"]
    pdf_path = f"dockets/{docket}.pdf"

    if os.path.exists(pdf_path):
        docket_cell = f'<a href="{pdf_path}" target="_blank">{escape(docket)}</a>'
    else:
        docket_cell = f'<span class="docket-num" onclick="navigator.clipboard.writeText(\'{docket}\')" title="Click to copy">{escape(docket)}</span>'

    return f"""        <tr data-newsworthy="{nw}" data-court="{court_label}" data-type="{escape(etype)}">
            <td class="time">{escape(time_str)}</td>
            <td class="room">{escape(e['courtroom'])}</td>
            <td><span class="badge {bc}">{escape(etype)}</span></td>
            <td>{docket_cell}</td>
            <td>{escape(e['caption'])}</td>
            <td class="{court_class}">{court_label}</td>
        </tr>
"""


def build_day_table(events, label_suffix=""):
    if not events:
        return '    <div class="no-events">No events scheduled</div>\n'
    html = f"""    <table class="events-table">
    <thead><tr>
        <th>Time</th>
        <th>Room</th>
        <th>Event</th>
        <th>Docket</th>
        <th>Caption</th>
        <th>Court</th>
    </tr></thead>
    <tbody>
"""
    for e in events:
        html += build_event_row(e)
    html += """    </tbody>
    </table>
"""
    return html


def build_calendar_html(all_events):
    """Generate HTML dashboard with Newsworthy / All Events tabs."""
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    # Filter to Common Pleas only for display (MC data kept in JSON)
    all_events = {
        date: {**day, "events": [e for e in day["events"] if e.get("is_cp", True)]}
        for date, day in all_events.items()
    }

    total_events = sum(len(d["events"]) for d in all_events.values())
    total_newsworthy = sum(
        sum(1 for e in d["events"] if e["is_newsworthy"])
        for d in all_events.values()
    )
    total_days = len(all_events)

    nw_by_type = {}
    for d in all_events.values():
        for e in d["events"]:
            if e["is_newsworthy"]:
                nw_by_type[e["event_type"]] = nw_by_type.get(e["event_type"], 0) + 1

    all_type_counts = {}
    for d in all_events.values():
        for e in d["events"]:
            all_type_counts[e["event_type"]] = all_type_counts.get(e["event_type"], 0) + 1

    nav_bar = """<div style="background:#1a1a2e;padding:10px 30px;">
    <a href="index.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">Home</a>
    <a href="philly_calendar.html" style="color:white;text-decoration:none;font-size:13px;margin-right:20px;font-weight:600;">Court Calendar</a>
    <a href="philly_felonies_dashboard.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">New Felony Filings</a>
    <a href="philly_plea_calendar.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">Plea Calendar</a>
    <a href="philly_new_pleas.html" style="color:#aaa;text-decoration:none;font-size:13px;">Guilty Plea Watch</a>
</div>
<div style="background:#fff3cd;color:#856404;text-align:center;padding:8px 20px;font-size:13px;font-weight:600;border-bottom:1px solid #ffc107;">DO NOT CITE DIRECTLY — ALWAYS CHECK THE DOCKET</div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Philadelphia Criminal Court Calendar</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }}
    .header {{ background: #1a1a2e; color: white; padding: 20px 30px; }}
    .header h1 {{ font-size: 24px; margin-bottom: 5px; }}
    .header .meta {{ color: #aaa; font-size: 13px; }}
    .stats {{ display: flex; gap: 15px; padding: 20px 30px; flex-wrap: wrap; }}
    .stat {{ background: white; border-radius: 8px; padding: 15px 20px; min-width: 150px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .stat .number {{ font-size: 28px; font-weight: 700; color: #1a1a2e; }}
    .stat .label {{ font-size: 12px; color: #666; text-transform: uppercase; }}
    .tab-bar {{ display: flex; padding: 0 30px; background: #e8e8e8; border-bottom: 2px solid #ccc; }}
    .tab {{ padding: 12px 24px; font-size: 15px; font-weight: 600; cursor: pointer; border: none; background: none; color: #666; border-bottom: 3px solid transparent; margin-bottom: -2px; }}
    .tab:hover {{ color: #333; }}
    .tab.active {{ color: #1a1a2e; border-bottom-color: #c0392b; background: #f5f5f5; }}
    .tab .tab-count {{ background: #c0392b; color: white; font-size: 11px; padding: 1px 7px; border-radius: 10px; margin-left: 6px; }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}
    .content {{ padding: 0 30px 30px; }}
    .controls {{ padding: 15px 30px; background: white; border-bottom: 1px solid #ddd; display: flex; gap: 15px; flex-wrap: wrap; align-items: center; }}
    .controls input[type="text"] {{ padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; width: 250px; font-size: 14px; }}
    .controls select {{ padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }}
    .day-section {{ margin-top: 25px; }}
    .day-header {{ font-size: 20px; font-weight: 700; color: #1a1a2e; padding: 10px 0; border-bottom: 3px solid #1a1a2e; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: baseline; }}
    .day-header .count {{ font-size: 14px; color: #666; font-weight: 400; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 20px; }}
    th {{ background: #2c3e50; color: white; text-align: left; padding: 10px 12px; font-size: 12px; text-transform: uppercase; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }}
    tr:hover {{ background: #f8f8ff; }}
    a {{ color: #2980b9; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; white-space: nowrap; }}
    .badge-trial {{ background: #fde8e8; color: #c0392b; }}
    .badge-sentencing {{ background: #fef3e2; color: #e67e22; }}
    .badge-prelim {{ background: #e8f0fd; color: #2c3e80; }}
    .badge-arraignment {{ background: #e8f4fd; color: #2980b9; }}
    .badge-vop {{ background: #f0e8fd; color: #8e44ad; }}
    .badge-motion {{ background: #e8fde8; color: #27ae60; }}
    .badge-plea {{ background: #fef9e7; color: #b7950b; }}
    .badge-other {{ background: #eee; color: #555; }}
    .time {{ font-weight: 600; white-space: nowrap; }}
    .room {{ white-space: nowrap; }}
    .docket-num {{ color: #2980b9; cursor: pointer; border-bottom: 1px dashed #2980b9; }}
    .docket-num:hover {{ background: #e8f4fd; }}
    .cp {{ color: #c0392b; font-weight: 500; }}
    .mc {{ color: #7f8c8d; }}
    .no-events {{ padding: 20px; text-align: center; color: #999; background: white; border-radius: 8px; }}
    .nav {{ display: flex; gap: 8px; padding: 10px 30px; background: #eee; overflow-x: auto; }}
    .nav a {{ padding: 6px 14px; background: white; border-radius: 4px; font-size: 13px; font-weight: 500; white-space: nowrap; box-shadow: 0 1px 2px rgba(0,0,0,0.1); cursor: pointer; }}
    .legend {{ padding: 10px 30px; background: #fafafa; border-bottom: 1px solid #eee; font-size: 12px; color: #666; display: flex; gap: 15px; flex-wrap: wrap; align-items: center; }}
    .legend span {{ font-weight: 600; }}
</style>
</head>
<body>
<div style="position:fixed;right:0;top:50%;transform:translateY(-50%);background:#c0392b;color:white;padding:12px 8px;font-size:11px;font-weight:700;letter-spacing:1px;writing-mode:vertical-rl;text-orientation:mixed;z-index:9999;border-radius:4px 0 0 4px;box-shadow:-2px 0 8px rgba(0,0,0,0.2);">DO NOT CITE DIRECTLY — ALWAYS CHECK THE DOCKET</div>

{nav_bar}

<div class="header">
    <h1>Philadelphia Criminal Court Calendar</h1>
    <div class="meta">Next {total_days} court days | Updated {now}</div>
</div>

<div class="stats">
    <div class="stat"><div class="number">{total_newsworthy}</div><div class="label">Newsworthy Events</div></div>
    <div class="stat"><div class="number">{nw_by_type.get('Jury Trial', 0) + nw_by_type.get('Waiver Trial', 0) + nw_by_type.get('Trial', 0)}</div><div class="label">Trials</div></div>
    <div class="stat"><div class="number">{nw_by_type.get('Sentencing', 0) + nw_by_type.get('Penalty Phase Hearing', 0) + nw_by_type.get('JLSWOP Resentencing', 0)}</div><div class="label">Sentencings</div></div>
    <div class="stat"><div class="number">{sum(v for k,v in nw_by_type.items() if 'Preliminary' in k)}</div><div class="label">Prelim Hearings</div></div>
    <div class="stat"><div class="number">{total_events}</div><div class="label">Total Events</div></div>
</div>

<div class="tab-bar">
    <button class="tab active" onclick="switchTab('newsworthy')">Newsworthy<span class="tab-count">{total_newsworthy}</span></button>
    <button class="tab" onclick="switchTab('all')">All Events<span class="tab-count">{total_events}</span></button>
</div>

<div class="legend">
    <span>Event types:</span>
    <span class="badge badge-trial">Trial</span>
    <span class="badge badge-sentencing">Sentencing</span>
    <span class="badge badge-prelim">Preliminary Hearing</span>
    <span class="badge badge-arraignment">Arraignment</span>
    <span class="badge badge-vop">VOP</span>
    <span class="badge badge-motion">Motion</span>
    <span class="badge badge-plea">Plea</span>
    <span class="badge badge-other">Other</span>
</div>

<div style="background:#f0f4ff;padding:12px 20px;margin:0 30px;border-radius:6px;font-size:13px;line-height:1.6;border-left:4px solid #2980b9;">
    <strong>What counts as "Newsworthy":</strong> Preliminary Hearings, Trials (Jury &amp; Waiver),
    Sentencings, Penalty Phase Hearings, JLSWOP Resentencings, Arraignment Preliminary Hearings,
    and Status Hearings. These are the event types most likely to produce public developments in a case.
    The "All Events" tab includes everything — motions, continuances, VOP hearings, etc.
</div>

"""

    # === NEWSWORTHY TAB ===
    html += """<div id="panel-newsworthy" class="tab-panel active">

<div class="controls">
    <input type="text" id="search-nw" placeholder="Search name, docket..." oninput="filterPanel('newsworthy')">
    <!-- Court filter hidden while MC is excluded from display -->
</div>

<div class="content">
"""

    for date_str, day_data in all_events.items():
        display = day_data["display"]
        nw_events = sorted(
            [e for e in day_data["events"] if e["is_newsworthy"]],
            key=lambda e: (e["event_datetime"], e["docket"]),
        )
        html += f"""
<div class="day-section">
    <div class="day-header">
        {display}
        <span class="count">{len(nw_events)} newsworthy events</span>
    </div>
{build_day_table(nw_events)}
</div>
"""

    html += """</div>
</div>
"""

    # === ALL EVENTS TAB ===
    html += """<div id="panel-all" class="tab-panel">

<div class="controls">
    <input type="text" id="search-all" placeholder="Search name, docket, event..." oninput="filterPanel('all')">
    <select id="typeFilter-all" onchange="filterPanel('all')">
        <option value="">All Event Types</option>
"""

    for etype, count in sorted(all_type_counts.items(), key=lambda x: -x[1]):
        html += f'        <option value="{escape(etype)}">{escape(etype)} ({count})</option>\n'

    html += """    </select>
    <!-- Court filter hidden while MC is excluded from display -->
</div>

<div class="content">
"""

    for date_str, day_data in all_events.items():
        display = day_data["display"]
        events = sorted(day_data["events"], key=lambda e: (
            0 if e["is_newsworthy"] else 1,
            e["event_datetime"],
            e["docket"],
        ))
        nw_count = sum(1 for e in events if e["is_newsworthy"])
        html += f"""
<div class="day-section">
    <div class="day-header">
        {display}
        <span class="count">{len(events)} events ({nw_count} newsworthy)</span>
    </div>
{build_day_table(events)}
</div>
"""

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
    const courtFilter = document.getElementById('courtFilter-' + panel).value;
    const typeEl = document.getElementById('typeFilter-' + panel);
    const typeFilter = typeEl ? typeEl.value : '';

    document.querySelectorAll('#panel-' + panel + ' .events-table tbody tr').forEach(row => {
        const text = row.textContent.toLowerCase();
        const court = row.dataset.court;
        const rtype = row.dataset.type;

        let show = true;
        if (q && !text.includes(q)) show = false;
        if (courtFilter && court !== courtFilter) show = false;
        if (typeFilter && rtype !== typeFilter) show = false;

        row.style.display = show ? '' : 'none';
    });
}
</script>
</body>
</html>"""

    return html


def build_index_html():
    """Generate index page linking all dashboards."""
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Av's Tools</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; min-height: 100vh; }}
    .header {{ background: #1a1a2e; color: white; padding: 40px 30px; text-align: center; }}
    .header h1 {{ font-size: 32px; margin-bottom: 8px; }}
    .header .meta {{ color: #aaa; font-size: 14px; }}
    .section-header {{ max-width: 1100px; margin: 30px auto 0; padding: 0 30px; }}
    .section-header h2 {{ font-size: 18px; color: #555; border-bottom: 2px solid #ddd; padding-bottom: 6px; }}
    .cards {{ display: flex; gap: 25px; padding: 20px 30px; max-width: 1100px; margin: 0 auto; flex-wrap: wrap; justify-content: center; }}
    .card {{ background: white; border-radius: 12px; padding: 30px; width: 320px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); transition: transform 0.15s, box-shadow 0.15s; }}
    .card:hover {{ transform: translateY(-3px); box-shadow: 0 4px 16px rgba(0,0,0,0.15); }}
    .card h2 {{ font-size: 20px; margin-bottom: 10px; }}
    .card p {{ font-size: 14px; color: #666; line-height: 1.5; margin-bottom: 20px; }}
    .card a {{ display: inline-block; padding: 10px 20px; background: #1a1a2e; color: white; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 14px; }}
    .card a:hover {{ background: #2c3e50; }}
    .card .icon {{ font-size: 36px; margin-bottom: 15px; }}
    .card-calendar {{ border-top: 4px solid #2980b9; }}
    .card-filings {{ border-top: 4px solid #c0392b; }}
    .card-pleas {{ border-top: 4px solid #8e1600; }}
    .card-plea-cal {{ border-top: 4px solid #d4a017; }}
    .card-edpa {{ border-top: 4px solid #2e86c1; }}
    .card-habeas {{ border-top: 4px solid #6c3483; }}
    .card-appellate {{ border-top: 4px solid #1e8449; }}
    .card-local {{ border-top: 4px solid #b9770e; }}
    .card-business {{ border-top: 4px solid #566573; }}
    .footer {{ text-align: center; padding: 30px; color: #999; font-size: 12px; }}
</style>
</head>
<body>
<div style="position:fixed;right:0;top:50%;transform:translateY(-50%);background:#c0392b;color:white;padding:12px 8px;font-size:11px;font-weight:700;letter-spacing:1px;writing-mode:vertical-rl;text-orientation:mixed;z-index:9999;border-radius:4px 0 0 4px;box-shadow:-2px 0 8px rgba(0,0,0,0.2);">DO NOT CITE DIRECTLY — ALWAYS CHECK THE DOCKET</div>

<div class="header">
    <h1>Av's Tools</h1>
    <div class="meta">Last updated {now}</div>
</div>

<div class="section-header"><h2>Philadelphia Criminal Courts</h2></div>
<div class="cards">
    <div class="card card-calendar">
        <div class="icon">&#x1f4c5;</div>
        <h2>Court Calendar</h2>
        <p>7-day calendar of criminal court events in Philadelphia. Preliminary hearings, trials, sentencings, and more — filtered for newsworthy events.</p>
        <a href="philly_calendar.html">View Calendar</a>
    </div>

    <div class="card card-filings">
        <div class="icon">&#x1f4cb;</div>
        <h2>New Felony Filings</h2>
        <p>Criminal dockets with felony charges filed in the past 24 hours. Includes charge details, bail, arresting agency, and next court date.</p>
        <a href="philly_felonies_dashboard.html">View Filings</a>
    </div>

    <div class="card card-plea-cal">
        <div class="icon">&#x2696;&#xfe0f;</div>
        <h2>Plea Calendar</h2>
        <p>Status conferences for murder, rape, and aggravated assault cases, plus all scheduled plea hearings. Where plea deals get discussed and finalized.</p>
        <a href="philly_plea_calendar.html">View Calendar</a>
    </div>

    <div class="card card-pleas">
        <div class="icon">&#x1f514;</div>
        <h2>Guilty Plea Watch</h2>
        <p>Daily scan of serious violent felony dockets. Flags cases where a guilty plea newly appeared since yesterday. Catches pleas from any hearing type.</p>
        <a href="philly_new_pleas.html">View Watch</a>
    </div>
</div>

<div class="section-header"><h2>Federal &mdash; Eastern District of Pennsylvania</h2></div>
<div class="cards">
    <div class="card card-edpa">
        <div class="icon">&#x1f3db;</div>
        <h2>DOJ Civil Cases</h2>
        <p>New civil cases filed by or against the U.S. government in E.D. Pa. Environmental enforcement, civil rights, fraud, and asset forfeiture actions.</p>
        <a href="civil_edpa_dashboard.html">View Cases</a>
    </div>

    <div class="card card-habeas">
        <div class="icon">&#x1f513;</div>
        <h2>Habeas Corpus</h2>
        <p>New habeas corpus petitions filed in E.D. Pa. Challenges to state and federal convictions, immigration detention, and other custody disputes.</p>
        <a href="habeas_edpa.html">View Petitions</a>
    </div>
</div>

<div class="section-header"><h2>PA &amp; Federal Appellate Courts</h2></div>
<div class="cards">
    <div class="card card-appellate">
        <div class="icon">&#x1f4c4;</div>
        <h2>New Filings</h2>
        <p>Recent filings at the PA Supreme, Superior, and Commonwealth Courts plus the Third Circuit. Ranked by precedential value and Philadelphia relevance.</p>
        <a href="https://abgutman.github.io/court-feed/index.html#filings">View Filings</a>
    </div>

    <div class="card card-appellate">
        <div class="icon">&#x1f4dc;</div>
        <h2>Opinions</h2>
        <p>Published and non-published opinions from PA appellate courts and the Third Circuit. Filtered and ranked for newsworthiness.</p>
        <a href="https://abgutman.github.io/court-feed/index.html#opinions">View Opinions</a>
    </div>

    <div class="card card-appellate">
        <div class="icon">&#x1f4c5;</div>
        <h2>Calendars</h2>
        <p>Upcoming oral argument calendars for PA appellate courts and the Third Circuit.</p>
        <a href="https://abgutman.github.io/court-feed/index.html#calendars">View Calendars</a>
    </div>
</div>

<div class="section-header"><h2>Local</h2></div>
<div class="cards">
    <div class="card card-local">
        <div class="icon">&#x1f3d8;</div>
        <h2>Lower Merion Court</h2>
        <p>Civil cases in Montgomery County CCP where a party has a Lower Merion address. New lawsuits, judgments, and hearings from the past 7 days.</p>
        <a href="montco_lm_dashboard.html">View Cases</a>
    </div>

    <div class="card card-local">
        <div class="icon">&#x1f3d8;</div>
        <h2>Greater Media Court</h2>
        <p>Civil cases in Delaware County CCP involving Media, Swarthmore, or Wallingford addresses. New filings and judgments from the past 7 days.</p>
        <a href="delco_media_dashboard.html">View Cases</a>
    </div>
</div>

<div class="section-header"><h2>Business</h2></div>
<div class="cards">
    <div class="card card-business">
        <div class="icon">&#x1f4c8;</div>
        <h2>Earnings Reports</h2>
        <p>Public companies HQ'd in the 8-county Philadelphia region. Recent quarterly earnings filings (last 90 days) and confirmed upcoming earnings calls.</p>
        <a href="earnings_dashboard.html">View reports</a>
    </div>
    <div class="card card-business">
        <div class="icon">&#x1f4f0;</div>
        <h2>Philly Business News Feed</h2>
        <p>All Yahoo Finance headlines from the last 48 hours for the 100+ public companies HQ'd in the region. Updated hourly during business hours.</p>
        <a href="news_feed.html">View feed</a>
    </div>
    <div class="card card-business">
        <img src="https://media.giphy.com/media/8nM6YNtvjuezzD7DNh/giphy.gif" style="width:48px;height:48px;border-radius:8px;object-fit:cover;">
        <h2>Bankruptcy Tracker</h2>
        <p>Chapter 11 filings from companies in the Philadelphia region, tracked across all federal courts nationwide.</p>
        <a href="bankruptcy_dashboard.html">View filings</a>
    </div>
</div>

<div class="footer">
    The Philadelphia Inquirer
</div>

</body>
</html>"""


def main():
    num_days = int(sys.argv[1]) if len(sys.argv) > 1 else 7

    print(f"Philadelphia Criminal Court Calendar")
    print(f"Fetching next {num_days} days...")
    print(f"{'='*50}\n")

    all_events = fetch_all_days(num_days)

    with open(OUTPUT_JSON, "w") as f:
        json.dump(all_events, f, indent=2)
    print(f"\nSaved data to {OUTPUT_JSON}")

    html = inject_auth(build_calendar_html(all_events))
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"Dashboard saved to {OUTPUT_HTML}")

    # Index page
    index_html = inject_auth(build_index_html())
    with open("index.html", "w") as f:
        f.write(index_html)
    print(f"Index page saved to index.html")

    total = sum(len(d["events"]) for d in all_events.values())
    newsworthy = sum(
        sum(1 for e in d["events"] if e["is_newsworthy"])
        for d in all_events.values()
    )
    print(f"\n{'='*50}")
    print(f"SUMMARY: {total} events across {len(all_events)} court days")
    print(f"Newsworthy: {newsworthy}")
    for date_str, day_data in all_events.items():
        n = len(day_data["events"])
        nw = sum(1 for e in day_data["events"] if e["is_newsworthy"])
        print(f"  {day_data['display']}: {n} events ({nw} newsworthy)")


if __name__ == "__main__":
    main()
