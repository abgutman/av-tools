#!/usr/bin/env python3
"""
region_engine.py — Cross-court, zip-filtered regional civil-lawsuit dashboards.

Each morning, pull newly-filed civil lawsuits from ALL FOUR area courts —
Philadelphia (FJD), Montgomery (PSI), Bucks (PSI), Delaware (C-Track) — and keep
any case where a LITIGANT'S zip code falls in an area's set. Two areas, each its
own dashboard; every lawsuit carries a badge naming its court of origin.

This replaces the single-county fetch_montco_lm.py / fetch_delco_media.py
dashboards: a Lower Merion resident sued in Philadelphia (or Bucks, or Delaware)
now surfaces on the Lower Merion page, which the old county-bound pages missed.

Pipeline
--------
1. For each court, list recently-commenced civil cases (cheap).
2. For each case not already evaluated, fetch its litigants' addresses (the
   network cost), reduce to city+zip, and cache the verdict so we never re-fetch.
   Cases matching EITHER area's zip set are stored in full; the rest are recorded
   as "evaluated" only (so they're skipped next run).
3. Each area dashboard is the slice of stored matches whose litigants fall in
   that area's zips, within a rolling 14-day window.

State: data lives in `region_state.json` (committed so GitHub Actions persists it
across runs). Only city+zip per litigant is stored — never the full street line
(av-tools is a public repo); the full address stays on the linked official docket.

Usage:
    python region_engine.py                 # full 14-day fetch, write dashboards
    python region_engine.py --daily         # 3-day incremental fetch (cron)
    python region_engine.py --dry-run       # fetch + report, write nothing
    python region_engine.py --area lower_merion --lookback 7
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import court_fetchers as cf

try:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from auth_gate import inject_auth
except ImportError:
    def inject_auth(html):
        return html

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "region_state.json")

RETENTION_DAYS = 14        # how long a matched case stays on the dashboards
NEW_DAYS = 3               # "New lawsuits" tab = filed within this many days
EVAL_KEEP_DAYS = 21        # prune the evaluated/seen cache beyond this

# ── Areas ────────────────────────────────────────────────────────────────────

AREAS = {
    "lower_merion": {
        "name": "Lower Merion",
        "output_html": "montco_lm_dashboard.html",
        "zips": {
            "19003", "19004", "19010", "19035", "19041",
            "19066", "19072", "19083", "19085", "19096",
        },
        "zip_names": {
            "19003": "Ardmore", "19004": "Bala Cynwyd", "19010": "Bryn Mawr",
            "19035": "Gladwyne", "19041": "Haverford", "19066": "Merion Station",
            "19072": "Narberth", "19083": "Havertown", "19085": "Villanova",
            "19096": "Wynnewood",
        },
        "cities": set(),
        "blurb": ("Ardmore, Bala Cynwyd, Bryn Mawr, Gladwyne, Haverford, Merion "
                  "Station, Narberth, Havertown, Villanova and Wynnewood"),
    },
    "greater_media": {
        "name": "Greater Media",
        "output_html": "delco_media_dashboard.html",
        "zips": {"19063", "19065", "19081", "19086", "19091"},
        "zip_names": {
            "19063": "Media", "19065": "Media", "19081": "Swarthmore",
            "19086": "Wallingford", "19091": "Media",
        },
        "cities": {"media", "swarthmore", "wallingford"},
        "blurb": ("Media (19063, 19065, 19091), Swarthmore (19081) and "
                  "Wallingford (19086)"),
    },
}

UNION_ZIPS = set().union(*(a["zips"] for a in AREAS.values()))
UNION_CITIES = set().union(*(a["cities"] for a in AREAS.values()))

# Colorblind-safe court badge palette (Okabe-Ito), white text.
COURT_BADGE = {
    "philadelphia": ("Philadelphia", "#0072B2"),
    "montgomery":   ("Montgomery",   "#009E73"),
    "bucks":        ("Bucks",        "#9467BD"),
    "delaware":     ("Delaware",     "#D55E00"),
}

FORECLOSURE_KEYWORDS = ("mortgage foreclosure", "foreclosure", "mortgage", "ejectment")


# ── State ────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.loads(open(STATE_FILE).read())
        except Exception:
            pass
    return {"evaluated": {}, "matches": {}, "last_run": None}


def save_state(state):
    state["last_run"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ── Matching ─────────────────────────────────────────────────────────────────

def party_in(party, zips, cities):
    if party.get("zip") in zips:
        return True
    return cities and party.get("city", "").strip().lower() in cities


def case_matches(record, zips, cities):
    return any(party_in(p, zips, cities) for p in record.get("parties", []))


def local_parties(record, area):
    return [p for p in record.get("parties", [])
            if party_in(p, area["zips"], area["cities"])]


def is_foreclosure(record):
    ct = (record.get("case_type") or "").lower()
    return any(kw in ct for kw in FORECLOSURE_KEYWORDS)


def _date(record):
    """Best available date as a date object: filing_date, else first_seen."""
    for key in ("filing_date", "first_seen"):
        val = record.get(key) or ""
        token = str(val)[:10]
        try:
            return datetime.strptime(token, "%Y-%m-%d").date()
        except ValueError:
            continue
    return None


# ── Fetch + classify ─────────────────────────────────────────────────────────

def run_fetch(state, fetch_days):
    session = cf.new_session()
    end = datetime.now()
    start = end - timedelta(days=fetch_days)
    evaluated, matches = state["evaluated"], state["matches"]
    now_iso = end.isoformat()

    for src in cf.build_sources():
        try:
            stubs = src.list_recent(session, start, end)
        except Exception as e:
            print(f"  [{src.court}] list_recent failed: {e} — skipping court.")
            continue
        new_count = matched_count = 0
        for stub in stubs:
            key = f"{src.court}:{stub['case_number']}"
            if key in evaluated:
                continue
            try:
                parties = src.parties_for(session, stub)
                record = src.normalize(stub, parties)
            except Exception as e:
                print(f"    [{key}] detail fetch failed: {e}")
                continue
            evaluated[key] = now_iso
            new_count += 1
            if cf.is_excluded_type(record.get("case_type", "")):
                continue
            if case_matches(record, UNION_ZIPS, UNION_CITIES):
                record["first_seen"] = now_iso
                matches[key] = record
                matched_count += 1
        print(f"  [{src.court}] {len(stubs)} recent · {new_count} newly evaluated · "
              f"{matched_count} new regional matches")

    prune(state)


def prune(state):
    cutoff_eval = (datetime.now() - timedelta(days=EVAL_KEEP_DAYS)).isoformat()
    state["evaluated"] = {k: v for k, v in state["evaluated"].items()
                          if (v or "") >= cutoff_eval}
    keep_after = (datetime.now() - timedelta(days=RETENTION_DAYS + 3)).date()
    kept = {}
    for k, rec in state["matches"].items():
        d = _date(rec)
        if d is None or d >= keep_after:
            kept[k] = rec
    state["matches"] = kept


# ── Dashboard ────────────────────────────────────────────────────────────────

def esc(text):
    if not text:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def municipality(party, area):
    return (area["zip_names"].get(party.get("zip", ""))
            or party.get("city") or party.get("zip") or "")


def case_card(rec, area):
    label, color = COURT_BADGE.get(rec["court"], (rec["court"].title(), "#555"))
    badge = (f'<span class="court-badge" style="background:{color}">'
             f'{esc(label)}</span>')

    party_lines = ""
    for p in local_parties(rec, area):
        muni = municipality(p, area)
        loc = f'<span class="locale">{esc(muni)} ({esc(p.get("zip",""))})</span>' if muni else ""
        party_lines += (f'<div class="local-party"><strong>{esc(p["role"])}:</strong> '
                        f'{esc(p["name"])} {loc}</div>')

    url = rec.get("url", "")
    if url:
        case_id_html = f'<a href="{esc(url)}" target="_blank" rel="noopener" class="case-number">{esc(rec["case_number"])}</a>'
        docket_btn = f'<a href="{esc(url)}" target="_blank" rel="noopener" class="docket-link">View Docket &rarr;</a>'
    else:
        # Philadelphia: no stable public per-case docket URL.
        case_id_html = f'<span class="case-number">{esc(rec["case_number"])}</span>'
        docket_btn = '<span class="docket-link nolink">FJD — search case no.</span>'

    case_type = esc(rec.get("case_type", "") or "—")
    status = rec.get("status", "")
    status_line = f' &middot; {esc(status)}' if status else ""

    return f'''<div class="case-card">
        <div class="case-header">
            {badge}{case_id_html}
            <span class="case-type">{case_type}</span>
            {docket_btn}
        </div>
        <div class="caption">{esc(rec.get("caption",""))}</div>
        <div class="case-meta">Filed: {esc(rec.get("filing_date","") or "—")}{status_line}</div>
        {party_lines}
    </div>'''


def section(title, icon, cases, section_id, area):
    if not cases:
        body = '<div class="empty">No cases in this category.</div>'
    else:
        body = "\n".join(case_card(c, area) for c in cases)
    return f'''<div class="section" id="{section_id}">
        <h2>{icon} {title} <span class="count">({len(cases)})</span></h2>
        {body}
    </div>'''


def build_dashboard(area_key, area, cases):
    now = datetime.now()
    cases = sorted(cases, key=lambda c: c.get("filing_date", ""), reverse=True)

    new_cutoff = (now - timedelta(days=NEW_DAYS)).date()
    new_cases = [c for c in cases if (_date(c) or now.date()) >= new_cutoff]
    foreclosures = [c for c in cases if is_foreclosure(c)]

    other_key = "greater_media" if area_key == "lower_merion" else "lower_merion"
    other = AREAS[other_key]

    # Per-court tally for the blurb.
    tally = {}
    for c in cases:
        tally[c["court"]] = tally.get(c["court"], 0) + 1
    tally_str = " · ".join(f"{COURT_BADGE[k][0]} {v}"
                           for k, v in sorted(tally.items())) or "none yet"

    legend = "".join(
        f'<span class="court-badge" style="background:{color}">{label}</span>'
        for _k, (label, color) in COURT_BADGE.items())

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>{esc(area["name"])} Civil Lawsuits — Regional Courts</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }}
.header {{ background: #1b2a3a; color: white; padding: 28px 20px; text-align: center; }}
.header h1 {{ font-size: 25px; margin-bottom: 6px; }}
.header .meta {{ color: #9fb3c8; font-size: 13px; }}
.nav {{ display: flex; justify-content: center; gap: 12px; padding: 12px 20px; background: #e7ebef; border-bottom: 1px solid #d6dce2; flex-wrap: wrap; }}
.nav a {{ font-size: 14px; font-weight: 600; color: #1b2a3a; text-decoration: none; padding: 6px 16px; border-radius: 6px; }}
.nav a:hover {{ background: #d6dce2; }}
.nav a.current {{ background: #1b2a3a; color: white; }}
.blurb {{ max-width: 900px; margin: 18px auto 0; padding: 16px 20px; background: #fff; border-radius: 8px; font-size: 13px; color: #555; line-height: 1.6; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }}
.blurb strong {{ color: #333; }}
.legend {{ max-width: 900px; margin: 10px auto 0; padding: 0 20px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; font-size: 12px; color: #777; }}
.legend .lbl {{ margin-right: 2px; }}
.tabs {{ display: flex; justify-content: center; gap: 10px; padding: 18px 20px; background: #fff; border-bottom: 1px solid #ddd; flex-wrap: wrap; margin-top: 14px; }}
.tab {{ padding: 8px 18px; border-radius: 20px; cursor: pointer; font-size: 14px; font-weight: 600; border: 2px solid #ddd; background: white; }}
.tab.active {{ background: #1b2a3a; color: white; border-color: #1b2a3a; }}
.tab:hover {{ border-color: #1b2a3a; }}
.content {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
.section {{ display: none; }}
.section.active {{ display: block; }}
.section h2 {{ font-size: 19px; margin-bottom: 14px; padding-bottom: 8px; border-bottom: 2px solid #1b2a3a; }}
.count {{ color: #888; font-weight: normal; font-size: 15px; }}
.case-card {{ background: white; border-radius: 8px; padding: 16px 18px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border-left: 4px solid #1b2a3a; }}
.case-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 6px; flex-wrap: wrap; }}
.court-badge {{ color: white; padding: 2px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; letter-spacing: .02em; white-space: nowrap; }}
.case-number {{ font-weight: 700; color: #1b2a3a; text-decoration: none; font-size: 15px; }}
a.case-number:hover {{ text-decoration: underline; }}
.docket-link {{ margin-left: auto; font-size: 12px; color: white; background: #1b2a3a; padding: 4px 12px; border-radius: 4px; text-decoration: none; font-weight: 600; white-space: nowrap; }}
a.docket-link:hover {{ background: #33506b; }}
.docket-link.nolink {{ background: #aeb6bf; cursor: default; }}
.case-type {{ background: #e8f0fe; color: #1a56db; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; }}
.caption {{ font-size: 15px; margin-bottom: 4px; }}
.case-meta {{ font-size: 13px; color: #666; margin-bottom: 8px; }}
.local-party {{ font-size: 13px; margin: 3px 0; padding: 4px 8px; background: #eef7f1; border-radius: 4px; }}
.locale {{ color: #2c7a4b; font-weight: 600; }}
.empty {{ color: #999; font-style: italic; padding: 20px; text-align: center; }}
.footer {{ text-align: center; padding: 26px 20px; color: #999; font-size: 12px; line-height: 1.6; }}
</style>
</head>
<body>

<div class="header">
    <h1>{esc(area["name"])} Civil Lawsuits</h1>
    <div class="meta">Across Philadelphia, Montgomery, Bucks &amp; Delaware county courts &middot; Updated {now.strftime("%B %d, %Y at %I:%M %p")}</div>
</div>

<div class="nav">
    <a href="{esc(area["output_html"])}" class="current">{esc(area["name"])}</a>
    <a href="{esc(other["output_html"])}">{esc(other["name"])}</a>
</div>

<div class="blurb">
    <strong>What this shows:</strong> Civil lawsuits filed in the past {RETENTION_DAYS} days in
    <strong>any</strong> of the four regional Court of Common Pleas systems &mdash; Philadelphia,
    Montgomery, Bucks and Delaware &mdash; where at least one party (plaintiff or defendant, not
    their attorney) has an address in {area["blurb"]}. Each case is tagged with the court where it
    was filed. Tax and municipal liens are excluded. In this window: {tally_str}.
</div>

<div class="legend"><span class="lbl">Courts:</span>{legend}</div>

<div class="tabs">
    <div class="tab active" onclick="showTab('new', this)">New Lawsuits ({len(new_cases)})</div>
    <div class="tab" onclick="showTab('foreclosures', this)">Foreclosures ({len(foreclosures)})</div>
    <div class="tab" onclick="showTab('all', this)">All Cases ({len(cases)})</div>
</div>

<div class="content">
    {section(f"New Lawsuits (last {NEW_DAYS} days)", "&#x1f4c4;", new_cases, "new", area)}
    {section("Foreclosures", "&#x1f3e0;", foreclosures, "foreclosures", area)}
    {section(f"All {esc(area['name'])} Cases (last {RETENTION_DAYS} days)", "&#x1f4cb;", cases, "all", area)}
</div>

<div class="footer">
    Source: public dockets of the Philadelphia (First Judicial District), Montgomery, Bucks and
    Delaware County Courts of Common Pleas.<br>
    Always confirm every case against the official court docket before relying on or publishing this information.
</div>

<script>
function showTab(id, el) {{
    document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById(id).classList.add('active');
    el.classList.add('active');
}}
document.getElementById('new').classList.add('active');
</script>

</body>
</html>'''
    return html


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--daily", action="store_true",
                    help="Incremental 3-day fetch window (for the daily cron).")
    ap.add_argument("--lookback", type=int, default=RETENTION_DAYS,
                    help="Fetch window in days for a full run (default 14).")
    ap.add_argument("--area", choices=list(AREAS.keys()),
                    help="Build only this area's dashboard.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch and report; write no state or HTML.")
    args = ap.parse_args()

    fetch_days = 3 if args.daily else args.lookback
    print(f"Regional fetch: window={fetch_days}d  retention={RETENTION_DAYS}d  "
          f"dry_run={args.dry_run}")

    state = load_state()
    run_fetch(state, fetch_days)

    if not args.dry_run:
        save_state(state)
        print(f"State saved: {len(state['matches'])} matches, "
              f"{len(state['evaluated'])} evaluated.")

    for area_key, area in AREAS.items():
        if args.area and area_key != args.area:
            continue
        cases = [r for r in state["matches"].values()
                 if case_matches(r, area["zips"], area["cities"])
                 and (_date(r) or datetime.now().date())
                 >= (datetime.now() - timedelta(days=RETENTION_DAYS)).date()]
        print(f"  {area['name']}: {len(cases)} cases in the {RETENTION_DAYS}-day window")
        if args.dry_run:
            continue
        html = inject_auth(build_dashboard(area_key, area, cases))
        with open(os.path.join(os.path.dirname(STATE_FILE), area["output_html"]), "w") as f:
            f.write(html)
        print(f"    wrote {area['output_html']}")


if __name__ == "__main__":
    main()
