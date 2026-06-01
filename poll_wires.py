#!/usr/bin/env python3
"""Tier 1 wire scraper — checks GlobeNewswire and PR Newswire for new
earnings press releases from the 12 Tier 1 SEC filers (IBX excluded — no SEC).

GlobeNewswire: search/keyword/{company-slug}/ — works with browser User-Agent.
PR Newswire:   news/{company-slug}/ — works with browser User-Agent.

BusinessWire is Cloudflare-protected; we'll skip it in v1. If we need it later,
we'll add a Playwright-based fallback.

State: wire_last_seen.json — maps ticker → set of release URLs already alerted.

Runs alongside poll_edgar.py during the same windows.
"""
import json, os, sys, subprocess, re, html, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).parent
STATE_FILE = HERE / "wire_last_seen.json"
LOG_FILE = HERE / "wire_log.txt"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15"
ET = timezone(timedelta(hours=-4))

# Per-Tier-1 company: which wires they use + search slug
# (We figured out the wire vendor by sampling recent 8-K exhibit 99.1 datelines.)
TIER_1_WIRES = {
    "CMCSA": {"name": "Comcast", "wires": {
        "self": "https://corporate.comcast.com/press/releases",
    }},
    "ARMK": {"name": "Aramark", "wires": {
        "businesswire_search": "https://www.businesswire.com/portal/site/home/?searchTerm=Aramark",
    }},
    "FMC": {"name": "FMC Corporation", "wires": {
        "businesswire_search": "https://www.businesswire.com/portal/site/home/?searchTerm=FMC+Corporation",
    }},
    "BDN": {"name": "Brandywine Realty Trust", "wires": {
        "globenewswire": "https://www.globenewswire.com/search/keyword/brandywine-realty-trust",
    }},
    "TOL": {"name": "Toll Brothers", "wires": {
        "globenewswire": "https://www.globenewswire.com/search/keyword/toll-brothers",
    }},
    "FIVE": {"name": "Five Below", "wires": {
        "globenewswire": "https://www.globenewswire.com/search/keyword/five-below",
    }},
    "URBN": {"name": "Urban Outfitters", "wires": {
        "globenewswire": "https://www.globenewswire.com/search/keyword/urban-outfitters",
    }},
    "CPB": {"name": "Campbell's", "wires": {
        "businesswire_search": "https://www.businesswire.com/portal/site/home/?searchTerm=Campbell+Soup",
    }},
    "BURL": {"name": "Burlington Stores", "wires": {
        "businesswire_search": "https://www.businesswire.com/portal/site/home/?searchTerm=Burlington+Stores",
    }},
    "LNC": {"name": "Lincoln Financial", "wires": {
        "businesswire_search": "https://www.businesswire.com/portal/site/home/?searchTerm=Lincoln+National",
    }},
    "QRTEA": {"name": "Qurate Retail / QVC", "wires": {
        "globenewswire": "https://www.globenewswire.com/search/keyword/qurate-retail",
    }},
    "COR": {"name": "Cencora", "wires": {
        "businesswire_search": "https://www.businesswire.com/portal/site/home/?searchTerm=Cencora",
    }},
}

# Earnings-related keywords to confirm a release is about earnings
EARNINGS_KW = re.compile(
    r"\b(reports|reported|announces|posts|posted|releases|earning|earnings|"
    r"quarterly|quarter|results|fiscal[- ]year|q[1-4]\b|first[- ]quarter|"
    r"second[- ]quarter|third[- ]quarter|fourth[- ]quarter|annual results)\b",
    re.I
)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def fetch(url, timeout=15):
    try:
        out = subprocess.run(
            ["curl", "-s", "-A", UA, "-L", "--max-redirs", "5", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout+3,
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception as e:
        log(f"  fetch error for {url}: {e}")
        return ""

def parse_globenewswire(slug_url, company_name):
    """Return list of {url, title, published, is_earnings} for recent releases."""
    html_text = fetch(slug_url)
    if not html_text:
        return []
    # Each release has /news-release/YYYY/MM/DD/{id}/.../en/{slug}.html
    # Limit to the first ~10 to avoid going far back
    urls = re.findall(r'href="(/news-release/\d{4}/\d{2}/\d{2}/[^"]+\.html)"', html_text)
    # Dedup but preserve order
    seen = set()
    uniq = []
    for u in urls:
        if u in seen: continue
        seen.add(u)
        uniq.append(u)
    out = []
    for path in uniq[:15]:  # first 15 newest
        full = "https://www.globenewswire.com" + path
        # Title from URL last segment
        title = path.rsplit("/", 1)[-1].replace(".html", "").replace("-", " ")
        is_earnings = bool(EARNINGS_KW.search(title))
        out.append({"url": full, "title": title, "is_earnings": is_earnings, "wire": "globenewswire"})
    return out

def parse_prnewswire(slug_url, company_name):
    html_text = fetch(slug_url)
    if not html_text:
        return []
    urls = re.findall(r'href="(/news-releases/[a-z0-9-]+-\d+\.html)"', html_text)
    seen = set(); uniq = []
    for u in urls:
        if u in seen: continue
        seen.add(u); uniq.append(u)
    out = []
    for path in uniq[:15]:
        full = "https://www.prnewswire.com" + path
        title = path.rsplit("/", 1)[-1]
        title = re.sub(r"-\d+\.html$", "", title).replace("-", " ")
        is_earnings = bool(EARNINGS_KW.search(title))
        out.append({"url": full, "title": title, "is_earnings": is_earnings, "wire": "prnewswire"})
    return out

def parse_comcast_self(url, company_name):
    """Comcast self-hosts at corporate.comcast.com/press."""
    html_text = fetch(url)
    if not html_text:
        return []
    # Look for press-release links — typically /press/releases/YYYY/...
    urls = re.findall(r'href="(/press/releases/\d{4}/[^"]+)"', html_text)
    if not urls:
        urls = re.findall(r'href="(https://corporate\.comcast\.com/press/[^"]+)"', html_text)
    seen = set(); uniq = []
    for u in urls:
        if u in seen: continue
        seen.add(u); uniq.append(u)
    out = []
    for path in uniq[:15]:
        if path.startswith("/"): path = "https://corporate.comcast.com" + path
        title = path.rsplit("/", 1)[-1].replace("-", " ")
        is_earnings = bool(EARNINGS_KW.search(title))
        out.append({"url": path, "title": title, "is_earnings": is_earnings, "wire": "comcast_self"})
    return out

PARSERS = {
    "globenewswire": parse_globenewswire,
    "prnewswire": parse_prnewswire,
    "self": parse_comcast_self,
}

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
    dry_run = "--dry-run" in args
    live = "--live" in args
    log(f"=== Wire poll starting (init={init_mode}, dry_run={dry_run}, live={live}) ===")

    state = load_state() if not init_mode else {}
    alerts = 0
    for tk, cfg in TIER_1_WIRES.items():
        company = cfg["name"]
        for wire_type, url in cfg["wires"].items():
            if wire_type == "businesswire_search":
                log(f"  {tk}: skipping BusinessWire (Cloudflare; deferred to Playwright)")
                continue
            parser = PARSERS.get(wire_type.split("_")[0])  # 'self' -> 'self', 'globenewswire' -> 'globenewswire'
            if not parser:
                continue
            releases = parser(url, company)
            if not releases:
                continue
            seen_for_ticker = state.get(tk, set())
            new_earnings = [r for r in releases if r["url"] not in seen_for_ticker and r["is_earnings"]]
            new_all = [r for r in releases if r["url"] not in seen_for_ticker]
            log(f"  {tk} ({wire_type}): {len(releases)} releases on page, {len(new_all)} unseen ({len(new_earnings)} earnings-flagged)")

            if not init_mode and new_earnings:
                for r in new_earnings:
                    msg = f"[wire alert] {tk} — {r['title'][:120]} — {r['url']}"
                    if dry_run or not live:
                        log(f"    [no-email mode] would alert: {msg}")
                    else:
                        # TODO: send_email implementation (same as poll_edgar.py)
                        log(f"    ✉ alert: {msg}")
                        alerts += 1

            # Update state
            state.setdefault(tk, set()).update(r["url"] for r in releases)
        time.sleep(0.3)  # be polite

    if not dry_run:
        save_state(state)
    log(f"=== Wire poll done. {alerts} alerts. State has {len(state)} tickers. ===\n")

if __name__ == "__main__":
    main()
