#!/usr/bin/env python3
"""
Weekly Local Court Digest

Reads state files for Lower Merion (Montco) and Greater Media (Delco),
filters for cases from the past 7 days, and emails a styled HTML digest.

Requires env vars: GMAIL_USER, GMAIL_APP_PASSWORD, ALERT_TO
"""

import html
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

STATE_LM = "montco_lm_state.json"
STATE_MEDIA = "delco_media_state.json"
LOOKBACK_DAYS = 7

FIRST_MESSAGE = """Bonjour Denali! I hope your sister's graduation was wonderful, and that you enjoy Montreal. Vive le Québec libre! I've been playing with building internal reporting tools, mostly for fun, and built a local tracker for court cases with plaintiff or defendant from Lower Merion and Greater Media. I'm not sure it's helpful but click around and tell me if you think this can be useful to you — I can also try to expand it, make it smaller. Whatever you want! It currently looks at Delco and Montco courts, so no cases from there that were filed in Philly Common Pleas (which would be a good chunk). This has been a great learning experience for me, so even if you don't use it at all no worries. Just tell me so I won't maintain it. Oh and it will send you this digest every week on Monday morning with everything from the past week. And no, the fact that I literally have a list of Lower Merion Zip codes will not make me ever stop asking you, what is Lower Merion?!"""


def esc(text):
    return html.escape(str(text)) if text else ""


def load_cases(state_file):
    if not os.path.exists(state_file):
        return []
    with open(state_file) as f:
        state = json.load(f)
    return list(state.get("cases", {}).values())


def filter_recent(cases, days=LOOKBACK_DAYS):
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = []
    for c in cases:
        filed = c.get("filed_date") or c.get("commenced") or ""
        if filed >= cutoff and c.get("lm_match") is not False and c.get("media_match") is not False:
            if c.get("lm_zips") or c.get("media_parties"):
                recent.append(c)
    return recent


def case_html_lm(c):
    url = esc(c.get("detail_url", "#"))
    case_num = esc(c.get("case_number", ""))
    case_type = esc(c.get("case_type", ""))
    plaintiff = esc(c.get("plaintiff", ""))
    defendant = esc(c.get("defendant", ""))
    filed = esc(c.get("commenced", ""))
    judge = c.get("judge", "")

    parties_html = ""
    for p in c.get("lm_parties", []):
        parties_html += f'<div style="font-size:13px;margin:2px 0;padding:3px 8px;background:#f0fdf4;border-radius:4px;">{esc(p["role"])}: <strong>{esc(p["name"])}</strong> <span style="color:#666;">{esc(p["address"])}</span></div>'

    return f'''<div style="background:white;border-radius:8px;padding:16px;margin-bottom:10px;border-left:4px solid #1b3a4b;">
        <div style="margin-bottom:4px;"><a href="{url}" style="font-weight:700;color:#1b3a4b;text-decoration:none;font-size:14px;">{case_num}</a> <span style="background:#e8f0fe;color:#1a56db;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">{case_type}</span></div>
        <div style="font-size:14px;margin-bottom:3px;">{plaintiff} v. {defendant}</div>
        <div style="font-size:12px;color:#666;margin-bottom:6px;">Filed: {filed}{f" | Judge: {esc(judge)}" if judge else ""}</div>
        {parties_html}
    </div>'''


def case_html_media(c):
    url = esc(c.get("detail_url", "#"))
    case_num = esc(c.get("case_number", ""))
    case_type = esc(c.get("case_type", ""))
    title = esc(c.get("title", ""))
    filed = esc(c.get("filed_date", ""))
    judge = c.get("judge", "")

    parties_html = ""
    for p in c.get("media_parties", []):
        parties_html += f'<div style="font-size:13px;margin:2px 0;padding:3px 8px;background:#f0fdf4;border-radius:4px;">{esc(p["role"])}: <strong>{esc(p["name"])}</strong> <span style="color:#666;">{esc(p["address"])}</span></div>'

    return f'''<div style="background:white;border-radius:8px;padding:16px;margin-bottom:10px;border-left:4px solid #2c3e50;">
        <div style="margin-bottom:4px;"><a href="{url}" style="font-weight:700;color:#2c3e50;text-decoration:none;font-size:14px;">{case_num}</a> <span style="background:#e8f0fe;color:#1a56db;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;">{case_type}</span></div>
        <div style="font-size:14px;margin-bottom:3px;">{title}</div>
        <div style="font-size:12px;color:#666;margin-bottom:6px;">Filed: {filed}{f" | Judge: {esc(judge)}" if judge else ""}</div>
        {parties_html}
    </div>'''


def build_email_html(lm_cases, media_cases, include_first_message=False):
    now = datetime.now().strftime("%B %d, %Y")
    week_start = (datetime.now() - timedelta(days=7)).strftime("%B %d")

    lm_cards = "\n".join(case_html_lm(c) for c in lm_cases) if lm_cases else '<div style="color:#999;font-style:italic;padding:12px;">No Lower Merion cases this week.</div>'
    media_cards = "\n".join(case_html_media(c) for c in media_cases) if media_cases else '<div style="color:#999;font-style:italic;padding:12px;">No Greater Media cases this week.</div>'

    first_msg_block = ""
    if include_first_message:
        first_msg_block = f'''<div style="background:#fffbeb;border:1px solid #f59e0b;border-radius:8px;padding:20px;margin-bottom:24px;font-size:14px;line-height:1.7;color:#333;">
            {esc(FIRST_MESSAGE)}
            <div style="margin-top:12px;"><a href="https://abgutman.github.io/av-tools/montco_lm_dashboard.html" style="display:inline-block;padding:8px 16px;background:#1b3a4b;color:white;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px;margin-right:8px;">Lower Merion Dashboard</a> <a href="https://abgutman.github.io/av-tools/delco_media_dashboard.html" style="display:inline-block;padding:8px 16px;background:#2c3e50;color:white;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px;">Greater Media Dashboard</a></div>
        </div>'''

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:20px;">

    <div style="background:#1a1a2e;color:white;padding:24px;text-align:center;border-radius:8px 8px 0 0;">
        <h1 style="margin:0;font-size:22px;">Local Court Digest</h1>
        <div style="color:#aaa;font-size:13px;margin-top:4px;">Week of {week_start} &ndash; {now}</div>
    </div>

    <div style="background:#f9f9f9;padding:24px;border-radius:0 0 8px 8px;">

        {first_msg_block}

        <h2 style="font-size:18px;color:#1b3a4b;border-bottom:2px solid #1b3a4b;padding-bottom:6px;margin-bottom:14px;">Lower Merion &mdash; Montgomery County <span style="color:#888;font-weight:normal;font-size:14px;">({len(lm_cases)})</span></h2>
        {lm_cards}

        <h2 style="font-size:18px;color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:6px;margin:24px 0 14px;">Greater Media &mdash; Delaware County <span style="color:#888;font-weight:normal;font-size:14px;">({len(media_cases)})</span></h2>
        {media_cards}

        <div style="text-align:center;margin-top:24px;">
            <a href="https://abgutman.github.io/av-tools/montco_lm_dashboard.html" style="display:inline-block;padding:10px 20px;background:#1b3a4b;color:white;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px;margin:4px;">Lower Merion Dashboard</a>
            <a href="https://abgutman.github.io/av-tools/delco_media_dashboard.html" style="display:inline-block;padding:10px 20px;background:#2c3e50;color:white;border-radius:6px;text-decoration:none;font-weight:600;font-size:13px;margin:4px;">Greater Media Dashboard</a>
        </div>

    </div>

    <div style="text-align:center;padding:16px;color:#999;font-size:11px;">
        Data from Montgomery County Prothonotary &amp; Delaware County C-Track Public Access
    </div>

</div>
</body>
</html>'''


def build_plaintext(lm_cases, media_cases, include_first_message=False):
    now = datetime.now().strftime("%B %d, %Y")
    week_start = (datetime.now() - timedelta(days=7)).strftime("%B %d")

    lines = [f"LOCAL COURT DIGEST — Week of {week_start} - {now}", ""]

    if include_first_message:
        lines.append(FIRST_MESSAGE)
        lines.append("")
        lines.append("Lower Merion: https://abgutman.github.io/av-tools/montco_lm_dashboard.html")
        lines.append("Greater Media: https://abgutman.github.io/av-tools/delco_media_dashboard.html")
        lines.append("")

    lines.append(f"LOWER MERION — Montgomery County ({len(lm_cases)} cases)")
    lines.append("-" * 50)
    for c in lm_cases:
        lines.append(f"  {c.get('case_number', '')} ({c.get('case_type', '')})")
        lines.append(f"  {c.get('plaintiff', '')} v. {c.get('defendant', '')}")
        lines.append(f"  Filed: {c.get('commenced', '')}")
        for p in c.get("lm_parties", []):
            lines.append(f"    {p['role']}: {p['name']} — {p['address']}")
        lines.append(f"  {c.get('detail_url', '')}")
        lines.append("")

    if not lm_cases:
        lines.append("  No Lower Merion cases this week.")
        lines.append("")

    lines.append(f"GREATER MEDIA — Delaware County ({len(media_cases)} cases)")
    lines.append("-" * 50)
    for c in media_cases:
        lines.append(f"  {c.get('case_number', '')} ({c.get('case_type', '')})")
        lines.append(f"  {c.get('title', '')}")
        lines.append(f"  Filed: {c.get('filed_date', '')}")
        for p in c.get("media_parties", []):
            lines.append(f"    {p['role']}: {p['name']} — {p['address']}")
        lines.append(f"  {c.get('detail_url', '')}")
        lines.append("")

    if not media_cases:
        lines.append("  No Greater Media cases this week.")
        lines.append("")

    return "\n".join(lines)


def send_digest(include_first_message=False):
    lm_all = load_cases(STATE_LM)
    media_all = load_cases(STATE_MEDIA)

    lm_recent = filter_recent(lm_all)
    media_recent = filter_recent(media_all)

    lm_recent.sort(key=lambda x: x.get("commenced", ""), reverse=True)
    media_recent.sort(key=lambda x: x.get("filed_date", ""), reverse=True)

    total = len(lm_recent) + len(media_recent)
    print(f"Lower Merion cases: {len(lm_recent)}")
    print(f"Greater Media cases: {len(media_recent)}")

    user = os.environ["GMAIL_USER"]
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("ALERT_TO", "agutman@inquirer.com")

    now_str = datetime.now().strftime("%b %d")
    msg = EmailMessage()
    if include_first_message:
        msg["Subject"] = "WHAT IS LOWER MERION?! We track court to find out."
    else:
        msg["Subject"] = f"Lower Merion and Greater Media court tracker — {now_str}"
    msg["From"] = user
    msg["To"] = to
    msg.set_content(build_plaintext(lm_recent, media_recent, include_first_message))
    msg.add_alternative(build_email_html(lm_recent, media_recent, include_first_message), subtype="html")

    with smtplib.SMTP("smtp.gmail.com", 587) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)

    print(f"Digest sent to {to}")


def main():
    first = "--first" in sys.argv
    send_digest(include_first_message=first)


if __name__ == "__main__":
    main()
