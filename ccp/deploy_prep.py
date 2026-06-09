"""
Produce the deployable, password-gated dashboard for av-tools.

Reads dashboard.html (ungated, for local use), injects the shared auth gate
(noindex + password + homepage link) via auth_gate.inject_auth, and writes
ccp_civil_dashboard.html — the file that gets pushed to the av-tools Pages site.

Keeps the local dashboard.html un-gated (no password prompt when opening from disk).
"""

import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))  # import auth_gate from claude_sandbox root
from auth_gate import inject_auth

src = (HERE / "dashboard.html").read_text(encoding="utf-8")
gated = inject_auth(src)
out = HERE / "ccp_civil_dashboard.html"
out.write_text(gated, encoding="utf-8")
print(f"Wrote {out} ({len(gated):,} bytes, password-gated)")
