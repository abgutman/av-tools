#!/usr/bin/env python3
"""Two-page earnings dashboard:

  recent_earnings.html   — events in the last 14 days, for "what just happened"
  upcoming_earnings.html — announced save-the-dates + companies whose last 8-K
                           item 2.02 is approaching/past 90 days, for "what's coming"

Reads:
  earnings_data/cache.json          — most recent event per company (from simple_earnings.py)
  earnings_data/upcoming_dates.json — confirmed future dates (manual + auto-populated)
  earnings_data/expanded_companies.json — company metadata

Both pages designed for fast newsroom scanning.
"""
import json, html
from pathlib import Path
from datetime import datetime, timezone, timedelta

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
CACHE_FILE = ED / "cache.json"
UPCOMING_FILE = ED / "upcoming_dates.json"
COMPANIES_FILE = ED / "expanded_companies.json"

from auth_gate import inject_auth

cache = json.loads(CACHE_FILE.read_text())
upcoming_raw = json.loads(UPCOMING_FILE.read_text()) if UPCOMING_FILE.exists() else {}
companies = json.loads(COMPANIES_FILE.read_text())

# Companies keyed by ticker for metadata lookup
company_by_ticker = {}
for c in companies:
    for t in (c.get("tickers") or [c.get("ticker_hint","")]):
        if t: company_by_ticker[t] = c

today = datetime.now(timezone.utc)
today_date = today.date()

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
        if "T" in iso:
            d = datetime.fromisoformat(iso.replace("Z","+00:00"))
        else:
            d = datetime.strptime(iso, "%Y-%m-%d")
        return d.strftime("%b %-d, %Y")
    except: return iso[:10]

def yahoo_search_url(ticker):
    """Default Yahoo Finance search URL for a ticker — for the 'Yahoo News' link."""
    return f"https://finance.yahoo.com/quote/{ticker}/news"

def edgar_filings_url(cik):
    if not cik: return None
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(cik):010d}&type=10-Q,10-K,8-K&dateb=&owner=include&count=20"

# =================== RECENT PAGE ===================

def build_recent_rows():
    """Events from cache.json with last_event_date in the last 14 days."""
    out = []
    for tk, v in cache.items():
        d = days_ago(v.get("last_event_date",""))
        if d is None or d > 14 or d < 0: continue
        out.append({
            "ticker": tk,
            "name": v.get("name",""),
            "date": v.get("last_event_date",""),
            "days": d,
            "title": v.get("last_event_title",""),
            "url": v.get("last_event_url",""),
            "cik": v.get("cik"),
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out

def render_recent_row(r):
    yahoo_link = f'<a href="{esc(yahoo_search_url(r["ticker"]))}" target="_blank" rel="noopener">Yahoo News</a>'
    edgar_link = ""
    if r.get("cik"):
        edgar_link = f' · <a href="{esc(edgar_filings_url(r["cik"]))}" target="_blank" rel="noopener">EDGAR</a>'
    source_link = ""
    if r.get("url"):
        source_link = f' · <a href="{esc(r["url"])}" target="_blank" rel="noopener">Source</a>'
    return f"""
    <tr>
      <td class="date">{fmt_date(r["date"])}<br><span class="dim">{r["days"]}d ago</span></td>
      <td class="tk">{esc(r["ticker"])}</td>
      <td class="nm">{esc(r["name"])}</td>
      <td class="title">{esc(r["title"])[:200]}</td>
      <td class="links">{yahoo_link}{edgar_link}{source_link}</td>
    </tr>"""

def build_recent_page():
    rows = build_recent_rows()
    body_rows = "".join(render_recent_row(r) for r in rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Recent earnings — Av's Tools</title>
{COMMON_STYLES}
</head>
<body>
<div class="container">
  <h1>Recent earnings reports</h1>
  <p class="meta">Earnings-related events filed in the last 14 days. Newest first. Updated {datetime.now().strftime("%Y-%m-%d %H:%M")}.</p>
  {render_tabs(active="recent")}
  <div class="stats">
    <div class="stat"><b>{len(rows)}</b> events in last 14 days</div>
  </div>
  {("<table><thead><tr><th>Filed</th><th>Ticker</th><th>Company</th><th>Title</th><th>Links</th></tr></thead><tbody>" + body_rows + "</tbody></table>") if rows else '<p class="empty">No earnings events in the last 14 days.</p>'}
</div>
</body>
</html>"""

# =================== UPCOMING PAGE ===================

def build_upcoming_rows():
    """
    Combine two sources:
      (a) Confirmed upcoming dates from upcoming_dates.json
      (b) Predicted-due companies: last 8-K item 2.02 is ≥75 days old, so next is imminent

    Same ticker can have both — confirmed date wins. Sort by expected date ascending.
    """
    by_ticker = {}

    # (a) Confirmed
    for tk, entry in upcoming_raw.items():
        if tk.startswith("_"): continue
        release = entry.get("release_date")
        call_d = entry.get("call_date")
        if not release and not call_d: continue
        comp = company_by_ticker.get(tk, {})
        cache_entry = cache.get(tk, {})
        by_ticker[tk] = {
            "ticker": tk,
            "name": cache_entry.get("name", comp.get("name","")),
            "cik": cache_entry.get("cik", comp.get("cik")),
            "expected_date": release or call_d,
            "call_date": call_d,
            "call_time": entry.get("call_time"),
            "status": "confirmed",
            "source_url": entry.get("source_url",""),
            "source_title": entry.get("source_title",""),
        }

    # (b) Predicted-due — last 8-K item 2.02 is 75+ days old
    for tk, v in cache.items():
        if tk in by_ticker: continue  # confirmed wins
        last_8k = v.get("last_8k_date","")
        if not last_8k: continue
        try:
            last_8k_dt = datetime.strptime(last_8k, "%Y-%m-%d").date()
        except: continue
        days_since = (today_date - last_8k_dt).days
        if days_since < 75: continue  # not due yet
        # Predicted next = last_8k + 91 days
        expected = last_8k_dt + timedelta(days=91)
        by_ticker[tk] = {
            "ticker": tk,
            "name": v.get("name",""),
            "cik": v.get("cik"),
            "expected_date": expected.isoformat(),
            "call_date": None,
            "call_time": None,
            "status": "predicted",
            "last_8k_date": last_8k,
            "days_since_last_8k": days_since,
            "source_url": "",
            "source_title": f"Last 8-K item 2.02 filed {last_8k}; next expected ~91 days later",
        }

    rows = list(by_ticker.values())
    rows.sort(key=lambda r: r["expected_date"])
    return rows

def render_upcoming_row(r):
    status_label = '<span class="badge confirmed">CONFIRMED</span>' if r["status"] == "confirmed" else '<span class="badge predicted">predicted</span>'
    call_info = ""
    if r.get("call_date"):
        time_str = f" @ {esc(r['call_time'])}" if r.get("call_time") else ""
        call_info = f'<br><span class="dim">call {fmt_date(r["call_date"])}{time_str}</span>'

    days_until = None
    try:
        d = datetime.strptime(r["expected_date"], "%Y-%m-%d").date()
        days_until = (d - today_date).days
    except: pass

    if days_until is not None:
        if days_until < 0:
            time_label = f'<span class="overdue">past expected — {abs(days_until)}d ago</span>'
        elif days_until == 0:
            time_label = '<span class="today">TODAY</span>'
        elif days_until <= 7:
            time_label = f'<span class="soon">in {days_until}d</span>'
        else:
            time_label = f'in {days_until}d'
    else:
        time_label = "—"

    src_link = ""
    if r.get("source_url"):
        src_link = f'<a href="{esc(r["source_url"])}" target="_blank" rel="noopener">Source</a> · '
    edgar_link = ""
    if r.get("cik"):
        edgar_link = f'<a href="{esc(edgar_filings_url(r["cik"]))}" target="_blank" rel="noopener">EDGAR</a>'
    yahoo_link = f' · <a href="{esc(yahoo_search_url(r["ticker"]))}" target="_blank" rel="noopener">Yahoo News</a>'

    return f"""
    <tr class="row-{r['status']}">
      <td class="date"><b>{fmt_date(r["expected_date"])}</b>{call_info}</td>
      <td class="when">{time_label}</td>
      <td class="stat-cell">{status_label}</td>
      <td class="tk">{esc(r["ticker"])}</td>
      <td class="nm">{esc(r["name"])}</td>
      <td class="ctx">{esc(r.get("source_title",""))[:180]}</td>
      <td class="links">{src_link}{edgar_link}{yahoo_link}</td>
    </tr>"""

def build_upcoming_page():
    rows = build_upcoming_rows()
    confirmed = [r for r in rows if r["status"] == "confirmed"]
    predicted = [r for r in rows if r["status"] == "predicted"]
    body_rows = "".join(render_upcoming_row(r) for r in rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Upcoming earnings — Av's Tools</title>
{COMMON_STYLES}
</head>
<body>
<div class="container">
  <h1>Upcoming earnings reports</h1>
  <p class="meta">Confirmed save-the-dates and companies whose next earnings is approaching based on quarterly cadence (last 8-K item 2.02 was ≥75 days ago). Soonest first. Updated {datetime.now().strftime("%Y-%m-%d %H:%M")}.</p>
  {render_tabs(active="upcoming")}
  <div class="stats">
    <div class="stat"><b>{len(confirmed)}</b> confirmed dates</div>
    <div class="stat"><b>{len(predicted)}</b> predicted due (≥75d since last 8-K)</div>
  </div>
  {("<table><thead><tr><th>Expected</th><th>When</th><th>Status</th><th>Ticker</th><th>Company</th><th>Context</th><th>Links</th></tr></thead><tbody>" + body_rows + "</tbody></table>") if rows else '<p class="empty">Nothing upcoming.</p>'}

  <p class="meta" style="margin-top:18px;font-size:11px;">
    <b>Confirmed</b> = explicit date from a save-the-date press release or manual entry.
    <b>Predicted</b> = expected based on 91-day quarterly cadence (most companies file Q+1 about 91 days after Q's 8-K item 2.02).
  </p>
</div>
</body>
</html>"""

# =================== SHARED CHROME ===================

def render_tabs(active):
    return f"""
  <div class="tabs">
    <a href="recent_earnings.html"{' class="active"' if active=='recent' else ''}>Recent earnings</a>
    <a href="upcoming_earnings.html"{' class="active"' if active=='upcoming' else ''}>Upcoming earnings</a>
  </div>"""

COMMON_STYLES = """<style>
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif; margin: 0; background: #f4f5f7; color: #1a1a2e; }
.container { max-width: 1400px; margin: 24px auto; padding: 0 24px; }
h1 { font-size: 24px; margin: 0 0 4px; }
.meta { color: #6c757d; font-size: 13px; margin-bottom: 14px; }

.tabs { display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 1px solid #d3d9de; }
.tabs a { padding: 9px 14px; text-decoration: none; color: #495057; font-size: 13.5px; font-weight: 500; border-bottom: 3px solid transparent; }
.tabs a.active { color: #1a1a2e; border-bottom-color: #1a1a2e; }
.tabs a:hover { background: #eef0f3; }

.stats { display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.stat { background: white; padding: 8px 14px; border-radius: 4px; font-size: 12.5px; box-shadow: 0 1px 2px rgba(0,0,0,0.06); }
.stat b { color: #1a1a2e; font-size: 15px; margin-right: 4px; }

table { width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border-radius: 4px; overflow: hidden; }
th { background: #1a1a2e; color: white; text-align: left; padding: 8px 11px; font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.4px; }
td { padding: 9px 11px; border-bottom: 1px solid #ececef; vertical-align: top; font-size: 13px; }
tr:hover { background: #fbfbfc; }
tr.row-confirmed { background: #fff9e3; }
tr.row-confirmed:hover { background: #fff4cc; }

.tk { font-family: ui-monospace, monospace; font-weight: 700; color: #2c5282; white-space: nowrap; }
.nm { font-weight: 500; min-width: 180px; }
.date { white-space: nowrap; min-width: 100px; }
.dim { color: #999; font-size: 11px; }
.title, .ctx { color: #2d3436; max-width: 520px; }
.links { white-space: nowrap; font-size: 12px; }
.links a { color: #2c5282; text-decoration: none; }
.links a:hover { text-decoration: underline; }

.when { white-space: nowrap; font-size: 12px; }
.overdue { color: #d63031; font-weight: 600; }
.today { color: #d63031; font-weight: 700; background: #ffeaa7; padding: 2px 6px; border-radius: 3px; }
.soon { color: #e17055; font-weight: 600; }

.badge { display: inline-block; padding: 2px 7px; border-radius: 3px; font-size: 10.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.3px; }
.badge.confirmed { background: #d63031; color: white; }
.badge.predicted { background: #dfe6e9; color: #495057; }

.empty { color: #6c757d; font-style: italic; padding: 30px; background: white; border-radius: 4px; text-align: center; }
</style>"""

# =================== WRITE FILES ===================

(HERE / "recent_earnings.html").write_text(inject_auth(build_recent_page()))
(HERE / "upcoming_earnings.html").write_text(inject_auth(build_upcoming_page()))

# Redirect the old earnings_dashboard.html to the recent page (for the homepage card link)
redirect_html = inject_auth(f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0; url=recent_earnings.html">
<title>Earnings — Av's Tools</title>
</head><body>
<p style="font-family:sans-serif;padding:30px;">Redirecting to <a href="recent_earnings.html">recent earnings</a>...</p>
</body></html>""")
(HERE / "earnings_dashboard.html").write_text(redirect_html)

# Stats
recent_rows = build_recent_rows()
upcoming_rows = build_upcoming_rows()
print(f"Recent earnings:   {len(recent_rows)} events in last 14 days")
print(f"Upcoming earnings: {len(upcoming_rows)} ({sum(1 for r in upcoming_rows if r['status']=='confirmed')} confirmed, {sum(1 for r in upcoming_rows if r['status']=='predicted')} predicted)")
