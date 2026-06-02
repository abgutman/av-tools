#!/usr/bin/env python3
"""Yahoo Finance news poller — covers BusinessWire / GlobeNewswire / PR Newswire
press releases for ALL Tier 1+2 companies, including the ones we couldn't
scrape directly (BusinessWire is Cloudflare-protected, but Yahoo Finance
syndicates the content via a public JSON API).

Endpoint: https://query1.finance.yahoo.com/v1/finance/search?q={TICKER}&newsCount=N
  - returns JSON, no auth, no Cloudflare
  - covers Business Wire, GlobeNewswire, PR Newswire, and dozens of others

For each tracked company:
  1. Query the Yahoo API for recent news
  2. Filter to PRESS-WIRE publishers (skip analyst coverage / Zacks / etc.)
  3. Compare against state in yahoo_last_seen.json
  4. On new wire releases, classify (earnings? save-the-date? other?) and alert.

Runs alongside poll_edgar.py during the same windows.
"""
import json, os, sys, subprocess, re, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
STATE_FILE = HERE / "yahoo_last_seen.json"
LOG_FILE = HERE / "yahoo_log.txt"
COMPANIES_FILE = HERE / "expanded_companies.json"
CONFIRMED_DATES_FILE = HERE / "confirmed_dates.json"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
ET = timezone(timedelta(hours=-4))

# Publishers we treat as authoritative wire press releases
PRESS_WIRE_PUBLISHERS = {
    "Business Wire", "BusinessWire",
    "GlobeNewswire", "Globe Newswire",
    "PR Newswire", "PRNewswire",
    "ACCESS Newswire",
    "CNW Group",
    "TMX Newsfile",
}

# Title patterns that strongly indicate an earnings press release
EARNINGS_TITLE = re.compile(
    r"\b(reports?|posted|announces?\s+(?:financial|earnings|results|second|third|fourth|first|q[1-4])|"
    r"results|earnings|fiscal[- ]year|quarterly\s+results)\b",
    re.I,
)
# Save-the-date title patterns (pre-announcement)
SAVE_THE_DATE_TITLE = re.compile(
    r"\b(confirms?|announces?\s+(?:date|timing|schedule)|to\s+(?:host|release|webcast|present)|"
    r"will\s+(?:host|release|webcast|report)|conference\s+call\s+(?:scheduled|date)|"
    r"sets\s+(?:date|earnings)|schedules?)\b",
    re.I,
)
# SAVE-THE-DATE requires FUTURE-tense earnings language. Critical to distinguish:
#   "Reports Q1 2026 results"        → earnings_press (the actual release)
#   "To Report Q1 2026 results on..."  → save_the_date (pre-announcement)
SAVE_THE_DATE_FUTURE_TENSE = re.compile(
    r"\b(to\s+(?:report|release|host|webcast|announce|present)\s+(?:[^.]{0,80}?)"
        r"(?:results|earnings|fiscal|quarter|q[1-4]\b|conference\s+call)|"
    r"will\s+(?:report|release|host|webcast|present|announce)\s+(?:[^.]{0,80}?)"
        r"(?:results|earnings|fiscal|quarter|q[1-4]\b|conference\s+call)|"
    r"confirms?\s+(?:[^.]{0,40}?)(?:earnings|results|quarter|fiscal)|"
    r"sets?\s+(?:earnings|release)\s+date|"
    r"schedules?\s+(?:earnings|conference\s+call|quarterly))",
    re.I,
)
# Investor-conference signals (EXCLUDE these — not earnings save-the-dates)
INVESTOR_CONFERENCE_NOISE = re.compile(
    r"\b(to\s+present\s+at|present\s+at\s+(?:the\s+)?(?:\w+\s+){0,3}conference|"
    r"speak\s+at|panel\s+discussion|fireside\s+chat|invest(?:or)?\s+day|"
    r"non-deal\s+roadshow|investor\s+conference|investor\s+meeting|"
    r"healthcare\s+conference|biotech\s+conference|tmt\s+conference|"
    r"summit|symposium)",
    re.I,
)

def looks_save_the_date(title):
    t = title.lower()
    if INVESTOR_CONFERENCE_NOISE.search(t): return False
    if not SAVE_THE_DATE_FUTURE_TENSE.search(t): return False
    return True

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def fetch_json(url, timeout=15):
    """Use curl-cffi via subprocess to call Python — keeps the script simple.
    Falls back to plain curl if curl-cffi unavailable."""
    try:
        from curl_cffi import requests
        r = requests.get(url, impersonate="chrome120", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except ImportError:
        # Fallback: plain curl
        out = subprocess.run(
            ["curl", "-s", "-A", UA, "-L", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout+3
        )
        try: return json.loads(out.stdout)
        except: return None
    except Exception as e:
        log(f"  fetch err: {e}")
        return None

def fetch_news(ticker, count=25):
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={ticker}&newsCount={count}"
    d = fetch_json(url)
    if not d: return []
    return d.get("news", [])

MONTHS = "(?:January|February|March|April|May|June|July|August|September|October|November|December)"
DATE_RE = re.compile(rf"{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?", re.I)

def extract_future_dates_from_title(title, publish_unix=None):
    """Best-effort: pull a date out of a save-the-date title.
    Example: 'NEXGEL To Report First Quarter 2026 Financial Results on May 15th' → 'May 15'.

    Use the article's publish time as the anchor: a date mentioned in an article
    published on May 12 referring to "May 15" should resolve to that same year,
    not a year later.
    """
    if not DATE_RE.search(title): return None
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?", title, re.I)
    if not m: return None
    mon_name = m.group(1)
    day = int(m.group(2))
    year_str = m.group(3)
    try:
        mon_num = datetime.strptime(mon_name[:3].capitalize(), "%b").month
    except: return None

    if year_str:
        year = int(year_str)
    else:
        # Anchor on the article's publish date if available, else now
        anchor = datetime.fromtimestamp(publish_unix) if publish_unix else datetime.now()
        candidate = datetime(anchor.year, mon_num, day)
        # If candidate is in the past relative to anchor BY MORE THAN A FEW DAYS, assume next year
        # (allow small backward range since the press release may reference today)
        if (anchor - candidate).days > 7:
            year = anchor.year + 1
        else:
            year = anchor.year
    return {
        "release_date": f"{year:04d}-{mon_num:02d}-{day:02d}",
        "call_date": None,
        "call_time": None,
    }

def classify_news(item):
    """Tag the news item: earnings_press / save_the_date / other_wire / non_wire.

    Save-the-date takes precedence over earnings_press — uses the
    looks_save_the_date helper which filters out investor-conference items.
    """
    publisher = item.get("publisher","")
    if publisher not in PRESS_WIRE_PUBLISHERS:
        return "non_wire"
    title = item.get("title","")
    if looks_save_the_date(title):
        return "save_the_date"
    if EARNINGS_TITLE.search(title.lower()):
        return "earnings_press"
    return "other_wire"

def load_state():
    if STATE_FILE.exists():
        d = json.loads(STATE_FILE.read_text())
        return {k: set(v) for k, v in d.items()}
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps({k: sorted(v) for k, v in state.items()}, indent=1))

def main():
    args = sys.argv[1:]
    init_mode = "--init" in args
    live = "--live" in args
    log(f"=== Yahoo news poll (init={init_mode}, live={live}) ===")

    companies = json.loads(COMPANIES_FILE.read_text())
    targets = [c for c in companies
               if c.get("priority_tier") in (1, 2)
               and (c.get("tickers") or [c.get("ticker_hint","")])[0]
               and c.get("cik")]
    log(f"Polling Yahoo news for {len(targets)} companies")

    state = load_state() if not init_mode else {}
    alerts = 0
    save_the_dates_captured = 0

    # Load existing confirmed dates so we can add save-the-date entries
    confirmed = {}
    if CONFIRMED_DATES_FILE.exists():
        confirmed = json.loads(CONFIRMED_DATES_FILE.read_text())

    new_confirmed_dates = 0

    for c in targets:
        ticker = (c.get("tickers") or [c.get("ticker_hint","")])[0]
        if not ticker: continue
        news = fetch_news(ticker, count=25)
        time.sleep(0.3)  # be polite

        seen = state.get(ticker, set())
        new_items = []
        for n in news:
            uuid = n.get("uuid")  # Yahoo's unique news ID
            if not uuid or uuid in seen: continue
            tag = classify_news(n)
            if tag in ("non_wire", "other_wire"): continue
            new_items.append({**n, "tag": tag})

        if new_items:
            log(f"  {ticker}: {len(new_items)} new wire item(s)")
            for n in new_items:
                ts = datetime.fromtimestamp(n.get("providerPublishTime",0), tz=ET)
                msg = f"[{ts.strftime('%a %b %d %I:%M %p ET')}] ({n['publisher']}) {n['title'][:120]}"
                if n["tag"] == "save_the_date":
                    log(f"    🗓 SAVE-THE-DATE: {msg}")
                    save_the_dates_captured += 1
                    # Extract upcoming date from title (anchored to publish date)
                    extracted = extract_future_dates_from_title(n["title"], n.get("providerPublishTime"))
                    if extracted:
                        # Add to confirmed_dates.json
                        rec = {
                            "release_date": extracted.get("release_date"),
                            "call_date": extracted.get("call_date"),
                            "call_time": extracted.get("call_time"),
                            "source_url": n.get("link",""),
                            "source_title": n["title"][:200],
                            "source_type": "yahoo_news",
                            "captured_at": datetime.now().isoformat(timespec="seconds"),
                        }
                        confirmed.setdefault(ticker, [])
                        if not any(x.get("source_url") == rec["source_url"] for x in confirmed[ticker]):
                            confirmed[ticker].append(rec)
                            new_confirmed_dates += 1
                            log(f"      + added confirmed date: release={rec['release_date']}")
                else:
                    log(f"    📰 EARNINGS PRESS: {msg}")
                if not init_mode and not live:
                    log(f"      [no-email mode] would alert")
                elif live:
                    pass  # TODO: send_email (reuse poll_edgar.py logic)
                alerts += 1

        # Update state
        state.setdefault(ticker, set()).update(n["uuid"] for n in news if n.get("uuid"))

    if not args or init_mode or "--no-save" not in args:
        save_state(state)
    # Save confirmed_dates updates
    if new_confirmed_dates > 0:
        CONFIRMED_DATES_FILE.write_text(json.dumps(confirmed, indent=1))
    log(f"=== Yahoo poll done. {alerts} alerts ({save_the_dates_captured} save-the-dates, {new_confirmed_dates} new confirmed dates added). ===\n")

if __name__ == "__main__":
    main()
