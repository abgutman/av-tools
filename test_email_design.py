#!/usr/bin/env python3
"""Send design-preview emails to agutman only. Delete after use."""
from email_utils import (
    send_email,
    subject_new_report, body_new_report_edgar,
    subject_save_the_date, body_save_the_date,
)

TO = ["agutman@inquirer.com"]

# Preview 1: EDGAR earnings report (based on the real CubeSmart filing)
ok1 = send_email(
    subject_new_report("CubeSmart", "CUBE"),
    body_new_report_edgar(
        "CubeSmart", "CUBE",
        filing_date="2026-06-01",
        url="https://www.sec.gov/Archives/edgar/data/1298675/000129867526000030/cube-20260601x8k.htm",
        accepted_at="2026-06-01T20:15:33.000Z",
        detected_at="2026-06-01T20:30:00.000Z",
    ),
    to=TO,
)
print("EDGAR email:", "sent" if ok1 else "failed")

# Preview 2: Save the date (based on the real Five Below entry)
ok2 = send_email(
    subject_save_the_date("Five Below, Inc.", "FIVE"),
    body_save_the_date(
        "Five Below, Inc.", "FIVE",
        release_date="2026-06-03",
        call_date="2026-06-03",
        call_time="16:30 ET",
        source_url="https://finance.yahoo.com/markets/stocks/articles/five-below-inc-announces-first-200100799.html",
        headline="Five Below, Inc. Announces First Quarter Fiscal 2026 Earnings Release and Conference Call Date",
        published_unix=1747742400,  # approx May 20, 2026 at 8 AM ET
    ),
    to=TO,
)
print("Save-the-date email:", "sent" if ok2 else "failed")
