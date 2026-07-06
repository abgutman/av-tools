"""
fjd_party_search.py — Philadelphia CCP civil participant/name search engine.

Source: First Judicial District public e-filing system (fjdefile.phila.gov),
the "Search by Participant Name" surface (zk_fjd_public_qry_01). Returns every
case in a date window where a party's name matches a prefix — plaintiff OR
defendant. State-court public record; non-commercial journalistic use only.

Reverse-engineered mechanics (verified live 2026-07-05), same family as the
docket-report tricks in fjd_docket.py:
  1. GET  zp_personcase_setup_idx  → seeds a session cookie + hidden (uid, o).
  2. POST zp_personcase_details_idx with last_name + begin/end dates. The
     reCAPTCHA token field (hash_code) can be BLANK — the endpoint does not
     enforce it, exactly like the docket report.
  3. The results HTML rides in the 302 *body* (the Location header is a decoy
     back to the setup page), so POST with allow_redirects=False and read r.text.

Match semantics (verified):
  - The name match is a CASE-INSENSITIVE FULL-STRING PREFIX on the party name.
    "PECO" matches "PECO ENERGY COMPANY" (and also "PECOLA"/"PECORA" — filter
    downstream). A term that is not a prefix of the stored name returns nothing.
  - Results are CAPPED AT 50 ROWS, sorted alphabetically. A too-broad prefix
    (e.g. "PHILADELPHIA") silently truncates, so callers pass specific prefixes
    and check `truncated` on the result.

Results table columns: Name/Company | Address | Party Type | Filing Date.
The Case ID + caption are embedded in the Address cell:
  "2301 MARKET STREET PHILADELPHIA PA 19103 Case ID: 260603709 PECO ENERGY ... VS ..."
"""

import re
import time
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("fjd_party_search")

BASE = "https://fjdefile.phila.gov/efsfjd"
SETUP = f"{BASE}/zk_fjd_public_qry_01.zp_personcase_setup_idx"
DETAILS = f"{BASE}/zk_fjd_public_qry_01.zp_personcase_details_idx"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

THROTTLE = 0.6            # polite delay (s) between searches
TIMEOUT = 60
RETRIES = 4
ROW_CAP = 50             # server truncates result sets at 50 rows

_MON = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL",
     "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


class PartySearchSession:
    """Holds a requests.Session and mints the (uid, o) form token per search.

    Unlike the docket token, the name-search token is cheap to harvest (one GET
    of the setup page) and is refreshed on each search to stay valid.
    """

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})

    def _form_token(self, html):
        uid = re.search(r'name="uid"\s+value="([^"]*)"', html, re.I)
        o = re.search(r'name="o"\s+value="([^"]*)"', html, re.I)
        return (uid.group(1) if uid else ""), (o.group(1) if o else "")

    def _post(self, last_name, begin_date, end_date):
        r = self.s.get(SETUP, timeout=TIMEOUT)
        r.raise_for_status()
        uid, o = self._form_token(r.text)
        data = {
            "uid": uid, "o": o,
            "last_name": last_name, "first_name": "", "middle_name": "",
            "soundex_ind": "",           # exact prefix, not phonetic
            "begin_date": begin_date, "end_date": end_date,
            "hash_code": "", "pass_code": "",
        }
        # allow_redirects=False: results ride in the 302 body; the Location
        # header points back at the setup page (a decoy) and must NOT be followed.
        return self.s.post(DETAILS, data=data, headers={"Referer": SETUP},
                           allow_redirects=False, timeout=TIMEOUT)

    def search(self, last_name, begin_date, end_date):
        """Return {rows: [...], truncated: bool}. Retries on network errors.

        Each row: {name, case_id, caption, party_type, filing_date (ISO),
        filing_date_raw, address}.
        """
        backoff = 4
        for attempt in range(1, RETRIES + 1):
            try:
                r = self._post(last_name, begin_date, end_date)
                rows = _parse_results(r.text)
                time.sleep(THROTTLE)
                return {"rows": rows, "truncated": len(rows) >= ROW_CAP}
            except requests.RequestException as e:
                log.warning("  search '%s' attempt %d/%d failed: %s",
                            last_name, attempt, RETRIES, e)
                if attempt < RETRIES:
                    time.sleep(backoff)
                    backoff *= 2
        return {"rows": [], "truncated": False}


def _parse_date(raw):
    """'02-JUL-2026' -> '2026-07-02' (ISO). '' on failure."""
    m = re.match(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})", raw or "")
    if not m:
        return ""
    mon = _MON.get(m.group(2).upper())
    if not mon:
        return ""
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(1)):02d}"


def _split_address(addr_text):
    """Address cell = '<address> Case ID: <case_id> <caption>'.

    Return (address, case_id, caption). Case IDs are alphanumeric
    (e.g. '2607W26001357'), not just digits.
    """
    m = re.search(r"Case ID:\s*(\S+)\s*(.*)$", addr_text, re.S)
    if not m:
        return addr_text.strip(), "", ""
    address = addr_text[:m.start()].strip()
    case_id = m.group(1).strip()
    caption = re.sub(r"\s+", " ", m.group(2)).strip()
    return address, case_id, caption


def _parse_results(html):
    """Parse the participant-search results table into row dicts.

    Uses lxml (FJD emits unclosed <td>/<tr> that html.parser mangles). The
    results table is identified by its column-header row, not document position.
    """
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all("table"):
        head = t.find("tr")
        if not head:
            continue
        hdr = [c.get_text(strip=True) for c in head.find_all(["td", "th"])]
        if hdr[:2] == ["Name/Company", "Address"]:
            rows = []
            for tr in t.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True).replace("\xa0", " ").strip()
                         for c in tr.find_all("td")]
                if len(cells) < 4:
                    continue
                name, addr_text, party_type, filed_raw = cells[0], cells[1], cells[2], cells[3]
                address, case_id, caption = _split_address(addr_text)
                if not case_id:
                    continue
                rows.append({
                    "name": name,
                    "case_id": case_id,
                    "caption": caption,
                    "party_type": party_type,
                    "filing_date": _parse_date(filed_raw),
                    "filing_date_raw": filed_raw,
                    "address": address,
                })
            return rows
    return []
