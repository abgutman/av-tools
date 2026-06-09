"""
Build clean dashboard dataset for the two tracked programs:
  CM  Civil Motions Program Hearings  (motion hearings)
  MJ  Trial Dates Certain

Reads the immutable raw HTML saved by the spike scrape (no network calls), re-parses
each program with its correct column layout, deduplicates, and writes a single JSON
that the dashboard embeds. Re-runnable: same raw HTML -> same output.

CM layout (8 data columns): Hearing Date/Time/Room | Case ID | Caption | Event Type |
                            Event Judge | Court Type | Case Type | Attorney Name
MJ layout (6 data columns): Hearing Date/Time/Room | Case ID | Caption | # Trial Days |
                            Case Type | Attorney Name
(The spike's generic parser mis-mapped CM's extra columns; this builder fixes that.)

Source: https://fjdefile.phila.gov/efsfjd/ (FJD public e-filing hearing lists)
Use is journalistic / non-commercial. Always verify against the source docket.
"""

import re
import csv
import glob
import json
import os
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

HERE = Path(__file__).parent
RAW_HTML = HERE / "data" / "raw" / "raw_html"
MENU_HTML = RAW_HTML / "MENU_0_menu.html"
OUT_JSON = HERE / "data" / "dashboard_data.json"

# Provenance: when the underlying raw HTML was captured (from the spike run).
SCRAPED_AT = "2026-06-09T15:15:37Z"  # CM/MJ collected in the main run
SOURCE_URL = "https://fjdefile.phila.gov/efsfjd/zk_fjd_public_qry_04.zp_legalhearlist_menu_idx"

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def iso_date(raw: str) -> str:
    """'09-JUN-2026' -> '2026-06-09'. Returns '' if unparseable."""
    m = re.match(r"(\d{1,2})-([A-Z]{3})-(\d{4})", raw.strip().upper())
    if not m:
        return ""
    d, mon, y = m.groups()
    if mon not in MONTHS:
        return ""
    return f"{y}-{MONTHS[mon]:02d}-{int(d):02d}"


def category_labels(html: str = None) -> dict:
    """Map every category code -> human label, from the menu page JS arrays.

    Pass live menu HTML, or leave None to read the saved menu file.
    """
    if html is None:
        html = MENU_HTML.read_text(encoding="utf-8")

    def arr(name):
        m = re.search(r"var\s+" + name + r"\s*=\s*new Array\((.*?)\)\s*;", html, re.S)
        return re.findall(r'"([^"]*)"', m.group(1)) if m else []

    return dict(zip(arr("cs"), arr("csd")))


def find_table(html: str):
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all("table"):
        thead = t.find("thead")
        if thead and "Hearing Date" in thead.get_text():
            return t
    return None


def split_datetime_cell(td) -> tuple:
    lines = [l.strip() for l in td.get_text("\n", strip=True).split("\n") if l.strip()]
    raw_date = lines[0] if len(lines) > 0 else ""
    time = lines[1] if len(lines) > 1 else ""
    room = lines[2] if len(lines) > 2 else ""
    location = " / ".join(lines[3:]) if len(lines) > 3 else ""
    return raw_date, time, room, location


def case_id_from(td) -> str:
    a = td.find("a")
    if a:
        m = re.search(r"case_id:\s*'(\d+)'", a.get("onclick", ""))
        if m:
            return m.group(1)
        return a.get_text(strip=True)
    return td.get_text(strip=True)


def attorneys_from(td) -> str:
    for i in td.find_all("i"):
        i.decompose()
    parts = [p.strip() for p in td.get_text("|").split("|") if p.strip()]
    return "; ".join(parts)


def parse_cm_rows(html: str) -> list:
    """Parse a CM (Civil Motions) results page. 8 data columns. Pure string in."""
    table = find_table(html)
    out = []
    if not table or not table.find("tbody"):
        return out
    for tr in table.find("tbody").find_all("tr", align="left"):
        td = tr.find_all("td")
        if len(td) < 8:
            continue
        raw_date, time, room, location = split_datetime_cell(td[0])
        out.append({
            "program": "CM",
            "program_label": "Civil Motions",
            "case_id": case_id_from(td[1]),
            "caption": td[2].get_text(strip=True),
            "raw_date": raw_date,
            "iso_date": iso_date(raw_date),
            "time": time,
            "room": room,
            "location": location,
            "event_type": td[3].get_text(strip=True),     # MOTION HEARING, ORAL ARGUMENT, ...
            "judge": td[4].get_text(strip=True),
            "court_type": td[5].get_text(strip=True),      # underlying case program
            "case_type": td[6].get_text(strip=True),
            "attorneys": attorneys_from(td[7]),
            "trial_days": "",
        })
    return out


def parse_mj_rows(html: str, cat: str, labels: dict) -> list:
    """Parse one MJ (Trial Dates Certain) sub-category page. 6 data columns."""
    table = find_table(html)
    out = []
    if not table or not table.find("tbody"):
        return out
    for tr in table.find("tbody").find_all("tr", align="left"):
        td = tr.find_all("td")
        if len(td) < 6:
            continue
        raw_date, time, room, location = split_datetime_cell(td[0])
        out.append({
            "program": "MJ",
            "program_label": "Trial Dates Certain",
            "case_id": case_id_from(td[1]),
            "caption": td[2].get_text(strip=True),
            "raw_date": raw_date,
            "iso_date": iso_date(raw_date),
            "time": time,
            "room": room,
            "location": location,
            "event_type": "",
            "judge": "",
            "court_type": labels.get(cat, ""),         # case-type grouping from category
            "case_type": td[4].get_text(strip=True),
            "attorneys": attorneys_from(td[5]),
            "trial_days": td[3].get_text(strip=True),   # # of trial days
        })
    return out


def parse_cm() -> list:
    """Offline: read saved CM raw HTML and parse."""
    f = RAW_HTML / "CM_4_XXX.html"
    if not f.exists():
        raise FileNotFoundError(f"Missing CM raw HTML: {f}")
    return parse_cm_rows(f.read_text(encoding="utf-8"))


def parse_mj(labels: dict) -> list:
    """Offline: read saved MJ sub-category HTML files and parse + union."""
    out = []
    for f in sorted(glob.glob(str(RAW_HTML / "MJ_*.html"))):
        cat = re.search(r"MJ_(\w+)\.html", os.path.basename(f)).group(1)
        if cat == "21":  # "All Programs" pseudo-category returns nothing for MJ
            continue
        out.extend(parse_mj_rows(Path(f).read_text(encoding="utf-8"), cat, labels))
    return out


def dedup(rows: list) -> list:
    seen, out = set(), []
    for r in rows:
        k = (r["program"], r["case_id"], r["iso_date"], r["time"], r["room"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def main():
    labels = category_labels()
    cm = dedup(parse_cm())
    mj = dedup(parse_mj(labels))
    rows = cm + mj
    rows.sort(key=lambda r: (r["iso_date"] or "9999", r["time"], r["program"]))

    payload = {
        "meta": {
            "scraped_at": SCRAPED_AT,
            "source_url": SOURCE_URL,
            "programs": {
                "CM": "Civil Motions (motion hearings, all judges)",
                "MJ": "Trial Dates Certain",
            },
            "date_range": {"start": "2026-06-09", "end": "2026-09-09"},
            "counts": {"CM": len(cm), "MJ": len(mj), "total": len(rows)},
            "note": "Calendar listing only. Verify every detail against the official "
                    "FJD docket before publishing.",
        },
        "hearings": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"CM hearings: {len(cm)}")
    print(f"MJ hearings: {len(mj)}")
    print(f"Total:       {len(rows)}")
    print(f"Wrote {OUT_JSON}")


if __name__ == "__main__":
    main()
