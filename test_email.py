#!/usr/bin/env python3
import sys
sys.path.insert(0, "/Users/gutmana/Desktop/claude_sandbox")
from email_utils import send_email

ok = send_email(
    subject="Test: earnings alert system",
    body="This is a test of the av-tools earnings alert system. If you got this, it works.\n",
)
print("✉ sent" if ok else "⚠ failed — check GMAIL_USER and GMAIL_APP_PASSWORD")
