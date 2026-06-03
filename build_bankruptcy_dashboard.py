#!/usr/bin/env python3
"""Generate bankruptcy_dashboard.html from bankruptcy_data/cases.json."""
import json, html as html_mod
from pathlib import Path
from datetime import datetime, timezone, timedelta

from auth_gate import inject_auth

ET = timezone(timedelta(hours=-4))

HERE = Path(__file__).parent
BD = HERE / "bankruptcy_data"
CASES_FILE = BD / "cases.json"
OUT_FILE = HERE / "bankruptcy_dashboard.html"

def esc(s):
    return html_mod.escape(str(s) if s is not None else "")

def fmt_date(iso):
    if not iso:
        return "—"
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        return d.strftime("%b %-d, %Y")
    except Exception:
        return iso[:10]

def build():
    cases = json.loads(CASES_FILE.read_text()) if CASES_FILE.exists() else {}

    today = datetime.now(ET).date()
    seven_days_ago = (today - timedelta(days=7)).isoformat()

    rows_by_date = sorted(cases.values(), key=lambda c: c.get("date_filed", ""), reverse=True)

    table_rows = []
    for c in rows_by_date:
        is_new = (c.get("date_filed", "") >= seven_days_ago)
        badge = ' <span class="new-badge">NEW</span>' if is_new else ""

        links = []
        if c.get("courtlistener_url"):
            links.append(f'<a href="{esc(c["courtlistener_url"])}" target="_blank">CourtListener</a>')
        if c.get("pacer_url"):
            links.append(f'<a href="{esc(c["pacer_url"])}" target="_blank">PACER</a>')

        table_rows.append(f"""      <tr>
        <td>{esc(fmt_date(c.get("date_filed")))}{badge}</td>
        <td class="debtor-name">{esc(c.get("debtor_name", c.get("case_name", "")))}</td>
        <td>{esc(c.get("docket_number", ""))}</td>
        <td>{esc(c.get("court", ""))}</td>
        <td>{esc(c.get("debtor_region", ""))}</td>
        <td>{" · ".join(links)}</td>
      </tr>""")

    updated = datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bankruptcy Tracker — Av's Tools</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif; background:#eef0f3; color:#1a1a2e; padding:0; }}
  .header {{ background:#8e1600; color:white; padding:28px 32px; }}
  .header h1 {{ font-size:24px; font-weight:700; margin-bottom:4px; }}
  .header p {{ font-size:14px; opacity:0.85; }}
  .container {{ max-width:1100px; margin:24px auto; padding:0 16px; }}
  .summary {{ display:flex; gap:16px; margin-bottom:20px; flex-wrap:wrap; }}
  .stat-card {{ background:white; border-radius:8px; padding:18px 24px; flex:1; min-width:140px; box-shadow:0 1px 3px rgba(0,0,0,0.06); }}
  .stat-card .num {{ font-size:28px; font-weight:700; color:#8e1600; }}
  .stat-card .label {{ font-size:13px; color:#6c757d; margin-top:4px; }}
  table {{ width:100%; background:white; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.06); border-collapse:collapse; }}
  thead {{ background:#f8f9fa; }}
  th {{ padding:12px 14px; text-align:left; font-size:12px; text-transform:uppercase; letter-spacing:0.5px; color:#6c757d; border-bottom:2px solid #e9ecef; }}
  td {{ padding:11px 14px; font-size:14px; border-bottom:1px solid #f0f0f0; vertical-align:top; }}
  tr:hover {{ background:#fafbfc; }}
  .debtor-name {{ font-weight:600; }}
  .new-badge {{ display:inline-block; background:#d4380d; color:white; font-size:10px; font-weight:700; padding:2px 6px; border-radius:3px; vertical-align:middle; margin-left:6px; }}
  a {{ color:#8e1600; text-decoration:none; font-weight:500; }}
  a:hover {{ text-decoration:underline; }}
  .footer {{ margin:24px 0; text-align:center; font-size:12px; color:#adb5bd; }}
  .footer a {{ color:#adb5bd; }}
  @media (max-width:700px) {{
    th, td {{ padding:8px 10px; font-size:13px; }}
    .summary {{ flex-direction:column; }}
  }}
</style>
</head>
<body>

<div class="header">
  <h1>Bankruptcy Tracker</h1>
  <p>Chapter 11 filings from the Philadelphia region &middot; All federal courts &middot; Updated {updated}</p>
</div>

<div class="container">

<div class="summary">
  <div class="stat-card">
    <div class="num">{len(rows_by_date)}</div>
    <div class="label">Total filings tracked</div>
  </div>
  <div class="stat-card">
    <div class="num">{sum(1 for c in rows_by_date if c.get("date_filed","") >= seven_days_ago)}</div>
    <div class="label">Filed in last 7 days</div>
  </div>
  <div class="stat-card">
    <div class="num">{len(set(c.get("court_id","") for c in rows_by_date))}</div>
    <div class="label">Courts represented</div>
  </div>
</div>

<table>
  <thead>
    <tr>
      <th>Date Filed</th>
      <th>Debtor</th>
      <th>Docket #</th>
      <th>Court</th>
      <th>Region</th>
      <th>Links</th>
    </tr>
  </thead>
  <tbody>
{"".join(table_rows) if table_rows else "    <tr><td colspan='6' style='text-align:center;padding:40px;color:#6c757d;'>No filings tracked yet. Run the backfill to populate.</td></tr>"}
  </tbody>
</table>

<div class="footer">
  <p>Av's Tools &middot; Philadelphia Inquirer newsroom monitor &middot; Built with <a href="https://claude.ai">Claude</a> (Anthropic AI)</p>
  <p style="margin-top:4px;">Source: <a href="https://www.courtlistener.com">CourtListener</a> / RECAP</p>
</div>

</div>
</body>
</html>"""

    page_html = inject_auth(page_html)
    OUT_FILE.write_text(page_html)
    print(f"Written {OUT_FILE.name} — {len(rows_by_date)} cases, updated {updated}")

if __name__ == "__main__":
    build()
