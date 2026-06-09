"""
deploy_prep.py — Apply password gate to dashboard + docket replicas.

Reads dashboard.html (ungated local preview), writes ccp_dockets_dashboard.html
(gated, for deployment to av-tools). Also gates all dockets/*.html in place.

Run after generate_dashboard.py.
"""

import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
from auth_gate import inject_auth

# Gate main dashboard
src = (HERE / "dashboard.html").read_text(encoding="utf-8")
out = HERE / "ccp_dockets_dashboard.html"
out.write_text(inject_auth(src), encoding="utf-8")
print(f"Gated dashboard -> {out}")

# Gate docket replicas in place
dockets_dir = HERE / "dockets"
if dockets_dir.exists():
    count = 0
    for f in dockets_dir.glob("*.html"):
        html = f.read_text(encoding="utf-8")
        f.write_text(inject_auth(html), encoding="utf-8")
        count += 1
    print(f"Gated {count} docket replicas in {dockets_dir}")
else:
    print("No dockets/ directory found — skipping replicas")
