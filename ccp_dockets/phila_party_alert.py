#!/usr/bin/env python3
"""
phila_party_alert.py — Daily morning alert of NEW Philadelphia civil court cases
where a party is (1) a Philadelphia government/public body whose name contains
"Philadelphia", or (2) a Philadelphia elected or top-cabinet official.

Two courts, one email (to Av + Sean Walsh):
  • PCCP — Philadelphia Court of Common Pleas (civil). Source: the FJD
    participant-name index. Reuses fjd_party_search.PartySearchSession and the
    truncation-safe window search from scrape_name_watch.py.
  • EDPA — U.S. District Court, Eastern District of Pennsylvania (civil).
    Source: CourtListener /search/?type=d&court=paed (the query shape from
    fetch_civil.py). Matches the caption AND any structured party names.

"New" = a rolling WINDOW_DAYS lookback, deduped against a seen-state file
(data/state_phila_alert.json). This mirrors the sibling party-watch: a filing
that was filed yesterday but indexed today is still caught. Each row shows its
ACTUAL filing date. First run per (court, list) seeds SILENTLY — no backlog blast.

Two journalist-editable watchlists:
  • phila_gov_watch.json — public entities (curated allowlist; format = name_watch.json)
  • phila_officials.json  — elected + top-cabinet officials. Matched on FULL name.
    A personal-name match is treated as POSSIBLE, not confirmed — the party could
    be a namesake — so every officials hit is flagged "verify identity".

Usage:
  python phila_party_alert.py                 # dry run: no email, state not advanced
  python phila_party_alert.py --live          # send email + advance state
  python phila_party_alert.py --window-days 10
  python phila_party_alert.py --skip-edpa     # PCCP only (e.g. if CL token missing)
"""

import argparse
import html as _h
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from fjd_party_search import PartySearchSession
from scrape_name_watch import name_matches, _search_window
from email_utils import send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("phila_alert")

DATA = HERE / "data"
GOV_FILE = HERE / "phila_gov_watch.json"
OFFICIALS_FILE = HERE / "phila_officials.json"
STATE_FILE = DATA / "state_phila_alert.json"

WINDOW_DAYS = 7               # rolling lookback; absorbs late indexing in both courts
RECIPIENT = ["agutman@inquirer.com", "swalsh@inquirer.com"]

CL_TOKEN = __import__("os").environ.get("COURTLISTENER_TOKEN", "")
CL_SEARCH = "https://www.courtlistener.com/api/rest/v4/search/"
ET = timezone(timedelta(hours=-4))   # EDT (UTC-4)

CV_RE = re.compile(r"\bcv\b", re.I)   # EDPA civil docket-number token, e.g. 2:26-cv-01234
HTML_RE = re.compile(r"<[^>]+>")

# FJD "T"-division case IDs (e.g. "2607T0149") are Revenue Dept TAX LIENS — routine
# automated collections filed against delinquent properties, ~34/day for the City alone.
# They are excluded by default: they're not newsworthy litigation AND their volume pushes
# the City past FJD's 50-row/day search cap, which silently truncates real cases. Pass
# --include-tax-liens to keep them.
TAX_LIEN_RE = re.compile(r"^\d{4}T", re.I)


# ── Config ────────────────────────────────────────────────────────────────────
def load_watchlists():
    """Return (gov_entries, official_entries). Officials file may not exist yet."""
    gov = json.loads(GOV_FILE.read_text()) if GOV_FILE.exists() else []
    officials = json.loads(OFFICIALS_FILE.read_text()) if OFFICIALS_FILE.exists() else []
    for e in gov:
        e["category"] = "gov"
    for e in officials:
        e["category"] = "official"
    return gov, officials


def match_strings(strings, entry):
    """True if ANY of the candidate strings satisfies the entry's match rule.
    Reuses name_watch's name_matches so PCCP party names and EDPA captions/
    parties are judged identically."""
    return any(name_matches(s, entry) for s in strings if s)


# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen": {}}      # {scope_key: {record_id: first_seen_iso}}


def save_state(state):
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── PCCP (FJD participant-name index) ──────────────────────────────────────────
def scan_pccp(entries, begin, end, exclude_tax=True):
    """Return {label: {case_id: record}} of current-window matches per entry.
    record = {id, source, category, caption, filing_date, matched_name, roles,
              url, verify}. Tax-lien (T-division) case IDs are dropped by default."""
    sess = PartySearchSession()
    out = {}
    tax_skipped = 0
    for entry in entries:
        label = entry["label"]
        matched = {}
        for query in entry.get("queries", []):
            for row in _search_window(sess, query, begin, end):
                if not name_matches(row["name"], entry):
                    continue
                cid = row["case_id"]
                if exclude_tax and TAX_LIEN_RE.match(cid):
                    tax_skipped += 1
                    continue
                rec = matched.get(cid)
                if not rec:
                    rec = {
                        "id": cid,
                        "source": "PCCP",
                        "category": entry["category"],
                        "label": label,
                        "caption": row["caption"],
                        "filing_date": row["filing_date"],
                        "case_type": "",   # filled from the docket for emailed cases (enrich_pccp_case_types)
                        "matched_name": row["name"],
                        "roles": set(),
                        "url": "",   # FJD docket replicas are behind the gate; no stable public URL
                        "verify": entry["category"] == "official",
                        "common_name": bool(entry.get("common_name")),
                    }
                    matched[cid] = rec
                role = (row["party_type"] or "").upper().strip()
                if role:
                    rec["roles"].add(role)
                if not rec["caption"] and row["caption"]:
                    rec["caption"] = row["caption"]
                if not rec["filing_date"] and row["filing_date"]:
                    rec["filing_date"] = row["filing_date"]
        for rec in matched.values():
            rec["roles"] = " & ".join(sorted(rec["roles"])) or "—"
        log.info("PCCP %-55s %d match(es)", label, len(matched))
        out[label] = matched
    if exclude_tax and tax_skipped:
        log.info("PCCP: excluded %d tax-lien (T-division) filing(s)", tax_skipped)
    return out


# ── EDPA (CourtListener search) ─────────────────────────────────────────────────
def cl_get(url, retries=3):
    """curl-based GET (avoids the Python 3.14/macOS urllib SSL issue; matches
    the proven approach in poll_gov_cases.py). Token optional but raises the
    rate limit when present."""
    hdrs = ["-H", "User-Agent: Inquirer Newsroom agutman@inquirer.com"]
    if CL_TOKEN:
        hdrs = ["-H", f"Authorization: Token {CL_TOKEN}"] + hdrs
    for attempt in range(retries + 1):
        try:
            out = subprocess.run(["curl", "-s", *hdrs, "--max-time", "30", url],
                                 capture_output=True, text=True, timeout=35)
            if out.returncode != 0:
                raise RuntimeError(f"curl exit {out.returncode}")
            data = json.loads(out.stdout)
            if isinstance(data, dict) and "429" in str(data.get("detail", "")):
                if attempt < retries:
                    time.sleep(15 * (attempt + 1))
                    continue
            return data
        except (json.JSONDecodeError, RuntimeError):
            if attempt < retries:
                time.sleep(5)
                continue
            raise


def _cl_url(begin):
    from urllib.parse import urlencode
    return CL_SEARCH + "?" + urlencode({
        "type": "d",
        "court": "paed",
        "filed_after": begin.isoformat(),
        "order_by": "dateFiled desc",
        "format": "json",
    })


def scan_edpa(gov_entries, official_entries, begin, page_cap=60):
    """Fetch EDPA civil dockets filed in the window, match caption + parties
    against every entry. Return {label: {docket_id: record}}."""
    entries = gov_entries + official_entries
    out = {e["label"]: {} for e in entries}
    url = _cl_url(begin)
    pages = seen_dockets = civil = 0
    while url and pages < page_cap:
        data = cl_get(url)
        pages += 1
        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            break
        for r in results:
            seen_dockets += 1
            docket_no = r.get("docketNumber", "") or ""
            if not CV_RE.search(docket_no):
                continue          # civil only (drop cr/mj/mc)
            civil += 1
            caption = HTML_RE.sub("", r.get("caseName", "") or "").strip()
            parties = r.get("party", []) if isinstance(r.get("party"), list) else []
            strings = [caption] + parties
            did = str(r.get("docket_id"))
            date_filed = r.get("dateFiled", "") or ""
            cl_url = "https://www.courtlistener.com" + (r.get("docket_absolute_url", "") or "")
            for entry in entries:
                if did in out[entry["label"]]:
                    continue
                if match_strings(strings, entry):
                    # Report the party string that matched, else the caption.
                    hit_name = next((p for p in parties if name_matches(p, entry)), caption)
                    out[entry["label"]][did] = {
                        "id": did,
                        "source": "EDPA",
                        "category": entry["category"],
                        "label": entry["label"],
                        "caption": caption,
                        "filing_date": date_filed,
                        "case_type": (r.get("suitNature") or "").strip() or "—",
                        "matched_name": hit_name,
                        "roles": docket_no,       # show the docket number in the role column
                        "url": cl_url,
                        "verify": entry["category"] == "official",
                        "common_name": bool(entry.get("common_name")),
                    }
        url = data.get("next") if isinstance(data, dict) else None
        time.sleep(0.5)
    log.info("EDPA: walked %d page(s), %d docket(s), %d civil", pages, seen_dockets, civil)
    for e in entries:
        n = len(out[e["label"]])
        if n:
            log.info("EDPA %-55s %d match(es)", e["label"], n)
    return out


# ── Case-type enrichment (PCCP) ─────────────────────────────────────────────────
def enrich_pccp_case_types(records):
    """Fill case_type on PCCP records by pulling each docket's Case Type field.
    Only called on the NEW (emailed) records, so it's ~a few dozen fetches/day,
    never the whole rolling window. EDPA case types come from suitNature already."""
    import fjd_docket as FD
    pccp = [r for r in records if r["source"] == "PCCP" and not r.get("case_type")]
    if not pccp:
        return
    sess = FD.FjdSession()
    sess.mint()
    for r in pccp:
        try:
            status, html = sess.fetch_docket(r["id"])
            r["case_type"] = (FD.parse_docket(html, r["id"]).get("case_type") or "—") \
                if status == FD.OK else "—"
        except Exception as e:
            log.warning("  case-type fetch failed for %s: %s", r["id"], e)
            r["case_type"] = "—"
    log.info("Enriched case type for %d PCCP case(s)", len(pccp))


# ── Diff against state ──────────────────────────────────────────────────────────
def diff_new(scope_key, matched_by_label, state, live, now_iso):
    """For one court, return (new_records, first_run_labels). Seeds silently on
    first sight of a label; advances state only when live."""
    seen = state.setdefault("seen", {}).setdefault(scope_key, {})
    new_records = []
    for label, recs in matched_by_label.items():
        label_seen = seen.setdefault(label, {})
        first_run = len(label_seen) == 0
        for rid, rec in recs.items():
            if rid not in label_seen:
                if not first_run:
                    new_records.append(rec)
            if live:
                label_seen.setdefault(rid, now_iso)
        if first_run and recs:
            log.info("  %s / %s: first run — seeded %d case(s) silently",
                     scope_key, label, len(recs))
    return new_records


# ── Email ────────────────────────────────────────────────────────────────────
SECTION_ORDER = [
    ("PCCP", "gov", "Common Pleas — Philadelphia government / public parties"),
    ("EDPA", "gov", "Federal (E.D. Pa.) — Philadelphia government / public parties"),
    ("PCCP", "official", "Common Pleas — Philadelphia officials (⚠ verify identity)"),
    ("EDPA", "official", "Federal (E.D. Pa.) — Philadelphia officials (⚠ verify identity)"),
]


def build_email(records, run_date, total):
    TD = "padding:8px 10px;font-size:12px;border-bottom:1px solid #eee;vertical-align:top;"

    def row_html(c):
        cap = _h.escape(c.get("caption", "") or "—")
        if c.get("url"):
            cap = f'<a href="{_h.escape(c["url"])}" style="color:#1a1a2e;">{cap}</a>'
        flag = ""
        if c["category"] == "official":
            note = "verify this is the official, not a namesake"
            if c.get("common_name"):
                note = "common name — HIGHER false-match risk; " + note
            flag = (f'<div style="color:#b45309;font-size:11px;margin-top:3px;">'
                    f'⚠ {note}</div>')
        return f"""
        <tr>
          <td style="{TD}font-family:monospace;color:#555;white-space:nowrap;">{_h.escape(c['id'])}</td>
          <td style="{TD}font-weight:600;">{cap}{flag}</td>
          <td style="{TD}white-space:nowrap;">{_h.escape(c.get('filing_date','') or '—')}</td>
          <td style="{TD}white-space:nowrap;color:#5a2c6e;">{_h.escape(c.get('case_type','') or '—')}</td>
          <td style="{TD}white-space:nowrap;">{_h.escape(str(c.get('roles','—')))}</td>
          <td style="{TD}">{_h.escape(c.get('matched_name','') or '—')}<div style="color:#888;font-size:10px;">{_h.escape(c['label'])}</div></td>
        </tr>"""

    sections_html = ""
    for source, category, heading in SECTION_ORDER:
        bucket = [c for c in records if c["source"] == source and c["category"] == category]
        if not bucket:
            continue
        bucket.sort(key=lambda c: (c.get("filing_date") or "", c["id"]), reverse=True)
        rows = "".join(row_html(c) for c in bucket)
        role_hdr = "Role" if source == "PCCP" else "Docket #"
        sections_html += f"""
        <tr>
          <td colspan="6" style="padding:12px 10px 7px;background:#e8edf2;
              font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;
              color:#1a1a2e;border-top:2px solid #b8c8d8;border-bottom:1px solid #d0dce8;">
            {_h.escape(heading)}<span style="font-weight:400;color:#666;"> ({len(bucket)})</span>
          </td>
        </tr>
        <tr style="background:#f6f7f9;">
          <th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;">{'Case ID' if source=='PCCP' else 'Docket ID'}</th>
          <th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;">Caption</th>
          <th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;">Filed</th>
          <th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;">Case type</th>
          <th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;">{role_hdr}</th>
          <th style="padding:6px 10px;text-align:left;font-size:10px;color:#888;">Matched party / watch</th>
        </tr>{rows}"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:24px 16px;background:#eef0f3;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif;">
<div style="max-width:860px;margin:0 auto;">

  <div style="background:#5a2c6e;padding:24px 28px;border-radius:10px 10px 0 0;">
    <p style="margin:0 0 6px;color:rgba(255,255,255,0.6);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;">Philadelphia in Court</p>
    <h1 style="margin:0 0 4px;color:white;font-size:22px;font-weight:700;">New civil cases involving the City &amp; its officials</h1>
    <p style="margin:0;color:rgba(255,255,255,0.85);font-size:16px;">{total} new case{"s" if total != 1 else ""} — {run_date}</p>
  </div>

  <div style="background:white;padding:24px 28px;">
    <p style="margin:0 0 18px;font-size:13px;color:#666;background:#f8f9fa;padding:12px 16px;
        border-left:4px solid #5a2c6e;border-radius:0 6px 6px 0;">
      New <strong>Philadelphia Common Pleas</strong> and <strong>E.D. Pa. federal</strong> <strong>civil</strong> cases
      from the last {WINDOW_DAYS} days where a party is a Philadelphia government/public body (name contains
      &ldquo;Philadelphia&rdquo;) or a Philadelphia elected/top-cabinet official.
      Officials are matched by <strong>full name</strong>; a match is <strong>possible, not confirmed</strong> —
      the party could be a namesake, so verify identity against the docket before relying on it.
      Confirm every detail against the official docket before publishing.
    </p>

    <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <tbody>{sections_html}
      </tbody>
    </table>
    </div>
  </div>

  <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #e9ecef;border-radius:0 0 10px 10px;">
    <p style="margin:0;font-size:12px;color:#aaa;line-height:1.6;">
      Sources: First Judicial District of Pennsylvania (fjdefile.phila.gov) for Common Pleas;
      CourtListener (courtlistener.com) for E.D. Pa. federal. Generated automatically at Av&rsquo;s request.<br>
      Always confirm against the official docket before relying on or publishing this information.
    </p>
  </div>

</div>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Send email and advance state")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--skip-edpa", action="store_true", help="PCCP only (e.g. no CL token)")
    ap.add_argument("--include-tax-liens", action="store_true",
                    help="Keep routine T-division tax liens (excluded by default)")
    args = ap.parse_args()

    gov, officials = load_watchlists()
    log.info("Watching %d gov entities + %d officials", len(gov), len(officials))

    state = load_state()
    today = datetime.now(timezone.utc).date()
    begin = today - timedelta(days=args.window_days)
    now_iso = datetime.now(timezone.utc).isoformat()

    new_records = []

    # PCCP
    pccp = scan_pccp(gov + officials, begin, today, exclude_tax=not args.include_tax_liens)
    new_records += diff_new("pccp", pccp, state, args.live, now_iso)

    # EDPA
    if args.skip_edpa:
        log.info("EDPA scan skipped (--skip-edpa)")
    elif not CL_TOKEN:
        log.warning("COURTLISTENER_TOKEN not set — EDPA scan runs unauthenticated (lower rate limit)")
        edpa = scan_edpa(gov, officials, begin)
        new_records += diff_new("edpa", edpa, state, args.live, now_iso)
    else:
        edpa = scan_edpa(gov, officials, begin)
        new_records += diff_new("edpa", edpa, state, args.live, now_iso)

    total = len(new_records)
    log.info("Total NEW across both courts: %d", total)

    if not args.live:
        log.info("Dry run — state not advanced, email not sent. Pass --live to activate.")
        return

    save_state(state)
    log.info("State saved")
    if total:
        enrich_pccp_case_types(new_records)     # pull Case Type from each new PCCP docket
        run_date = datetime.now(ET).strftime("%B %-d, %Y %-I:%M %p ET")
        subject = f"Philadelphia in Court — {total} new civil case{'s' if total != 1 else ''}"
        body = build_email(new_records, run_date, total)
        sent = send_email(subject, body, log_fn=log.info, to=RECIPIENT)
        log.info("Email %s", "sent" if sent else "skipped (no creds)")
    else:
        log.info("No new cases — email skipped")


if __name__ == "__main__":
    main()
