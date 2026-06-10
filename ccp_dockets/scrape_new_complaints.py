"""
scrape_new_complaints.py — Daily scan of new CCP civil complaints.

Enumerates case_ids from the last known sequence upward, filters by exclude
rules, writes data/complaints.json, and emails a digest.

Usage:
    python scrape_new_complaints.py              # scan + write JSON, no email
    python scrape_new_complaints.py --live       # also send email, save state
    python scrape_new_complaints.py --max-misses 12
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from fjd_docket import FjdSession, parse_docket, current_yymm, make_case_id, OK, MISSING
from email_utils import send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("complaints")

DATA = HERE / "data"
STATE_FILE = DATA / "state_complaints.json"
OUTPUT_FILE = DATA / "complaints.json"

# Dashboard shows a rolling window, not just the latest scan, so a barren scan
# (e.g. one that runs before the day's docket numbers are issued) never blanks
# the page. Cases drop off once their filing_date is older than this.
WINDOW_DAYS = 30

RECIPIENT = ["agutman@inquirer.com"]
DASHBOARD_URL = "https://abgutman.github.io/av-tools/ccp_dockets_dashboard.html"

# ── Exclude filter ─────────────────────────────────────────────────────────────
# Keep everything EXCEPT these. Unknown types default to INCLUDED.
EXCLUDE_CONTAINS = ["lien", "penndot", "parking", "certified"]
EXCLUDE_STARTSWITH = ["mc -"]
EXCLUDE_ENDSWITH = ["-mr"]
EXCLUDE_EXACT = {
    "self assessed taxes",
    "credit card collection",
    "auction motor vehicle",
    "ejectment",
    "quiet title",
}


def should_include(case_type: str) -> bool:
    ct = case_type.lower().strip()
    if ct in EXCLUDE_EXACT:
        return False
    for s in EXCLUDE_CONTAINS:
        if s in ct:
            return False
    for s in EXCLUDE_STARTSWITH:
        if ct.startswith(s):
            return False
    for s in EXCLUDE_ENDSWITH:
        if ct.endswith(s):
            return False
    return True


# ── State ──────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_seq_by_month": {}}


def save_state(state):
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Email ──────────────────────────────────────────────────────────────────────
def _party_str(plaintiffs, defendants):
    p = "; ".join(plaintiffs[:2]) or "—"
    d = "; ".join(defendants[:2]) or "—"
    ep = f" +{len(plaintiffs)-2}" if len(plaintiffs) > 2 else ""
    ed = f" +{len(defendants)-2}" if len(defendants) > 2 else ""
    return f"{p}{ep}", f"{d}{ed}"


def _entry_str(entry):
    if not entry:
        return "—"
    return f"{entry.get('date', '')} — {entry.get('type', '')}"


def build_email(cases, run_date):
    import html as _h
    rows_html = ""
    for c in cases:
        ps, ds = _party_str(c.get("plaintiffs", []), c.get("defendants", []))
        last = _entry_str(c.get("last_entry"))
        cid = _h.escape(c["case_id"])
        caption = _h.escape(c.get("caption", ""))
        filed = _h.escape(c.get("filing_date", ""))
        ct = _h.escape(c.get("case_type", ""))
        rows_html += f"""
        <tr>
          <td style="padding:8px 10px;font-family:monospace;font-size:12px;color:#555;
              white-space:nowrap;border-bottom:1px solid #eee;">{cid}</td>
          <td style="padding:8px 10px;font-weight:600;font-size:13px;
              border-bottom:1px solid #eee;">{caption}</td>
          <td style="padding:8px 10px;font-size:12px;white-space:nowrap;
              border-bottom:1px solid #eee;">{filed}</td>
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #eee;">{ct}</td>
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #eee;">{_h.escape(ps)}</td>
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #eee;">{_h.escape(ds)}</td>
          <td style="padding:8px 10px;font-size:12px;color:#555;
              border-bottom:1px solid #eee;">{_h.escape(last)}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px 16px;background:#eef0f3;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif;">
<div style="max-width:860px;margin:0 auto;">

  <div style="background:#1a1a2e;padding:24px 28px;border-radius:10px 10px 0 0;">
    <p style="margin:0 0 6px;color:rgba(255,255,255,0.6);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;">CCP Dockets Monitor</p>
    <h1 style="margin:0 0 4px;color:white;font-size:22px;font-weight:700;">New Civil Complaints</h1>
    <p style="margin:0;color:rgba(255,255,255,0.85);font-size:16px;">{len(cases)} case{"s" if len(cases) != 1 else ""} — {run_date}</p>
  </div>

  <div style="background:white;padding:24px 28px;">
    <p style="margin:0 0 18px;font-size:13px;color:#666;background:#f8f9fa;padding:12px 16px;
        border-left:4px solid #1a1a2e;border-radius:0 6px 6px 0;">
      New complaints filed since the last scan. Excludes liens, parking, MC appeals, and other filtered types.
      Verify all details against the official FJD docket before relying on or publishing.
    </p>

    <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#f0f0f0;">
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;white-space:nowrap;">Case ID</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Caption</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;white-space:nowrap;">Filed</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Type</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Plaintiff(s)</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Defendant(s)</th>
          <th style="padding:8px 10px;text-align:left;border-bottom:2px solid #ddd;">Last entry</th>
        </tr>
      </thead>
      <tbody>{rows_html}
      </tbody>
    </table>
    </div>

    <div style="margin-top:24px;">
      <a href="{DASHBOARD_URL}" style="display:inline-block;background:#1a1a2e;color:white;padding:11px 22px;
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


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="Send email and advance state pointer")
    ap.add_argument("--max-misses", type=int, default=20,
                    help="Stop after N consecutive missing case_ids. Must exceed "
                         "the largest gap of reserved-but-unfiled sequence numbers, "
                         "or the scan stops short of the real frontier.")
    args = ap.parse_args()

    state = load_state()
    yymm = current_yymm()
    last_seq = state["last_seq_by_month"].get(yymm, 0)
    log.info("Scan start: YYMM=%s last_seq=%d max_misses=%d",
             yymm, last_seq, args.max_misses)

    sess = FjdSession()
    results = []
    misses = 0
    max_seq_seen = last_seq
    seq = last_seq + 1

    while misses < args.max_misses:
        case_id = make_case_id(yymm, seq)
        status, html = sess.fetch_docket(case_id)

        if status == MISSING:
            misses += 1
            log.debug("  %s MISSING (%d/%d)", case_id, misses, args.max_misses)
        elif status == OK:
            misses = 0
            max_seq_seen = seq
            parsed = parse_docket(html, case_id)
            ct = parsed.get("case_type", "")
            if should_include(ct):
                results.append(parsed)
                log.info("  %s INCLUDED  %s | %s", case_id, ct, parsed.get("caption", ""))
            else:
                log.info("  %s excluded  %s", case_id, ct)
        else:
            log.warning("  %s BOUNCE — skipping", case_id)

        seq += 1

    log.info("Scan done. %d included / max_seq=%d", len(results), max_seq_seen)

    # Merge this run's new cases into a rolling window rather than overwriting,
    # so the dashboard never goes blank on a scan that finds nothing. Dedup by
    # case_id (a fresh parse wins), then drop anything filed before the cutoff.
    DATA.mkdir(exist_ok=True)
    existing = []
    if OUTPUT_FILE.exists():
        try:
            existing = json.loads(OUTPUT_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []
    merged = {c["case_id"]: c for c in existing}
    for c in results:
        merged[c["case_id"]] = c
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=WINDOW_DAYS)).isoformat()
    # Keep cases newer than the cutoff; keep undated cases rather than silently
    # dropping them (sentinel sorts last so they don't crowd the top).
    window = [c for c in merged.values()
              if (c.get("filing_date") or "9999-99-99") >= cutoff]
    window.sort(key=lambda c: (c.get("filing_date") or "", c.get("case_id", "")),
                reverse=True)
    OUTPUT_FILE.write_text(json.dumps(window, indent=2))
    log.info("Wrote %s (%d new this scan, %d in %d-day window)",
             OUTPUT_FILE, len(results), len(window), WINDOW_DAYS)

    if args.live:
        state["last_seq_by_month"][yymm] = max_seq_seen
        save_state(state)
        log.info("State saved: [%s]=%d", yymm, max_seq_seen)

        if results:
            run_date = datetime.now(timezone.utc).strftime("%B %-d, %Y")
            subject = f"CCP New Complaints — {run_date} ({len(results)} cases)"
            body = build_email(results, run_date)
            sent = send_email(subject, body, log_fn=log.info, to=RECIPIENT)
            log.info("Email %s", "sent" if sent else "skipped (no creds)")
        else:
            log.info("No new cases — email skipped")
    else:
        log.info("Dry run — state not advanced, email not sent. Pass --live to activate.")


if __name__ == "__main__":
    main()
