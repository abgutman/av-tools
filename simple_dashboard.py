#!/usr/bin/env python3
"""Two earnings pages, focused for newsroom scanning:

  recent_earnings.html   — each company's MOST RECENT 8-K item 2.02 filing
                           (last 90 days only). EDGAR source of truth — Yahoo
                           events excluded so save-the-dates can't sneak in.
  upcoming_earnings.html — confirmed save-the-dates only. Release info + call
                           info per row.

Past-due / dormant filers (no 8-K item 2.02 in 90+ days) are linked at the
bottom of each page in a small diagnostic note — kept around but not noise.
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

company_by_ticker = {}
for c in companies:
    for t in (c.get("tickers") or [c.get("ticker_hint","")]):
        if t: company_by_ticker[t] = c

today = datetime.now(timezone.utc)
today_date = today.date()

def esc(s): return html.escape(str(s) if s is not None else "")

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
    return f"https://finance.yahoo.com/quote/{ticker}/news"

def edgar_filings_url(cik):
    if not cik: return None
    return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={int(cik):010d}&type=10-Q,10-K,8-K&dateb=&owner=include&count=20"

# =================== RECENT PAGE — last 90 days of EDGAR 8-K item 2.02 ===================

def build_recent_rows():
    """For each company in cache, take their last_8k_date (from EDGAR submissions),
    and include them if within the last 90 days. EDGAR-only — never Yahoo."""
    cutoff = (today_date - timedelta(days=90)).isoformat()
    out = []
    for tk, v in cache.items():
        last_8k = v.get("last_8k_date","")
        if not last_8k or last_8k < cutoff: continue
        try:
            d = datetime.strptime(last_8k, "%Y-%m-%d").date()
        except: continue
        days = (today_date - d).days
        out.append({
            "ticker": tk,
            "name": v.get("name",""),
            "cik": v.get("cik"),
            "filing_date": last_8k,
            "days": days,
            "edgar_url": v.get("last_8k_url",""),
        })
    out.sort(key=lambda x: x["filing_date"], reverse=True)
    return out

def render_recent_row(r):
    edgar_link = ""
    if r.get("edgar_url"):
        edgar_link = f'<a href="{esc(r["edgar_url"])}" target="_blank" rel="noopener">View 8-K filing</a>'
    elif r.get("cik"):
        edgar_link = f'<a href="{esc(edgar_filings_url(r["cik"]))}" target="_blank" rel="noopener">View 8-K filing</a>'
    yahoo_link = f'<a href="{esc(yahoo_search_url(r["ticker"]))}" target="_blank" rel="noopener">Yahoo News</a>'
    return f"""
    <tr>
      <td class="date"><b>{fmt_date(r["filing_date"])}</b><br><span class="dim">{r["days"]}d ago</span></td>
      <td class="tk">{esc(r["ticker"])}</td>
      <td class="nm">{esc(r["name"])}</td>
      <td class="links">{edgar_link} · {yahoo_link}</td>
    </tr>"""

def build_recent_page():
    rows = build_recent_rows()
    body_rows = "".join(render_recent_row(r) for r in rows)
    table_html = f"<table><thead><tr><th>Filed</th><th>Ticker</th><th>Company</th><th>Links</th></tr></thead><tbody>{body_rows}</tbody></table>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Recent earnings — Av's Tools</title>{COMMON_STYLES}</head>
<body><div class="container">
  <h1>Recent earnings reports</h1>
  <p class="meta">Each company's most recent SEC 8-K item 2.02 filing (the actual quarterly earnings release), if filed in the last 90 days. EDGAR source of truth. Newest first. Updated {datetime.now().strftime("%Y-%m-%d %H:%M")}.</p>
  {render_tabs(active="recent")}
  <div class="stats"><div class="stat"><b>{len(rows)}</b> companies filed in last 90 days</div></div>
  {table_html if rows else '<p class="empty">No 8-K item 2.02 filings in the last 90 days.</p>'}
  {build_dormant_note()}
</div></body></html>"""

# =================== UPCOMING PAGE — confirmed dates only ===================

def build_upcoming_rows():
    out = []
    for tk, entry in upcoming_raw.items():
        if tk.startswith("_"): continue
        if not isinstance(entry, dict): continue
        release = entry.get("release_date")
        call_d = entry.get("call_date")
        if not release and not call_d: continue
        # Skip past dates
        primary = release or call_d
        if primary < today_date.isoformat(): continue
        comp = company_by_ticker.get(tk, {})
        cache_entry = cache.get(tk, {})
        out.append({
            "ticker": tk,
            "name": cache_entry.get("name", comp.get("name","")) or entry.get("name",""),
            "cik": cache_entry.get("cik", comp.get("cik")),
            "release_date": release,
            "release_time": entry.get("release_time"),
            "call_date": call_d,
            "call_time": entry.get("call_time"),
            "source_url": entry.get("source_url",""),
        })
    out.sort(key=lambda r: r["release_date"] or r["call_date"] or "9999")
    return out

def render_upcoming_row(r):
    primary = r.get("release_date") or r.get("call_date")
    try:
        d = datetime.strptime(primary, "%Y-%m-%d").date()
        days_until = (d - today_date).days
    except: days_until = None

    if days_until is None:
        when_label = "—"
    elif days_until < 0:
        when_label = f'<span class="overdue">{abs(days_until)}d ago</span>'
    elif days_until == 0:
        when_label = '<span class="today">TODAY</span>'
    elif days_until <= 7:
        when_label = f'<span class="soon">in {days_until}d</span>'
    else:
        when_label = f'in {days_until}d'

    # Release info column: date + time
    rel = ""
    if r.get("release_date"):
        rel_t = f' · {esc(r["release_time"])}' if r.get("release_time") else ""
        rel = f'Release: {fmt_date(r["release_date"])}{rel_t}'
    # Call info
    call = ""
    if r.get("call_date"):
        call_t = f' · {esc(r["call_time"])}' if r.get("call_time") else ""
        call = f'Call: {fmt_date(r["call_date"])}{call_t}'
    release_info = (rel + (("<br>" + call) if call else "")) if (rel or call) else "—"

    src_link = ""
    if r.get("source_url"):
        src_link = f'<a href="{esc(r["source_url"])}" target="_blank" rel="noopener">Source</a> · '
    edgar_link = ""
    if r.get("cik"):
        edgar_link = f'<a href="{esc(edgar_filings_url(r["cik"]))}" target="_blank" rel="noopener">EDGAR</a>'

    return f"""
    <tr>
      <td class="date"><b>{fmt_date(primary)}</b></td>
      <td class="when">{when_label}</td>
      <td class="tk">{esc(r["ticker"])}</td>
      <td class="nm">{esc(r["name"])}</td>
      <td class="rel-info">{release_info}</td>
      <td class="links">{src_link}{edgar_link}</td>
    </tr>"""

def build_upcoming_page():
    rows = build_upcoming_rows()
    body_rows = "".join(render_upcoming_row(r) for r in rows)
    table_html = f"<table><thead><tr><th>Date</th><th>When</th><th>Ticker</th><th>Company</th><th>Release information</th><th>Links</th></tr></thead><tbody>{body_rows}</tbody></table>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Upcoming earnings — Av's Tools</title>{COMMON_STYLES}</head>
<body><div class="container">
  <h1>Upcoming earnings reports</h1>
  <p class="meta">Confirmed earnings releases and conference calls, sourced from save-the-date press releases and manual entries. Soonest first. Updated {datetime.now().strftime("%Y-%m-%d %H:%M")}.</p>
  {render_tabs(active="upcoming")}
  <div class="stats"><div class="stat"><b>{len(rows)}</b> confirmed upcoming events</div></div>
  {table_html if rows else '<p class="empty">No confirmed upcoming earnings dates. Add entries to earnings_data/upcoming_dates.json as you learn them.</p>'}
  {build_dormant_note()}
</div></body></html>"""

# =================== Dormant filers note ===================

def build_dormant_note():
    """Companies whose last 8-K item 2.02 is older than 90 days. Listed compactly
    as a footnote so reporters know they're in our database but aren't recently active."""
    dormant = []
    cutoff = (today_date - timedelta(days=90)).isoformat()
    for tk, v in cache.items():
        last_8k = v.get("last_8k_date","")
        if not last_8k: continue
        if last_8k >= cutoff: continue
        try:
            d = datetime.strptime(last_8k, "%Y-%m-%d").date()
            days = (today_date - d).days
        except: continue
        dormant.append({"ticker": tk, "name": v.get("name",""), "days": days, "last": last_8k})
    dormant.sort(key=lambda x: -x["days"])
    if not dormant: return ""
    items = []
    for d in dormant[:60]:  # cap the display
        items.append(f'<span class="dormant-item"><b>{esc(d["ticker"])}</b> {esc(d["name"])[:32]} <span class="dim">({d["days"]}d)</span></span>')
    extra = f" + {len(dormant)-60} more" if len(dormant) > 60 else ""
    return f"""
    <details class="dormant">
      <summary>Filers with no 8-K item 2.02 in 90+ days ({len(dormant)})</summary>
      <p class="dim" style="margin: 6px 0 10px; font-size: 12px;">These are companies in our list whose last SEC quarterly-earnings filing is over 90 days old — may be inactive, delinquent, or off the typical cadence (e.g., 20-F filers, micro-caps). Listed compactly so we don't lose track of them but they don't clutter the upcoming view.</p>
      <div class="dormant-grid">{"".join(items)}</div>
      <p class="dim" style="font-size: 11px; margin-top: 8px;">{extra}</p>
    </details>"""

# =================== Shared chrome ===================

def render_tabs(active):
    return f"""
  <div class="tabs">
    <a href="recent_earnings.html"{' class="active"' if active=='recent' else ''}>Recent earnings</a>
    <a href="upcoming_earnings.html"{' class="active"' if active=='upcoming' else ''}>Upcoming earnings</a>
  </div>"""

COMMON_STYLES = """<style>
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif; margin: 0; background: #f4f5f7; color: #1a1a2e; }
.container { max-width: 1300px; margin: 24px auto; padding: 0 24px; }
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
th { background: #1a1a2e; color: white; text-align: left; padding: 9px 12px; font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.4px; }
td { padding: 10px 12px; border-bottom: 1px solid #ececef; vertical-align: top; font-size: 13.5px; }
tr:hover { background: #fbfbfc; }

.tk { font-family: ui-monospace, monospace; font-weight: 700; color: #2c5282; white-space: nowrap; }
.nm { font-weight: 500; min-width: 200px; }
.date { white-space: nowrap; min-width: 120px; }
.dim { color: #999; font-size: 11px; }
.rel-info { color: #2d3436; line-height: 1.55; }
.links { white-space: nowrap; font-size: 12.5px; }
.links a { color: #2c5282; text-decoration: none; }
.links a:hover { text-decoration: underline; }

.when { white-space: nowrap; font-size: 12.5px; }
.overdue { color: #d63031; font-weight: 600; }
.today { color: #d63031; font-weight: 700; background: #ffeaa7; padding: 2px 6px; border-radius: 3px; }
.soon { color: #e17055; font-weight: 700; }

.empty { color: #6c757d; font-style: italic; padding: 30px; background: white; border-radius: 4px; text-align: center; }

.dormant { margin-top: 30px; background: white; padding: 14px 18px; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
.dormant summary { cursor: pointer; font-weight: 600; color: #495057; font-size: 13px; }
.dormant summary:hover { color: #1a1a2e; }
.dormant-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 4px 14px; margin-top: 8px; }
.dormant-item { font-size: 12px; color: #495057; padding: 3px 0; border-bottom: 1px dotted #ececef; }
.dormant-item b { color: #2c5282; font-family: ui-monospace, monospace; }
</style>"""

# =================== Write files ===================

(HERE / "recent_earnings.html").write_text(inject_auth(build_recent_page()))
(HERE / "upcoming_earnings.html").write_text(inject_auth(build_upcoming_page()))

# Old URL → recent
(HERE / "earnings_dashboard.html").write_text(inject_auth(
    '<!DOCTYPE html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=recent_earnings.html">'
    '<title>Earnings</title></head><body><p>Redirecting...</p></body></html>'
))

recent_rows = build_recent_rows()
upcoming_rows = build_upcoming_rows()
print(f"Recent:   {len(recent_rows)} companies filed 8-K item 2.02 in last 90 days")
print(f"Upcoming: {len(upcoming_rows)} confirmed dates")
