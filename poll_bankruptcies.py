#!/usr/bin/env python3
"""Chapter 11 bankruptcy tracker — scans all federal courts, filters by local zip.

Modes:
  python poll_bankruptcies.py backfill              # April 1 2026 → today
  python poll_bankruptcies.py backfill --start=2026-05-25  # custom start
  python poll_bankruptcies.py poll                  # since last scan
  python poll_bankruptcies.py poll --live           # also send email alerts

Data source: CourtListener/RECAP API (token via COURTLISTENER_TOKEN env var).
Matches debtor zip codes against ~450 local zips covering the
8-county Philadelphia region (Philly + PA suburbs + South Jersey).
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from email_utils import send_email, subject_bankruptcy_alert, body_bankruptcy_alert

HERE = Path(__file__).parent
BD = HERE / "bankruptcy_data"
CASES_FILE = BD / "cases.json"
STATE_FILE = BD / "scan_state.json"
ZIPS_FILE = BD / "local_zips.json"
LOG_FILE = BD / "poll_bankruptcy_log.txt"

API_BASE = "https://www.courtlistener.com/api/rest/v4"
TOKEN = os.environ.get("COURTLISTENER_TOKEN", "")

ET = timezone(timedelta(hours=-4))

# ── Logging ──────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ── API helpers ──────────────────────────────────────────────────────────────

def api_get(url, retries=2):
    for attempt in range(retries + 1):
        try:
            out = subprocess.run(
                ["curl", "-s", "-H", f"Authorization: Token {TOKEN}",
                 "-H", "User-Agent: Inquirer Newsroom agutman@inquirer.com",
                 "--max-time", "30", url],
                capture_output=True, text=True, timeout=35,
            )
            if out.returncode != 0:
                raise RuntimeError(f"curl exit {out.returncode}")
            data = json.loads(out.stdout)
            if isinstance(data, dict) and "429" in str(data.get("detail", "")):
                if attempt < retries:
                    wait = 15 * (attempt + 1)
                    log(f"  rate-limited, sleeping {wait}s")
                    time.sleep(wait)
                    continue
            return data
        except (json.JSONDecodeError, RuntimeError) as e:
            if attempt < retries:
                time.sleep(5)
                continue
            raise

# ── Zip matching ─────────────────────────────────────────────────────────────

def load_local_zips():
    return json.loads(ZIPS_FILE.read_text())

ZIP_RE = re.compile(r"\b(\d{5})\b")
SLUG_RE = re.compile(r"[^a-z0-9]+")

def slugify(name):
    return SLUG_RE.sub("-", name.lower()).strip("-")[:60]

def extract_debtor_zip(extra_info, local_zips):
    """Extract a local zip from party_types[].extra_info text.

    Returns (zip, region) tuple or (None, None).
    Skips lines containing 'Tax ID' or 'EIN' to avoid false matches.
    """
    if not extra_info:
        return None, None
    for line in extra_info.split("\n"):
        if "Tax ID" in line or "EIN" in line:
            continue
        for m in ZIP_RE.finditer(line):
            z = m.group(1)
            if z in local_zips:
                return z, local_zips[z].get("region", "")
    return None, None

# ── State ────────────────────────────────────────────────────────────────────

def load_cases():
    if CASES_FILE.exists():
        return json.loads(CASES_FILE.read_text())
    return {}

def save_cases(cases):
    CASES_FILE.write_text(json.dumps(cases, indent=1))

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1))

# ── Core scan ────────────────────────────────────────────────────────────────

def scan_dockets(filed_after, filed_before=None, live=False):
    """Search for Chapter 11 dockets, fetch party data, match local zips."""
    local_zips = load_local_zips()
    cases = load_cases()
    new_hits = 0
    dockets_checked = 0
    parties_fetched = 0

    params = f"q=chapter%3A11&type=d&filed_after={filed_after}&order_by=dateFiled+asc"
    if filed_before:
        params += f"&filed_before={filed_before}"
    url = f"{API_BASE}/search/?{params}&format=json"

    while url:
        log(f"  fetching search page: ...{url[-80:]}")
        data = api_get(url)
        time.sleep(0.5)

        results = data.get("results", [])
        if not results:
            break

        for r in results:
            docket_id = str(r.get("docket_id", ""))
            if not docket_id or docket_id in cases:
                dockets_checked += 1
                continue

            case_name = r.get("caseName", "")
            # Strip HTML that sometimes appears in case names
            case_name = re.sub(r"<[^>]+>", "", case_name).strip()
            date_filed = r.get("dateFiled", "")
            court = r.get("court", "")
            court_id = r.get("court_id", "")
            docket_number = r.get("docketNumber", "")
            pacer_case_id = r.get("pacer_case_id", "")

            # Fetch party data for this docket
            party_url = f"{API_BASE}/parties/?docket={docket_id}&format=json"
            try:
                party_data = api_get(party_url)
                parties_fetched += 1
                time.sleep(0.5)
            except Exception as e:
                log(f"  party fetch failed for {docket_id}: {e}")
                dockets_checked += 1
                continue

            matched_zip = None
            matched_region = None
            debtor_name = case_name

            for party in party_data.get("results", []):
                for pt in party.get("party_types", []):
                    if pt.get("name") != "Debtor":
                        continue
                    extra = pt.get("extra_info", "")
                    z, region = extract_debtor_zip(extra, local_zips)
                    if z:
                        matched_zip = z
                        matched_region = region
                        debtor_name = party.get("name", case_name)
                        break
                if matched_zip:
                    break

            dockets_checked += 1

            if not matched_zip:
                continue

            cl_url = f"https://www.courtlistener.com/docket/{docket_id}/{slugify(case_name)}/"
            pacer_url = ""
            if pacer_case_id and court_id:
                pacer_url = f"https://ecf.{court_id}.uscourts.gov/cgi-bin/DktRpt.pl?{pacer_case_id}"

            cases[docket_id] = {
                "docket_id": int(docket_id),
                "case_name": case_name,
                "debtor_name": debtor_name,
                "docket_number": docket_number,
                "date_filed": date_filed,
                "court": court,
                "court_id": court_id,
                "debtor_zip": matched_zip,
                "debtor_region": matched_region,
                "courtlistener_url": cl_url,
                "pacer_url": pacer_url,
                "captured_at": datetime.now(ET).isoformat(timespec="seconds"),
            }
            new_hits += 1
            log(f"  + HIT: {debtor_name} | {docket_number} | {court_id} | zip={matched_zip} ({matched_region})")

            if live:
                try:
                    send_email(
                        subject_bankruptcy_alert(debtor_name),
                        body_bankruptcy_alert(
                            debtor_name, court, date_filed,
                            matched_zip, matched_region,
                            cl_url, pacer_url,
                            docket_id=docket_id,
                        ),
                        log_fn=log,
                    )
                    log(f"  email sent for {debtor_name}")
                except Exception as e:
                    log(f"  email error for {debtor_name}: {e}")

        url = data.get("next")

    save_cases(cases)
    log(f"  checked {dockets_checked} dockets, fetched {parties_fetched} party records, {new_hits} new local hits")
    return new_hits

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not TOKEN:
        log("ERROR: COURTLISTENER_TOKEN not set")
        sys.exit(1)

    args = sys.argv[1:]
    mode = args[0] if args else "poll"
    live = "--live" in args

    state = load_state()

    if mode == "backfill":
        start = "2026-04-01"
        end = None
        for a in args:
            if a.startswith("--start="):
                start = a.split("=", 1)[1]
            elif a.startswith("--end="):
                end = a.split("=", 1)[1]
        log(f"=== Backfill start (from {start} to {end or 'today'}, live={live}) ===")
        hits = scan_dockets(filed_after=start, filed_before=end, live=live)
        state["backfill_done"] = True
        state["backfill_through"] = datetime.now(ET).strftime("%Y-%m-%d")
        save_state(state)
        log(f"=== Backfill done. {hits} local hits. ===\n")

    elif mode == "poll":
        last = state.get("last_poll_date")
        if not last:
            last = (datetime.now(ET) - timedelta(days=1)).strftime("%Y-%m-%d")
        log(f"=== Poll start (since {last}, live={live}) ===")
        hits = scan_dockets(filed_after=last, live=live)
        state["last_poll_date"] = datetime.now(ET).strftime("%Y-%m-%d")
        save_state(state)
        log(f"=== Poll done. {hits} new local hits. ===\n")

    else:
        print(f"Unknown mode: {mode}. Use 'backfill' or 'poll'.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
