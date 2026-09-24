"""
scrape_type_watch.py — Intraday alert on new CCP civil filings by CASE TYPE.

The FJD participant-name index can't search by case type, so this watch
enumerates new case_ids incrementally (same engine as the daily complaints
scan) with its OWN sequence pointer, checks each new case against the rules
in type_watch.json, and emails immediately when one qualifies. Runs every
30 minutes so time-sensitive filings (equity/TRO) don't wait for the 8pm
daily digest — the daily scan remains the comprehensive backstop.

Each type_watch.json entry:
    label                      — section heading in the alert email
    case_type_startswith       — keep if the Case Type starts with ANY of these
    exclude_plaintiff_pattern  — drop if ANY plaintiff matches this regex

State (data/state_type_watch.json) holds only last_seq_by_month. Seed it from
the daily scan's pointer before first deploy or the first run walks the whole
month's backlog.

Usage:
    python scrape_type_watch.py            # scan, report matches, no email/state
    python scrape_type_watch.py --live     # send email + advance pointer
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

from fjd_docket import FjdSession, parse_docket, current_yymm, make_case_id, OK, MISSING
from email_utils import send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("type_watch")

DATA = HERE / "data"
CONFIG_FILE = HERE / "type_watch.json"
STATE_FILE = DATA / "state_type_watch.json"

RECIPIENT = ["agutman@inquirer.com"]
DASHBOARD_URL = "https://abgutman.github.io/av-tools/ccp_dockets_dashboard.html"


# ── Match rule ───────────────────────────────────────────────────────────────
def case_matches(parsed, entry):
    """Test one parsed docket against one type_watch.json entry."""
    ct = (parsed.get("case_type") or "").upper().strip()
    prefixes = [p.upper() for p in entry.get("case_type_startswith", [])]
    if prefixes and not any(ct.startswith(p) for p in prefixes):
        return False
    excl = entry.get("exclude_plaintiff_pattern")
    if excl:
        for p in parsed.get("plaintiffs", []):
            if re.search(excl, p or "", re.I):
                return False
    return True


# ── State ────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_seq_by_month": {}}


def save_state(state):
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Email ────────────────────────────────────────────────────────────────────
def build_email(hits_by_label, run_date, total):
    TD = "padding:8px 10px;font-size:12px;border-bottom:1px solid #eee;vertical-align:top;"
    sections_html = ""
    for label, cases in hits_by_label:
        if not cases:
            continue
        rows_html = ""
        for c in cases:
            ps = "; ".join(c.get("plaintiffs", [])[:3]) or "—"
            ds = "; ".join(c.get("defendants", [])[:3]) or "—"
            rows_html += f"""
        <tr>
          <td style="{TD}font-family:monospace;color:#555;white-space:nowrap;">{_h.escape(c['case_id'])}</td>
          <td style="{TD}font-weight:600;">{_h.escape(c.get('caption',''))}</td>
          <td style="{TD}white-space:nowrap;">{_h.escape(c.get('filing_date','') or '—')}</td>
          <td style="{TD}">{_h.escape(c.get('case_type',''))}</td>
          <td style="{TD}">{_h.escape(ps)}</td>
          <td style="{TD}">{_h.escape(ds)}</td>
        </tr>"""
        sections_html += f"""
        <tr>
          <td colspan="6" style="padding:10px 10px 7px;background:#e8edf2;
              font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;
              color:#1a1a2e;border-top:2px solid #b8c8d8;border-bottom:1px solid #d0dce8;">
            {_h.escape(label)}<span style="font-weight:400;color:#666;"> ({len(cases)})</span>
          </td>
        </tr>{rows_html}"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px 16px;background:#eef0f3;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif;">
<div style="max-width:900px;margin:0 auto;">

  <div style="background:#8a4b1f;padding:24px 28px;border-radius:10px 10px 0 0;">
    <p style="margin:0 0 6px;color:rgba(255,255,255,0.6);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;">CCP Dockets Monitor</p>
    <h1 style="margin:0 0 4px;color:white;font-size:22px;font-weight:700;">Case-Type Watch — New Filing</h1>
    <p style="margin:0;color:rgba(255,255,255,0.85);font-size:16px;">{total} new case{"s" if total != 1 else ""} matching a watched case type — {run_date}</p>
  </div>

  <div style="background:white;padding:24px 28px;">
    <p style="margin:0 0 18px;font-size:13px;color:#666;background:#f8f9fa;padding:12px 16px;
        border-left:4px solid #8a4b1f;border-radius:0 6px 6px 0;">
      New Philadelphia Common Pleas civil filings matching a watched case-type rule, found by intraday
      docket enumeration. Verify all details against the official FJD docket before relying on or publishing.
    </p>

    <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#f0f0f0;">
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;white-space:nowrap;">Case ID</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Caption</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;white-space:nowrap;">Filed</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Case type</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Plaintiff(s)</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Defendant(s)</th>
        </tr>
      </thead>
      <tbody>{sections_html}
      </tbody>
    </table>
    </div>

    <div style="margin-top:24px;">
      <a href="{DASHBOARD_URL}" style="display:inline-block;background:#8a4b1f;color:white;padding:11px 22px;
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
                    help="Send email and advance state pointer")
    ap.add_argument("--max-misses", type=int, default=20,
                    help="Stop after N consecutive missing case_ids (same rationale "
                         "as the daily scan: must exceed reserved-but-unfiled gaps)")
    args = ap.parse_args()

    config = json.loads(CONFIG_FILE.read_text())
    state = load_state()
    yymm = current_yymm()
    last_seq = state["last_seq_by_month"].get(yymm, 0)
    log.info("Type-watch scan: YYMM=%s last_seq=%d rules=%d",
             yymm, last_seq, len(config))

    sess = FjdSession()
    hits_by_label = [(e["label"], []) for e in config]
    misses = 0
    max_seq_seen = last_seq
    seq = last_seq + 1
    scanned = 0

    while misses < args.max_misses:
        case_id = make_case_id(yymm, seq)
        status, html = sess.fetch_docket(case_id)

        if status == MISSING:
            misses += 1
        elif status == OK:
            misses = 0
            max_seq_seen = seq
            scanned += 1
            parsed = parse_docket(html, case_id)
            for entry, (label, hits) in zip(config, hits_by_label):
                if case_matches(parsed, entry):
                    hits.append(parsed)
                    log.info("  %s HIT [%s]  %s | %s", case_id, label,
                             parsed.get("case_type", ""), parsed.get("caption", ""))
                    break
        else:
            log.warning("  %s BOUNCE — skipping", case_id)
        seq += 1

    total = sum(len(h) for _, h in hits_by_label)
    log.info("Scan done: %d new case(s) checked, %d hit(s), max_seq=%d",
             scanned, total, max_seq_seen)

    if args.live:
        state["last_seq_by_month"][yymm] = max_seq_seen
        save_state(state)
        log.info("State saved: [%s]=%d", yymm, max_seq_seen)
        if total:
            ET = timezone(timedelta(hours=-4))
            run_date = datetime.now(ET).strftime("%B %-d, %Y %-I:%M %p ET")
            names = ", ".join(lbl for lbl, hs in hits_by_label if hs)
            subject = f"CCP Type Watch — {total} new ({names})"
            body = build_email(hits_by_label, run_date, total)
            sent = send_email(subject, body, log_fn=log.info, to=RECIPIENT)
            log.info("Email %s", "sent" if sent else "skipped (no creds)")
        else:
            log.info("No matching cases — email skipped")
    else:
        log.info("Dry run — state not advanced, email not sent. Pass --live to activate.")


if __name__ == "__main__":
    main()
