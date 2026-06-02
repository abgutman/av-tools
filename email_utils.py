#!/usr/bin/env python3
"""Shared email sender for earnings alert scripts.

To change alert text: edit the TEMPLATES section below.
"""
import os, smtplib, ssl
from email.mime.text import MIMEText

# ── Recipients & credentials ─────────────────────────────────────────────────
EMAIL_TO = ["agutman@inquirer.com", "EPalan@inquirer.com", "eravitch@inquirer.com"]
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# ── TEMPLATES — edit the strings below to change email text ──────────────────

def subject_new_report(name, ticker):
    return f"New earning report: {name}"

def subject_save_the_date(name, ticker):
    return f"Save the date: {name}"

def body_new_report_edgar(name, ticker, filing_date, url):
    return (
        f"New quarterly earnings filing detected on SEC EDGAR.\n\n"
        f"Company:  {name} ({ticker})\n"
        f"Filed:    {filing_date}\n"
        f"Filing:   {url}\n\n"
        f"This is the official 8-K item 2.02 — the formal SEC submission of the\n"
        f"quarterly earnings press release. The wire press release typically posts\n"
        f"5–15 minutes before EDGAR accepts the 8-K.\n\n"
        f"Dashboard: https://abgutman.github.io/av-tools/recent_earnings.html\n"
    )

def body_new_report_wire(name, ticker, published_str, headline, url):
    return (
        f"New wire-published earnings item detected.\n\n"
        f"Company:   {name} ({ticker})\n"
        f"Headline:  {headline}\n"
        f"Published: {published_str}\n"
        f"Link:      {url}\n\n"
        f"This matched our wire-publisher + earnings-keyword filters. It may be\n"
        f"the actual results release, a save-the-date, or a related filing.\n"
        f"Read the headline to determine which.\n\n"
        f"Dashboard: https://abgutman.github.io/av-tools/recent_earnings.html\n"
    )

def body_save_the_date(name, ticker, release_date, call_date, call_time, source_url, headline):
    lines = [
        "Save-the-date earnings announcement detected.",
        "",
        f"Company:      {name} ({ticker})",
    ]
    if release_date:
        lines.append(f"Release date: {release_date}")
    if call_date:
        lines.append(f"Call date:    {call_date}")
    if call_time:
        lines.append(f"Call time:    {call_time}")
    lines += [
        f"Source:       {headline[:120]}",
        f"Link:         {source_url}",
        "",
        "Dashboard: https://abgutman.github.io/av-tools/upcoming_earnings.html",
    ]
    return "\n".join(lines) + "\n"


# ── Sender ────────────────────────────────────────────────────────────────────

def send_email(subject, body, log_fn=None, to=None):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        if log_fn:
            log_fn(f"⚠ No Gmail creds; would have sent: {subject}")
        return False
    recipients = to if to is not None else EMAIL_TO
    if isinstance(recipients, str):
        recipients = [recipients]
    msg = MIMEText(body, "plain")
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)
    return True
