#!/usr/bin/env python3
"""Save-the-date scraper using EDGAR 8-K filings.

When a public company pre-announces an upcoming earnings release + conference call,
they almost always file an 8-K with item 7.01 (Reg FD) — and attach the press
release as exhibit 99.1. We can pull this from EDGAR for ALL SEC filers, regardless
of which wire they use (BusinessWire / GlobeNewswire / PR Newswire).

This is the BusinessWire workaround: even though BusinessWire is Cloudflare-protected,
the same content is filed on EDGAR via 8-K 7.01 within the same day. We sacrifice
the 3-15 minute wire lead but gain universal coverage.

For each Tier 1 company:
  1. Pull 8-K filings from last 120 days
  2. Skip 8-Ks with item 2.02 (those ARE the earnings releases, not save-the-dates)
  3. For 8-Ks with item 7.01 / 8.01: fetch all .htm exhibits in the filing
  4. Parse content for upcoming-event language
  5. Extract release_date / call_date / call_time
  6. Filter to future dates only

Output: merged into earnings_data/confirmed_dates.json (additive — preserves
GlobeNewswire-sourced entries from poll_save_the_date.py).
"""
import json, re, html, sys, subprocess, time
from datetime import datetime, date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
COMPANIES_FILE = HERE / "expanded_companies.json"
CACHE = HERE / "submissions_cache"
OUT_FILE = HERE / "confirmed_dates.json"
LOG_FILE = HERE / "save_the_date_edgar_log.txt"

UA = "Inquirer Newsroom agutman@inquirer.com"

# 8-K items that often accompany a save-the-date press release
PRE_ANNOUNCE_ITEMS = {"7.01", "8.01"}
# Item 2.02 = the actual earnings release; we explicitly EXCLUDE it here.

# Wire dateline patterns to help identify press releases
WIRE_HINTS = re.compile(r"\b(BUSINESS\s*WIRE|GLOBE\s*NEWSWIRE|PR\s*NEWSWIRE|BUSINESSWIRE)\b", re.I)

# Earnings-announcement signal words
SAVE_THE_DATE_HINTS = re.compile(
    r"(will\s+release|will\s+report|to\s+release|to\s+report|"
    r"announces?\s+(?:date|schedule|timing)|will\s+host|to\s+host|"
    r"will\s+announce|will\s+webcast|will\s+conduct|will\s+discuss|"
    r"earnings\s+release\s+(?:date|schedule)|"
    r"conference\s+call\s+(?:scheduled|will\s+be\s+held|on)|"
    r"webcast\s+(?:will\s+be|scheduled))",
    re.I
)

EARNINGS_HINTS = re.compile(
    r"\b(earnings|quarterly\s+results?|fiscal[- ]year|"
    r"first[-\s]quarter|second[-\s]quarter|third[-\s]quarter|fourth[-\s]quarter|"
    r"q[1-4]\b|results)\b",
    re.I
)

MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(
    rf"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    rf"({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})",
    re.I
)
TIME_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*(a\.?m\.?|p\.?m\.?)\s*(?:\([^)]*\))?\s*"
    r"(?:Eastern\s*Time|ET|EDT|EST)?",
    re.I
)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{ts}] {msg}\n")
    print(f"[{ts}] {msg}", file=sys.stderr)

def fetch(url, timeout=15):
    try:
        out = subprocess.run(
            ["curl", "-s", "-A", UA, "-L", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout+3,
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception as e:
        log(f"  fetch error: {e}")
        return ""

def cik_padded(c): return f"{int(c):010d}"

def normalize_date(month_name, day, year):
    """('July', '22', '2026') → '2026-07-22'."""
    try:
        m = datetime.strptime(month_name[:3].capitalize(), "%b").month
        return f"{int(year):04d}-{m:02d}-{int(day):02d}"
    except:
        return None

def normalize_time(time_str):
    """'9:00 a.m. Eastern Time' → '09:00 ET'."""
    if not time_str: return None
    m = TIME_RE.search(time_str)
    if not m: return time_str
    t = m.group(1)
    ampm = m.group(2).replace(".", "").lower()
    # Convert to 24h-ish but keep with ET
    h, mn = t.split(":")
    h = int(h)
    if ampm == "pm" and h < 12: h += 12
    if ampm == "am" and h == 12: h = 0
    return f"{h:02d}:{mn} ET"

def parse_save_the_date_text(text):
    """Look in plain text for the save-the-date phrasing. Return dict or None."""
    txt = html.unescape(re.sub(r"<[^>]+>", " ", text))
    txt = re.sub(r"\s+", " ", txt)

    # Must look like a save-the-date announcement
    if not SAVE_THE_DATE_HINTS.search(txt):
        return None
    if not EARNINGS_HINTS.search(txt):
        return None
    # Exclude texts that ARE the actual earnings press release (would have headline numbers like "$X million")
    # Heuristic: if it has the phrase "reported [a/its]" + dollar amount, it's the release itself
    if re.search(r"reported\s+(?:a|its)?\s*(?:net|operating|first[-\s]quarter|second[-\s]quarter|third[-\s]quarter|fourth[-\s]quarter|quarterly)\s+(?:profit|loss|income|earnings|revenue|sales)", txt, re.I):
        return None

    release_date = None
    call_date = None
    call_time = None

    # Look for "release ... results ... on DATE" / "report ... on DATE"
    rel_m = re.search(
        rf"(?:will\s+release|to\s+release|will\s+report|to\s+report)\s+(?:its\s+)?"
        rf"(?:[^.]{{0,80}}?)(?:results|earnings|financial\s+results)"
        rf"\s+(?:[^.]{{0,80}}?)?"
        rf"(?:on|after\s+the\s+market\s+(?:close|opens)\s+(?:on)?)\s+"
        rf"(?:[A-Z][a-z]+day,?\s+)?({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})",
        txt, re.I
    )
    if rel_m:
        release_date = normalize_date(rel_m.group(1), rel_m.group(2), rel_m.group(3))

    # Look for "conference call ... DATE ... at TIME"
    call_m = re.search(
        rf"(?:conference\s+call|earnings\s+call|webcast).{{0,200}}?"
        rf"(?:[A-Z][a-z]+day,?\s+)?({MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})"
        rf".{{0,80}}?(\d{{1,2}}:\d{{2}}\s*[ap]\.?m\.?\s*(?:\([^)]*\))?\s*(?:Eastern\s*Time|ET|EDT|EST)?)",
        txt, re.I
    )
    if call_m:
        call_date = normalize_date(call_m.group(1), call_m.group(2), call_m.group(3))
        call_time = normalize_time(call_m.group(4))
    else:
        # Sometimes the call date IS the release date
        call_time_alone = re.search(
            r"(?:conference\s+call|earnings\s+call|webcast)[^.]{0,200}?"
            r"(\d{1,2}:\d{2}\s*[ap]\.?m\.?\s*(?:\([^)]*\))?\s*(?:Eastern\s*Time|ET|EDT|EST)?)",
            txt, re.I
        )
        if call_time_alone:
            call_time = normalize_time(call_time_alone.group(1))

    if release_date or call_date or call_time:
        return {
            "release_date": release_date,
            "call_date": call_date,
            "call_time": call_time,
        }
    return None

def get_exhibits(cik, accession):
    """Return list of .htm files in the filing (excluding the cover doc + xbrl)."""
    acc_clean = accession.replace("-", "")
    idx_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/index.json"
    idx_text = fetch(idx_url)
    try:
        items = json.loads(idx_text).get("directory", {}).get("item", [])
    except:
        return []
    files = []
    for it in items:
        name = it.get("name", "")
        if not name.endswith(".htm"): continue
        # Skip XBRL viewer pages and report extracts
        if name.startswith("R") and name[1:2].isdigit(): continue
        if "FilingSummary" in name: continue
        if name == "MetaLinks.json": continue
        files.append(name)
    return files

def scan_company(cik, ticker):
    """Walk last 120 days of 8-K filings, look for save-the-date announcements."""
    cf = CACHE / f"CIK{cik_padded(cik)}.json"
    if not cf.exists():
        return []
    sub = json.loads(cf.read_text())
    rec = sub.get("filings",{}).get("recent",{}) or {}
    forms = rec.get("form", [])
    cutoff = (date.today() - timedelta(days=120)).isoformat()

    found = []
    for i in range(len(forms)):
        if forms[i] != "8-K": continue
        filing_date = rec["filingDate"][i]
        if filing_date < cutoff: break  # past lookback

        items_str = (rec.get("items") or [""])[i] or ""
        items_set = {it.strip() for it in items_str.split(",")}

        # Skip the earnings release itself (item 2.02)
        if "2.02" in items_set: continue
        # Only investigate Reg-FD / Other-Events 8-Ks
        if not (items_set & PRE_ANNOUNCE_ITEMS): continue

        accession = rec["accessionNumber"][i]
        primary_doc = rec["primaryDocument"][i]
        log(f"  {ticker}: scanning 8-K {filing_date} items=[{items_str}] acc={accession}")

        # Try the primary doc first, then any other .htm exhibits
        exhibits = get_exhibits(rec["cik"] if "cik" in rec else cik, accession)
        # Reorder: try exhibits 99.* first (they're the press release usually)
        exhibits = sorted(exhibits, key=lambda x: (0 if "99" in x or "ex99" in x.lower() else 1, x))
        time.sleep(0.12)

        result = None
        for ex in exhibits[:5]:  # cap at 5 exhibits per filing
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-','')}/{ex}"
            content = fetch(url)
            time.sleep(0.12)
            if not content: continue
            result = parse_save_the_date_text(content)
            if result: break

        if not result: continue
        today_iso = date.today().isoformat()
        if (result.get("release_date") or "0000") < today_iso and \
           (result.get("call_date") or "0000") < today_iso:
            continue  # both dates in the past

        found.append({
            "release_date": result.get("release_date"),
            "call_date": result.get("call_date"),
            "call_time": result.get("call_time"),
            "source_url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-','')}/{primary_doc}",
            "source_title": f"8-K item {items_str} filed {filing_date}",
            "source_type": "edgar_8k",
            "captured_at": datetime.now().isoformat(timespec="seconds"),
        })
        log(f"    + release={result.get('release_date')} call={result.get('call_date')} {result.get('call_time')}")
    return found

def main():
    args = sys.argv[1:]
    only_tier_1 = "--tier1" in args
    log("=== EDGAR-based save-the-date scan starting ===")

    companies = json.loads(COMPANIES_FILE.read_text())
    targets = [c for c in companies if c.get("cik") and c.get("priority_tier") == 1] \
              if only_tier_1 else \
              [c for c in companies if c.get("cik") and c.get("priority_tier") in (1, 2, 3)]
    log(f"Scanning {len(targets)} companies")

    existing = {}
    if OUT_FILE.exists():
        existing = json.loads(OUT_FILE.read_text())

    total_new = 0
    for c in targets:
        cik = c.get("cik")
        ticker = (c.get("tickers") or [c.get("ticker_hint","")])[0]
        if not ticker: continue
        try:
            found = scan_company(cik, ticker)
        except Exception as e:
            log(f"  ⚠ {ticker} scan error: {e}")
            continue

        if not found: continue
        # Merge with existing entries, dedup by source_url
        existing.setdefault(ticker, [])
        existing_urls = {x.get("source_url") for x in existing[ticker]}
        for rec in found:
            if rec["source_url"] in existing_urls: continue
            existing[ticker].append(rec)
            total_new += 1

    OUT_FILE.write_text(json.dumps(existing, indent=1))
    log(f"=== Done. {total_new} new save-the-dates added across all tracked companies. ===")

if __name__ == "__main__":
    main()
