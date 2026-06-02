#!/usr/bin/env python3
"""Minimal earnings dashboard — one table, sortable by most recent event.

Reads cache.json (produced by simple_earnings.py) and renders earnings_dashboard.html.
Ignoring UX polish per Av's direction — focus is on getting all the information visible.
"""
import json, html
from pathlib import Path
from datetime import datetime, timezone, timedelta

HERE = Path(__file__).parent
CACHE_FILE = HERE / "earnings_data" / "cache.json"
COMPANIES_FILE = HERE / "earnings_data" / "expanded_companies.json"
OUT = HERE / "earnings_dashboard.html"

from auth_gate import inject_auth

cache = json.loads(CACHE_FILE.read_text())
companies = json.loads(COMPANIES_FILE.read_text())

# Build ticker -> tier map
tier_for = {}
for c in companies:
    for t in (c.get("tickers") or [c.get("ticker_hint","")]):
        if t: tier_for[t] = c.get("priority_tier")

# Also include companies in our list that have NO cache entry (no 8-K item 2.02 ever filed)
all_tickers = set(tier_for.keys()) | set(cache.keys())
for tk in all_tickers:
    if tk not in cache and tier_for.get(tk) in (1, 2, 3):
        # Find their company info
        comp = next((c for c in companies if tk in (c.get("tickers") or [c.get("ticker_hint","")])), None)
        if comp:
            cache[tk] = {
                "ticker": tk,
                "name": comp.get("name","") or comp.get("seed_name",""),
                "cik": comp.get("cik"),
                "priority_tier": comp.get("priority_tier"),
                "last_event_date": "",
                "last_event_title": "(no 8-K item 2.02 ever filed — small/inactive filer)",
                "last_event_source": "",
                "last_event_url": "",
            }

today = datetime.now(timezone.utc)

def esc(s): return html.escape(str(s) if s is not None else "")

def days_ago(iso):
    if not iso: return None
    try:
        d = datetime.fromisoformat(iso.replace("Z","+00:00"))
        return (today - d).days
    except: return None

def fmt_date(iso):
    if not iso: return "—"
    try:
        d = datetime.fromisoformat(iso.replace("Z","+00:00"))
        return d.strftime("%b %d, %Y")
    except: return iso[:10]

# Sort by last_event_date descending (newest first), missing dates last
rows = sorted(cache.values(), key=lambda v: v.get("last_event_date","0"), reverse=True)

# Tier colors
TIER_BG = {1: "#fde2e4", 2: "#e0e6ef", 3: "#dde6dc", "X-untracked": "#f0f0f0"}

def build_row(v):
    tk = v.get("ticker","")
    name = v.get("name","")
    tier = v.get("priority_tier") or tier_for.get(tk, "?")
    last_date = v.get("last_event_date","")
    title = v.get("last_event_title","")
    source = v.get("last_event_source","")
    url = v.get("last_event_url","")
    publisher = v.get("last_event_publisher","")
    cik = v.get("cik")
    edgar_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(cik):010d}&type=10-Q,10-K,8-K" if cik else ""

    d_ago = days_ago(last_date)
    if d_ago is None:
        d_label = "—"
        d_class = "missing"
    elif d_ago <= 14:
        d_label = f"{d_ago}d ago"
        d_class = "recent"
    elif d_ago <= 90:
        d_label = f"{d_ago}d ago"
        d_class = "ok"
    else:
        d_label = f"{d_ago}d ago"
        d_class = "stale"

    source_label = source.upper() if source else "—"
    tier_bg = TIER_BG.get(tier, "#fff")

    link_html = ""
    if url:
        link_html = f'<a href="{esc(url)}" target="_blank" rel="noopener">Source</a>'
    if edgar_url:
        link_html += f' · <a href="{esc(edgar_url)}" target="_blank" rel="noopener">EDGAR</a>'

    return f"""
    <tr>
      <td class="tier" style="background:{tier_bg}">{esc(tier)}</td>
      <td class="tk">{esc(tk)}</td>
      <td class="nm">{esc(name)}</td>
      <td class="date">{fmt_date(last_date)}</td>
      <td class="days {d_class}">{d_label}</td>
      <td class="title">{esc(title)[:160]}</td>
      <td class="src">{esc(source_label)}{f" · {esc(publisher)}" if publisher else ""}</td>
      <td class="links">{link_html}</td>
    </tr>"""

table_body = "".join(build_row(v) for v in rows)

# Stats
total = len(rows)
recent = sum(1 for v in rows if (days_ago(v.get("last_event_date","")) or 999) <= 14)
manual = sum(1 for v in rows if v.get("last_event_source") == "manual")
yahoo = sum(1 for v in rows if v.get("last_event_source") == "yahoo")
edgar = sum(1 for v in rows if v.get("last_event_source") == "edgar")
empty = sum(1 for v in rows if not v.get("last_event_date"))

html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Earnings Tracker — Av's Tools</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif; margin: 0; background: #f4f5f7; color: #1a1a2e; }}
.container {{ max-width: 1500px; margin: 24px auto; padding: 0 24px; }}
h1 {{ font-size: 24px; margin: 0 0 4px; }}
.meta {{ color: #6c757d; font-size: 13px; margin-bottom: 12px; }}
.stats {{ display: flex; gap: 14px; margin-bottom: 14px; flex-wrap: wrap; }}
.stat {{ background: white; padding: 8px 14px; border-radius: 4px; font-size: 12.5px; box-shadow: 0 1px 2px rgba(0,0,0,0.06); }}
.stat b {{ color: #1a1a2e; font-size: 15px; margin-right: 4px; }}

table {{ width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border-radius: 4px; overflow: hidden; }}
th {{ background: #1a1a2e; color: white; text-align: left; padding: 8px 10px; font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.4px; }}
td {{ padding: 8px 10px; border-bottom: 1px solid #ececef; vertical-align: top; font-size: 12.5px; }}
tr:hover {{ background: #fbfbfc; }}

.tier {{ font-weight: 700; text-align: center; width: 36px; }}
.tk {{ font-family: ui-monospace, monospace; font-weight: 700; color: #2c5282; white-space: nowrap; }}
.nm {{ font-weight: 500; min-width: 200px; }}
.date {{ white-space: nowrap; min-width: 100px; }}
.days {{ white-space: nowrap; font-weight: 600; }}
.days.recent {{ color: #d63031; background: #fff4cd; padding: 2px 6px; border-radius: 3px; display: inline-block; }}
.days.ok {{ color: #495057; }}
.days.stale {{ color: #6c757d; font-style: italic; }}
.days.missing {{ color: #b2b2b8; font-style: italic; }}
.title {{ color: #2d3436; max-width: 460px; }}
.src {{ font-size: 11.5px; color: #6c757d; white-space: nowrap; }}
.links a {{ color: #2c5282; text-decoration: none; font-size: 12px; }}
.links a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<div class="container">
  <h1>Earnings Tracker</h1>
  <p class="meta">
    Tracking {total} public companies HQ'd in the 8-county Philadelphia region.
    Sorted by most recent earnings-related event.
    Updated {datetime.now().strftime("%Y-%m-%d %H:%M")}.
  </p>
  <div class="stats">
    <div class="stat"><b>{total}</b>total tracked</div>
    <div class="stat"><b>{recent}</b>recent (≤14d)</div>
    <div class="stat"><b>{yahoo}</b>Yahoo-detected</div>
    <div class="stat"><b>{edgar}</b>EDGAR baseline only</div>
    <div class="stat"><b>{manual}</b>manual override</div>
    {f'<div class="stat"><b>{empty}</b>no events found</div>' if empty else ''}
  </div>

  <table>
    <thead>
      <tr>
        <th>T</th><th>Ticker</th><th>Company</th><th>Last event</th><th>Ago</th><th>Title</th><th>Source</th><th>Links</th>
      </tr>
    </thead>
    <tbody>{table_body}</tbody>
  </table>

  <p class="meta" style="margin-top: 16px; font-size: 11px;">
    <b>Tier</b>: 1 = priority watch (13 companies, full multi-signal). 2 = standard (~90). 3 = mid-day filers.<br>
    <b>Source</b>: EDGAR = last 8-K item 2.02 filed at SEC. YAHOO = wire press release detected via Yahoo Finance API. MANUAL = entered by hand.<br>
    <b>Ago</b>: red &amp; highlighted = within last 14 days. Italic = older than 90 days (filer may be inactive or off-cycle).
  </p>
</div>
</body>
</html>"""

OUT.write_text(inject_auth(html_doc))
print(f"Wrote {OUT} ({total} rows, {recent} recent)")
