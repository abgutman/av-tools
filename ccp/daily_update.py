"""
Daily live update for the CCP Civil dashboard (Motion Hearings + Trial Dates Certain).

Fetches a rolling forward window of CM (Civil Motions, all judges) and MJ (Trial Dates
Certain) hearings directly from the FJD public e-filing hearing lists, parses them with
the SAME logic as the offline builder (imported from build_dashboard_data.py), writes
data/dashboard_data.json, and regenerates dashboard.html.

No reCAPTCHA on these endpoints. Use is journalistic / non-commercial.
Always verify against the official docket before publishing.

Usage:
    python daily_update.py [--window-days N]   (default 120)
"""

import argparse
import re
import sys
import time
import json
import subprocess
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from build_dashboard_data import (
    category_labels, parse_cm_rows, parse_mj_rows, dedup, OUT_JSON, HERE,
)

BASE = "https://fjdefile.phila.gov/efsfjd/"
MENU = BASE + "zk_fjd_public_qry_04.zp_legalhearlist_menu_idx"
DETAILS = BASE + "zk_fjd_public_qry_04.zp_legalhearlist_details_idx"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DELAY = 1.5
RETRIES = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("daily_update")


def fetch(session, method, url, **kw):
    backoff = 4
    for attempt in range(1, RETRIES + 1):
        try:
            r = (session.get if method == "GET" else session.post)(
                url, timeout=60, allow_redirects=True, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            log.warning("  attempt %d/%d failed: %s", attempt, RETRIES, e)
            if attempt < RETRIES:
                time.sleep(backoff)
                backoff *= 2
    return None


def mj_categories(menu_html: str) -> list:
    """Live cs_MJ codes, minus the empty 'All Programs' pseudo-category (21)."""
    m = re.search(r"var cs_MJ\s*=\s*new Array\((.*?)\)\s*;", menu_html, re.S)
    codes = re.findall(r'"([^"]*)"', m.group(1)) if m else []
    return [c for c in codes if c != "21"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=120,
                    help="forward window from today (default 120)")
    args = ap.parse_args()

    start = date.today()
    end = start + timedelta(days=args.window_days)
    s_iso, e_iso = start.isoformat(), end.isoformat()
    log.info("Window: %s -> %s", s_iso, e_iso)

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Referer": MENU})

    # 1. menu (seed cookie + live category labels)
    r = fetch(session, "GET", MENU)
    if r is None:
        log.error("Could not reach FJD menu page — aborting (possible IP block?)")
        sys.exit(2)
    time.sleep(DELAY)
    labels = category_labels(r.text)
    mj_cats = mj_categories(r.text)
    log.info("MJ categories: %s", mj_cats)

    rows = []

    # 2. CM — Civil Motions, all judges
    log.info("Fetching CM (Civil Motions, all judges)...")
    r = fetch(session, "POST", DETAILS, data={
        "program": "CM", "category": "4",
        "sched_date": s_iso, "sched_date_2": e_iso, "judge_id": "XXX"})
    time.sleep(DELAY)
    cm = dedup(parse_cm_rows(r.text)) if r else []
    log.info("  CM rows: %d", len(cm))

    # 3. MJ — Trial Dates Certain, union of sub-categories
    mj_all = []
    for cat in mj_cats:
        log.info("Fetching MJ category %s (%s)...", cat, labels.get(cat, "?"))
        r = fetch(session, "POST", DETAILS, data={
            "program": "MJ", "category": cat,
            "sched_date": s_iso, "sched_date_2": e_iso})
        time.sleep(DELAY)
        if r:
            mj_all.extend(parse_mj_rows(r.text, cat, labels))
    mj = dedup(mj_all)
    log.info("  MJ rows: %d", len(mj))

    if not cm and not mj:
        log.error("Zero rows for both programs — refusing to overwrite data. "
                  "Likely an IP block or site change. Aborting.")
        sys.exit(3)

    rows = sorted(cm + mj, key=lambda r: (r["iso_date"] or "9999", r["time"], r["program"]))

    payload = {
        "meta": {
            "scraped_at": datetime.now(timezone(timedelta(hours=-4))).strftime("%Y-%m-%dT%H:%M:%S EDT"),
            "source_url": MENU,
            "programs": {
                "CM": "Civil Motions (motion hearings, all judges)",
                "MJ": "Trial Dates Certain",
            },
            "date_range": {"start": s_iso, "end": e_iso},
            "counts": {"CM": len(cm), "MJ": len(mj), "total": len(rows)},
            "note": "Calendar listing only. Verify every detail against the official "
                    "FJD docket before publishing.",
        },
        "hearings": rows,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("Wrote %s (CM %d, MJ %d, total %d)", OUT_JSON, len(cm), len(mj), len(rows))

    # 4. regenerate the HTML
    subprocess.run([sys.executable, str(HERE / "generate_dashboard.py")], check=True)
    log.info("Dashboard regenerated.")


if __name__ == "__main__":
    main()
