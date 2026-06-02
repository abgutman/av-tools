#!/usr/bin/env python3
"""Hourly Yahoo Finance news feed — ALL headlines for tracked companies.

Not earnings-specific. Just everything Yahoo carries for our companies, deduped
and sorted by publish time. Reporter view: 'what business news happened today
for Philadelphia companies?'

For each tracked company:
  1. Query Yahoo Finance: ?q={ticker}&newsCount=25
  2. Add ALL items to news_feed.json (no filter)
  3. Dedupe by UUID — keep the union of associated tickers per article
  4. Drop entries older than 48 hours so the feed stays focused on "today / yesterday"
"""
import json, os, sys, subprocess, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ED = HERE / "earnings_data"
COMPANIES_FILE = ED / "expanded_companies.json"
FEED_FILE = ED / "news_feed.json"
LOG_FILE = ED / "news_feed_log.txt"

LOOKBACK_HOURS = 48

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def fetch_json(url, timeout=12):
    try:
        from curl_cffi import requests
        r = requests.get(url, impersonate="chrome120", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except ImportError:
        out = subprocess.run(
            ["curl","-s","-A","Mozilla/5.0","-L","--max-time",str(timeout), url],
            capture_output=True, text=True, timeout=timeout+3,
        )
        try: return json.loads(out.stdout)
        except: return None
    except Exception as e:
        log(f"  fetch err: {e}")
        return None

def load_feed():
    if FEED_FILE.exists():
        return json.loads(FEED_FILE.read_text())
    return {"items": []}

def save_feed(feed):
    FEED_FILE.write_text(json.dumps(feed, indent=1))

def main():
    log("=== News feed poll start ===")
    companies = json.loads(COMPANIES_FILE.read_text())
    targets = [c for c in companies
               if c.get("priority_tier") in (1, 2, 3)
               and (c.get("tickers") or [c.get("ticker_hint","")])[0]]
    log(f"Polling Yahoo for {len(targets)} companies")

    feed = load_feed()
    by_uuid = {item.get("uuid"): item for item in feed.get("items", []) if item.get("uuid")}

    new_count = 0
    fetched = 0
    for c in targets:
        ticker = (c.get("tickers") or [c.get("ticker_hint","")])[0]
        name = c.get("name","") or c.get("seed_name","")
        if not ticker: continue
        data = fetch_json(f"https://query1.finance.yahoo.com/v1/finance/search?q={ticker}&newsCount=25")
        time.sleep(0.2)
        if not data: continue
        fetched += 1
        for item in data.get("news", []):
            uuid = item.get("uuid")
            if not uuid: continue
            existing = by_uuid.get(uuid)
            if existing:
                # Add this ticker to the article's tickers list if not already there
                ts = existing.setdefault("tickers", [])
                if ticker not in ts:
                    ts.append(ticker)
                continue
            # New article
            by_uuid[uuid] = {
                "uuid": uuid,
                "tickers": [ticker],
                "company": name,
                "title": item.get("title",""),
                "publisher": item.get("publisher",""),
                "link": item.get("link",""),
                "published_unix": item.get("providerPublishTime", 0),
                "captured_at": datetime.now().isoformat(timespec="seconds"),
            }
            new_count += 1

    # Drop entries older than the lookback window
    cutoff = datetime.now(timezone.utc).timestamp() - LOOKBACK_HOURS * 3600
    kept = [v for v in by_uuid.values() if v.get("published_unix",0) >= cutoff]
    # Sort newest first
    kept.sort(key=lambda x: x.get("published_unix",0), reverse=True)

    save_feed({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "items": kept,
    })
    log(f"=== Done. Fetched {fetched} companies, {new_count} new items, {len(kept)} total in {LOOKBACK_HOURS}h window ===")

if __name__ == "__main__":
    main()
