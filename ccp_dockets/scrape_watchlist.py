"""
scrape_watchlist.py — Daily check on watched CCP cases.

Reads watchlist.json, fetches each case, detects docket changes vs stored
snapshot, emails alerts on changes, and writes data/watchlist_view.json
(summary rows for the dashboard) and data/state_watchlist.json (full dockets
for replica page generation).

Usage:
    python scrape_watchlist.py          # fetch + write JSONs, no email
    python scrape_watchlist.py --live   # also send change-alert emails
"""

import argparse
import json
import logging
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from fjd_docket import FjdSession, parse_docket, docket_signature, OK
from email_utils import send_email

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("watchlist")

DATA = HERE / "data"
WATCHLIST_FILE = HERE / "watchlist.json"
STATE_FILE = DATA / "state_watchlist.json"
VIEW_FILE = DATA / "watchlist_view.json"

RECIPIENT = ["agutman@inquirer.com"]
DASHBOARD_URL = "https://abgutman.github.io/av-tools/ccp_dockets_dashboard.html"


# ── I/O helpers ────────────────────────────────────────────────────────────────
def load_watchlist():
    if not WATCHLIST_FILE.exists():
        return []
    return json.loads(WATCHLIST_FILE.read_text())


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Diff helper ────────────────────────────────────────────────────────────────
def new_entries(parsed, prev_count):
    """Return entries added since the snapshot (by position, newest-last)."""
    entries = parsed.get("entries", [])
    if prev_count is None or prev_count >= len(entries):
        return []
    return entries[prev_count:]


# ── Email ──────────────────────────────────────────────────────────────────────
def build_alert_email(case_id, note, parsed, added):
    import html as _h

    caption = _h.escape(parsed.get("caption", case_id))
    case_type = _h.escape(parsed.get("case_type", ""))
    plaintiffs = _h.escape("; ".join(parsed.get("plaintiffs", [])) or "—")
    defendants = _h.escape("; ".join(parsed.get("defendants", [])) or "—")
    note_line = (
        f'<p style="margin:0 0 8px;font-size:13px;color:#555;">'
        f'<strong>Note:</strong> {_h.escape(note)}</p>'
    ) if note else ""

    rows_html = ""
    for e in added:
        rows_html += (
            f"<tr>"
            f'<td style="padding:7px 10px;font-size:12px;white-space:nowrap;border-bottom:1px solid #eee;">'
            f'{_h.escape(e.get("date",""))}</td>'
            f'<td style="padding:7px 10px;font-size:13px;font-weight:500;border-bottom:1px solid #eee;">'
            f'{_h.escape(e.get("type",""))}</td>'
            f'<td style="padding:7px 10px;font-size:12px;color:#555;border-bottom:1px solid #eee;">'
            f'{_h.escape(e.get("party",""))}</td>'
            f"</tr>"
        )

    count_word = "Entry" if len(added) == 1 else "Entries"
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px 16px;background:#eef0f3;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Helvetica,Arial,sans-serif;">
<div style="max-width:620px;margin:0 auto;">

  <div style="background:#2c5f2e;padding:24px 28px;border-radius:10px 10px 0 0;">
    <p style="margin:0 0 6px;color:rgba(255,255,255,0.6);font-size:11px;text-transform:uppercase;letter-spacing:1.5px;">Watchlist Alert</p>
    <h1 style="margin:0 0 4px;color:white;font-size:20px;font-weight:700;">{caption}</h1>
    <p style="margin:0;color:rgba(255,255,255,0.85);font-size:14px;">{case_id} &middot; {case_type}</p>
  </div>

  <div style="background:white;padding:24px 28px;">
    {note_line}
    <p style="margin:0 0 16px;font-size:12px;color:#555;">{plaintiffs} <strong>v.</strong> {defendants}</p>

    <h2 style="font-size:14px;font-weight:700;margin:0 0 8px;border-bottom:2px solid #2c5f2e;padding-bottom:4px;">
      {len(added)} New Docket {count_word}
    </h2>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="background:#f5f5f5;">
          <th style="padding:7px 10px;text-align:left;font-size:11px;text-transform:uppercase;color:#777;border-bottom:2px solid #ddd;">Date</th>
          <th style="padding:7px 10px;text-align:left;font-size:11px;text-transform:uppercase;color:#777;border-bottom:2px solid #ddd;">Type</th>
          <th style="padding:7px 10px;text-align:left;font-size:11px;text-transform:uppercase;color:#777;border-bottom:2px solid #ddd;">Party</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>

    <div style="margin-top:22px;">
      <a href="{DASHBOARD_URL}" style="display:inline-block;background:#2c5f2e;color:white;padding:10px 20px;border-radius:7px;text-decoration:none;font-weight:700;font-size:13px;">View Dashboard →</a>
    </div>
  </div>

  <div style="background:#f8f9fa;padding:12px 28px;border-radius:0 0 10px 10px;">
    <p style="margin:0;font-size:12px;color:#aaa;line-height:1.6;">
      Source: First Judicial District of Pennsylvania — fjdefile.phila.gov.<br>
      Always confirm against the official docket before relying on or publishing this information.
    </p>
  </div>

</div>
</body>
</html>"""


# ── View row helper ────────────────────────────────────────────────────────────
def _view_row(case_id, note, parsed):
    last = parsed.get("last_entry") or {}
    return {
        "case_id": case_id,
        "note": note,
        "case_type": parsed.get("case_type", ""),
        "caption": parsed.get("caption", ""),
        "filing_date": parsed.get("filing_date", ""),
        "status": parsed.get("status", ""),
        "plaintiffs": parsed.get("plaintiffs", []),
        "defendants": parsed.get("defendants", []),
        "entry_count": parsed.get("entry_count", 0),
        "last_entry_type": last.get("type", ""),
        "last_entry_date": last.get("date", ""),
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="Send change-alert emails")
    args = ap.parse_args()

    watchlist = load_watchlist()
    state = load_state()
    sess = FjdSession()
    view_rows = []
    alerts = []  # (case_id, note, parsed, added_entries)

    for item in watchlist:
        case_id = item["case_id"]
        note = item.get("note", "")
        log.info("Checking %s  %s", case_id, f"({note})" if note else "")

        status, html = sess.fetch_docket(case_id)
        if status != OK:
            log.warning("  %s status=%s — skipping this run", case_id, status)
            if case_id in state:
                prev_docket = state[case_id].get("docket", {})
                view_rows.append(_view_row(case_id, note, prev_docket))
            continue

        parsed = parse_docket(html, case_id)
        sig = docket_signature(parsed)
        prev = state.get(case_id, {})
        prev_sig = prev.get("sig")
        prev_count = prev.get("entry_count")

        if prev_sig is None:
            log.info("  %s — first time on watchlist, snapshot saved", case_id)
        elif sig != prev_sig:
            log.info("  %s CHANGED: %r -> %r", case_id, prev_sig, sig)
            added = new_entries(parsed, prev_count)
            if added:
                alerts.append((case_id, note, parsed, added))
        else:
            log.info("  %s unchanged", case_id)

        state[case_id] = {
            "sig": sig,
            "entry_count": parsed["entry_count"],
            "note": note,
            "docket": parsed,
        }
        view_rows.append(_view_row(case_id, note, parsed))

    DATA.mkdir(exist_ok=True)
    VIEW_FILE.write_text(json.dumps(view_rows, indent=2))
    save_state(state)
    log.info("Wrote view (%d rows) and state (%d cases)", len(view_rows), len(state))

    if args.live and alerts:
        for case_id, note, parsed, added in alerts:
            caption = parsed.get("caption", case_id)
            subject = f"CCP Docket Update: {caption}"
            body = build_alert_email(case_id, note, parsed, added)
            sent = send_email(subject, body, log_fn=log.info, to=RECIPIENT)
            log.info("Alert %s for %s", "sent" if sent else "skipped (no creds)", case_id)
    elif alerts and not args.live:
        log.info("%d change(s) detected — pass --live to send alerts", len(alerts))
    else:
        log.info("No changes to report")


if __name__ == "__main__":
    main()
