#!/usr/bin/env python3
"""Generate per-case detail HTML pages from docket entry data."""
import json, html as html_mod
from pathlib import Path
from datetime import datetime, timezone, timedelta

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from auth_gate import inject_auth

ET = timezone(timedelta(hours=-4))

HERE = Path(__file__).parent
BD = HERE / "bankruptcy_data"
ENTRIES_DIR = BD / "docket_entries"
CASES_FILE = BD / "cases.json"
OUT_DIR = HERE / "bankruptcy_cases"

def esc(s):
    return html_mod.escape(str(s) if s is not None else "")

def fmt_date(iso):
    if not iso:
        return ""
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return d.strftime("%b %-d, %Y")
    except Exception:
        return iso[:10]

CATEGORY_COLORS = {
    "petition":         "#8e1600",
    "declaration":      "#5b2c6f",
    "affidavit":        "#5b2c6f",
    "opinion":          "#1a5276",
    "dip_financing":    "#7d6608",
    "first_day":        "#1e8449",
    "bid_sale":         "#a04000",
    "plan":             "#1a5276",
    "confirmation":     "#0b5345",
    "bar_date":         "#6c3483",
    "341_meeting":      "#117a65",
    "examiner_trustee": "#922b21",
}

CATEGORY_LABELS = {
    "petition":         "Petition",
    "declaration":      "Declaration",
    "affidavit":        "Affidavit",
    "opinion":          "Opinion",
    "dip_financing":    "DIP Financing",
    "first_day":        "First Day",
    "bid_sale":         "Bid / Sale",
    "plan":             "Plan",
    "confirmation":     "Confirmation",
    "bar_date":         "Bar Date",
    "341_meeting":      "341 Meeting",
    "examiner_trustee": "Examiner/Trustee",
}

STATUS_BADGES = {
    "happened":  '<span style="display:inline-block;background:#1e8449;color:white;font-size:10px;font-weight:700;padding:2px 8px;border-radius:3px;">HAPPENED</span>',
    "scheduled": '<span style="display:inline-block;background:#2874a6;color:white;font-size:10px;font-weight:700;padding:2px 8px;border-radius:3px;">SCHEDULED</span>',
    "unknown":   '<span style="display:inline-block;background:#aab7b8;color:white;font-size:10px;font-weight:700;padding:2px 8px;border-radius:3px;">UNKNOWN</span>',
}

KEY_DATE_LABELS = [
    ("petition_date",     "Petition Filed"),
    ("first_day_hearing", "First Day Hearing"),
    ("341_meeting",       "341 Meeting of Creditors"),
    ("bar_date",          "Claims Bar Date"),
    ("sale_deadline",     "Sale / Bid Deadline"),
    ("plan_confirmation", "Plan Confirmation"),
]

def build_case_page(case, entries_data):
    debtor = esc(case.get("debtor_name", case.get("case_name", "")))
    docket_num = esc(case.get("docket_number", ""))
    court = esc(case.get("court", ""))
    region = esc(case.get("debtor_region", ""))
    date_filed = fmt_date(case.get("date_filed"))
    cl_url = esc(case.get("courtlistener_url", ""))
    pacer_url = esc(case.get("pacer_url", ""))

    links_html = ""
    if cl_url:
        links_html += f'<a href="{cl_url}" target="_blank">CourtListener</a>'
    if pacer_url:
        if links_html:
            links_html += " · "
        links_html += f'<a href="{pacer_url}" target="_blank">PACER</a>'

    flags = entries_data.get("flags", {})
    flag_badges = []
    if flags.get("has_dip_financing"):
        flag_badges.append('<span style="display:inline-block;background:#7d6608;color:white;font-size:11px;font-weight:600;padding:4px 10px;border-radius:4px;margin:2px 4px 2px 0;">DIP Financing</span>')
    if flags.get("has_sale_process"):
        flag_badges.append('<span style="display:inline-block;background:#a04000;color:white;font-size:11px;font-weight:600;padding:4px 10px;border-radius:4px;margin:2px 4px 2px 0;">Sale Process</span>')
    if flags.get("has_examiner_or_trustee"):
        flag_badges.append('<span style="display:inline-block;background:#922b21;color:white;font-size:11px;font-weight:600;padding:4px 10px;border-radius:4px;margin:2px 4px 2px 0;">Examiner/Trustee Appointed</span>')
    flags_html = "\n".join(flag_badges) if flag_badges else ""

    key_dates = entries_data.get("key_dates", {})
    timeline_rows = []
    for key, label in KEY_DATE_LABELS:
        kd = key_dates.get(key, {"date": None, "status": "unknown", "source": None})
        date_display = fmt_date(kd.get("date")) if kd.get("date") else "Not yet set"
        status = kd.get("status", "unknown")
        badge = STATUS_BADGES.get(status, STATUS_BADGES["unknown"])
        source = esc(kd.get("source", "")) if kd.get("source") else ""
        timeline_rows.append(f"""      <tr>
        <td style="font-weight:600;">{esc(label)}</td>
        <td>{esc(date_display)}</td>
        <td>{badge}</td>
        <td style="font-size:12px;color:#6c757d;">{source}</td>
      </tr>""")

    classified = entries_data.get("classified_entries", [])
    filing_rows = []
    for entry in reversed(classified):
        cat = entry.get("category", "")
        color = CATEGORY_COLORS.get(cat, "#6c757d")
        label = CATEGORY_LABELS.get(cat, cat)
        desc = esc(entry.get("description", ""))
        docs = entry.get("recap_documents", [])
        doc_links = []
        for d in docs[:3]:
            doc_desc = esc(d.get("description", "Document"))[:60] or "Document"
            doc_links.append(f'<a href="{esc(d["url"])}" target="_blank">{doc_desc}</a>')
        docs_html = " · ".join(doc_links) if doc_links else '<span style="color:#adb5bd;">—</span>'

        filing_rows.append(f"""      <tr>
        <td style="white-space:nowrap;">{entry.get("entry_number", "")}</td>
        <td style="white-space:nowrap;">{esc(fmt_date(entry.get("date_filed")))}</td>
        <td><span style="display:inline-block;background:{color};color:white;font-size:10px;font-weight:700;padding:2px 8px;border-radius:3px;">{esc(label)}</span></td>
        <td>{desc}</td>
        <td>{docs_html}</td>
      </tr>""")

    updated = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")
    last_fetched = entries_data.get("last_fetched", "unknown")
    total_entries = entries_data.get("total_entries", 0)
    classified_count = len(classified)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{debtor} — Bankruptcy Detail — Av's Tools</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif; background:#eef0f3; color:#1a1a2e; padding:0; }}
  .header {{ background:#8e1600; color:white; padding:28px 32px; }}
  .header h1 {{ font-size:22px; font-weight:700; margin-bottom:4px; }}
  .header p {{ font-size:14px; opacity:0.85; }}
  .header a {{ color:rgba(255,255,255,0.9); }}
  .container {{ max-width:1100px; margin:24px auto; padding:0 16px; }}
  .card {{ background:white; border-radius:8px; padding:22px 28px; box-shadow:0 1px 3px rgba(0,0,0,0.06); margin-bottom:20px; }}
  .card h2 {{ font-size:16px; font-weight:700; margin-bottom:14px; color:#2c3e50; }}
  .meta-grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(200px, 1fr)); gap:12px; }}
  .meta-item .label {{ font-size:12px; color:#6c757d; text-transform:uppercase; letter-spacing:0.5px; }}
  .meta-item .value {{ font-size:15px; font-weight:600; margin-top:2px; }}
  table {{ width:100%; border-collapse:collapse; }}
  thead {{ background:#f8f9fa; }}
  th {{ padding:10px 12px; text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:0.5px; color:#6c757d; border-bottom:2px solid #e9ecef; }}
  td {{ padding:10px 12px; font-size:13px; border-bottom:1px solid #f0f0f0; vertical-align:top; }}
  tr:hover {{ background:#fafbfc; }}
  a {{ color:#8e1600; text-decoration:none; font-weight:500; }}
  a:hover {{ text-decoration:underline; }}
  .footer {{ margin:24px 0; text-align:center; font-size:12px; color:#adb5bd; }}
  .footer a {{ color:#adb5bd; }}
  .back-link {{ margin-bottom:16px; font-size:14px; }}
  @media (max-width:700px) {{
    th, td {{ padding:8px 8px; font-size:12px; }}
    .meta-grid {{ grid-template-columns:1fr; }}
  }}
</style>
</head>
<body>

<div style="position:fixed;right:0;top:50%;transform:translateY(-50%);background:#c0392b;color:white;padding:12px 8px;font-size:11px;font-weight:700;letter-spacing:1px;writing-mode:vertical-rl;text-orientation:mixed;z-index:9999;border-radius:4px 0 0 4px;box-shadow:-2px 0 8px rgba(0,0,0,0.2);">AI-BUILT DASHBOARD &mdash; NEVER CITE DIRECTLY &mdash; ALWAYS CHECK THE DOCKET</div>

<div class="header">
  <h1>{debtor}</h1>
  <p>{docket_num} &middot; {court} &middot; Filed {date_filed}</p>
</div>

<div class="container">

<p class="back-link">&larr; <a href="bankruptcy_dashboard.html">Back to all filings</a></p>

<div class="card">
  <h2>Case Summary</h2>
  <div class="meta-grid">
    <div class="meta-item">
      <div class="label">Debtor</div>
      <div class="value">{debtor}</div>
    </div>
    <div class="meta-item">
      <div class="label">Docket Number</div>
      <div class="value">{docket_num}</div>
    </div>
    <div class="meta-item">
      <div class="label">Court</div>
      <div class="value">{court}</div>
    </div>
    <div class="meta-item">
      <div class="label">Region</div>
      <div class="value">{region}</div>
    </div>
    <div class="meta-item">
      <div class="label">Date Filed</div>
      <div class="value">{date_filed}</div>
    </div>
    <div class="meta-item">
      <div class="label">Links</div>
      <div class="value">{links_html}</div>
    </div>
  </div>
  {f'<div style="margin-top:16px;">{flags_html}</div>' if flags_html else ""}
</div>

<div class="card">
  <h2>Key Dates</h2>
  <p style="font-size:12px;color:#6c757d;margin-bottom:12px;">
    <strong style="color:#1e8449;">HAPPENED</strong> = date passed, filing found &nbsp;|&nbsp;
    <strong style="color:#2874a6;">SCHEDULED</strong> = future date found &nbsp;|&nbsp;
    <strong style="color:#aab7b8;">UNKNOWN</strong> = not found on docket yet
  </p>
  <table>
    <thead>
      <tr><th>Milestone</th><th>Date</th><th>Status</th><th>Source</th></tr>
    </thead>
    <tbody>
{"".join(timeline_rows)}
    </tbody>
  </table>
</div>

<div class="card">
  <h2>Key Filings</h2>
  <p style="font-size:12px;color:#6c757d;margin-bottom:12px;">{classified_count} key filings identified from {total_entries} total docket entries</p>
  <table>
    <thead>
      <tr><th>#</th><th>Date</th><th>Type</th><th>Description</th><th>Documents</th></tr>
    </thead>
    <tbody>
{"".join(filing_rows) if filing_rows else "      <tr><td colspan='5' style='text-align:center;padding:30px;color:#6c757d;'>No classified filings found. Run fetch_docket_entries.py to populate.</td></tr>"}
    </tbody>
  </table>
</div>

<div class="footer">
  <p>Av's Tools &middot; Newsroom monitor &middot; Built with <a href="https://claude.ai">Claude</a> (Anthropic AI)</p>
  <p style="margin-top:4px;">Source: <a href="https://www.courtlistener.com">CourtListener</a> / RECAP &middot; Docket entries last fetched {esc(last_fetched[:16] if len(last_fetched) > 16 else last_fetched)}</p>
</div>

</div>
</body>
</html>"""

    return inject_auth(page)


def build():
    cases = json.loads(CASES_FILE.read_text()) if CASES_FILE.exists() else {}
    if not cases:
        print("No cases in cases.json")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    built = 0

    for docket_id, case in cases.items():
        entries_file = ENTRIES_DIR / f"{docket_id}.json"
        if not entries_file.exists():
            continue

        entries_data = json.loads(entries_file.read_text())
        page_html = build_case_page(case, entries_data)
        out_path = OUT_DIR / f"{docket_id}.html"
        out_path.write_text(page_html)
        built += 1
        print(f"  Built {out_path.name} — {case.get('debtor_name', docket_id)}")

    print(f"Built {built} case detail pages in {OUT_DIR}")


if __name__ == "__main__":
    build()
