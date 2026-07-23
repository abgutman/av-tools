"""
scrape_trial_dispositions.py — Daily "how did trial-listed cases end?" scanner.

We already know every case scheduled for trial: the FJD "Trial Dates Certain" (MJ)
calendar lists them, and each row carries a real case_id. So instead of guessing case
numbers, we watch those specific dockets for a terminal disposition — verdict/finding,
settlement, discontinuance, judgment, non pros, default — and feed the day's new ones
into the daily CCP email (see scrape_new_complaints.py).

Detection is a TRACKED POOL, not a day-of check. A case listed for trial often settles
or is continued, and the disposition entry can post days after the trial date. So when a
case first appears on the calendar we record it (as its non-terminal "LISTED FOR TRIAL"
status), then poll its docket daily until the case-level Status field turns terminal.
We report that transition once and retire the case. Cases that never resolve age out of
the pool after AGING_DAYS.

Why the case-level Status field (not docket entry types): a concluded docket's Status
reads "SETTLED PRIOR TO ASSGN TRL JUD" / "FINDING FOR PLAINTIFF" / "JUDGMENT ENTERED",
while its newest *entry* is usually a procedural "NOTICE GIVEN UNDER RULE 236". The
Status is the authoritative disposition. The STATUS_MAP below was built by polling all
~134 then-current trial-listed dockets live (July 2026); see ccp_dockets/README.md.

Reuses the shared engine in fjd_docket.py (DO NOT modify it — see ccp_dockets/CLAUDE.md).
Public record, journalistic / non-commercial use. Always verify against the official
docket before publishing.

Usage:
    python scrape_trial_dispositions.py            # scan, log findings, no state/email
    python scrape_trial_dispositions.py --live     # advance pool state + send email
    python scrape_trial_dispositions.py --window-days 120 --aging-days 45
"""

import argparse
import html as _h
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from fjd_docket import FjdSession, parse_docket, OK  # noqa: E402
from email_utils import send_email                    # noqa: E402

log = logging.getLogger("dispositions")

DATA = HERE / "data"
STATE_FILE = DATA / "state_trial_dispositions.json"
LOG_FILE = DATA / "dispositions_log.json"   # rolling record of dispositions (incl. order text)
LOG_WINDOW_DAYS = 180                        # retention window for the log

BASE = "https://fjdefile.phila.gov/efsfjd/"
MENU = BASE + "zk_fjd_public_qry_04.zp_legalhearlist_menu_idx"
DETAILS = BASE + "zk_fjd_public_qry_04.zp_legalhearlist_details_idx"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

WINDOW_DAYS = 120   # forward MJ calendar window to harvest trial-listed cases
AGING_DAYS = 45     # stop polling a case this many days after its trial date

RECIPIENT = ["agutman@inquirer.com"]
DASHBOARD_URL = "https://abgutman.github.io/av-tools/ccp_dockets_dashboard.html"

_MON3 = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL",
     "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


# --------------------------------------------------------------------------- #
# Disposition classification
# --------------------------------------------------------------------------- #
# Case-level Status string (normalized: upper + single-spaced) -> category label.
# Every value here is TERMINAL (the case has concluded and we report it once).
# Built from a live sweep of all trial-listed dockets; extend from run logs when
# an unknown status surfaces (see classify_status fallback below).
STATUS_MAP = {
    # Finding / verdict (bench or jury decision on the merits)
    "FINDING FOR PLAINTIFF": "Finding / verdict",
    "FINDING FOR DEFENDANT": "Finding / verdict",
    "COURT FINDING": "Finding / verdict",
    "DAMAGES ASSESSED": "Damages assessed",
    # Settlement (agreed resolution)
    "SETTLED PRIOR TO ASSGN TRL JUD": "Settlement",
    "SETTLED AFTER ASSGN TRIAL JUDG": "Settlement",
    "SETTLED BY STIPULATION": "Settlement",
    "STIPULATION - FINAL DISPO": "Settlement",
    "PRAEC/SETTLE DISCONTINUE END": "Settlement",
    # Voluntary discontinuance
    "PRAECIPE TO DISCONTINUE": "Discontinuance",
    # Judgment entered
    "JUDGMENT ENTERED": "Judgment",
    "JUDGMENT ENTERED BY AGREEMENT": "Judgment",
    "JUDGMENT ON COURT'S FINDING": "Judgment",
    "JUDGMENT ON COURT'S ORDER": "Judgment",
    "JUDGMENT-COURT ORDER FINAL DIS": "Judgment",
    "ORDER ENTERED - FINAL DISPOS": "Judgment",
    # Non pros / dismissal for inactivity
    "JUDGMENT OF NON PROS ENTERED": "Non pros (dismissed)",
    # Default judgment
    "COURT ORDERED DEFAULT JUDGMENT": "Default judgment",
    "JUDGMENT ENTERED BY DEFAULT": "Default judgment",
    # Left this court
    "TRANSFER TO ORPHANS COURT": "Transferred out",
}

# Statuses that mean "still pending — keep watching" (not a disposition).
NONTERMINAL_EXACT = {
    "LISTED FOR TRIAL",
    "LISTED IN TRIAL READY POOL",
    "LISTED-PROJ. SETTLEMENT CONF.",
    "LISTED FOR SETTLEMENT CONF",
    "LISTED FOR STATUS CONFERENCE",
    "LISTED-STATUS CONF PARTITION",
    "LISTED FOR PRE-TRIAL CONF",
    "LISTED/ASSESSMNT DAMAGES HRNG",
    "HELD UNDER ADVISEMENT",
    "STAYED BY ORDER OF COURT",
    "DEFERRED - BANKRUPTCY",
}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().upper())


def classify_status(status):
    """Map a case Status to (category, is_terminal, known).

    Exact STATUS_MAP hit wins. Otherwise a conservative keyword fallback so a new
    FJD status string is still caught rather than silently missed. Truly unrecognized
    statuses return (None, False, False) — kept in the pool and logged, then surfaced
    as 'Unclassified' if they age out, so nothing is dropped without a trace.
    """
    s = _norm(status)
    if not s:
        return None, False, False
    if s in STATUS_MAP:
        return STATUS_MAP[s], True, True
    if s in NONTERMINAL_EXACT:
        return None, False, True
    # keep-watching keywords
    if s.startswith("LISTED") or "HELD UNDER ADVISEMENT" in s \
            or "STAYED" in s or s.startswith("DEFERRED") or "CONTINUED" in s:
        return None, False, True
    # terminal keyword fallback (order matters: most specific first)
    if "NON PROS" in s:
        return "Non pros (dismissed)", True, False
    if "DEFAULT" in s:
        return "Default judgment", True, False
    if "SETTLED" in s or "SETTLE" in s:
        return "Settlement", True, False
    if "DISCONTINU" in s:
        return "Discontinuance", True, False
    if "VERDICT" in s or "FINDING FOR" in s:
        return "Finding / verdict", True, False
    if "DISMISS" in s:
        return "Dismissed", True, False
    if "JUDGMENT" in s:
        return "Judgment", True, False
    return None, False, False


# entry-type keywords used only to date a disposition (best-effort)
_ENTRY_KW = {
    "Finding / verdict": ("VERDICT", "FINDING FOR", "COURT FINDING"),
    "Damages assessed": ("DAMAGES", "ASSESS"),
    "Settlement": ("SETTLE", "STIPULAT", "DISCONTINUE"),
    "Discontinuance": ("DISCONTINU",),
    "Judgment": ("JUDGMENT", "ORDER ENTERED"),
    "Non pros (dismissed)": ("NON PROS",),
    "Default judgment": ("DEFAULT",),
    "Dismissed": ("DISMISS",),
    "Transferred out": ("TRANSFER",),
}


def _entry_iso(raw):
    """'09-JUN-2026' -> '2026-06-09'; '' on failure."""
    m = re.match(r"(\d{2})-([A-Z]{3})-(\d{4})", (raw or "").strip().upper())
    if not m:
        return ""
    mon = _MON3.get(m.group(2))
    return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}" if mon else ""


def disposition_date(entries, category):
    """Best-effort ISO date of the disposition: newest docket entry whose type
    matches the category, else the newest entry of any kind. '' if no entries."""
    kws = _ENTRY_KW.get(category, ())
    best = ""
    best_generic = ""
    for e in entries:
        iso = _entry_iso(e.get("date", ""))
        if not iso:
            continue
        if iso > best_generic:
            best_generic = iso
        etype = _norm(e.get("type", ""))
        if kws and any(k in etype for k in kws) and iso > best:
            best = iso
    return best or best_generic


# --------------------------------------------------------------------------- #
# Order text (the wording of the order on the docket — not the purchasable PDF)
# --------------------------------------------------------------------------- #
# The full order language rides in single-cell <tr> rows in the docket-entries
# table (e.g. "IT IS HEREBY ORDERED THAT JUDGMENT OF POSSESSION IS HEREBY
# ENTERED... BY THE COURT: KENNEDY, J. 6/10/2026"). The disposition order is the
# newest such row; scheduling / case-management orders are filtered out.
_ORDER_MARKERS = (
    "IT IS HEREBY ORDERED", "IT IS ORDERED", "ORDERED AND DECREED",
    "THE COURT FINDS", "VERDICT IS ENTERED", "FINDING FOR", "JUDGMENT IN FAVOR",
    "JUDGMENT OF POSSESSION", "JUDGMENT IS", "BY THE COURT", "AWARDS",
    "DAMAGES IN THE", "IS DISCONTINUED", "IS SETTLED", "IS DISMISSED", "NON PROS",
    "FAILURE TO FILE ANSWER",
)
# category -> keywords that identify THIS disposition's order among several orders
_ORDER_CAT_KW = {
    "Finding / verdict": ("FINDING FOR", "VERDICT", "THE COURT FINDS"),
    "Damages assessed": ("DAMAGES", "AWARDS", "THE COURT FINDS"),
    "Settlement": ("SETTLED", "DISCONTINU", "STIPULAT"),
    "Discontinuance": ("DISCONTINU",),
    "Judgment": ("JUDGMENT",),
    "Non pros (dismissed)": ("NON PROS",),
    "Default judgment": ("DEFAULT", "JUDGMENT IN FAVOR", "FAILURE TO FILE ANSWER"),
    "Dismissed": ("DISMISS",),
    "Transferred out": ("TRANSFER",),
}
_CTRL_PREFIX = re.compile(r"^\d{2,3}-\d{6,9}\s*-?\s*")
ORDER_MAX = 800


def extract_order_text(html, category):
    """Return the docket's disposition order wording (excerpt), or '' if none.

    Picks the newest single-cell order-language row; prefers one whose text
    matches the disposition category so a trailing procedural order doesn't win.
    """
    soup = BeautifulSoup(html, "lxml")
    # the docket-entries table carries the order rows
    best_tbl = None
    for t in soup.find_all("table"):
        head = t.find("tr")
        if not head:
            continue
        ht = head.get_text(" ", strip=True).lower()
        if all(l in ht for l in ("filing date", "docket type", "filing party")):
            if best_tbl is None or len(t.find_all("tr")) > len(best_tbl.find_all("tr")):
                best_tbl = t
    if best_tbl is None:
        return ""

    candidates = []
    for tr in best_tbl.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) != 1:
            continue
        txt = re.sub(r"\s+", " ", cells[0].get_text(" ", strip=True).replace("\xa0", " ")).strip()
        if not txt or txt.lower() == "none.":
            continue
        up = txt.upper()
        if (up.startswith("NOTICE GIVEN ON") or up.startswith("CLICK LINK")
                or up.startswith("OF CASE MANAGEMENT") or up.startswith("OF SETTLEMENT")
                or up.startswith("OF ARBITRATION") or up.startswith("E-FILING NUMBER")
                or up.startswith("AFFIDAVIT OF SERVICE") or up == "ADD ALL TO CART"):
            continue
        if not any(m in up for m in _ORDER_MARKERS):
            continue
        # drop scheduling / case-management orders
        if ("ASSIGNED TO THE" in up and "POOL" in up) or "ANTICIPATE TRIAL TO BEGIN" in up:
            continue
        candidates.append(_CTRL_PREFIX.sub("", txt).strip())

    if not candidates:
        return ""
    cat_kw = _ORDER_CAT_KW.get(category, ())
    matches = [c for c in candidates if any(k in c.upper() for k in cat_kw)]
    chosen = (matches or candidates)[-1]
    if len(chosen) > ORDER_MAX:
        chosen = chosen[:ORDER_MAX].rstrip() + " …"
    return chosen


# --------------------------------------------------------------------------- #
# Monetary award (clean $ figure extracted from the order wording)
# --------------------------------------------------------------------------- #
# A dollar amount is public record (routinely published) and NOT PII, so unlike
# the free-text order it is safe to keep in the committed log + digest. We pull it
# from the disposition order text and only for PLAINTIFF-favorable outcomes, so a
# defense verdict's incidental figure is never mislabeled as an award.
_AWARD_MONEY = re.compile(r"\$[\d,]+(?:\.\d{2})?")
_AWARD_PHRASE = re.compile(
    r"(?:TOTAL AWARD OF|AWARD OF|IN THE (?:TOTAL )?AMOUNT OF|AWARDED[^$]{0,40}?"
    r"|DAMAGES[^$]{0,40}?)(\$[\d,]+(?:\.\d{2})?)", re.I)


def _plaintiff_favorable(status, category):
    s = _norm(status)
    return ("FOR PLAINTIFF" in s or "FOR PLTF" in s
            or category in ("Damages assessed", "Default judgment", "Judgment"))


def extract_award(order_text, status, category):
    """Return the plaintiff's monetary award as a clean '$N,NNN.NN' string, or None.

    Prefers an amount that follows an award phrase ('IN THE AMOUNT OF $…', 'TOTAL
    AWARD OF $…'); else the largest dollar figure in the order (drops the $5 filing
    boilerplate). Only for plaintiff-favorable dispositions — ejectment possession
    findings and defense verdicts return None (no monetary award). Best-effort: the
    trial-court figure; verify the molded/final judgment against the docket.
    """
    if not order_text or not _plaintiff_favorable(status, category):
        return None
    ph = _AWARD_PHRASE.findall(order_text)
    if ph:
        return ph[0]
    allm = [m for m in _AWARD_MONEY.findall(order_text) if m not in ("$5", "$5.00")]
    if allm:
        return max(allm, key=lambda x: float(x.replace("$", "").replace(",", "")))
    return None


# --------------------------------------------------------------------------- #
# Trial-calendar (MJ) harvest — self-contained, no cross-project imports
# --------------------------------------------------------------------------- #
def _mj_categories(menu_html):
    """Live cs_MJ codes, minus the empty 'All Programs' pseudo-category (21)."""
    m = re.search(r"var cs_MJ\s*=\s*new Array\((.*?)\)\s*;", menu_html, re.S)
    codes = re.findall(r'"([^"]*)"', m.group(1)) if m else []
    return [c for c in codes if c != "21"]


def _find_table(html):
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all("table"):
        thead = t.find("thead")
        if thead and "Hearing Date" in thead.get_text():
            return t
    return None


def _case_id_from(td):
    a = td.find("a")
    if a:
        m = re.search(r"case_id:\s*'(\d+)'", a.get("onclick", ""))
        if m:
            return m.group(1)
        return a.get_text(strip=True)
    return td.get_text(strip=True)


def _parse_mj_rows(html):
    """Parse one MJ results page -> [{case_id, trial_date, caption, case_type}]."""
    table = _find_table(html)
    out = []
    if not table or not table.find("tbody"):
        return out
    for tr in table.find("tbody").find_all("tr", align="left"):
        td = tr.find_all("td")
        if len(td) < 6:
            continue
        raw_date = td[0].get_text("\n", strip=True).split("\n")[0]
        out.append({
            "case_id": _case_id_from(td[1]),
            "caption": td[2].get_text(strip=True),
            "trial_date": _entry_iso(raw_date),
            "case_type": td[4].get_text(strip=True),
        })
    return out


def fetch_mj_calendar(window_days=WINDOW_DAYS):
    """Return list of trial-listed cases across the forward window (deduped by id)."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": MENU})
    r = s.get(MENU, timeout=60)
    r.raise_for_status()
    cats = _mj_categories(r.text)
    start = date.today()
    s_iso, e_iso = start.isoformat(), (start + timedelta(days=window_days)).isoformat()
    rows = {}
    for cat in cats:
        rr = s.post(DETAILS, timeout=60, data={
            "program": "MJ", "category": cat,
            "sched_date": s_iso, "sched_date_2": e_iso})
        if rr.ok:
            for row in _parse_mj_rows(rr.text):
                if row["case_id"]:
                    rows.setdefault(row["case_id"], row)  # first (earliest) wins
    return list(rows.values())


# --------------------------------------------------------------------------- #
# State (the watched pool)
# --------------------------------------------------------------------------- #
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state):
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def append_to_log(dispositions, today_iso):
    """Merge new dispositions into a rolling record file, deduped by case_id, dropping
    anything older than LOG_WINDOW_DAYS. This is the durable/dashboard-facing capture, so
    the free-text order wording is DELIBERATELY EXCLUDED — the order can carry addresses,
    lockout terms and other party detail we keep out of the committed/public-facing record.
    The order text lives only in the (private, Av-only) email."""
    DATA.mkdir(exist_ok=True)
    existing = []
    if LOG_FILE.exists():
        try:
            existing = json.loads(LOG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []
    merged = {d["case_id"]: d for d in existing}
    for d in dispositions:
        rec = {k: v for k, v in d.items() if k != "order_text"}  # strip order wording
        merged[d["case_id"]] = {**rec, "logged_at": today_iso}
    cutoff = (date.fromisoformat(today_iso) - timedelta(days=LOG_WINDOW_DAYS)).isoformat()
    window = [d for d in merged.values()
              if (d.get("disposition_date") or d.get("logged_at") or "9999-99-99") >= cutoff]
    window.sort(key=lambda d: (d.get("disposition_date") or "", d.get("case_id", "")),
                reverse=True)
    LOG_FILE.write_text(json.dumps(window, indent=2))
    return len(window)


def ensure_files_exist(state, today_iso):
    """Guarantee both data files exist on disk (state pool + rolling log) so the
    nightly workflow's `git add` never aborts — even when we skip the scan
    because the calendar fetch failed or returned 0 rows. Writes the current
    (unchanged) pool and a merged/pruned log; identical content is a no-op diff."""
    save_state(state)
    append_to_log([], today_iso)


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #
def run_scan(session, live, window_days=WINDOW_DAYS, aging_days=AGING_DAYS):
    """Update the pool, poll active dockets, return the day's NEW dispositions.

    Each disposition dict: {case_id, caption, trial_date, category, status,
    disposition_date, plaintiffs, defendants}. State is persisted only when live.
    """
    today = datetime.now(timezone(timedelta(hours=-4))).date()  # ET
    today_iso = today.isoformat()
    state = load_state()

    # 1. Harvest the forward calendar; add unseen cases to the pool.
    try:
        cal = fetch_mj_calendar(window_days)
    except requests.RequestException as e:
        log.error("MJ calendar fetch failed (%s) — skipping disposition scan", e)
        if live:
            ensure_files_exist(state, today_iso)
        return []
    if not cal:
        log.warning("MJ calendar returned 0 rows — possible IP block/site change; "
                    "skipping disposition scan (pool untouched)")
        if live:
            ensure_files_exist(state, today_iso)
        return []
    added = 0
    for c in cal:
        if c["case_id"] not in state:
            state[c["case_id"]] = {
                "caption": c["caption"], "trial_date": c["trial_date"],
                "case_type": c.get("case_type", ""), "first_seen": today_iso,
                "last_status": "", "reported": False, "retired": False,
            }
            added += 1
    log.info("Calendar: %d rows, %d new to pool; pool size %d", len(cal), added, len(state))

    # 2. Poll active cases whose trial date has arrived; classify Status.
    dispositions = []
    polled = 0
    for cid, rec in state.items():
        if rec.get("retired"):
            continue
        trial = rec.get("trial_date") or ""
        # only poll once the trial date has arrived (or is unknown)
        if trial and trial > today_iso:
            continue
        # age-out guard
        aged_out = bool(trial) and today > (date.fromisoformat(trial) + timedelta(days=aging_days))

        status, html = "", ""
        st, dhtml = session.fetch_docket(cid)
        if st == OK:
            parsed = parse_docket(dhtml, cid)
            status = parsed.get("status", "")
            polled += 1
        else:
            log.debug("  %s docket %s — skip this run", cid, st)
            if not aged_out:
                continue

        category, is_terminal, known = classify_status(status)
        rec["last_status"] = status

        if is_terminal:
            # A case that was ALREADY terminal the first time we ever polled it is
            # baselined silently (we can't attribute it to "today") — mirrors the
            # watchlist "no alert on first add" rule. In steady state a case enters
            # the pool as LISTED FOR TRIAL, so a terminal status is a fresh transition.
            first_ever_poll = not rec.get("_polled_before")
            rec["_polled_before"] = True
            rec["retired"] = True
            rec["reported"] = True
            if first_ever_poll and rec.get("first_seen") == today_iso:
                log.info("  %s baselined (already %s on first sight)", cid, _norm(status))
                continue
            disp_date = disposition_date(parsed.get("entries", []), category)
            order_text = extract_order_text(dhtml, category)
            d = {
                "case_id": cid,
                "caption": parsed.get("caption") or rec.get("caption", ""),
                "case_type": parsed.get("case_type") or rec.get("case_type", "") or "UNKNOWN",
                "trial_date": trial,
                "category": category,
                "status": status.strip(),
                "known": known,
                "disposition_date": disp_date or today_iso,
                # award: a clean dollar figure only (public record, NOT PII — unlike the
                # full order_text, which is stripped from the committed log). Carried into
                # dispositions_log.json and the digest so a money verdict shows its amount.
                "award": extract_award(order_text, status, category),
                "order_text": order_text,
                "plaintiffs": parsed.get("plaintiffs", []),
                "defendants": parsed.get("defendants", []),
            }
            dispositions.append(d)
            log.info("  %s DISPOSED %-20s %s | %s", cid, category, status.strip(),
                     d["caption"])
            if not known:
                log.warning("  %s matched via keyword fallback — status %r not in "
                            "STATUS_MAP; consider adding it", cid, status.strip())
        else:
            rec["_polled_before"] = True
            if not known and status:
                log.warning("  %s UNKNOWN status %r — kept watching", cid, status.strip())
            if aged_out:
                rec["retired"] = True
                if not known and status:
                    # never resolved to a known status; surface rather than drop
                    dispositions.append({
                        "case_id": cid,
                        "caption": rec.get("caption", ""),
                        "case_type": rec.get("case_type", "") or "UNKNOWN",
                        "trial_date": trial,
                        "category": "Unclassified — verify",
                        "status": status.strip(),
                        "known": False,
                        "disposition_date": today_iso,
                        "order_text": "",
                        "plaintiffs": [], "defendants": [],
                    })
                    log.warning("  %s aged out UNCLASSIFIED (%r) — reported for review",
                                cid, status.strip())
                else:
                    log.info("  %s aged out unresolved (%s) — retired", cid,
                             _norm(status) or "no status")

    log.info("Polled %d dockets; %d new disposition(s)", polled, len(dispositions))

    if live:
        save_state(state)
        log.info("Pool state saved (%d cases).", len(state))
        # ALWAYS (re)write the rolling log — even with 0 new dispositions — so the
        # file always exists on disk. A zero-disposition night used to leave
        # dispositions_log.json absent, which made the nightly workflow's
        # `git add ... dispositions_log.json` abort (exit 128 under
        # `bash -eo pipefail`) and drop the entire ccp_dockets/data commit.
        total = append_to_log(dispositions, today_iso)
        log.info("Logged %d new disposition(s) to %s (%d in %d-day window).",
                 len(dispositions), LOG_FILE.name, total, LOG_WINDOW_DAYS)
    else:
        log.info("Dry run — pool state/log NOT saved. Pass --live to persist.")
    return dispositions


# --------------------------------------------------------------------------- #
# Email section (imported by scrape_new_complaints.py)
# --------------------------------------------------------------------------- #
def _party_str(plaintiffs, defendants):
    p = "; ".join(plaintiffs[:2]) or "—"
    d = "; ".join(defendants[:2]) or "—"
    ep = f" +{len(plaintiffs)-2}" if len(plaintiffs) > 2 else ""
    ed = f" +{len(defendants)-2}" if len(defendants) > 2 else ""
    return f"{p}{ep}", f"{d}{ed}"


def build_dispositions_section(dispos):
    """HTML fragment: a 'Trial Dispositions' block grouped by CASE TYPE (ejectment,
    med mal, etc.), each row carrying the disposition + the docket order wording.
    Returns '' when there are no dispositions."""
    if not dispos:
        return ""
    from collections import defaultdict
    grouped = defaultdict(list)
    for d in dispos:
        grouped[d.get("case_type") or "UNKNOWN"].append(d)
    order = sorted(grouped.keys(), key=lambda c: (-len(grouped[c]), c))

    TD = "padding:8px 10px;font-size:12px;border-bottom:1px solid #f0f0f0;vertical-align:top;"
    body = ""
    for ctype in order:
        rows = ""
        for d in grouped[ctype]:
            ps, ds = _party_str(d.get("plaintiffs", []), d.get("defendants", []))
            order_text = d.get("order_text", "")
            order_row = ""
            if order_text:
                order_row = f"""
        <tr>
          <td colspan="5" style="padding:2px 10px 12px 10px;border-bottom:1px solid #eee;">
            <span style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#2e7d55;">Order:</span>
            <span style="font-size:12px;color:#444;font-style:italic;line-height:1.5;"> {_h.escape(order_text)}</span>
          </td>
        </tr>"""
            rows += f"""
        <tr>
          <td style="{TD}font-family:monospace;color:#555;white-space:nowrap;">{_h.escape(d['case_id'])}</td>
          <td style="{TD}font-weight:600;">{_h.escape(d.get('caption',''))}<div style="font-weight:400;color:#888;font-size:11px;margin-top:2px;">{_h.escape(ps)} <span style="color:#bbb;">v.</span> {_h.escape(ds)}</div></td>
          <td style="{TD}"><span style="display:inline-block;background:#e8f2ec;color:#14331f;font-weight:600;padding:2px 7px;border-radius:4px;font-size:11px;white-space:nowrap;">{_h.escape(d.get('category',''))}</span><div style="color:#666;font-size:11px;margin-top:3px;">{_h.escape(d.get('status',''))}</div></td>
          <td style="{TD}white-space:nowrap;">{_h.escape(d.get('trial_date',''))}</td>
          <td style="{TD}white-space:nowrap;">{_h.escape(d.get('disposition_date',''))}</td>
        </tr>{order_row}"""
        body += f"""
        <tr>
          <td colspan="5" style="padding:10px 10px 7px;background:#e8f2ec;
              font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
              color:#14331f;border-top:2px solid #b8d8c4;border-bottom:1px solid #d0e8dc;">
            {_h.escape(ctype)}<span style="font-weight:400;color:#666;"> ({len(grouped[ctype])})</span>
          </td>
        </tr>{rows}"""

    return f"""
  <div style="background:white;padding:8px 28px 24px;">
    <h2 style="margin:22px 0 4px;font-size:18px;font-weight:700;color:#14331f;">Trial Dispositions</h2>
    <p style="margin:0 0 18px;font-size:13px;color:#666;background:#f4f8f5;padding:12px 16px;
        border-left:4px solid #2e7d55;border-radius:0 6px 6px 0;">
      How trial-listed cases concluded, grouped by case type and detected from the case
      docket's status field. A trial <em>listing</em> does not mean a trial was held. The
      order text is the wording on the docket (not the sealed/purchasable PDF) — verify
      against the official docket before relying on or publishing this.
    </p>
    <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#f0f0f0;">
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;white-space:nowrap;">Case ID</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Caption / parties</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Disposition</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;white-space:nowrap;">Trial date</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;white-space:nowrap;">Disposed</th>
        </tr>
      </thead>
      <tbody>{body}
      </tbody>
    </table>
    </div>
  </div>"""


def _standalone_email(dispos, run_date):
    """A dispositions-only email, for solo testing of this module."""
    section = build_dispositions_section(dispos)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px 16px;background:#eef0f3;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif;">
<div style="max-width:860px;margin:0 auto;">
  <div style="background:#14331f;padding:24px 28px;border-radius:10px 10px 0 0;">
    <p style="margin:0 0 6px;color:rgba(255,255,255,0.6);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;">CCP Dockets Monitor</p>
    <h1 style="margin:0 0 4px;color:white;font-size:22px;font-weight:700;">Trial Dispositions</h1>
    <p style="margin:0;color:rgba(255,255,255,0.85);font-size:16px;">{len(dispos)} disposition{"s" if len(dispos)!=1 else ""} — {run_date}</p>
  </div>
  {section}
  <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #e9ecef;border-radius:0 0 10px 10px;">
    <p style="margin:0;font-size:12px;color:#aaa;line-height:1.6;">
      Source: First Judicial District of Pennsylvania — fjdefile.phila.gov.<br>
      Always confirm against the official docket before relying on or publishing this information.
    </p>
  </div>
</div></body></html>"""


# --------------------------------------------------------------------------- #
def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="Persist pool state and send a (standalone) email")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--aging-days", type=int, default=AGING_DAYS)
    args = ap.parse_args()

    sess = FjdSession()
    dispos = run_scan(sess, args.live, args.window_days, args.aging_days)

    for d in dispos:
        log.info("  -> %s | %s | %s (%s)", d["case_id"], d["category"],
                 d["status"], d["disposition_date"])

    if args.live and dispos:
        run_date = datetime.now(timezone(timedelta(hours=-4))).strftime("%B %-d, %Y")
        subject = f"CCP Trial Dispositions — {run_date} ({len(dispos)})"
        sent = send_email(subject, _standalone_email(dispos, run_date),
                          log_fn=log.info, to=RECIPIENT)
        log.info("Email %s", "sent" if sent else "skipped (no creds)")
    elif args.live:
        log.info("No new dispositions — email skipped")


if __name__ == "__main__":
    main()
