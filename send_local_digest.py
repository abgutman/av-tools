#!/usr/bin/env python3
"""
Weekly Local Court Digest

Reads region_state.json (the unified multi-court regional state), filters for
cases from the past 7 days, and emails a styled HTML digest.

Only regions with in_digest=True in regions.py are included. Regions are grouped
by recipient set: a region may carry its own "recipients" dict in regions.py
({"to": [...], "cc": [...]}); regions without one fall back to the default
(ALERT_TO / ALERT_CC). One email is sent per distinct recipient group.

  - Lower Merion + Greater Media -> default (dsagner, cc agutman)
  - Abington and Cheltenham       -> agutman + jrohan

Requires env vars: GMAIL_USER, GMAIL_APP_PASSWORD
Optional: ALERT_TO (default dsagner@inquirer.com), ALERT_CC (default agutman@inquirer.com)
"""

import html
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage

import regions

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "region_state.json")
LOOKBACK_DAYS = 7

COURT_BADGE_COLORS = {
    "philadelphia": "#0072B2",
    "montgomery":   "#009E73",
    "bucks":        "#9467BD",
    "delaware":     "#D55E00",
}
COURT_LABELS = {
    "philadelphia": "Philadelphia",
    "montgomery":   "Montgomery",
    "bucks":        "Bucks",
    "delaware":     "Delaware",
}


def e(text):
    return html.escape(str(text)) if text else ""


def load_region_state():
    if not os.path.exists(STATE_FILE):
        print(f"State file not found: {STATE_FILE}")
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def party_in_area(party, area_key):
    a = regions.AREAS[area_key]
    return regions.party_in(party, a["zips"], a["cities"])


def split_by_area(matches):
    """Return dict {area_key: [cases]} for digest regions, each sorted filing_date desc."""
    digest_keys = [k for k, a in regions.AREAS.items() if a["in_digest"]]
    result = {k: [] for k in digest_keys}
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    for rec in matches.values():
        filed = (rec.get("filing_date") or rec.get("first_seen", ""))[:10]
        if filed < cutoff:
            continue
        for k in digest_keys:
            a = regions.AREAS[k]
            if any(regions.party_in(p, a["zips"], a["cities"])
                   for p in rec.get("parties", [])):
                result[k].append(rec)
    for k in digest_keys:
        result[k].sort(key=lambda c: c.get("filing_date", ""), reverse=True)
    return result


def local_parties_for(rec, area_key):
    return [p for p in rec.get("parties", []) if party_in_area(p, area_key)]


def municipality(party, area_key):
    return (regions.AREAS[area_key]["zip_names"].get(party.get("zip", ""))
            or party.get("city") or party.get("zip") or "")


def court_badge_html(court):
    color = COURT_BADGE_COLORS.get(court, "#555")
    label = COURT_LABELS.get(court, court.title())
    return (f'<span style="background:{color};color:white;padding:2px 8px;'
            f'border-radius:10px;font-size:11px;font-weight:700;">{e(label)}</span>')


def case_html(rec, area_key, accent):
    url = e(rec.get("url", ""))
    case_num = e(rec.get("case_number", ""))
    case_type = e(rec.get("case_type", "—"))
    caption = e(rec.get("caption", ""))
    filed = e(rec.get("filing_date", ""))
    court = rec.get("court", "")
    badge = court_badge_html(court)

    case_link = (f'<a href="{url}" style="font-weight:700;color:{accent};'
                 f'text-decoration:none;font-size:14px;">{case_num}</a>'
                 if url else
                 f'<span style="font-weight:700;color:{accent};font-size:14px;">{case_num}</span>')

    parties_html = ""
    for p in local_parties_for(rec, area_key):
        muni = municipality(p, area_key)
        loc = f' <span style="color:#2c7a4b;font-weight:600;">({e(muni)})</span>' if muni else ""
        parties_html += (f'<div style="font-size:13px;margin:2px 0;padding:3px 8px;'
                         f'background:#f0fdf4;border-radius:4px;">'
                         f'{e(p["role"])}: <strong>{e(p["name"])}</strong>{loc}</div>')

    return f'''<div style="background:white;border-radius:8px;padding:16px;margin-bottom:10px;border-left:4px solid {accent};">
        <div style="margin-bottom:4px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            {case_link} {badge}
            <span style="background:#e8f0fe;color:#1a56db;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">{case_type}</span>
        </div>
        <div style="font-size:14px;margin-bottom:3px;">{caption}</div>
        <div style="font-size:12px;color:#666;margin-bottom:6px;">Filed: {filed}</div>
        {parties_html}
    </div>'''


def case_text(rec, area_key):
    court = COURT_LABELS.get(rec.get("court", ""), "?")
    lines = [
        f"  [{court}] {rec.get('case_number','')} ({rec.get('case_type','')})",
        f"  {rec.get('caption','')}",
        f"  Filed: {rec.get('filing_date','')}",
    ]
    for p in local_parties_for(rec, area_key):
        muni = municipality(p, area_key)
        lines.append(f"    {p['role']}: {p['name']}" + (f" ({muni})" if muni else ""))
    if rec.get("url"):
        lines.append(f"  {rec['url']}")
    return "\n".join(lines)


def build_email_html(area_cases):
    """area_cases: dict {area_key: [cases]} for digest regions in insertion order."""
    now = datetime.now().strftime("%B %d, %Y")
    week_start = (datetime.now() - timedelta(days=7)).strftime("%B %d")

    sections_html = ""
    first = True
    for area_key, cases in area_cases.items():
        area = regions.AREAS[area_key]
        name = area["name"]
        accent = area["accent"]
        margin_top = "0" if first else "24px"
        first = False

        if cases:
            cards = "\n".join(case_html(c, area_key, accent) for c in cases)
        else:
            cards = f'<div style="color:#999;font-style:italic;padding:12px;">No {e(name)} cases this week.</div>'

        sections_html += f'''
        <h2 style="font-size:18px;color:{accent};border-bottom:2px solid {accent};padding-bottom:6px;margin:{margin_top} 0 14px;">{e(name)} <span style="color:#888;font-weight:normal;font-size:14px;">({len(cases)})</span></h2>
        {cards}'''

    buttons_html = ""
    for area_key, cases in area_cases.items():
        area = regions.AREAS[area_key]
        url = f"https://abgutman.github.io/av-tools/{area['output_html']}"
        buttons_html += (f'<a href="{url}" style="display:inline-block;padding:10px 20px;'
                         f'background:{area["accent"]};color:white;border-radius:6px;'
                         f'text-decoration:none;font-weight:600;font-size:13px;margin:4px;">'
                         f'{e(area["name"])} Dashboard</a>\n            ')

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:20px;">

    <div style="background:#1a1a2e;color:white;padding:24px;text-align:center;border-radius:8px 8px 0 0;">
        <h1 style="margin:0;font-size:22px;">Local Court Digest</h1>
        <div style="color:#aaa;font-size:13px;margin-top:4px;">Week of {week_start} &ndash; {now} &middot; All 4 regional courts</div>
    </div>

    <div style="background:#f9f9f9;padding:24px;border-radius:0 0 8px 8px;">
        {sections_html}

        <div style="text-align:center;margin-top:24px;">
            {buttons_html}
        </div>

    </div>

    <div style="text-align:center;padding:16px;color:#999;font-size:11px;">
        Lawsuits where a party has an address in this area, across Philadelphia, Montgomery, Bucks and Delaware county courts.<br>
        Always confirm against the official court docket before relying on or publishing this information.
    </div>

</div>
</body>
</html>'''


def build_plaintext(area_cases):
    now = datetime.now().strftime("%B %d, %Y")
    week_start = (datetime.now() - timedelta(days=7)).strftime("%B %d")
    lines = [f"LOCAL COURT DIGEST — Week of {week_start} - {now}", ""]
    for area_key, cases in area_cases.items():
        name = regions.AREAS[area_key]["name"]
        lines.append(f"{name.upper()} ({len(cases)} cases)")
        lines.append("-" * 50)
        for c in cases:
            lines.append(case_text(c, area_key))
            lines.append("")
        if not cases:
            lines += [f"  No {name} cases this week.", ""]
    return "\n".join(lines)


def recipients_for(area_key, default_to, default_cc):
    """(to_tuple, cc_tuple) for a region — its own recipients or the default."""
    r = regions.AREAS[area_key].get("recipients")
    if r:
        to = tuple(r.get("to") or [default_to])
        cc = tuple(r.get("cc") or [])
    else:
        to = (default_to,)
        cc = (default_cc,) if default_cc else ()
    return to, cc


def group_by_recipients(area_cases, default_to, default_cc):
    """Return dict {(to_tuple, cc_tuple): {area_key: cases}} preserving order."""
    groups = {}
    for area_key, cases in area_cases.items():
        key = recipients_for(area_key, default_to, default_cc)
        groups.setdefault(key, {})[area_key] = cases
    return groups


def send_digest():
    state = load_region_state()
    area_cases = split_by_area(state.get("matches", {}))

    for k in area_cases:
        print(f"{regions.AREAS[k]['name']} cases: {len(area_cases[k])}")

    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    default_to = os.environ.get("ALERT_TO") or "dsagner@inquirer.com"
    default_cc = os.environ.get("ALERT_CC") or "agutman@inquirer.com"

    groups = group_by_recipients(area_cases, default_to, default_cc)
    now_str = datetime.now().strftime("%b %d")

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(user, pwd)
        for (to, cc), group_cases in groups.items():
            names = [regions.AREAS[k]["name"] for k in group_cases]
            subject = f"{', '.join(names)} court tracker — {now_str}"

            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = user
            msg["To"] = ", ".join(to)
            if cc:
                msg["Cc"] = ", ".join(cc)
            msg.set_content(build_plaintext(group_cases))
            msg.add_alternative(build_email_html(group_cases), subtype="html")
            s.send_message(msg)

            cc_note = f" (cc: {', '.join(cc)})" if cc else ""
            print(f"Digest sent to {', '.join(to)}{cc_note} — {', '.join(names)}")


def main():
    send_digest()


if __name__ == "__main__":
    main()
