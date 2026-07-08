#!/usr/bin/env python3
"""
court_fetchers.py — Per-court "recent civil filings" fetchers for the regional
zip-filtered lawsuit dashboards.

Four area courts, one normalized record shape. Each source exposes:

    list_recent(session, date_from, date_to) -> [stub, ...]
        Cheap discovery of recently-commenced civil cases. `stub` carries the
        case number, display fields, a deep-link url, and whatever id the
        detail call needs. NO party addresses yet (except Philadelphia, whose
        details are pre-attached — see below).

    parties_for(session, stub) -> [party, ...]
        The (often network) call that yields litigant addresses. Returns a list
        of {"name", "role", "city", "zip"} — LITIGANTS ONLY (attorneys, team
        leaders, and the court are dropped, so we never flag a case as "local"
        just because the law firm sits in a local zip).

    normalize(stub, parties) -> record
        The shared contract the engine stores and renders:
            {court, case_number, caption, case_type, filing_date,
             status, url, parties:[{name, role, city, zip}]}

Design notes
------------
* Pure `requests` + stdlib only — NO lxml/bs4 — so this runs in the flat
  GitHub Actions deploy repo (which only `pip install requests`).
* Addresses are reduced to CITY + ZIP at fetch time; the full street line is
  never stored or rendered (av-tools is a public repo). The full address stays
  one click away on the official court docket.
* Zip filtering is NOT done here — the engine applies each area's zip set to
  the shared result set. These fetchers return every recent case.

Philadelphia is special: there is no public per-case docket search, so we reuse
the canonical `complaints.json` produced daily by the FJD complaint scan
(ccp_dockets/scrape_new_complaints.py). Its `parties[]` already carry addresses,
so Philadelphia's parties are pre-attached to the stub and `parties_for` is free.
"""

import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlencode, quote

import requests


# ── Party helpers ────────────────────────────────────────────────────────────

# Roles that are NOT the litigant whose residence we care about. Matching on an
# attorney's office zip (or a team-leader/court record) would create false
# "local" hits, so we exclude them everywhere.
_NON_LITIGANT = ("ATTORNEY", "COUNSEL", "TEAM LEADER", "COURT", "JUDGE",
                 "FIRM", "ESQUIRE", "GAL ", "GUARDIAN AD LITEM")

# "<ST> <zip>" anywhere in an address string. We take the LAST occurrence —
# a litigant line ends in its state/zip; any contact info that trails it has no
# state+zip of its own, so the litigant's zip still wins.
_STATE_ZIP = re.compile(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b")


def is_litigant(role):
    """True for plaintiff/defendant-type roles; False for attorneys/court/etc."""
    r = (role or "").upper()
    return not any(tok in r for tok in _NON_LITIGANT)


def derive_city_zip(address):
    """Pull (city, zip) out of a free-text address string. ('', '') if none.

    Zip is reliable. City is best-effort (the last comma-separated chunk before
    the state) and may be ''; the engine prefers its own zip→municipality map
    for display, so an empty city here is harmless.
    """
    if not address:
        return "", ""
    matches = list(_STATE_ZIP.finditer(address))
    if not matches:
        return "", ""
    last = matches[-1]
    zp = last.group(2)
    before = address[:last.start()].strip().rstrip(",").strip()
    chunk = before.split(",")[-1].strip()
    # Only trust a short trailing chunk as the city; long runs are street lines.
    city = chunk.title() if chunk and len(chunk.split()) <= 3 else ""
    return city, zp


def make_party(name, role, address="", city="", zip_code=""):
    """Normalize one party to {name, role, city, zip}. Drops the street line."""
    if address and not zip_code:
        city, zip_code = derive_city_zip(address)
    return {
        "name": (name or "").strip(),
        "role": (role or "").strip(),
        "city": (city or "").strip(),
        "zip": (zip_code or "").strip(),
    }


def caption_from(plaintiff, defendant):
    p = (plaintiff or "").strip() or "—"
    d = (defendant or "").strip() or "—"
    return f"{p} v. {d}"


# Administrative filings that are not adversarial lawsuits. Tax/municipal liens
# flood the PSI feeds (esp. Bucks) and are excluded from the "new lawsuits" view.
# Philadelphia is already lien-filtered upstream in complaints.json; Delaware
# drops liens at the source query. This catches the rest court-side.
_EXCLUDE_TYPE = ("lien",)


def is_excluded_type(case_type):
    ct = (case_type or "").lower()
    return any(tok in ct for tok in _EXCLUDE_TYPE)


# ── Base class ───────────────────────────────────────────────────────────────

class CourtSource:
    court = ""          # canonical key: philadelphia | montgomery | bucks | delaware
    label = ""          # display name

    def list_recent(self, session, date_from, date_to):
        raise NotImplementedError

    def parties_for(self, session, stub):
        raise NotImplementedError

    def normalize(self, stub, parties):
        return {
            "court": self.court,
            "case_number": stub["case_number"],
            "caption": stub.get("caption", ""),
            "case_type": stub.get("case_type", ""),
            "filing_date": stub.get("filing_date", ""),   # YYYY-MM-DD
            "status": stub.get("status", ""),
            "url": stub.get("url", ""),
            "parties": parties,
        }


# ── Montgomery + Bucks — "PSI" Web Viewer (same ASP.NET software) ─────────────

class PsiSource(CourtSource):
    """Prothonotary PSI viewer. Montgomery and Bucks differ only by base URL."""

    # Grid row → (internal_id, case_number). Case-insensitive: Montco serves
    # /psi/, Bucks redirects to /PSI/. Tolerant of column layout — we only need
    # the detail-link id and the case number; full data comes from the POST.
    _GRID = re.compile(
        r"/v/detail/Case/(\d+)['\"]?[^>]*>\s*Select\s*</a>.*?<td>\s*(20\d{2}-\d+)\s*</td>",
        re.I | re.S,
    )

    def __init__(self, court, label, base):
        self.court = court
        self.label = label
        self.base = base.rstrip("/")

    def _search_url(self, date_from, date_to, count=50, skip=0):
        params = {
            "Q": "", "IncludeSoundsLike": "false", "Count": str(count),
            "fromAdv": "1", "CaseNumber": "", "ParcelNumber": "",
            "CaseType": "", "DateCommencedFrom": date_from,
            "DateCommencedTo": date_to,
            "IncludeInitialFilings": "false", "IncludeInitialEFilings": "false",
            "FilingType": "", "FilingDateFrom": "", "FilingDateTo": "",
            "IncludeSubsequentFilings": "false", "IncludeSubsequentEFilings": "false",
            "Court": "C", "JudgeID": "", "Attorney": "", "AttorneyID": "",
            "Grid": "true", "Sort": "DateCommenced desc",
        }
        if skip:
            params["Skip"] = str(skip)
        return f"{self.base}/psi/v/search/case?" + urlencode(params, quote_via=quote)

    def list_recent(self, session, date_from, date_to):
        # Bootstrap the session cookie (PSI bounces a cold request).
        session.get(f"{self.base}/psi/v/search/case", timeout=30)
        df = date_from.strftime("%m/%d/%Y")
        dt = date_to.strftime("%m/%d/%Y")
        stubs, skip, page_size = [], 0, 50
        while True:
            # The grid query is slow under wide date windows — give it room.
            resp = session.get(self._search_url(df, dt, page_size, skip), timeout=90)
            rows = self._GRID.findall(resp.text)
            if not rows:
                break
            for internal_id, case_number in rows:
                stubs.append({
                    "case_number": case_number,
                    "internal_id": internal_id,
                    "url": f"{self.base}/psi/v/detail/Case/{internal_id}",
                })
            if len(rows) < page_size:
                break
            skip += page_size
            time.sleep(0.2)
        # De-dupe by case number, preserving order.
        seen, out = set(), []
        for s in stubs:
            if s["case_number"] not in seen:
                seen.add(s["case_number"])
                out.append(s)
        return out

    def _detail(self, session, internal_id):
        url = f"{self.base}/psi/v/detail/Case/{internal_id}/data"
        try:
            resp = session.post(url, json={"DocketRange": "100"}, timeout=20)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            return None
        return None

    @staticmethod
    def _detail_field(detail_html, label):
        """Read one value from the flat 'LABEL | VALUE' detail definition list."""
        m = re.search(
            re.escape(label) + r"\s*</[^>]+>\s*<[^>]+>\s*([^<]+)", detail_html)
        return m.group(1).strip() if m else ""

    def parties_for(self, session, stub):
        data = self._detail(session, stub["internal_id"])
        stub["_detail_data"] = data  # cache for normalize()
        if not data:
            return []
        parties = []
        for html in data.get("Relates", []):
            head = html[:400]
            if "Plaintiff" in head:
                role = "Plaintiff"
            elif "Defendant" in head:
                role = "Defendant"
            else:
                continue
            for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S):
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)
                if len(cells) < 2:
                    continue
                # First non-action cell is the name; the next is the address.
                name_idx = None
                for i, cell in enumerate(cells):
                    if "Select" in cell or "noprint" in cell:
                        continue
                    name_idx = i
                    break
                if name_idx is None or name_idx + 1 >= len(cells):
                    continue
                name = re.sub(r"<[^>]+>", "", cells[name_idx]).strip()
                name = re.split(r"\s*\(", name)[0].strip()  # drop alias parens
                addr_raw = re.sub(r"<br\s*/?>", ", ", cells[name_idx + 1])
                addr = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", addr_raw)).strip()
                if name and name.lower() != "name" and is_litigant(role):
                    parties.append(make_party(name, role, address=addr))
        return parties

    def normalize(self, stub, parties):
        data = stub.get("_detail_data") or {}
        detail = data.get("Detail", "") if isinstance(data, dict) else ""
        plaintiff = self._detail_field(detail, "Caption Plaintiff")
        defendant = self._detail_field(detail, "Caption Defendant")
        case_type = self._detail_field(detail, "Case Type")
        commenced = self._detail_field(detail, "Commencement Date")
        status = self._detail_field(detail, "Status")
        rec = super().normalize(stub, parties)
        rec["caption"] = caption_from(plaintiff, defendant) if (plaintiff or defendant) else stub.get("caption", "")
        rec["case_type"] = case_type
        rec["status"] = status
        rec["filing_date"] = _to_iso(commenced)
        return rec


# ── Delaware — Thomson Reuters C-Track REST API ──────────────────────────────

class CTrackSource(CourtSource):
    court = "delaware"
    label = "Delaware"
    API = "https://delcopublicaccessapi.co.delaware.pa.us/api/v1"
    PORTAL = "https://delcopublicaccess.co.delaware.pa.us"
    SKIP_TYPES = {"Municipal Lien", "Lien", "Non-Reportable"}

    def list_recent(self, session, date_from, date_to):
        df = date_from.strftime("%Y-%m-%d")
        dt = date_to.strftime("%Y-%m-%d")
        stubs, page, total = [], 1, None
        while True:
            cases, total_hdr = self._search(session, df, dt, page)
            if not cases:
                break
            if total is None:
                total = total_hdr
            stubs.extend(cases)
            if total and len(stubs) >= total:
                break
            page += 1
            time.sleep(0.4)
        stubs = [s for s in stubs if s["case_type"] not in self.SKIP_TYPES]
        # De-dup by case number (pagination may repeat items near page boundaries).
        seen, out = set(), []
        for s in stubs:
            if s["case_number"] and s["case_number"] not in seen:
                seen.add(s["case_number"])
                out.append(s)
        return out

    def _search(self, session, df, dt, page):
        params = {
            "queryString": "true",
            "searchFields[0].searchType": "",
            "searchFields[0].operation": ">=",
            "searchFields[0].values[0]": df,
            "searchFields[0].indexFieldName": "filedDate",
            "searchFields[1].searchType": "",
            "searchFields[1].operation": "<=",
            "searchFields[1].values[0]": dt,
            "searchFields[1].indexFieldName": "filedDate",
            "page": str(page), "pageSize": "20",
            "sortField": "filedDate", "sortDirection": "desc",
        }
        url = f"{self.API}/cases/search?{urlencode(params)}"
        for attempt in range(3):
            try:
                resp = session.get(url, timeout=25)
                break
            except Exception:
                if attempt == 2:
                    return [], 0
                time.sleep(3)
        if resp.status_code != 200:
            return [], 0
        data = resp.json()
        total = int(resp.headers.get("X-CTrack-Paging-TotalCount", "0"))
        out = []
        for item in data.get("resultItems", []):
            row = item.get("rowMap", {})
            filed = (row.get("filedDate") or "")[:10]
            out.append({
                "case_number": row.get("caseNumber", ""),
                "case_id": row.get("caseID", ""),
                "caption": row.get("shortTitle", ""),
                "case_type": row.get("caseType", ""),
                "classification": row.get("caseClassification", ""),
                "filing_date": filed,
                "url": f"{self.PORTAL}/#/cases/{row.get('caseID', '')}",
            })
        return out, total

    def parties_for(self, session, stub):
        url = f"{self.API}/cases/{stub['case_id']}/parties"
        raw = []
        for attempt in range(3):
            try:
                resp = session.get(url, timeout=25)
                if resp.status_code == 200:
                    raw = resp.json()
                break
            except Exception:
                if attempt == 2:
                    return []
                time.sleep(2)
        parties = []
        for group in raw:
            members = group if isinstance(group, list) else [group]
            for p in members:
                if not isinstance(p, dict):
                    continue
                name_info = p.get("partyName") or {}
                if not name_info:
                    continue
                role = name_info.get("role", "")
                if not is_litigant(role):
                    continue
                addr = p.get("address") or {}
                parties.append(make_party(
                    name_info.get("displayName", ""),
                    role,
                    city=addr.get("city", ""),
                    zip_code=(addr.get("postalCode") or "").strip()[:5],
                ))
        return parties

    def normalize(self, stub, parties):
        rec = super().normalize(stub, parties)
        # C-Track classification is more descriptive than the bare case type.
        rec["case_type"] = stub.get("classification") or stub.get("case_type", "")
        return rec


# ── Philadelphia — reuse complaints.json from the FJD complaint scan ──────────

class PhillySource(CourtSource):
    court = "philadelphia"
    label = "Philadelphia"

    def __init__(self, complaints_path=None):
        self.complaints_path = complaints_path or _find_complaints_json()

    def list_recent(self, session, date_from, date_to):
        path = self.complaints_path
        if not path or not os.path.exists(path):
            print(f"  [philadelphia] complaints.json not found ({path}) — skipping Philadelphia.")
            return []
        try:
            cases = json.loads(open(path).read())
        except Exception as e:
            print(f"  [philadelphia] could not read complaints.json: {e}")
            return []
        stubs = []
        for c in cases:
            stubs.append({
                "case_number": c.get("case_id", ""),
                "caption": c.get("caption", ""),
                "case_type": c.get("case_type", ""),
                "filing_date": c.get("filing_date", ""),
                "status": c.get("status", ""),
                "url": "",  # FJD has no stable public per-case docket URL
                "_raw_parties": c.get("parties", []),
            })
        return stubs

    def parties_for(self, session, stub):
        parties = []
        for p in stub.get("_raw_parties", []):
            role = p.get("type", "")
            if not is_litigant(role):
                continue
            parties.append(make_party(p.get("name", ""), role,
                                      address=p.get("address", "")))
        return parties


# ── Shared utilities ─────────────────────────────────────────────────────────

def _to_iso(mdy):
    """'6/10/2026' or '6/10/2026 9:00:44 AM' -> '2026-06-10'. Pass-through else."""
    if not mdy:
        return ""
    token = mdy.strip().split()[0]
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(token, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return token


def _find_complaints_json():
    """Locate the FJD complaints.json across dev and flat-deploy layouts."""
    env = os.environ.get("REGION_COMPLAINTS")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "ccp_dockets", "data", "complaints.json"),  # dev (local/)
        os.path.join(os.getcwd(), "ccp_dockets", "data", "complaints.json"),  # flat deploy
        os.path.join(here, "ccp_dockets", "data", "complaints.json"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    return os.path.abspath(candidates[0])


def build_sources(complaints_path=None):
    """The four court sources, in display order."""
    return [
        PhillySource(complaints_path),
        PsiSource("montgomery", "Montgomery", "https://courtsapp.montcopa.org"),
        PsiSource("bucks", "Bucks", "https://propublic.buckscountyonline.org"),
        CTrackSource(),
    ]


def philly_last_updated(path=None):
    """Return the mtime of complaints.json as a datetime, or None if absent."""
    p = path or _find_complaints_json()
    if not p or not os.path.exists(p):
        return None
    return datetime.fromtimestamp(os.path.getmtime(p))


def new_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/html, */*",
    })
    return s
