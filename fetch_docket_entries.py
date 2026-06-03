#!/usr/bin/env python3
"""Fetch docket entries from CourtListener for tracked bankruptcy cases.

Usage:
  python fetch_docket_entries.py              # all cases (incremental)
  python fetch_docket_entries.py 73205273     # single case by docket_id
"""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
BD = HERE / "bankruptcy_data"
CASES_FILE = BD / "cases.json"
ENTRIES_DIR = BD / "docket_entries"
STATE_FILE = BD / "docket_fetch_state.json"
LOG_FILE = BD / "poll_bankruptcy_log.txt"

API_BASE = "https://www.courtlistener.com/api/rest/v4"
TOKEN = os.environ.get("COURTLISTENER_TOKEN", "")

ET = timezone(timedelta(hours=-4))

CATEGORIES = [
    ("petition",          re.compile(r"(?:voluntary\s+)?petition|chapter\s+11\s+petition", re.I)),
    ("declaration",       re.compile(r"\bdeclaration\b", re.I)),
    ("affidavit",         re.compile(r"\baffidavit\b", re.I)),
    ("opinion",           re.compile(r"\bopinion\b|memorandum\s+opinion|order\s+and\s+opinion", re.I)),
    ("dip_financing",     re.compile(r"debtor.in.possession|DIP\s+(?:financing|motion|order|facility|credit)|postpetition\s+(?:financing|credit)", re.I)),
    ("first_day",         re.compile(r"first[\s-]day", re.I)),
    ("bid_sale",          re.compile(r"bid\s+procedures|sale\s+(?:motion|order)|auction|stalking\s+horse", re.I)),
    ("plan",              re.compile(r"plan\s+of\s+reorganization|disclosure\s+statement|chapter\s+11\s+plan", re.I)),
    ("confirmation",      re.compile(r"confirmation\s+(?:order|hearing)|order\s+confirming", re.I)),
    ("bar_date",          re.compile(r"bar\s+date|claims\s+bar|proof\s+of\s+claim\s+deadline", re.I)),
    ("341_meeting",       re.compile(r"\b341\b|meeting\s+of\s+creditors", re.I)),
    ("examiner_trustee",  re.compile(r"appoint.*(?:examiner|chapter\s+11\s+trustee)|(?:examiner|special\s+master)\s+(?:appointed|report)", re.I)),
]

EXAMINER_TRUSTEE_EXCLUDE = re.compile(r"US\s+Trustee|indenture\s+trustee|United\s+States\s+Trustee", re.I)

KEY_DATE_MAP = {
    "first_day":    "first_day_hearing",
    "341_meeting":  "341_meeting",
    "bar_date":     "bar_date",
    "confirmation": "plan_confirmation",
    "bid_sale":     "sale_deadline",
}

def log(msg):
    ts = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

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

def classify(description):
    hits = []
    for cat, pat in CATEGORIES:
        if pat.search(description):
            if cat == "examiner_trustee" and EXAMINER_TRUSTEE_EXCLUDE.search(description):
                continue
            hits.append(cat)
    return hits

def extract_recap_docs(entry):
    docs = []
    for rd in entry.get("recap_documents", []):
        url = rd.get("filepath_ia") or rd.get("filepath_local") or ""
        if not url and rd.get("id"):
            url = f"https://www.courtlistener.com/docket-entry/{entry.get('id')}/recapdocument/{rd['id']}/"
        if url:
            docs.append({
                "url": url,
                "description": (rd.get("description") or rd.get("plain_text", ""))[:150],
            })
    return docs

def determine_status(date_str):
    if not date_str:
        return "unknown"
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        today = datetime.now(ET).date()
        return "happened" if d <= today else "scheduled"
    except Exception:
        return "unknown"

def fetch_case(docket_id, cases, state):
    case = cases.get(str(docket_id))
    if not case:
        log(f"  docket {docket_id} not in cases.json, skipping")
        return None

    case_state = state.get(str(docket_id), {})
    last_entry = case_state.get("last_entry_number", 0)

    existing_path = ENTRIES_DIR / f"{docket_id}.json"
    existing = {}
    if existing_path.exists():
        existing = json.loads(existing_path.read_text())

    existing_classified = existing.get("classified_entries", [])

    url = f"{API_BASE}/docket-entries/?docket={docket_id}&order_by=entry_number&format=json"
    if last_entry > 0:
        url += f"&entry_number__gt={last_entry}"

    all_entries = []
    pages = 0
    while url:
        log(f"  fetching entries page {pages+1}: ...{url[-80:]}")
        data = api_get(url)
        time.sleep(1)
        pages += 1

        results = data.get("results", [])
        if not results:
            break
        all_entries.extend(results)
        url = data.get("next")

    new_classified = []
    max_entry = last_entry
    for entry in all_entries:
        entry_num = entry.get("entry_number") or 0
        if entry_num > max_entry:
            max_entry = entry_num

        desc = entry.get("description", "")
        cats = classify(desc)
        if not cats:
            continue

        docs = extract_recap_docs(entry)
        for cat in cats:
            new_classified.append({
                "entry_number": entry_num,
                "date_filed": entry.get("date_filed", ""),
                "description": desc[:200],
                "category": cat,
                "recap_documents": docs,
            })

    merged_classified = existing_classified + new_classified
    merged_classified.sort(key=lambda e: e.get("entry_number", 0))

    key_dates = existing.get("key_dates", {
        "petition_date":     {"date": None, "status": "unknown", "source": None},
        "first_day_hearing": {"date": None, "status": "unknown", "source": None},
        "341_meeting":       {"date": None, "status": "unknown", "source": None},
        "bar_date":          {"date": None, "status": "unknown", "source": None},
        "plan_confirmation": {"date": None, "status": "unknown", "source": None},
        "sale_deadline":     {"date": None, "status": "unknown", "source": None},
    })

    key_dates["petition_date"] = {
        "date": case.get("date_filed"),
        "status": determine_status(case.get("date_filed")),
        "source": "case filing",
    }

    for entry in new_classified:
        cat = entry["category"]
        date_key = KEY_DATE_MAP.get(cat)
        if not date_key:
            continue
        if key_dates[date_key]["date"] is None:
            key_dates[date_key] = {
                "date": entry["date_filed"],
                "status": determine_status(entry["date_filed"]),
                "source": f"entry #{entry['entry_number']}",
            }

    flags = {
        "has_examiner_or_trustee": any(e["category"] == "examiner_trustee" for e in merged_classified),
        "has_dip_financing": any(e["category"] == "dip_financing" for e in merged_classified),
        "has_sale_process": any(e["category"] == "bid_sale" for e in merged_classified),
    }

    result = {
        "docket_id": int(docket_id),
        "last_fetched": datetime.now(ET).isoformat(timespec="seconds"),
        "last_entry_number": max_entry,
        "total_entries": len(all_entries) + existing.get("total_entries", 0) if last_entry > 0 else len(all_entries),
        "classified_entries": merged_classified,
        "key_dates": key_dates,
        "flags": flags,
    }

    ENTRIES_DIR.mkdir(parents=True, exist_ok=True)
    (ENTRIES_DIR / f"{docket_id}.json").write_text(json.dumps(result, indent=1))

    state[str(docket_id)] = {
        "last_fetched": datetime.now(ET).strftime("%Y-%m-%d"),
        "last_entry_number": max_entry,
    }

    log(f"  {case.get('debtor_name', docket_id)}: {len(all_entries)} entries fetched, {len(new_classified)} classified, {len(merged_classified)} total classified")
    return result

def main():
    if not TOKEN:
        log("ERROR: COURTLISTENER_TOKEN not set")
        sys.exit(1)

    cases = json.loads(CASES_FILE.read_text()) if CASES_FILE.exists() else {}
    if not cases:
        log("No cases in cases.json")
        sys.exit(0)

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}

    target_ids = sys.argv[1:] if len(sys.argv) > 1 else list(cases.keys())

    log(f"=== Fetching docket entries for {len(target_ids)} case(s) ===")
    for did in target_ids:
        try:
            fetch_case(did, cases, state)
        except Exception as e:
            log(f"  ERROR fetching {did}: {e}")

    STATE_FILE.write_text(json.dumps(state, indent=1))
    log("=== Done fetching docket entries ===\n")

if __name__ == "__main__":
    main()
