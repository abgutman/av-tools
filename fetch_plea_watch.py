#!/usr/bin/env python3
"""Guilty Plea Watch: daily scan of serious violent felony dockets for new pleas.

Maintains a watchlist of open cases charged with murder (18 § 2501-2504),
attempted murder, rape (18 § 3121), or aggravated assault (18 § 2702).
Each run downloads fresh docket sheets and compares against the previous
run's state. New guilty pleas are flagged on philly_new_pleas.html.

The watchlist is seeded from:
  - Plea calendar data (status conference cases with target charges)
  - Felony filings with target charges
  - Any manually added dockets

Usage:
    python fetch_plea_watch.py                      # daily: re-download + scan watchlist
    python fetch_plea_watch.py --skip-download      # scan using cached PDFs only
    python fetch_plea_watch.py --seed-only          # build watchlist from data files, don't scan
    python fetch_plea_watch.py --broad-seed         # discover all CP dockets (3 yrs) + screen first batch
    python fetch_plea_watch.py --screen             # screen next batch of unscreened candidates
    python fetch_plea_watch.py --screen --screen-size=500  # screen 500 at a time

Manual tracking: add docket numbers to watch_cases.txt (one per line)
"""

import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from html import escape, unescape

import PyPDF2
from playwright.sync_api import sync_playwright

from auth_gate import inject_auth

BASE_URL = "https://ujsportal.pacourts.us"
WATCHLIST_FILE = "plea_watchlist.json"
OUTPUT_HTML = "philly_new_pleas.html"
CALENDAR_JSON = "philly_calendar.json"
FELONIES_JSON = "philly_felonies.json"
MANUAL_WATCH_FILE = "watch_cases.txt"

TARGET_SECTIONS = {
    "2501", "2502", "2503", "2504",  # Murder / Homicide / Manslaughter
    "2702",                           # Aggravated Assault
    "2718",                           # Strangulation
    "2901",                           # Kidnapping
    "3121",                           # Rape
    "3123",                           # IDSI
    "3124.1",                         # Sexual Assault
    "3125",                           # Aggravated Indecent Assault
    "6312",                           # Child Pornography
}

TARGET_DESCRIPTIONS = [
    re.compile(r"Murder", re.IGNORECASE),
    re.compile(r"Homicide", re.IGNORECASE),
    re.compile(r"Voluntary Manslaughter", re.IGNORECASE),
    re.compile(r"Involuntary Manslaughter", re.IGNORECASE),
    re.compile(r"Aggravated Assault", re.IGNORECASE),
    re.compile(r"\bRape\b", re.IGNORECASE),
    re.compile(r"Robbery.+Serious Bodily", re.IGNORECASE),
    re.compile(r"Strangulation", re.IGNORECASE),
    re.compile(r"Child Pornography", re.IGNORECASE),
    re.compile(r"\bIDSI\b", re.IGNORECASE),
    re.compile(r"Involuntary Deviate Sexual", re.IGNORECASE),
    re.compile(r"Aggravated Indecent Assault", re.IGNORECASE),
    re.compile(r"Kidnapping", re.IGNORECASE),
]

GUILTY_PATTERNS = [
    re.compile(r"Guilty Plea", re.IGNORECASE),
    re.compile(r"Negotiated Guilty", re.IGNORECASE),
    re.compile(r"Nolo Contendere", re.IGNORECASE),
    re.compile(r"Plea of Guilty", re.IGNORECASE),
    re.compile(r"Non-Negotiated Guilty", re.IGNORECASE),
    re.compile(r"Open Guilty Plea", re.IGNORECASE),
]


def extract_charges(text):
    """Parse charges from docket sheet text."""
    charges = []
    idx = text.find("CHARGES")
    if idx < 0:
        return charges
    charges_text = text[idx:]
    disp_idx = charges_text.find("DISPOSITION")
    if disp_idx > 0:
        charges_text = charges_text[:disp_idx]

    statute_pattern = re.compile(r"(\d+)\s*§\s*([\d.]+(?:\.\d+)?)")
    lines = charges_text.split("\n")
    current_desc = ""

    for line in lines:
        line = line.strip()
        if not line or line.startswith("Seq.") or line.startswith("CHARGES"):
            continue
        match = statute_pattern.search(line)
        if match:
            title = match.group(1)
            section = match.group(2)
            desc_part = line[:match.start()].strip()
            if current_desc:
                desc_part = current_desc + " " + desc_part
            desc_part = re.sub(r"[UF]\d?\s+\d{6,}-\d.*", "", desc_part).strip()
            desc_part = re.sub(r"\s+\d+\s+\d+\s*$", "", desc_part).strip()
            grade = ""
            grade_match = re.search(r"\b(F1|F2|F3|M1|M2|M3|S|U)\b", line[:match.start()])
            if grade_match:
                grade = grade_match.group(1)
            charges.append({
                "statute": f"{title} § {section}",
                "description": desc_part,
                "grade": grade,
                "section": section,
            })
            current_desc = ""
        elif line and not re.match(r"^\d", line) and "Printed:" not in line:
            current_desc = (current_desc + " " + line).strip() if current_desc else line

    return charges


def has_target_charge(charges):
    for ch in charges:
        if ch.get("section", "") in TARGET_SECTIONS:
            return True
        desc = ch.get("description", "")
        for pat in TARGET_DESCRIPTIONS:
            if pat.search(desc):
                return True
    return False


def get_target_charge_names(charges):
    names = []
    for ch in charges:
        section = ch.get("section", "")
        desc = ch.get("description", "")
        if section in TARGET_SECTIONS or any(p.search(desc) for p in TARGET_DESCRIPTIONS):
            names.append(desc[:80] if desc else ch.get("statute", ""))
    return names


def extract_guilty_pleas(text):
    """Parse docket sheet text for guilty plea dispositions."""
    pleas = []
    disp_idx = text.find("DISPOSITION SENTENCING")
    if disp_idx < 0:
        disp_idx = text.find("DISPOSITION")
    if disp_idx < 0:
        return pleas

    disp_text = text[disp_idx:]

    for pattern in GUILTY_PATTERNS:
        for match in pattern.finditer(disp_text):
            start = max(0, match.start() - 200)
            end = min(len(disp_text), match.end() + 100)
            context = disp_text[start:end].replace("\n", " ").strip()
            context = re.sub(r"\s+", " ", context)

            plea_date = ""
            for dm in re.finditer(r"(\d{1,2}/\d{1,2}/\d{4})", context):
                preceding = context[:dm.start()].rstrip()
                if preceding.endswith("Printed:"):
                    continue
                plea_date = dm.group(1)
                break

            pleas.append({
                "type": match.group(0),
                "date": plea_date,
                "context": context[:300],
            })

    seen = set()
    unique = []
    for p in pleas:
        key = f"{p['type']}|{p['date']}"
        if key not in seen:
            seen.add(key)
            unique.append(p)

    return unique


def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    return {"watchlist": {}, "history": [], "last_updated": ""}


def save_watchlist(data):
    data["last_updated"] = datetime.now().isoformat()
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)


def seed_watchlist(db):
    """Add target-offense cases from calendar and felony filings to the watchlist."""
    added = 0
    today = datetime.now().strftime("%Y-%m-%d")

    if os.path.exists(CALENDAR_JSON):
        with open(CALENDAR_JSON) as f:
            cal = json.load(f)
        status_types = {"Status", "Status Listing", "Status Hearing", "Initial Status Listing"}
        for date_str, day in cal.items():
            for e in day["events"]:
                d = e["docket"]
                if not d.startswith("CP-51-CR") or d in db["watchlist"]:
                    continue
                if e["event_type"] not in status_types:
                    continue
                pdf_path = f"dockets/{d}.pdf"
                if not os.path.exists(pdf_path):
                    continue
                try:
                    reader = PyPDF2.PdfReader(pdf_path)
                    text = "".join(pg.extract_text() or "" for pg in reader.pages)
                    charges = extract_charges(text)
                    if has_target_charge(charges):
                        db["watchlist"][d] = {
                            "caption": e["caption"],
                            "charges": get_target_charge_names(charges),
                            "added": today,
                            "source": "calendar",
                            "last_checked": "",
                            "has_plea": False,
                            "plea_details": [],
                            "plea_first_seen": None,
                        }
                        added += 1
                except Exception:
                    pass

    if os.path.exists(FELONIES_JSON):
        with open(FELONIES_JSON) as f:
            felonies = json.load(f)
        for c in felonies:
            d = c["docket"]
            if d in db["watchlist"]:
                continue
            charges = c.get("charges", [])
            target_names = []
            for ch in charges:
                statute = ch.get("statute", "")
                desc = ch.get("description", "")
                for s in TARGET_SECTIONS:
                    if s in statute:
                        target_names.append(desc[:80] if desc else statute)
                        break
                else:
                    for pat in TARGET_DESCRIPTIONS:
                        if pat.search(desc):
                            target_names.append(desc[:80])
                            break
            if target_names:
                db["watchlist"][d] = {
                    "caption": c.get("caption", ""),
                    "charges": target_names,
                    "added": today,
                    "source": "felony_filings",
                    "last_checked": "",
                    "has_plea": False,
                    "plea_details": [],
                    "plea_first_seen": None,
                }
                added += 1

    print(f"  Seeded {added} new cases from calendar + felony data")
    return db


def seed_manual_cases(db):
    """Add cases from watch_cases.txt to the watchlist."""
    if not os.path.exists(MANUAL_WATCH_FILE):
        return db
    added = 0
    today = datetime.now().strftime("%Y-%m-%d")
    with open(MANUAL_WATCH_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            docket = line.split()[0]
            if docket in db["watchlist"]:
                continue
            db["watchlist"][docket] = {
                "caption": "",
                "charges": ["Manual watch"],
                "added": today,
                "source": "manual",
                "last_checked": "",
                "has_plea": False,
                "plea_details": [],
                "plea_first_seen": None,
            }
            added += 1
    if added:
        print(f"  Added {added} manual cases from {MANUAL_WATCH_FILE}")
    return db


def fill_manual_captions(db):
    """Fill in captions and charges for manual cases from their downloaded PDFs."""
    for docket, info in db["watchlist"].items():
        if info["source"] != "manual" or info["caption"]:
            continue
        pdf_path = f"dockets/{docket}.pdf"
        if not os.path.exists(pdf_path):
            continue
        try:
            reader = PyPDF2.PdfReader(pdf_path)
            text = "".join(pg.extract_text() or "" for pg in reader.pages)
            cap_match = re.search(r"Commonwealth\s+(?:of PA\s+)?v[s.]?\s+(.+?)(?:\n|Docket)", text)
            if cap_match:
                info["caption"] = f"Comm. v. {cap_match.group(1).strip()}"
            charges = extract_charges(text)
            if charges:
                target = get_target_charge_names(charges)
                if target:
                    info["charges"] = target
                else:
                    info["charges"] = [c.get("description", c.get("statute", ""))[:80] for c in charges[:5]]
        except Exception:
            pass


CANDIDATES_FILE = "broad_candidates.json"
SCREEN_BATCH_SIZE = 200


def broad_seed_discover(years_back=3):
    """Search UJS by filing date, week by week, to collect all CP-51-CR docket numbers."""
    from datetime import timedelta
    today_dt = datetime.now()

    if os.path.exists(CANDIDATES_FILE):
        with open(CANDIDATES_FILE) as f:
            cdata = json.load(f)
        all_dockets = set(cdata.get("candidates", []))
        already_screened = set(cdata.get("screened", []))
        last_week_searched = cdata.get("last_week_searched", "")
        print(f"  Resuming: {len(all_dockets)} known, {len(already_screened)} screened")
    else:
        all_dockets = set()
        already_screened = set()
        last_week_searched = ""

    start_dt = today_dt - timedelta(days=365 * years_back)
    total_weeks = int((today_dt - start_dt).days / 7) + 1

    print(f"\n  Discovering CP-51-CR dockets filed {start_dt.strftime('%Y-%m-%d')} to {today_dt.strftime('%Y-%m-%d')}...")
    print(f"  {total_weeks} weeks to search\n")

    ROTATE_EVERY = 20

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = None
        page = None
        searches_this_ctx = 0

        for week_idx in range(total_weeks):
            week_start = start_dt + timedelta(weeks=week_idx)
            week_end = min(week_start + timedelta(days=6), today_dt)
            start_str = week_start.strftime("%Y-%m-%d")
            end_str = week_end.strftime("%Y-%m-%d")

            if last_week_searched and start_str <= last_week_searched:
                continue

            if ctx is None or searches_this_ctx >= ROTATE_EVERY:
                if ctx:
                    ctx.close()
                ctx = browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                )
                page = ctx.new_page()
                searches_this_ctx = 0

            print(f"  [{week_idx+1}/{total_weeks}] {start_str} to {end_str}...", end=" ", flush=True)
            try:
                page.goto(f"{BASE_URL}/CaseSearch", wait_until="networkidle")
                page.select_option('select[title="Search By"]', label="Date Filed")
                time.sleep(1)
                page.fill('input[name="FiledStartDate"]', start_str)
                page.fill('input[name="FiledEndDate"]', end_str)
                adv = page.query_selector('input[name="AdvanceSearch"]')
                if adv:
                    adv.check()
                    time.sleep(1)
                page.select_option('select[title="County"]', label="Philadelphia")
                time.sleep(0.5)
                page.click("#btnSearch")
                page.wait_for_load_state("networkidle", timeout=30000)
                time.sleep(5)

                rows = page.query_selector_all("tr")
                week_new = 0
                for row in rows[1:]:
                    cells = row.query_selector_all("td")
                    if len(cells) < 12:
                        continue
                    docket = cells[2].inner_text().strip()
                    if docket.startswith("CP-51-CR-") and docket not in all_dockets:
                        all_dockets.add(docket)
                        week_new += 1

                print(f"{week_new} new")
                last_week_searched = start_str
                searches_this_ctx += 1

            except Exception as e:
                print(f"ERROR: {e}")
                if ctx:
                    ctx.close()
                ctx = None
                searches_this_ctx = 0

            if (week_idx + 1) % 20 == 0:
                with open(CANDIDATES_FILE, "w") as f:
                    json.dump({
                        "candidates": sorted(all_dockets),
                        "screened": sorted(already_screened),
                        "last_week_searched": last_week_searched,
                        "discovered": datetime.now().isoformat(),
                    }, f, indent=2)
                print(f"  [checkpoint: {len(all_dockets)} dockets saved]")

            time.sleep(1)

        if ctx:
            ctx.close()
        browser.close()

    with open(CANDIDATES_FILE, "w") as f:
        json.dump({
            "candidates": sorted(all_dockets),
            "screened": sorted(already_screened),
            "last_week_searched": last_week_searched,
            "discovered": datetime.now().isoformat(),
        }, f, indent=2)

    unscreened = len(all_dockets - already_screened)
    print(f"\n  Done. {len(all_dockets)} total candidates, {unscreened} unscreened.")
    print(f"  Saved to {CANDIDATES_FILE}")
    return all_dockets


def broad_seed_screen(db, batch_size=None):
    """Download a batch of unscreened candidates, check charges, add target cases."""
    if not os.path.exists(CANDIDATES_FILE):
        print("  No candidates file. Run --broad-seed first to discover dockets.")
        return db

    batch_size = batch_size or SCREEN_BATCH_SIZE

    with open(CANDIDATES_FILE) as f:
        cdata = json.load(f)
    candidates = set(cdata.get("candidates", []))
    screened = set(cdata.get("screened", []))
    in_watchlist = set(db["watchlist"].keys())

    unscreened = candidates - screened - in_watchlist
    if not unscreened:
        print("  All candidates have been screened.")
        return db

    def year_from_docket(d):
        m = re.search(r"-(\d{4})$", d)
        return int(m.group(1)) if m else 0
    to_screen = sorted(unscreened, key=lambda d: (-year_from_docket(d), d))

    batch = to_screen[:batch_size]

    already_have = [d for d in batch if os.path.exists(f"dockets/{d}.pdf")]
    need_download = [d for d in batch if not os.path.exists(f"dockets/{d}.pdf")]

    print(f"\n  Screening {len(batch)} of {len(to_screen)} remaining candidates...")
    print(f"  {len(already_have)} already have PDFs, {len(need_download)} need download")

    if need_download:
        downloaded = download_fresh_pdfs(need_download)
        print(f"  Downloaded {len(downloaded)} PDFs")

    today = datetime.now().strftime("%Y-%m-%d")
    added = 0
    actually_screened = 0
    for docket in batch:
        pdf_path = f"dockets/{docket}.pdf"
        if not os.path.exists(pdf_path):
            continue
        try:
            reader = PyPDF2.PdfReader(pdf_path)
            text = "".join(pg.extract_text() or "" for pg in reader.pages)
            charges = extract_charges(text)
            screened.add(docket)
            actually_screened += 1
            if has_target_charge(charges):
                caption = ""
                cap_match = re.search(r"Commonwealth\s+(?:of PA\s+)?v[s.]?\s+(.+?)(?:\n|Docket)", text)
                if cap_match:
                    caption = f"Comm. v. {cap_match.group(1).strip()}"
                db["watchlist"][docket] = {
                    "caption": caption,
                    "charges": get_target_charge_names(charges),
                    "added": today,
                    "source": "broad_seed",
                    "last_checked": "",
                    "has_plea": False,
                    "plea_details": [],
                    "plea_first_seen": None,
                }
                added += 1
            else:
                os.remove(pdf_path)
        except Exception:
            pass

    cdata["screened"] = sorted(screened)
    cdata["last_screened"] = datetime.now().isoformat()
    with open(CANDIDATES_FILE, "w") as f:
        json.dump(cdata, f, indent=2)

    skipped = len(batch) - actually_screened
    remaining = len(to_screen) - actually_screened
    print(f"  Parsed {actually_screened}: {added} had target charges, {skipped} had no PDF (will retry), {remaining} still unscreened")


def try_download(page, href, tmp_path):
    with page.expect_download(timeout=30000) as dl_info:
        page.evaluate(
            """(href) => {
                const a = document.createElement('a');
                a.href = href;
                a.style.display = 'none';
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            }""",
            href,
        )
    dl_info.value.save_as(tmp_path)


def get_fresh_link(page, docket_number):
    page.goto(f"{BASE_URL}/CaseSearch", wait_until="networkidle")
    page.select_option('select[title="Search By"]', label="Docket Number")
    time.sleep(1)
    page.fill('input[name="DocketNumber"]', docket_number)
    page.click("#btnSearch")
    page.wait_for_load_state("networkidle", timeout=30000)
    time.sleep(3)
    content = page.content()
    m = re.search(
        r'href="(/Report/CpDocketSheet\?docketNumber=' + re.escape(docket_number) + r'[^"]+)"',
        content,
    )
    if m:
        return unescape(m.group(1))
    return ""


def download_fresh_pdfs(dockets):
    """Download fresh docket PDFs for all watchlist cases."""
    os.makedirs("dockets", exist_ok=True)
    results = {}
    to_process = list(dockets)
    total = len(to_process)

    SESSION_LIMIT = 35
    COOLDOWN = 180
    consecutive_fails = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = None
        page = None
        in_session = 0

        for idx, docket in enumerate(to_process):
            if ctx is None or in_session >= SESSION_LIMIT:
                if ctx:
                    ctx.close()
                if idx > 0:
                    print(f"\n  Cooling down {COOLDOWN}s before next batch...", flush=True)
                    time.sleep(COOLDOWN)
                print(f"\n  New session starting at docket {idx+1}/{total}...", flush=True)
                ctx = browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    accept_downloads=True,
                )
                page = ctx.new_page()
                in_session = 0
                consecutive_fails = 0

            pdf_path = f"dockets/{docket}.pdf"
            tmp_path = f"/tmp/watch_dl_{idx}.pdf"
            print(f"  [{idx+1}/{total}] {docket}...", end=" ", flush=True)

            try:
                link = get_fresh_link(page, docket)
            except Exception:
                link = ""
                consecutive_fails += 1

            if not link:
                print("no link")
                consecutive_fails += 1
                if consecutive_fails >= 5:
                    print("  5 consecutive failures — rotating context...", flush=True)
                    ctx.close()
                    ctx = None
                continue

            try:
                try_download(page, link, tmp_path)
                shutil.copy2(tmp_path, pdf_path)
                os.remove(tmp_path)
                results[docket] = pdf_path
                print("OK")
                consecutive_fails = 0
                in_session += 1
                time.sleep(1)
            except Exception as e:
                print(f"FAILED: {e}")
                consecutive_fails += 1
                if consecutive_fails >= 5:
                    print("  5 consecutive failures — rotating context...", flush=True)
                    ctx.close()
                    ctx = None

        if ctx:
            ctx.close()
        browser.close()

    return results


def scan_for_pleas(db):
    """Parse all watchlist docket PDFs for guilty pleas."""
    today = datetime.now().strftime("%Y-%m-%d")
    new_pleas = {}

    for docket, info in db["watchlist"].items():
        pdf_path = f"dockets/{docket}.pdf"
        if not os.path.exists(pdf_path):
            continue

        try:
            reader = PyPDF2.PdfReader(pdf_path)
            text = "".join(pg.extract_text() or "" for pg in reader.pages)
            pleas = extract_guilty_pleas(text)
        except Exception:
            continue

        previously_had_plea = info.get("has_plea", False)
        old_plea_keys = set(
            f"{p['type']}|{p['date']}" for p in info.get("plea_details", [])
        )

        if pleas:
            current_keys = set(f"{p['type']}|{p['date']}" for p in pleas)
            brand_new_keys = current_keys - old_plea_keys

            info["has_plea"] = True
            info["plea_details"] = pleas
            info["last_checked"] = today

            if not previously_had_plea or brand_new_keys:
                if not info.get("plea_first_seen"):
                    info["plea_first_seen"] = today
                new_pleas[docket] = {
                    "caption": info["caption"],
                    "charges": info["charges"],
                    "pleas": pleas,
                    "pdf_path": f"dockets/{docket}.pdf",
                    "is_brand_new": not previously_had_plea,
                    "first_seen": info["plea_first_seen"],
                }
        else:
            info["last_checked"] = today

    return new_pleas


RECENT_PLEA_DAYS = 14

def build_watch_html(new_pleas, db):
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    last_check = db.get("last_updated", "")
    if last_check:
        try:
            last_check = datetime.fromisoformat(last_check).strftime("%B %d at %I:%M %p")
        except ValueError:
            pass

    watchlist = db["watchlist"]

    cutoff_dt = datetime.now() - timedelta(days=RECENT_PLEA_DAYS)
    recent_pleas = dict(new_pleas)
    for docket, info in watchlist.items():
        if docket in recent_pleas:
            continue
        if not info.get("has_plea") or not info.get("plea_details"):
            continue
        plea_dates = []
        for p in info["plea_details"]:
            try:
                plea_dates.append(datetime.strptime(p["date"], "%m/%d/%Y"))
            except (KeyError, ValueError):
                pass
        if plea_dates and max(plea_dates) >= cutoff_dt:
            recent_pleas[docket] = {
                "caption": info.get("caption", ""),
                "charges": info.get("charges", []),
                "pleas": info.get("plea_details", []),
                "pdf_path": f"dockets/{docket}.pdf",
                "is_brand_new": False,
                "first_seen": info.get("plea_first_seen", ""),
            }
    total_watched = len(watchlist)
    total_with_pleas = sum(1 for v in watchlist.values() if v.get("has_plea"))
    total_open = total_watched - total_with_pleas

    nav_bar = """<div style="background:#1a1a2e;padding:10px 30px;">
    <a href="index.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">Home</a>
    <a href="philly_calendar.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">Court Calendar</a>
    <a href="philly_felonies_dashboard.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">New Felony Filings</a>
    <a href="philly_plea_calendar.html" style="color:#aaa;text-decoration:none;font-size:13px;margin-right:20px;">Plea Calendar</a>
    <a href="philly_new_pleas.html" style="color:white;text-decoration:none;font-size:13px;font-weight:600;">Guilty Plea Watch</a>
</div>
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Guilty Plea Watch — Philadelphia Courts</title>
<style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }}
    .header {{ background: #8e1600; color: white; padding: 20px 30px; }}
    .header h1 {{ font-size: 24px; margin-bottom: 5px; }}
    .header .meta {{ color: #ddd; font-size: 13px; }}
    .stats {{ display: flex; gap: 15px; padding: 20px 30px; flex-wrap: wrap; }}
    .stat {{ background: white; border-radius: 8px; padding: 15px 20px; min-width: 150px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
    .stat .number {{ font-size: 28px; font-weight: 700; color: #8e1600; }}
    .stat .label {{ font-size: 12px; color: #666; text-transform: uppercase; }}
    .explainer {{ background: #fff3cd; padding: 15px 20px; border-radius: 8px; margin: 0 30px; font-size: 14px; line-height: 1.6; }}
    .explainer strong {{ color: #8e1600; }}
    .tab-bar {{ display: flex; padding: 0 30px; background: #e8e8e8; border-bottom: 2px solid #ccc; margin-top: 20px; }}
    .tab {{ padding: 12px 24px; font-size: 15px; font-weight: 600; cursor: pointer; border: none; background: none; color: #666; border-bottom: 3px solid transparent; margin-bottom: -2px; }}
    .tab:hover {{ color: #333; }}
    .tab.active {{ color: #1a1a2e; border-bottom-color: #8e1600; background: #f5f5f5; }}
    .tab .tab-count {{ background: #8e1600; color: white; font-size: 11px; padding: 1px 7px; border-radius: 10px; margin-left: 6px; }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}
    .content {{ padding: 0 30px 30px; }}
    .controls {{ padding: 15px 30px; display: flex; gap: 15px; }}
    .controls input {{ padding: 8px 12px; border: 1px solid #ccc; border-radius: 4px; width: 300px; font-size: 14px; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-top: 20px; }}
    th {{ background: #8e1600; color: white; text-align: left; padding: 10px 12px; font-size: 12px; text-transform: uppercase; }}
    td {{ padding: 10px 12px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }}
    tr:hover {{ background: #fff8f5; }}
    a {{ color: #2980b9; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .plea-type {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; background: #fde8e8; color: #8e1600; }}
    .charge-tag {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: 600; margin: 1px 2px; }}
    .charge-murder {{ background: #2c0000; color: white; }}
    .charge-assault {{ background: #fde8e8; color: #8e1600; }}
    .charge-rape {{ background: #4a0030; color: white; }}
    .charge-sex {{ background: #5c0050; color: white; }}
    .charge-robbery {{ background: #8b4513; color: white; }}
    .charge-other {{ background: #eee; color: #555; }}
    .new-badge {{ display: inline-block; padding: 2px 10px; border-radius: 3px; font-size: 11px; font-weight: 700; background: #c0392b; color: white; animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.7; }} }}
    .status-open {{ color: #27ae60; font-weight: 600; }}
    .status-plea {{ color: #c0392b; font-weight: 600; }}
    .no-events {{ padding: 40px; text-align: center; color: #666; background: white; border-radius: 8px; font-size: 16px; margin-top: 20px; }}
    .history {{ margin-top: 30px; padding: 20px 30px; }}
    .history h3 {{ font-size: 16px; margin-bottom: 10px; }}
    .history-entry {{ font-size: 13px; padding: 5px 0; border-bottom: 1px solid #eee; }}
</style>
</head>
<body>
<div style="position:fixed;right:0;top:50%;transform:translateY(-50%);background:#c0392b;color:white;padding:12px 8px;font-size:11px;font-weight:700;letter-spacing:1px;writing-mode:vertical-rl;text-orientation:mixed;z-index:9999;border-radius:4px 0 0 4px;box-shadow:-2px 0 8px rgba(0,0,0,0.2);">DO NOT CITE DIRECTLY — ALWAYS CHECK THE DOCKET</div>

{nav_bar}

<div class="header">
    <h1>Guilty Plea Watch</h1>
    <div class="meta">Updated {now} | Previous check: {last_check or 'first run'}</div>
</div>

<div class="stats">
    <div class="stat"><div class="number">{len(recent_pleas)}</div><div class="label">New Pleas (Last {RECENT_PLEA_DAYS} Days)</div></div>
    <div class="stat"><div class="number">{total_with_pleas}</div><div class="label">Total with Pleas</div></div>
    <div class="stat"><div class="number">{total_open}</div><div class="label">Still Open</div></div>
    <div class="stat"><div class="number">{total_watched}</div><div class="label">Cases Watched</div></div>
</div>

<div class="explainer">
    <strong>What this page does:</strong> Tracks {total_watched} open cases charged with murder, manslaughter,
    aggravated assault, strangulation, kidnapping, rape, IDSI, sexual assault, aggravated indecent assault,
    robbery with SBI, or child pornography in Philadelphia. Every day, each docket sheet is re-downloaded and parsed.
    When a guilty plea newly appears on a docket, it's flagged here.<br>
    <strong>New pleas since last check are shown first.</strong> The full watchlist is in the second tab.
</div>

<div class="tab-bar">
    <button class="tab active" onclick="switchTab('new')">New Pleas<span class="tab-count">{len(recent_pleas)}</span></button>
    <button class="tab" onclick="switchTab('watchlist')">Full Watchlist<span class="tab-count">{total_watched}</span></button>
    <button class="tab" onclick="switchTab('add')">+ Add Case</button>
</div>
"""

    # === NEW PLEAS TAB ===
    html += """<div id="panel-new" class="tab-panel active">
<div class="controls">
    <input type="text" id="search-new" placeholder="Search name, docket..." oninput="filterPanel('new')">
</div>
<div class="content">
"""

    if recent_pleas:
        html += """<table>
<thead><tr>
    <th></th><th>Docket</th><th>Defendant</th><th>Charges</th><th>Plea Details</th>
</tr></thead>
<tbody>
"""
        for docket in sorted(recent_pleas.keys(), key=lambda d: recent_pleas[d].get("first_seen", "") or "9999", reverse=True):
            info = recent_pleas[docket]
            pdf_path = info.get("pdf_path", "")
            docket_cell = f'<a href="{escape(pdf_path)}" target="_blank">{escape(docket)}</a>' if pdf_path else escape(docket)

            charges_html = ""
            for ch_name in info.get("charges", []):
                lower = ch_name.lower()
                if "murder" in lower or "homicide" in lower or "manslaughter" in lower:
                    css = "charge-murder"
                elif "rape" in lower or "idsi" in lower or "deviate sexual" in lower or "indecent assault" in lower or "child porn" in lower or "sexual assault" in lower:
                    css = "charge-sex"
                elif "robbery" in lower:
                    css = "charge-robbery"
                elif "assault" in lower or "strangulation" in lower:
                    css = "charge-assault"
                else:
                    css = "charge-other"
                charges_html += f'<span class="charge-tag {css}">{escape(ch_name[:50])}</span> '

            plea_html = ""
            for pl in info.get("pleas", []):
                plea_html += f'<div><span class="plea-type">{escape(pl["type"])}</span>'
                if pl.get("date"):
                    plea_html += f' <span style="font-size:12px;color:#666;">({pl["date"]})</span>'
                plea_html += "</div>"

            first_seen = info.get("first_seen", "") or watchlist.get(docket, {}).get("plea_first_seen", "")
            if docket in new_pleas:
                badge = '<span class="new-badge">NEW</span>'
            elif first_seen:
                badge = f'<span class="new-badge" style="background:#e8590c;">Found {first_seen}</span>'
            else:
                badge = '<span class="new-badge">RECENT</span>'

            html += f"""<tr>
    <td>{badge}</td>
    <td>{docket_cell}</td>
    <td>{escape(info.get('caption', ''))}</td>
    <td>{charges_html}</td>
    <td>{plea_html}</td>
</tr>
"""
        html += "</tbody></table>\n"
    else:
        html += f'<div class="no-events">No new guilty pleas in the last {RECENT_PLEA_DAYS} days.</div>\n'

    html += """</div>
</div>
"""

    # === FULL WATCHLIST TAB ===
    html += """<div id="panel-watchlist" class="tab-panel">
<div class="controls">
    <input type="text" id="search-watchlist" placeholder="Search name, docket, charge..." oninput="filterPanel('watchlist')">
</div>
<div class="content">
<table>
<thead><tr>
    <th>Status</th><th>Docket</th><th>Defendant</th><th>Charges</th><th>Source</th><th>Added</th><th>Plea</th>
</tr></thead>
<tbody>
"""

    def plea_sort_key(item):
        d, info = item
        if not info.get("has_plea"):
            return (1, "", d)
        dates = [p.get("date", "") for p in info.get("plea_details", [])]
        parsed = []
        for dt in dates:
            try:
                parsed.append(datetime.strptime(dt, "%m/%d/%Y").strftime("%Y-%m-%d"))
            except (ValueError, TypeError):
                pass
        newest = max(parsed) if parsed else ""
        return (0, newest, d)

    sorted_watchlist = sorted(watchlist.items(), key=plea_sort_key, reverse=True)
    sorted_watchlist = sorted(sorted_watchlist, key=lambda x: 0 if x[1].get("has_plea") else 1)

    for docket, info in sorted_watchlist:
        pdf_path = f"dockets/{docket}.pdf"
        has_pdf = os.path.exists(pdf_path)
        docket_cell = f'<a href="{escape(pdf_path)}" target="_blank">{escape(docket)}</a>' if has_pdf else escape(docket)

        charges_html = ""
        for ch_name in info.get("charges", []):
            lower = ch_name.lower()
            if "murder" in lower or "homicide" in lower or "manslaughter" in lower:
                css = "charge-murder"
            elif "rape" in lower or "idsi" in lower or "deviate sexual" in lower or "indecent assault" in lower or "child porn" in lower or "sexual assault" in lower:
                css = "charge-sex"
            elif "robbery" in lower:
                css = "charge-robbery"
            elif "assault" in lower or "strangulation" in lower:
                css = "charge-assault"
            elif "kidnapping" in lower:
                css = "charge-murder"
            else:
                css = "charge-other"
            charges_html += f'<span class="charge-tag {css}">{escape(ch_name[:50])}</span> '

        if info.get("has_plea"):
            status_cell = '<span class="status-plea">PLEA</span>'
            plea_cell = ""
            for pl in info.get("plea_details", []):
                plea_cell += f'<span class="plea-type">{escape(pl["type"])}</span> '
                if pl.get("date"):
                    plea_cell += f'<span style="font-size:11px;color:#666;">({pl["date"]})</span> '
            if info.get("plea_first_seen"):
                plea_cell += f'<div style="font-size:11px;color:#999;">First seen: {info["plea_first_seen"]}</div>'
        else:
            status_cell = '<span class="status-open">OPEN</span>'
            plea_cell = '<span style="color:#999;font-size:12px;">None detected</span>'

        source = info.get("source", "")
        added = info.get("added", "")

        html += f"""<tr>
    <td>{status_cell}</td>
    <td>{docket_cell}</td>
    <td>{escape(info.get('caption', ''))}</td>
    <td>{charges_html}</td>
    <td style="font-size:11px;">{escape(source)}</td>
    <td style="font-size:11px;">{escape(added)}</td>
    <td>{plea_cell}</td>
</tr>
"""

    html += """</tbody></table>
</div>
</div>
"""




    # === ADD CASE TAB ===
    html += """<div id="panel-add" class="tab-panel">
<div class="content" style="max-width:600px;margin:20px auto;padding:30px;">
    <h3 style="margin-bottom:15px;">Add a Case to the Watchlist</h3>
    <p style="font-size:14px;color:#666;margin-bottom:20px;">
        Enter a CP-51-CR docket number below to track it for guilty pleas.
        The case will be picked up on the next daily run, its docket sheet downloaded,
        and it will appear on the Full Watchlist tab.
    </p>
    <div style="display:flex;gap:10px;margin-bottom:15px;">
        <input type="text" id="manual-docket" placeholder="CP-51-CR-0001234-2026"
               style="flex:1;padding:10px 14px;border:1px solid #ccc;border-radius:4px;font-size:15px;">
        <button onclick="addManualCase()"
                style="padding:10px 20px;background:#8e1600;color:white;border:none;border-radius:4px;font-size:14px;font-weight:600;cursor:pointer;">
            Add
        </button>
    </div>
    <div id="manual-status" style="font-size:13px;color:#27ae60;display:none;margin-bottom:15px;"></div>
    <div id="manual-list-section">
        <h4 style="margin:20px 0 10px;font-size:14px;">Manually Added (pending next run):</h4>
        <div id="manual-list" style="font-size:13px;"></div>
    </div>
</div>
</div>
"""

    html += """
<script>
function switchTab(tab) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    event.target.closest('.tab').classList.add('active');
    document.getElementById('panel-' + tab).classList.add('active');
}

function filterPanel(panel) {
    const q = document.getElementById('search-' + panel).value.toLowerCase();
    document.querySelectorAll('#panel-' + panel + ' table tbody tr').forEach(row => {
        row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
}

function getManualCases() {
    return JSON.parse(localStorage.getItem('plea_watch_manual') || '[]');
}

function saveManualCases(cases) {
    localStorage.setItem('plea_watch_manual', JSON.stringify(cases));
}

function addManualCase() {
    const input = document.getElementById('manual-docket');
    const docket = input.value.trim().toUpperCase();
    if (!docket.match(/^CP-51-CR-\\d{7}-\\d{4}$/)) {
        document.getElementById('manual-status').textContent = 'Invalid format. Use CP-51-CR-XXXXXXX-YYYY';
        document.getElementById('manual-status').style.color = '#c0392b';
        document.getElementById('manual-status').style.display = 'block';
        return;
    }
    const cases = getManualCases();
    if (cases.includes(docket)) {
        document.getElementById('manual-status').textContent = 'Already in list.';
        document.getElementById('manual-status').style.color = '#666';
        document.getElementById('manual-status').style.display = 'block';
        return;
    }
    cases.push(docket);
    saveManualCases(cases);
    input.value = '';
    document.getElementById('manual-status').innerHTML =
        'Added <strong>' + docket + '</strong>. Copy the list below into <code>watch_cases.txt</code> for the daily script to pick it up.';
    document.getElementById('manual-status').style.color = '#27ae60';
    document.getElementById('manual-status').style.display = 'block';
    renderManualList();
}

function removeManualCase(docket) {
    const cases = getManualCases().filter(d => d !== docket);
    saveManualCases(cases);
    renderManualList();
}

function renderManualList() {
    const cases = getManualCases();
    const el = document.getElementById('manual-list');
    if (!cases.length) {
        el.innerHTML = '<span style="color:#999;">None yet.</span>';
        document.getElementById('manual-list-section').style.display = 'none';
        return;
    }
    document.getElementById('manual-list-section').style.display = 'block';
    let html = '<div style="background:#f9f9f9;padding:10px;border-radius:4px;margin-bottom:10px;">';
    cases.forEach(d => {
        html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #eee;">';
        html += '<code>' + d + '</code>';
        html += '<button onclick="removeManualCase(\\''+d+'\\');return false;" style="background:none;border:none;color:#c0392b;cursor:pointer;font-size:12px;">remove</button>';
        html += '</div>';
    });
    html += '</div>';
    html += '<button onclick="copyManualList()" style="padding:6px 14px;background:#1a1a2e;color:white;border:none;border-radius:4px;font-size:12px;cursor:pointer;">Copy list for watch_cases.txt</button>';
    el.innerHTML = html;
}

function copyManualList() {
    const cases = getManualCases();
    const text = cases.join('\\n') + '\\n';
    navigator.clipboard.writeText(text).then(() => {
        document.getElementById('manual-status').textContent = 'Copied! Paste into watch_cases.txt';
        document.getElementById('manual-status').style.color = '#27ae60';
        document.getElementById('manual-status').style.display = 'block';
    });
}

renderManualList();
</script>
</body>
</html>"""

    return html


def main():
    skip_download = "--skip-download" in sys.argv
    seed_only = "--seed-only" in sys.argv
    broad_seed = "--broad-seed" in sys.argv
    screen = "--screen" in sys.argv

    screen_size = SCREEN_BATCH_SIZE
    for arg in sys.argv:
        if arg.startswith("--screen-size="):
            screen_size = int(arg.split("=")[1])

    print("Guilty Plea Watch")
    print("=" * 50)

    db = load_watchlist()
    existing = len(db["watchlist"])
    print(f"\nLoaded watchlist: {existing} cases")

    print("\nSeeding watchlist...")
    db = seed_watchlist(db)
    db = seed_manual_cases(db)

    if broad_seed:
        print("\nStep 1: Discovering all CP-51-CR dockets (3 years)...")
        broad_seed_discover()
        print("\nStep 2: Screening first batch for target charges...")
        broad_seed_screen(db, batch_size=screen_size)
        save_watchlist(db)

    if screen:
        print("\nScreening unscreened candidates...")
        broad_seed_screen(db, batch_size=screen_size)
        save_watchlist(db)

    total = len(db["watchlist"])
    print(f"Watchlist now has {total} cases")

    if seed_only:
        save_watchlist(db)
        print("Seed-only mode. Saved watchlist.")
        return

    daily = "--daily" in sys.argv

    if broad_seed or screen:
        skip_download = True

    dockets_to_scan = list(db["watchlist"].keys())

    if daily:
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_dockets = []
        if os.path.exists(CALENDAR_JSON):
            with open(CALENDAR_JSON) as f:
                cal = json.load(f)
            if today_str in cal:
                for e in cal[today_str]["events"]:
                    d = e.get("docket", "")
                    if d in db["watchlist"] and not db["watchlist"][d].get("has_plea"):
                        today_dockets.append(d)
        today_dockets = list(set(today_dockets))
        if today_dockets:
            print(f"\nDaily mode: {len(today_dockets)} open watchlist cases have court events today")
            downloaded = download_fresh_pdfs(today_dockets)
            print(f"  Downloaded {len(downloaded)} PDFs")
        else:
            print(f"\nDaily mode: no open watchlist cases have court events today")
        have_cached = [d for d in dockets_to_scan if os.path.exists(f"dockets/{d}.pdf")]
        print(f"Scanning {len(have_cached)} cached PDFs.")
    elif skip_download:
        have_cached = [d for d in dockets_to_scan if os.path.exists(f"dockets/{d}.pdf")]
        print(f"\nSkipping downloads. {len(have_cached)} cached PDFs available.")
    else:
        print(f"\nDownloading fresh docket PDFs for all {len(dockets_to_scan)} cases...")
        downloaded = download_fresh_pdfs(dockets_to_scan)
        print(f"  Downloaded {len(downloaded)} PDFs")

    fill_manual_captions(db)

    print(f"\nScanning {len(dockets_to_scan)} dockets for guilty pleas...")
    new_pleas = scan_for_pleas(db)
    print(f"  Found {len(new_pleas)} new pleas")

    db["history"].append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "new_pleas": list(new_pleas.keys()),
        "total_watched": total,
        "total_with_pleas": sum(1 for v in db["watchlist"].values() if v.get("has_plea")),
    })

    save_watchlist(db)

    print(f"\nBuilding watch page...")
    html = inject_auth(build_watch_html(new_pleas, db))
    with open(OUTPUT_HTML, "w") as f:
        f.write(html)
    print(f"Saved to {OUTPUT_HTML}")

    print(f"\n{'='*50}")
    print(f"SUMMARY:")
    print(f"  Cases watched: {total}")
    print(f"  New pleas this run: {len(new_pleas)}")
    total_pleas = sum(1 for v in db["watchlist"].values() if v.get("has_plea"))
    print(f"  Total with pleas: {total_pleas}")
    print(f"  Still open: {total - total_pleas}")

    if new_pleas:
        print(f"\nNEW GUILTY PLEAS:")
        for d, info in new_pleas.items():
            types = ", ".join(p["type"] for p in info["pleas"])
            print(f"  {d}: {info['caption'][:50]} [{types}]")


if __name__ == "__main__":
    main()
