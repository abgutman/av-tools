"""
scrape_name_watch.py — Watch CCP civil filings by/against named entities.

For each entry in name_watch.json, runs the FJD participant name search over a
rolling date window, keeps rows whose party name matches the entry's rule
(by OR against — any party role), dedups by case_id, and emails a digest of
NEW matching cases. First run per label seeds silently (no email), like the
case watchlist. Writes data/name_watch_view.json for the dashboard's Party Watch tab.

Usage:
    python scrape_name_watch.py           # search + write view JSON, no email
    python scrape_name_watch.py --live     # also send email + advance state
    python scrape_name_watch.py --window-days 21
"""

import argparse
import html as _h
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from fjd_party_search import PartySearchSession
from email_utils import send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("name_watch")

DATA = HERE / "data"
CONFIG_FILE = HERE / "name_watch.json"
STATE_FILE = DATA / "state_name_watch.json"
VIEW_FILE = DATA / "name_watch_view.json"

WINDOW_DAYS = 14          # rolling lookback; absorbs late FJD indexing
RECIPIENT = ["agutman@inquirer.com"]
DASHBOARD_URL = "https://abgutman.github.io/av-tools/ccp_dockets_dashboard.html"


# ── Match rule ───────────────────────────────────────────────────────────────
def name_matches(name, entry):
    """Case-insensitive test of a party Name/Company against an entry's rule.

    `pattern`          — a regex; keep if it matches anywhere in the name.
                         Use for word-boundary precision, e.g. "\\bpeco\\b"
                         keeps PECO / PECO ENERGY COMPANY but drops PECOLA/PECORA.
    `must_contain`     — keep if the name contains ANY of these substrings.
    `must_contain_all` — keep if the name contains ALL of these substrings.
    An entry may set any combination; all present rules must pass. No rule =
    keep everything the prefix query returned.
    """
    n = (name or "").lower()
    pattern = entry.get("pattern")
    any_of = [t.lower() for t in entry.get("must_contain", [])]
    all_of = [t.lower() for t in entry.get("must_contain_all", [])]
    if pattern and not re.search(pattern, n, re.I):
        return False
    if any_of and not any(t in n for t in any_of):
        return False
    if all_of and not all(t in n for t in all_of):
        return False
    return True


# ── Search one entry (with truncation-safe date subdivision) ──────────────────
def _search_window(sess, query, begin, end, depth=0):
    """Return rows for one prefix over [begin, end]. If the server truncates
    (50-row cap), split the window and recurse so nothing is silently missed."""
    res = sess.search(query, begin.isoformat(), end.isoformat())
    rows = res["rows"]
    if res["truncated"] and (end - begin).days >= 1 and depth < 6:
        mid = begin + (end - begin) / 2
        mid = mid.date() if hasattr(mid, "date") else mid
        log.warning("  '%s' %s..%s hit the 50-row cap — subdividing",
                    query, begin, end)
        left = _search_window(sess, query, begin, mid, depth + 1)
        right = _search_window(sess, query, mid + timedelta(days=1), end, depth + 1)
        return left + right
    if res["truncated"]:
        log.warning("  '%s' %s..%s still truncated at single-day granularity — "
                    "some rows may be unseen", query, begin, end)
    return rows


def search_entry(sess, entry, begin, end):
    """Run every prefix for an entry, filter by rule, dedup by case_id.

    Returns {case_id: row} where row carries merged party roles for that case.
    """
    matched = {}
    for query in entry.get("queries", []):
        for row in _search_window(sess, query, begin, end):
            if not name_matches(row["name"], entry):
                continue
            cid = row["case_id"]
            if cid not in matched:
                matched[cid] = {
                    "case_id": cid,
                    "caption": row["caption"],
                    "filing_date": row["filing_date"],
                    "matched_name": row["name"],
                    "roles": set(),
                }
            role = (row["party_type"] or "").upper().strip()
            if role:
                matched[cid]["roles"].add(role)
            # Prefer a non-empty caption/date if this row has one
            if not matched[cid]["caption"] and row["caption"]:
                matched[cid]["caption"] = row["caption"]
            if not matched[cid]["filing_date"] and row["filing_date"]:
                matched[cid]["filing_date"] = row["filing_date"]
    # Freeze roles into a stable, human-readable string
    for m in matched.values():
        m["roles"] = " & ".join(sorted(m["roles"])) or "—"
    return matched


# ── State ────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen": {}}      # {label: {case_id: first_seen_iso}}


def save_state(state):
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Email ────────────────────────────────────────────────────────────────────
def build_email(new_by_label, run_date, total):
    TD = "padding:8px 10px;font-size:12px;border-bottom:1px solid #eee;vertical-align:top;"
    sections_html = ""
    for label, cases in new_by_label:
        if not cases:
            continue
        rows_html = ""
        for c in cases:
            rows_html += f"""
        <tr>
          <td style="{TD}font-family:monospace;color:#555;white-space:nowrap;">{_h.escape(c['case_id'])}</td>
          <td style="{TD}font-weight:600;">{_h.escape(c.get('caption',''))}</td>
          <td style="{TD}white-space:nowrap;">{_h.escape(c.get('filing_date','') or '—')}</td>
          <td style="{TD}white-space:nowrap;">{_h.escape(c.get('roles','—'))}</td>
          <td style="{TD}">{_h.escape(c.get('matched_name',''))}</td>
        </tr>"""
        sections_html += f"""
        <tr>
          <td colspan="5" style="padding:10px 10px 7px;background:#e8edf2;
              font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
              color:#1a1a2e;border-top:2px solid #b8c8d8;border-bottom:1px solid #d0dce8;">
            {_h.escape(label)}<span style="font-weight:400;color:#666;"> ({len(cases)})</span>
          </td>
        </tr>{rows_html}"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px 16px;background:#eef0f3;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif;">
<div style="max-width:820px;margin:0 auto;">

  <div style="background:#5a2c6e;padding:24px 28px;border-radius:10px 10px 0 0;">
    <p style="margin:0 0 6px;color:rgba(255,255,255,0.6);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;">CCP Dockets Monitor</p>
    <h1 style="margin:0 0 4px;color:white;font-size:22px;font-weight:700;">Party Watch — New Filings</h1>
    <p style="margin:0;color:rgba(255,255,255,0.85);font-size:16px;">{total} new case{"s" if total != 1 else ""} by or against watched names — {run_date}</p>
  </div>

  <div style="background:white;padding:24px 28px;">
    <p style="margin:0 0 18px;font-size:13px;color:#666;background:#f8f9fa;padding:12px 16px;
        border-left:4px solid #5a2c6e;border-radius:0 6px 6px 0;">
      New Philadelphia Common Pleas civil cases where a watched entity appears as a party (plaintiff or defendant),
      found via the FJD participant-name index. Verify all details against the official FJD docket before relying on or publishing.
    </p>

    <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#f0f0f0;">
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;white-space:nowrap;">Case ID</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Caption</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;white-space:nowrap;">Filed</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Role</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Matched name</th>
        </tr>
      </thead>
      <tbody>{sections_html}
      </tbody>
    </table>
    </div>

    <div style="margin-top:24px;">
      <a href="{DASHBOARD_URL}" style="display:inline-block;background:#5a2c6e;color:white;padding:11px 22px;
          border-radius:7px;text-decoration:none;font-weight:700;font-size:13px;">View Dashboard →</a>
    </div>
  </div>

  <div style="background:#f8f9fa;padding:14px 28px;border-top:1px solid #e9ecef;border-radius:0 0 10px 10px;">
    <p style="margin:0;font-size:12px;color:#aaa;line-height:1.6;">
      Source: First Judicial District of Pennsylvania — fjdefile.phila.gov.<br>
      Always confirm against the official docket before relying on or publishing this information.
    </p>
  </div>

</div>
</body>
</html>"""


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="Send email and advance state")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS,
                    help="Rolling lookback window for filing dates")
    args = ap.parse_args()

    config = json.loads(CONFIG_FILE.read_text())
    state = load_state()
    seen = state.setdefault("seen", {})

    today = datetime.now(timezone.utc).date()
    begin = today - timedelta(days=args.window_days)
    now_iso = datetime.now(timezone.utc).isoformat()

    sess = PartySearchSession()
    view_labels = []
    new_by_label = []
    total_new = 0

    for entry in config:
        label = entry["label"]
        matched = search_entry(sess, entry, begin, today)
        log.info("%s: %d matching case(s) in %d-day window",
                 label, len(matched), args.window_days)

        label_seen = seen.setdefault(label, {})
        first_run = len(label_seen) == 0
        new_cases = [m for cid, m in matched.items() if cid not in label_seen]

        if first_run:
            log.info("  %s: first run — seeding %d case(s) silently (no email)",
                     label, len(matched))
        elif new_cases:
            log.info("  %s: %d NEW case(s): %s", label, len(new_cases),
                     ", ".join(c["case_id"] for c in new_cases))

        # View: all current matches (rolling), newest first
        rows = sorted(matched.values(),
                      key=lambda c: (c.get("filing_date") or "", c["case_id"]),
                      reverse=True)
        view_labels.append({"label": label, "rows": rows})

        # Only cases in the window count as "new" alerts; first run seeds silently.
        if not first_run:
            new_by_label.append((label, sorted(
                new_cases, key=lambda c: (c.get("filing_date") or "", c["case_id"]),
                reverse=True)))
            total_new += len(new_cases)

        if args.live:
            for cid in matched:
                label_seen.setdefault(cid, now_iso)

    # Always refresh the dashboard view (rolling snapshot)
    DATA.mkdir(exist_ok=True)
    ET = timezone(timedelta(hours=-4))
    VIEW_FILE.write_text(json.dumps({
        "labels": view_labels,
        "generated_at": datetime.now(ET).strftime("%Y-%m-%d %H:%M EDT"),
        "window_days": args.window_days,
    }, indent=2))
    log.info("Wrote %s", VIEW_FILE)

    if args.live:
        save_state(state)
        log.info("State saved (%d labels)", len(seen))
        if total_new:
            run_date = datetime.now(ET).strftime("%B %-d, %Y %-I:%M %p ET")
            names = ", ".join(lbl for lbl, cs in new_by_label if cs)
            subject = f"CCP Party Watch — {total_new} new ({names})"
            body = build_email(new_by_label, run_date, total_new)
            sent = send_email(subject, body, log_fn=log.info, to=RECIPIENT)
            log.info("Email %s", "sent" if sent else "skipped (no creds)")
        else:
            log.info("No new cases — email skipped")
    else:
        log.info("Dry run — state not advanced, email not sent. Pass --live to activate.")


if __name__ == "__main__":
    main()
