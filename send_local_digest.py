#!/usr/bin/env python3
"""
Weekly Local Court Digest

Reads region_state.json (the unified multi-court regional state), filters for
cases from the past 7 days, and emails a styled HTML digest to Denali Sagner.

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

AREA_ZIPS = {
    "lower_merion": {
        "19003", "19004", "19010", "19035", "19041",
        "19066", "19072", "19083", "19085", "19096",
    },
    "greater_media": {"19063", "19065", "19081", "19086", "19091"},
}
AREA_CITIES = {
    "lower_merion": set(),
    "greater_media": {"media", "swarthmore", "wallingford"},
}
AREA_ZIP_NAMES = {
    "lower_merion": {
        "19003": "Ardmore", "19004": "Bala Cynwyd", "19010": "Bryn Mawr",
        "19035": "Gladwyne", "19041": "Haverford", "19066": "Merion Station",
        "19072": "Narberth", "19083": "Havertown", "19085": "Villanova",
        "19096": "Wynnewood",
    },
    "greater_media": {
        "19063": "Media", "19065": "Media", "19081": "Swarthmore",
        "19086": "Wallingford", "19091": "Media",
    },
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
    zips = AREA_ZIPS[area_key]
    cities = AREA_CITIES[area_key]
    if party.get("zip") in zips:
        return True
    return bool(cities and party.get("city", "").strip().lower() in cities)


def split_by_area(matches):
    """Return (lm_cases, media_cases) each sorted by filing_date desc."""
    lm, media = [], []
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    for rec in matches.values():
        filed = (rec.get("filing_date") or rec.get("first_seen", ""))[:10]
        if filed < cutoff:
            continue
        in_lm = any(party_in_area(p, "lower_merion") for p in rec.get("parties", []))
        in_media = any(party_in_area(p, "greater_media") for p in rec.get("parties", []))
        if in_lm:
            lm.append(rec)
        if in_media:
            media.append(rec)
    lm.sort(key=lambda c: c.get("filing_date", ""), reverse=True)
    media.sort(key=lambda c: c.get("filing_date", ""), reverse=True)
    return lm, media


def local_parties_for(rec, area_key):
    return [p for p in rec.get("parties", []) if party_in_area(p, area_key)]


def municipality(party, area_key):
    return (AREA_ZIP_NAMES[area_key].get(party.get("zip", ""))
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


def build_email_html(lm_cases, media_cases):
    now = datetime.now().strftime("%B %d, %Y")
    week_start = (datetime.now() - timedelta(days=7)).strftime("%B %d")

    lm_cards = ("\n".join(case_html(c, "lower_merion", "#1b3a4b") for c in lm_cases)
                if lm_cases else
                '<div style="color:#999;font-style:italic;padding:12px;">No Lower Merion cases this week.</div>')
    media_cards = ("\n".join(case_html(c, "greater_media", "#2c3e50") for c in media_cases)
                   if media_cases else
                   '<div style="color:#999;font-style:italic;padding:12px;">No Greater Media cases this week.</div>')

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

        <h2 style="font-size:18px;color:#1b3a4b;border-bottom:2px solid #1b3a4b;padding-bottom:6px;margin-bottom:14px;">Lower Merion <span style="color:#888;font-weight:normal;font-size:14px;">({len(lm_cases)})</span></h2>
        {lm_cards}

        <h2 style="font-size:18px;color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;margin:24px 0 14px;">Greater Media <span style="color:#888;font-weight:normal;font-size:14px;">({len(media_cases)})</span></h2>
        {media_cards}

        <div style="text-align:center;margin-top:24px;">
            <a href="https://abgutman.github.io/av-tools/montco_lm_dashboard.html" style="display:inline-block;padding:10px 20px;background:#1b3a4b;color:white;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px;margin:4px;">Lower Merion Dashboard</a>
            <a href="https://abgutman.github.io/av-tools/delco_media_dashboard.html" style="display:inline-block;padding:10px 20px;background:#2c3e50;color:white;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px;margin:4px;">Greater Media Dashboard</a>
        </div>

    </div>

    <div style="text-align:center;padding:16px;color:#999;font-size:11px;">
        Lawsuits where a party has an address in this area, across Philadelphia, Montgomery, Bucks and Delaware county courts.<br>
        Always confirm against the official court docket before relying on or publishing this information.
    </div>

</div>
</body>
</html>'''


def build_plaintext(lm_cases, media_cases):
    now = datetime.now().strftime("%B %d, %Y")
    week_start = (datetime.now() - timedelta(days=7)).strftime("%B %d")
    lines = [f"LOCAL COURT DIGEST — Week of {week_start} - {now}", ""]
    lines.append(f"LOWER MERION ({len(lm_cases)} cases)")
    lines.append("-" * 50)
    for c in lm_cases:
        lines.append(case_text(c, "lower_merion"))
        lines.append("")
    if not lm_cases:
        lines += ["  No Lower Merion cases this week.", ""]
    lines.append(f"GREATER MEDIA ({len(media_cases)} cases)")
    lines.append("-" * 50)
    for c in media_cases:
        lines.append(case_text(c, "greater_media"))
        lines.append("")
    if not media_cases:
        lines += ["  No Greater Media cases this week.", ""]
    return "\n".join(lines)


def send_digest():
    state = load_region_state()
    lm_cases, media_cases = split_by_area(state.get("matches", {}))

    print(f"Lower Merion cases: {len(lm_cases)}")
    print(f"Greater Media cases: {len(media_cases)}")

    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("ALERT_TO") or "dsagner@inquirer.com"
    cc = os.environ.get("ALERT_CC") or "agutman@inquirer.com"

    now_str = datetime.now().strftime("%b %d")
    msg = EmailMessage()
    msg["Subject"] = f"Lower Merion and Greater Media court tracker — {now_str}"
    msg["From"] = user
    msg["To"] = to
    msg["Cc"] = cc
    msg.set_content(build_plaintext(lm_cases, media_cases))
    msg.add_alternative(build_email_html(lm_cases, media_cases), subtype="html")

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)

    print(f"Digest sent to {to} (cc: {cc})")


def main():
    send_digest()


if __name__ == "__main__":
    main()
