#!/usr/bin/env python3
"""EDGAR poller — detect new earnings-relevant filings and fire email alerts.

For each company in expanded_companies.json (excluding X-untracked + IBX),
  - Fetch /submissions/CIK{padded}.json
  - Compare against state in last_seen.json
  - On new 8-K item 2.02 / 10-Q / 10-K → email alert + update state

Run every 5 min via GitHub Actions cron during pre/post-market windows.

Usage:
  python3 poll_edgar.py                # run once, send alerts
  python3 poll_edgar.py --init         # initialize state without sending alerts
  python3 poll_edgar.py --dry-run      # check filings but don't send/save
"""
import json, os, sys, subprocess, time, smtplib, ssl
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

HERE = Path(__file__).parent
COMPANIES = HERE / "expanded_companies.json"
STATE_FILE = HERE / "last_seen.json"
LOG_FILE = HERE / "poll_log.txt"

UA = "Inquirer Newsroom agutman@inquirer.com"
EMAIL_TO = "agutman@inquirer.com"
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# Forms we care about for earnings alerts
EARNINGS_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A"}
INTERESTING_8K_ITEMS = {"2.02"}  # Results of Operations and Financial Condition

ET = timezone(timedelta(hours=-4))  # EDT (June)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def cik_padded(cik): return f"{int(cik):010d}"

def fetch_submissions(cik):
    url = f"https://data.sec.gov/submissions/CIK{cik_padded(cik)}.json"
    try:
        out = subprocess.run(["curl", "-s", "-A", UA, "-L", "--max-time", "20", url],
                             capture_output=True, text=True, timeout=23)
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout else None
    except Exception as e:
        log(f"  fetch failed for CIK {cik}: {e}")
        return None

def classify_filing(form, items):
    """Return tag describing what kind of earnings event this is, or None to skip."""
    if form in ("10-K", "10-K/A"):
        return "annual"
    if form in ("10-Q", "10-Q/A"):
        return "quarterly"
    if form in ("8-K", "8-K/A"):
        if any(it.strip() in INTERESTING_8K_ITEMS for it in (items or "").split(",")):
            return "earnings_press"
        # Other 8-K items — only flag a few high-value ones
        items_set = {it.strip() for it in (items or "").split(",")}
        if "1.01" in items_set: return "material_agreement"
        if "1.02" in items_set: return "termination_of_agreement"
        if "2.05" in items_set: return "restructuring"
        if "5.02" in items_set: return "exec_change"
        return None  # ignore other 8-Ks for now
    return None

def find_new_filings(sub, last_seen_accessions):
    """Walk recent filings (only the last 7 days) and return new earnings-relevant items.

    We bound to a 7-day lookback because EDGAR returns 1000 historical filings per company,
    and we don't want to flag old filings as 'new' just because they fell out of our state buffer.
    Anything newer than 7 days that we haven't seen → genuine new filing → alert.
    """
    rec = sub.get("filings", {}).get("recent", {}) or {}
    forms = rec.get("form", [])
    new_items = []
    cutoff = (datetime.now().date() - timedelta(days=7)).isoformat()
    for i in range(len(forms)):
        filing_date = rec["filingDate"][i]
        if filing_date < cutoff:
            break  # filings are sorted most-recent first; we're past the lookback
        accession = rec["accessionNumber"][i]
        if accession in last_seen_accessions: continue
        form = forms[i]
        if form not in EARNINGS_FORMS: continue
        items = (rec.get("items") or [""])[i] or ""
        tag = classify_filing(form, items)
        if not tag: continue
        new_items.append({
            "accession": accession,
            "form": form,
            "items": items,
            "filing_date": filing_date,
            "report_date": (rec.get("reportDate") or [""])[i],
            "accept_dt": rec["acceptanceDateTime"][i],
            "primary_doc": rec["primaryDocument"][i],
            "tag": tag,
        })
    return new_items

def format_alert_subject(ticker, name, filing):
    et = datetime.fromisoformat(filing["accept_dt"].replace("Z","+00:00")).astimezone(ET)
    tag = filing["tag"]
    label = {
        "earnings_press": "EARNINGS PRESS RELEASE",
        "quarterly": "10-Q QUARTERLY REPORT",
        "annual": "10-K ANNUAL REPORT",
        "material_agreement": "MATERIAL AGREEMENT (8-K item 1.01)",
        "termination_of_agreement": "TERMINATION (8-K item 1.02)",
        "restructuring": "RESTRUCTURING / EXIT COSTS (8-K item 2.05)",
        "exec_change": "EXECUTIVE CHANGE (8-K item 5.02)",
    }.get(tag, tag)
    return f"[earnings tracker] {ticker} — {label} — {et.strftime('%a %b %d %I:%M %p ET')}"

def format_alert_body(ticker, name, cik, filing):
    et = datetime.fromisoformat(filing["accept_dt"].replace("Z","+00:00")).astimezone(ET)
    acc_no_clean = filing["accession"].replace("-", "")
    filing_index_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_padded(cik)}&type=&dateb=&owner=include&count=10"
    primary_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no_clean}/{filing['primary_doc']}"
    return f"""
{ticker} — {name}

What:           {filing['form']} ({filing['tag']})
SEC items:      {filing['items'] or '(none)'}
Filed:          {et.strftime('%A %B %d, %Y at %I:%M:%S %p ET')}
Period covered: {filing['report_date'] or '(n/a)'}

Direct filing:  {primary_url}
All filings:    {filing_index_url}

—
This alert was triggered by an EDGAR poll. The wire press release (if any) typically
publishes a few minutes earlier on GlobeNewswire / BusinessWire / PR Newswire.
""".strip()

def send_email(subject, body):
    """Send via Gmail SMTP using app password."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log(f"  ⚠ No GMAIL_USER/GMAIL_APP_PASSWORD env — printing alert to stderr instead")
        print(f"--- WOULD SEND EMAIL ---", file=sys.stderr)
        print(f"To: {EMAIL_TO}", file=sys.stderr)
        print(f"Subject: {subject}", file=sys.stderr)
        print(body, file=sys.stderr)
        print(f"-------", file=sys.stderr)
        return False
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    return True

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1))

def main():
    args = sys.argv[1:]
    init_mode = "--init" in args
    dry_run = "--dry-run" in args
    live = "--live" in args  # SAFETY: emails only fire if --live is passed AND not dry-run
    if not live and not init_mode:
        log("⚠ Running WITHOUT --live flag: alerts will be logged but NOT emailed.")
    log(f"=== EDGAR poll starting (init={init_mode}, dry_run={dry_run}, live={live}) ===")

    companies = json.loads(COMPANIES.read_text())
    state = load_state() if not init_mode else {}

    tracked = [c for c in companies if c.get("priority_tier") in (1, 2, 3) and c.get("cik")]
    log(f"Tracking {len(tracked)} companies (Tier 1/2/3 with CIK)")

    alerts_sent = 0
    for c in tracked:
        cik = c.get("cik")
        if not cik: continue
        ticker = (c.get("tickers") or [c.get("ticker_hint","")])[0]
        name = c.get("name") or c.get("seed_name","")
        sub = fetch_submissions(cik)
        if not sub:
            continue
        time.sleep(0.12)  # rate-limit politeness

        cik_key = cik_padded(cik)
        last_seen = set(state.get(cik_key, []))
        new_items = find_new_filings(sub, last_seen)

        if new_items:
            log(f"  {ticker}: {len(new_items)} new earnings-relevant filing(s)")
            for f in new_items:
                if not init_mode:
                    subj = format_alert_subject(ticker, name, f)
                    body = format_alert_body(ticker, name, cik, f)
                    if dry_run or not live:
                        log(f"    [no-email mode] would alert: {subj}")
                    else:
                        try:
                            send_email(subj, body)
                            alerts_sent += 1
                            log(f"    ✉ alert sent: {subj}")
                        except Exception as e:
                            log(f"    ⚠ email send failed: {e}")

        # Update state: keep ALL accession numbers from this fetch (most recent ~1000)
        rec = sub.get("filings",{}).get("recent",{}) or {}
        all_accs = rec.get("accessionNumber", [])
        state[cik_key] = all_accs[:200]  # keep most-recent 200 to bound state size

    if not dry_run:
        save_state(state)

    log(f"=== Poll complete. Sent {alerts_sent} alerts. State has {len(state)} CIKs. ===\n")

if __name__ == "__main__":
    main()
