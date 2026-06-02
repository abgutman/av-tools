#!/usr/bin/env python3
"""Render news_feed.html from news_feed.json — all Yahoo headlines for tracked
Philadelphia-area companies, deduped, sorted newest first."""
import json, html
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
FEED_FILE = ED / "news_feed.json"
OUT = HERE / "news_feed.html"

from auth_gate import inject_auth

feed = json.loads(FEED_FILE.read_text()) if FEED_FILE.exists() else {"items": [], "generated_at": None}
items = feed.get("items", [])

ET = timezone(timedelta(hours=-4))
now = datetime.now(timezone.utc)

def esc(s): return html.escape(str(s) if s is not None else "")

def fmt_ts(unix):
    if not unix: return "—"
    dt = datetime.fromtimestamp(unix, tz=timezone.utc).astimezone(ET)
    age = now - datetime.fromtimestamp(unix, tz=timezone.utc)
    secs = int(age.total_seconds())
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 24*3600:
        h = secs // 3600
        return f"{h}h ago"
    days = secs // 86400
    return f"{days}d ago" + f" · {dt.strftime('%b %-d %I:%M %p ET')}"

def fmt_full_ts(unix):
    if not unix: return ""
    dt = datetime.fromtimestamp(unix, tz=timezone.utc).astimezone(ET)
    return dt.strftime("%a %b %-d, %I:%M %p ET")

def render_row(item):
    tickers = item.get("tickers", [])
    ticker_html = " · ".join(f'<span class="tk">{esc(t)}</span>' for t in tickers[:4])
    if len(tickers) > 4:
        ticker_html += f' <span class="dim">+{len(tickers)-4}</span>'
    return f"""
    <tr>
      <td class="ts" title="{fmt_full_ts(item.get('published_unix',0))}">{fmt_ts(item.get('published_unix',0))}</td>
      <td class="tickers">{ticker_html}</td>
      <td class="title"><a href="{esc(item.get('link',''))}" target="_blank" rel="noopener">{esc(item.get('title',''))}</a></td>
      <td class="pub">{esc(item.get('publisher',''))}</td>
    </tr>"""

body_rows = "".join(render_row(it) for it in items)
gen = feed.get("generated_at", "")

html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Philly Business News — Av's Tools</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif; margin: 0; background: #f4f5f7; color: #1a1a2e; }}
.container {{ max-width: 1400px; margin: 24px auto; padding: 0 24px; }}
h1 {{ font-size: 24px; margin: 0 0 4px; }}
.meta {{ color: #6c757d; font-size: 13px; margin-bottom: 14px; }}
.stats {{ display: flex; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }}
.stat {{ background: white; padding: 8px 14px; border-radius: 4px; font-size: 12.5px; box-shadow: 0 1px 2px rgba(0,0,0,0.06); }}
.stat b {{ color: #1a1a2e; font-size: 15px; margin-right: 4px; }}
table {{ width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border-radius: 4px; overflow: hidden; }}
th {{ background: #1a1a2e; color: white; text-align: left; padding: 9px 12px; font-weight: 600; font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.4px; }}
td {{ padding: 9px 12px; border-bottom: 1px solid #ececef; vertical-align: top; font-size: 13.5px; }}
tr:hover {{ background: #fbfbfc; }}
.ts {{ white-space: nowrap; min-width: 90px; color: #495057; font-size: 12.5px; }}
.tickers {{ white-space: nowrap; min-width: 130px; font-size: 12px; }}
.tickers .tk {{ font-family: ui-monospace, monospace; font-weight: 700; color: #2c5282; }}
.dim {{ color: #999; font-size: 11px; }}
.title {{ color: #1a1a2e; line-height: 1.4; }}
.title a {{ color: #1a1a2e; text-decoration: none; font-weight: 500; }}
.title a:hover {{ color: #2c5282; text-decoration: underline; }}
.pub {{ font-size: 12px; color: #6c757d; white-space: nowrap; max-width: 140px; }}
.empty {{ color: #6c757d; font-style: italic; padding: 30px; background: white; border-radius: 4px; text-align: center; }}
</style>
</head>
<body><div class="container">
  <h1>Philly Business News Feed</h1>
  <p class="meta">All Yahoo Finance headlines from the last 48 hours for the 100+ public companies HQ'd in the 8-county Philadelphia region. Sorted newest first. Updated hourly during business hours.</p>
  <div class="stats">
    <div class="stat"><b>{len(items)}</b> headlines</div>
    <div class="stat"><b>{len(set(t for it in items for t in it.get('tickers',[])))}</b> companies with news</div>
    <div class="stat dim" style="font-size:11px">Last update: {esc(gen[:16] if gen else 'unknown')}</div>
  </div>
  {("<table><thead><tr><th>When</th><th>Tickers</th><th>Headline</th><th>Source</th></tr></thead><tbody>" + body_rows + "</tbody></table>") if items else '<p class="empty">No news in the last 48 hours.</p>'}
</div></body></html>"""

OUT.write_text(inject_auth(html_doc))
print(f"Wrote {OUT} ({len(items)} items)")
