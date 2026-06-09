"""
fjd_docket.py — Philadelphia CCP civil docket scraper engine.

Source: First Judicial District public e-filing system (fjdefile.phila.gov).
Pulls full civil docket reports without touching reCAPTCHA or the paid document
cart. State-court public record; non-commercial journalistic use only.

How it works (see ccp_dockets/README.md for the full reverse-engineering notes):
  1. mint a session token from the public calendar (no CAPTCHA on calendar/dockets)
  2. POST that token + a case_id to zk_fjd_public_qry_03.zp_dktrpt_frames
  3. the full docket HTML rides in the 302 *body* (the Location header is a decoy),
     so we POST with allow_redirects=False and read r.text

case_id format: YYMM + 5-digit sequence, incrementing in filing order
  e.g. 260600784 = filed June 2026, sequence 784. A non-existent sequence returns
  a short "The case does not exist or is unavailable" page (end-of-stack sentinel).
"""

import re
import time
import logging
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("fjd_docket")

BASE = "https://fjdefile.phila.gov/efsfjd"
MENU = f"{BASE}/zk_fjd_public_qry_04.zp_legalhearlist_menu_idx"
DETAILS = f"{BASE}/zk_fjd_public_qry_04.zp_legalhearlist_details_idx"
DKTRPT = f"{BASE}/zk_fjd_public_qry_03.zp_dktrpt_frames"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

THROTTLE = 0.6            # polite delay (s) between docket requests
TIMEOUT = 60
RETRIES = 4

# response classification
OK, MISSING, BOUNCE = "ok", "missing", "bounce"

_MONTHS = {m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], 1)}


# --------------------------------------------------------------------------- #
# Session / token minting
# --------------------------------------------------------------------------- #
class FjdSession:
    """Holds a requests.Session plus a generic docket token (uid, o).

    One minted token unlocks ANY case_id (the token is session-bound, not
    case-bound), so we mint once and reuse it across many fetches, re-minting
    only when the server bounces us back to the search menu.
    """

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self.uid = None
        self.o = None
        self.mint()

    def _form_token(self, html):
        uid = re.search(r'NAME="uid"\s+VALUE="([^"]*)"', html, re.I)
        o = re.search(r'NAME="o"\s+VALUE="([^"]*)"', html, re.I)
        return (uid.group(1) if uid else ""), (o.group(1) if o else "")

    def mint(self):
        """Seed a session and harvest a docket (uid, o) from a calendar query."""
        r = self.s.get(MENU, timeout=TIMEOUT)
        r.raise_for_status()
        muid, mo = self._form_token(r.text)
        # A 30-day forward window of CM civil motions always returns rows, each
        # carrying a docket onclick with the (uid, o) we need.
        today = datetime.now().date()
        data = {
            "program": "CM", "category": "4", "judge_id": "XXX",
            "sched_date": today.isoformat(),
            "sched_date_2": (today + timedelta(days=30)).isoformat(),
            "pool_month": "", "pool_year": "",
            "uid": muid, "o": mo, "hash_code": "", "pass_code": "",
        }
        r2 = self.s.post(DETAILS, data=data,
                         headers={"Referer": MENU}, timeout=TIMEOUT)
        m = re.search(
            r"zp_dktrpt_frames',\s*\{\s*case_id:\s*'[^']*',\s*"
            r"uid:\s*'([^']*)',\s*o:\s*'([^']*)'", r2.text)
        if not m:
            raise RuntimeError("mint failed: no docket token in calendar results")
        self.uid, self.o = m.group(1), m.group(2)
        log.info("minted docket token uid=%s", self.uid)
        return self.uid, self.o

    def _post_docket(self, case_id):
        # allow_redirects=False: the docket HTML is in the 302 body; the Location
        # header points at the search menu (a decoy) and must NOT be followed.
        return self.s.post(
            DKTRPT,
            data={"case_id": case_id, "uid": self.uid, "o": self.o,
                  "hash_code": "", "pass_code": ""},
            headers={"Referer": DETAILS},
            allow_redirects=False, timeout=TIMEOUT,
        )

    def fetch_docket(self, case_id):
        """Return (status, html). status in {OK, MISSING, BOUNCE}.

        Auto re-mints once on a bounce. Network errors retried with backoff.
        """
        backoff = 4
        for attempt in range(1, RETRIES + 1):
            try:
                r = self._post_docket(case_id)
                html = r.text
                status = classify(html)
                if status == BOUNCE and attempt == 1:
                    log.info("bounce on %s — re-minting token", case_id)
                    self.mint()
                    continue
                time.sleep(THROTTLE)
                return status, html
            except requests.RequestException as e:
                log.warning("  %s attempt %d/%d failed: %s",
                            case_id, attempt, RETRIES, e)
                if attempt < RETRIES:
                    time.sleep(backoff)
                    backoff *= 2
        return BOUNCE, ""


def classify(html):
    if "does not exist or is unavailable" in html:
        return MISSING
    if "Case ID:</b>" in html or "Case Caption:</b>" in html:
        return OK
    return BOUNCE


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _field(html, label):
    """Header fields render as `<b>LABEL:</b><td>&nbsp;VALUE<`. Some values sit
    one line down, so collapse whitespace first."""
    flat = re.sub(r"\s+", " ", html)
    m = re.search(re.escape(f"{label}:</b>") + r"\s*(?:</td>\s*)?<td>\s*&nbsp;([^<]*)", flat)
    return m.group(1).strip() if m else ""


def _parse_filing_date(raw):
    """'Thursday , June 04th, 2026' -> '2026-06-04' (ISO). '' on failure."""
    m = re.search(r"([A-Za-z]+)\s+0*(\d{1,2})[a-z]{2},?\s+(\d{4})", raw)
    if not m:
        return ""
    mon = _MONTHS.get(m.group(1).upper())
    if not mon:
        return ""
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"


def _find_table(soup, *col_labels):
    """Find the docket table whose header row carries all col_labels.

    FJD's malformed HTML makes lxml reorder tables relative to their <h3>
    headings, so we match on the column header row instead. Several spurious
    1-row artifact tables share the same header, so we return the richest match.
    """
    best = None
    for t in soup.find_all("table"):
        head = t.find("tr")
        if not head:
            continue
        htext = head.get_text(" ", strip=True).lower()
        if all(lbl.lower() in htext for lbl in col_labels):
            if best is None or len(t.find_all("tr")) > len(best.find_all("tr")):
                best = t
    return best


def _parties(soup):
    """Return list of {seq, type, name, address}. Address rows follow each party."""
    t = _find_table(soup, "Seq", "Assoc", "Name")
    out = []
    if not t:
        return out
    cur = None
    for tr in t.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        txts = [c.get_text(" ", strip=True).replace("\xa0", " ").strip() for c in cells]
        # data row: seq | assoc | expn | type | name  (5 cells, first numeric)
        if len(txts) >= 5 and txts[0].isdigit():
            cur = {"seq": txts[0], "type": txts[3], "name": txts[4], "address": ""}
            out.append(cur)
        elif cur is not None and txts and txts[0].lower().startswith("address"):
            addr = " ".join(t for t in txts if t and not t.lower().startswith("address"))
            cur["address"] = re.split(r"\s*Aliases:", addr)[0].strip()
    return out


def _entries(soup):
    """Return docket entries [{date, time, type, party}] newest-last as listed."""
    t = _find_table(soup, "Filing Date", "Docket Type", "Filing Party")
    out = []
    if not t:
        return out
    for tr in t.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        dt = cells[0].get_text(" ", strip=True).replace("\xa0", " ")
        m = re.search(r"(\d{2}-[A-Z]{3}-\d{4})\s*(\d{1,2}:\d{2}\s*[AP]M)?", dt)
        if not m:
            continue
        out.append({
            "date": m.group(1),
            "time": (m.group(2) or "").strip(),
            "type": cells[1].get_text(" ", strip=True).replace("\xa0", " "),
            "party": cells[2].get_text(" ", strip=True).replace("\xa0", " "),
        })
    return out


def _events(soup):
    """Case Event Schedule [{event, datetime, room, location, judge}]."""
    t = _find_table(soup, "Event", "Room", "Judge")
    out = []
    if not t:
        return out
    for tr in t.find_all("tr"):
        cells = [c.get_text(" ", strip=True).replace("\xa0", " ")
                 for c in tr.find_all("td")]
        if len(cells) >= 5 and cells[0]:
            out.append({"event": cells[0], "datetime": cells[1],
                        "room": cells[2], "location": cells[3], "judge": cells[4]})
    return out


def parse_docket(html, case_id=None):
    """Parse a docket HTML body into a structured dict."""
    # lxml parses FJD's malformed tables (unclosed <td>/<tr>) cleanly;
    # html.parser merges cells and breaks party/entry extraction.
    soup = BeautifulSoup(html, "lxml")
    filing_raw = _field(html, "Filing Date")
    entries = _entries(soup)
    parties = _parties(soup)
    last = entries[-1] if entries else None
    plaintiffs = [p["name"] for p in parties if "PLAINTIFF" in p["type"].upper()
                  and "ATTORNEY" not in p["type"].upper()]
    defendants = [p["name"] for p in parties if "DEFENDANT" in p["type"].upper()
                  and "ATTORNEY" not in p["type"].upper()]
    return {
        "case_id": _field(html, "Case ID") or (case_id or ""),
        "caption": _field(html, "Case Caption"),
        "filing_date": _parse_filing_date(filing_raw),
        "filing_date_raw": filing_raw,
        "court": _field(html, "Court"),
        "case_type": _field(html, "Case Type"),
        "jury": _field(html, "Jury"),
        "status": _field(html, "Status"),
        "plaintiffs": plaintiffs,
        "defendants": defendants,
        "parties": parties,
        "events": _events(soup),
        "entries": entries,
        "last_entry": last,
        "entry_count": len(entries),
    }


def docket_signature(parsed):
    """Stable change-detection key: entry count + newest entry text."""
    last = parsed.get("last_entry") or {}
    return f"{parsed.get('entry_count', 0)}|{last.get('date', '')}|{last.get('type', '')}"


# --------------------------------------------------------------------------- #
# case_id helpers
# --------------------------------------------------------------------------- #
def current_yymm(dt=None):
    dt = dt or datetime.now()
    return f"{dt.year % 100:02d}{dt.month:02d}"


def make_case_id(yymm, seq):
    return f"{yymm}{seq:05d}"
