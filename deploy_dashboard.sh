#!/bin/bash
# Deploy updated dashboard to GitHub Pages
# Run from claude_sandbox directory

set -e
cd "$(dirname "$0")"

echo "=== Court Dashboard Daily Update ==="
echo "$(date)"
echo

# 1. Fetch court calendar
echo "--- Fetching court calendar ---"
python3 fetch_philly_calendar.py

# 2. Fetch new felony filings
echo "--- Fetching felony filings ---"
python3 fetch_philly_felonies.py

# 3. Fetch plea calendar
echo "--- Fetching plea calendar ---"
python3 fetch_philly_pleas.py

# 4. Run plea watch (daily mode)
echo "--- Running plea watch ---"
python3 fetch_plea_watch.py --daily

# 5. Fetch Lower Merion civil cases (Montco)
echo "--- Fetching Lower Merion cases ---"
python3 fetch_montco_lm.py --daily

# 6. Fetch Media civil cases (Delco)
echo "--- Fetching Media cases ---"
python3 fetch_delco_media.py --daily

# 7. Refresh earnings calendar (EDGAR cadence + dashboard rebuild)
echo "--- Refreshing earnings calendar ---"
cd earnings_data
python3 derive_cadence.py 2>&1 | tail -3
cd ..
python3 build_earnings_dashboard.py

# 8. Rebuild bankruptcy dashboard
echo "--- Rebuilding bankruptcy dashboard ---"
python3 build_bankruptcy_dashboard.py

# 9. Copy updated files to deploy repo
echo "--- Deploying to GitHub ---"
DEPLOY_DIR="/tmp/court-dashboard"

cp index.html "$DEPLOY_DIR/"
cp philly_calendar.html "$DEPLOY_DIR/"
cp philly_felonies_dashboard.html "$DEPLOY_DIR/"
cp philly_plea_calendar.html "$DEPLOY_DIR/"
cp philly_new_pleas.html "$DEPLOY_DIR/"
cp civil_edpa_dashboard.html "$DEPLOY_DIR/"
cp habeas_edpa.html "$DEPLOY_DIR/"
cp montco_lm_dashboard.html "$DEPLOY_DIR/"
cp delco_media_dashboard.html "$DEPLOY_DIR/"
cp earnings_dashboard.html "$DEPLOY_DIR/"
cp bankruptcy_dashboard.html "$DEPLOY_DIR/"
cp robots.txt "$DEPLOY_DIR/"
cp -r dockets "$DEPLOY_DIR/"

# Also sync data files the workflow needs
cp plea_watchlist.json "$DEPLOY_DIR/"
cp broad_candidates.json "$DEPLOY_DIR/"
cp philly_calendar.json "$DEPLOY_DIR/"
cp philly_felonies.json "$DEPLOY_DIR/" 2>/dev/null || true
cp montco_lm_state.json "$DEPLOY_DIR/" 2>/dev/null || true
cp delco_media_state.json "$DEPLOY_DIR/" 2>/dev/null || true

cd "$DEPLOY_DIR"
git add -A
if git diff --cached --quiet; then
    echo "No changes to commit"
else
    git commit -m "Daily scrape $(date +'%Y-%m-%d %H:%M')"
    git push
    echo "Pushed to GitHub Pages"
fi

echo
echo "=== Done ==="
