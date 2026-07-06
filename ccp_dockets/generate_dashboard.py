"""
generate_dashboard.py — Build dashboard.html and docket replica pages.

Reads:
  data/complaints.json      — full parsed dockets from Tab 1 scan
  data/state_watchlist.json — full dockets + notes for Tab 2
  watchlist.json            — case ordering + notes

Writes:
  dashboard.html            — 2-tab password-free local preview
  dockets/<case_id>.html    — full docket replicas (one per case)

Run deploy_prep.py after this to produce the gated ccp_dockets_dashboard.html.
"""

import html as _h
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "data"
DOCKETS_DIR = HERE / "dockets"
DOCKETS_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate")

REDACT_PII = False  # set True to strip party addresses from replica pages


# ── Helpers ────────────────────────────────────────────────────────────────────
def esc(s):
    return _h.escape(str(s) if s is not None else "")


def short_parties(plaintiffs, defendants):
    p = "; ".join(plaintiffs[:2]) or "—"
    d = "; ".join(defendants[:2]) or "—"
    ep = f" +{len(plaintiffs)-2}" if len(plaintiffs) > 2 else ""
    ed = f" +{len(defendants)-2}" if len(defendants) > 2 else ""
    return f"{p}{ep}", f"{d}{ed}"


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    log.warning("Missing %s — using default", path)
    return default


# ── Docket replica pages ───────────────────────────────────────────────────────
_DOCKET_CSS = """
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       color: #1a1a1a; background: #f5f5f3; }
.back { display: inline-block; margin: 14px 20px 6px;
        font-size: .82rem; color: #1a1a2e; text-decoration: none;
        font-weight: 600; }
.back:hover { text-decoration: underline; }
header { background: #1a1a2e; color: white; padding: 20px 24px 22px; }
header h1 { margin: 0 0 6px; font-size: 1.25rem; line-height: 1.3; }
.meta-pills { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.pill { background: rgba(255,255,255,0.12); padding: 4px 10px; border-radius: 4px;
        font-size: .78rem; white-space: nowrap; }
.pill span { opacity: .7; margin-right: 4px; }
section { background: white; margin: 14px 16px; border-radius: 8px;
          box-shadow: 0 1px 4px rgba(0,0,0,.07); overflow: hidden; }
section h2 { margin: 0; padding: 12px 16px; background: #f0f0f0;
             font-size: .85rem; text-transform: uppercase; letter-spacing: .04em;
             color: #444; border-bottom: 1px solid #ddd; }
table { width: 100%; border-collapse: collapse; font-size: .85rem; }
th { padding: 8px 12px; text-align: left; background: #fafafa; border-bottom: 1px solid #e8e8e8;
     font-size: .75rem; text-transform: uppercase; letter-spacing: .04em; color: #777;
     white-space: nowrap; }
td { padding: 9px 12px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #f8f8f8; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: .75rem;
         background: #e8f0f8; color: #1a4a8a; font-weight: 600; }
.mono { font-family: ui-monospace, Menlo, monospace; font-size: .82rem; }
.addr { color: #555; font-size: .8rem; }
.notice { margin: 14px 16px; padding: 10px 14px; background: #fff8e6;
          border: 1px solid #e6c34d; border-radius: 6px;
          font-size: .82rem; color: #5a4a10; }
footer { margin: 14px 16px 30px; font-size: .78rem; color: #888; line-height: 1.6; }
@media (max-width: 600px) {
  table, thead, tbody, tr, th, td { display: block; }
  thead { display: none; }
  td { padding: 5px 12px; }
  td::before { content: attr(data-label); display: block; font-size: .68rem;
               text-transform: uppercase; color: #999; letter-spacing: .04em; }
}
"""


def _render_docket_page(parsed, note=""):
    case_id = esc(parsed.get("case_id", ""))
    caption = esc(parsed.get("caption", ""))
    filing_date = esc(parsed.get("filing_date", ""))
    court = esc(parsed.get("court", ""))
    case_type = esc(parsed.get("case_type", ""))
    jury = esc(parsed.get("jury", ""))
    status = esc(parsed.get("status", ""))
    note_html = (
        f'<div class="notice"><strong>Note:</strong> {esc(note)}</div>'
    ) if note else ""

    # Parties
    parties = parsed.get("parties", [])
    party_rows = ""
    for p in parties:
        addr = "" if REDACT_PII else esc(p.get("address", ""))
        addr_cell = f'<br><span class="addr">{addr}</span>' if addr else ""
        party_rows += (
            f"<tr>"
            f'<td class="mono" data-label="Seq">{esc(p.get("seq",""))}</td>'
            f'<td data-label="Type"><span class="badge">{esc(p.get("type",""))}</span></td>'
            f'<td data-label="Name"><strong>{esc(p.get("name",""))}</strong>{addr_cell}</td>'
            f"</tr>"
        )

    # Events
    events = parsed.get("events", [])
    events_section = ""
    if events:
        event_rows = ""
        for e in events:
            event_rows += (
                f"<tr>"
                f'<td data-label="Event">{esc(e.get("event",""))}</td>'
                f'<td data-label="Date/Time">{esc(e.get("datetime",""))}</td>'
                f'<td data-label="Room">{esc(e.get("room",""))}</td>'
                f'<td data-label="Location">{esc(e.get("location",""))}</td>'
                f'<td data-label="Judge">{esc(e.get("judge",""))}</td>'
                f"</tr>"
            )
        events_section = f"""
<section>
  <h2>Case Event Schedule ({len(events)})</h2>
  <table>
    <thead><tr>
      <th>Event</th><th>Date / Time</th><th>Room</th><th>Location</th><th>Judge</th>
    </tr></thead>
    <tbody>{event_rows}</tbody>
  </table>
</section>"""

    # Docket entries
    entries = parsed.get("entries", [])
    entry_rows = ""
    for e in entries:
        entry_rows += (
            f"<tr>"
            f'<td class="mono" data-label="Date" style="white-space:nowrap;">{esc(e.get("date",""))}</td>'
            f'<td data-label="Time" style="white-space:nowrap;">{esc(e.get("time",""))}</td>'
            f'<td data-label="Type">{esc(e.get("type",""))}</td>'
            f'<td data-label="Party">{esc(e.get("party",""))}</td>'
            f"</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{caption} — CCP Docket</title>
<style>{_DOCKET_CSS}</style>
</head>
<body>
<a class="back" href="../ccp_dockets_dashboard.html" style="display:inline-block;margin:14px 20px 6px;font-size:.82rem;color:#1a1a2e;text-decoration:none;font-weight:600;">← Back to dashboard</a>

<header>
  <h1>{caption}</h1>
  <div class="meta-pills">
    <div class="pill"><span>Case ID</span>{case_id}</div>
    <div class="pill"><span>Filed</span>{filing_date}</div>
    <div class="pill"><span>Type</span>{case_type}</div>
    <div class="pill"><span>Court</span>{court}</div>
    <div class="pill"><span>Jury</span>{jury}</div>
    <div class="pill"><span>Status</span>{status}</div>
  </div>
</header>

{note_html}

<section>
  <h2>Parties ({len(parties)})</h2>
  <table>
    <thead><tr><th>Seq</th><th>Type</th><th>Name / Address</th></tr></thead>
    <tbody>{party_rows}</tbody>
  </table>
</section>

{events_section}

<section>
  <h2>Docket Entries ({len(entries)})</h2>
  <table>
    <thead><tr><th>Date</th><th>Time</th><th>Type</th><th>Party</th></tr></thead>
    <tbody>{entry_rows}</tbody>
  </table>
</section>

<footer>
  Source: First Judicial District of Pennsylvania (<a href="https://fjdefile.phila.gov/efsfjd/" style="color:#888;">fjdefile.phila.gov</a>).
  This is an automated extract for journalistic reference. Documents shown in the docket entries
  are available on the official FJD docket (paid download) — not here.
  <strong>Always confirm this information against the official docket before relying on or publishing it.</strong>
</footer>
</body>
</html>"""


# ── Dashboard HTML ─────────────────────────────────────────────────────────────
_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>CCP Civil Dockets — New Cases &amp; Watchlist</title>
<style>
  :root {
    --complaints: #1a1a2e; --watchlist: #2c5f2e; --partywatch: #5a2c6e;
    --ink: #1a1a1a; --muted: #5a5a5a; --line: #d8d8d8; --bg: #f7f7f5; --card: #fff;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: var(--ink); background: var(--bg); }
  header { padding: 18px 18px 12px; border-bottom: 2px solid var(--ink); background: var(--card); }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: .88rem; margin: 0; }
  .notice { background: #fff8e6; border: 1px solid #e6c34d; border-radius: 6px;
            padding: 8px 12px; margin: 10px 18px 0; font-size: .82rem; color: #5a4a10; }
  .tabs { display: flex; gap: 4px; padding: 12px 18px 0; background: var(--card); }
  .tab { padding: 9px 16px; border: 1px solid var(--line); border-bottom: none;
         border-radius: 7px 7px 0 0; cursor: pointer; background: #ededea;
         font-size: .9rem; font-weight: 600; color: var(--muted); }
  .tab[aria-selected="true"] { background: var(--card); color: var(--ink); }
  .tab.complaints[aria-selected="true"] { box-shadow: inset 0 3px 0 var(--complaints); }
  .tab.watchlist[aria-selected="true"] { box-shadow: inset 0 3px 0 var(--watchlist); }
  .tab.partywatch[aria-selected="true"] { box-shadow: inset 0 3px 0 var(--partywatch); }
  .role { display: inline-block; padding: 2px 7px; border-radius: 3px; font-size: .74rem;
          font-weight: 600; background: #efe4f5; color: #5a2c6e; }
  .controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: end;
              padding: 12px 18px; background: var(--card); border-bottom: 1px solid var(--line);
              border-top: 1px solid var(--line); position: sticky; top: 0; z-index: 5; }
  .ctl { display: flex; flex-direction: column; gap: 2px; }
  .ctl label { font-size: .7rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
  .ctl input { padding: 6px 8px; border: 1px solid var(--line); border-radius: 5px;
               font-size: .88rem; background: #fff; min-height: 34px; min-width: 220px; }
  .spacer { flex: 1; }
  button.ghost { padding: 7px 12px; border: 1px solid var(--ink); background: #fff;
                 color: var(--ink); border-radius: 5px; cursor: pointer; font-size: .83rem;
                 min-height: 34px; }
  .count { padding: 6px 18px; font-size: .83rem; color: var(--muted); }
  .count b { color: var(--ink); }
  .type-hdr td { background: #e8edf2; padding: 10px 10px 7px; font-size: .71rem;
                 font-weight: 700; text-transform: uppercase; letter-spacing: .06em;
                 color: #1a1a2e; border-top: 2px solid #b8c8d8;
                 border-bottom: 1px solid #d0dce8; cursor: default; }
  .type-hdr td .tc { font-weight: 400; color: #777; }
  .type-hdr:hover td { background: #e8edf2 !important; }
  .wrap { padding: 0 18px 60px; }
  table { width: 100%; border-collapse: collapse; background: var(--card); font-size: .85rem; }
  thead th { background: #efefec; text-align: left; padding: 8px 10px;
             border-bottom: 2px solid var(--line); cursor: pointer; white-space: nowrap;
             font-size: .72rem; text-transform: uppercase; letter-spacing: .03em; }
  thead th[aria-sort="ascending"]::after  { content: " \\2191"; }
  thead th[aria-sort="descending"]::after { content: " \\2193"; }
  tbody td { padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
  tbody tr { cursor: pointer; }
  tbody tr:hover td { background: #f0f6fb; }
  .caption { font-weight: 600; }
  .cid { font-family: ui-monospace, Menlo, monospace; font-size: .8rem; color: var(--muted); }
  .badge { display: inline-block; padding: 2px 7px; border-radius: 3px; font-size: .74rem;
           background: #e8f0f8; color: #1a4a8a; font-weight: 600; }
  .note { color: #2c5f2e; font-style: italic; font-size: .82rem; }
  .last-entry { font-size: .8rem; color: var(--muted); }
  footer { padding: 16px 18px; font-size: .78rem; color: var(--muted);
           border-top: 1px solid var(--line); background: var(--card); }
  @media (max-width: 700px) {
    thead { display: none; }
    table, tbody, tr, td { display: block; width: 100%; }
    tbody tr { border: 1px solid var(--line); border-radius: 6px; margin: 10px 0; background: #fff; }
    tbody td { border: none; padding: 4px 10px; }
    tbody td::before { content: attr(data-label); display: block; font-size: .66rem;
                       text-transform: uppercase; color: var(--muted); letter-spacing: .03em; }
  }
</style>
</head>
<body>
<header>
  <h1>Philadelphia Common Pleas — Civil Dockets</h1>
  <p class="sub">New cases (rolling 30-day window) and watchlist. Generated __GENERATED__.</p>
</header>

<div class="notice" role="note">
  <strong>Not an official record.</strong> Always confirm against the
  <a href="https://fjdefile.phila.gov/efsfjd/" style="color:#5a4a10;">official FJD docket</a>
  before relying on or publishing any information from this dashboard.
</div>

<div class="tabs" role="tablist">
  <button class="tab complaints" role="tab" data-tab="complaints" aria-selected="true">
    New Cases (<span id="cnt-complaints">0</span>)
  </button>
  <button class="tab watchlist" role="tab" data-tab="watchlist" aria-selected="false">
    Watchlist (<span id="cnt-watchlist">0</span>)
  </button>
  <button class="tab partywatch" role="tab" data-tab="partywatch" aria-selected="false">
    Party Watch (<span id="cnt-partywatch">0</span>)
  </button>
</div>

<div id="panel">
  <div class="controls" id="controls"></div>
  <div id="exclusion-note" style="display:none;margin:0 18px;padding:8px 12px;
       background:#f0f4f8;border:1px solid #c8d8e8;border-radius:5px;
       font-size:.78rem;color:#3a4a5a;line-height:1.6;">
    <strong>Excluded types:</strong>
    anything containing "lien," "parking," "penndot," or "certified" &middot;
    starts with "MC&nbsp;-" &middot; ends with "-MR" &middot;
    exact: self assessed taxes, credit card collection, auction motor vehicle,
    ejectment, quiet title
  </div>
  <div id="partywatch-note" style="display:none;margin:0 18px;padding:8px 12px;
       background:#f4eef8;border:1px solid #d8c8e4;border-radius:5px;
       font-size:.78rem;color:#4a3a5a;line-height:1.6;">
    New civil cases where a <strong>watched entity</strong> appears as a party
    (plaintiff <em>or</em> defendant), found via the FJD participant-name index over a
    rolling window. Edit <code>name_watch.json</code> to add names.
  </div>
  <p class="count" id="count" aria-live="polite"></p>
  <div class="wrap">
    <table id="tbl" aria-label="Cases">
      <thead><tr id="thead-row"></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>
</div>

<footer>
  Source: First Judicial District of Pennsylvania public e-filing system (fjdefile.phila.gov).
  Data captured via automated scrape; schedules and filings change without notice.
  Always confirm against the official docket. Not an official court record.
</footer>

<script id="payload" type="application/json">__DATA__</script>
<script>
(function () {
  var P = JSON.parse(document.getElementById('payload').textContent);
  var TABS = {
    complaints: {
      data: P.complaints,
      label: 'new case',
      cols: [
        {k:'case_id',    label:'Case ID',    cell:function(r){ return '<span class="cid">'+r.case_id+'</span>'; }},
        {k:'caption',    label:'Caption',    cell:function(r){ return '<span class="caption">'+r.caption+'</span>'; }},
        {k:'filing_date',label:'Filed',      cell:function(r){ return r.filing_date||'—'; }},
        {k:'plaintiffs', label:'Plaintiffs', cell:function(r){ return esc(r.plaintiffs_str); }},
        {k:'defendants', label:'Defendants', cell:function(r){ return esc(r.defendants_str); }},
        {k:'last_entry', label:'Last entry', cell:function(r){
          return '<span class="last-entry">'+(r.last_entry_date?r.last_entry_date+' — ':'')+r.last_entry_type+'</span>';
        }}
      ]
    },
    watchlist: {
      data: P.watchlist,
      label: 'case',
      cols: [
        {k:'case_id',    label:'Case ID',      cell:function(r){ return '<span class="cid">'+r.case_id+'</span>'; }},
        {k:'note',       label:'Note',         cell:function(r){ return r.note?'<span class="note">'+esc(r.note)+'</span>':''; }},
        {k:'case_type',  label:'Type',         cell:function(r){ return '<span class="badge">'+r.case_type+'</span>'; }},
        {k:'caption',    label:'Caption',      cell:function(r){ return '<span class="caption">'+r.caption+'</span>'; }},
        {k:'plaintiffs', label:'Plaintiffs',   cell:function(r){ return esc(r.plaintiffs_str); }},
        {k:'defendants', label:'Defendants',   cell:function(r){ return esc(r.defendants_str); }},
        {k:'last_entry', label:'Last filing',  cell:function(r){
          return '<span class="last-entry">'+(r.last_entry_date?r.last_entry_date+' — ':'')+r.last_entry_type+'</span>';
        }}
      ]
    },
    partywatch: {
      data: [],   // flattened below; rendered grouped by watched label
      label: 'match',
      cols: [
        {k:'matched_name',label:'Matched name',cell:function(r){ return '<strong>'+esc(r.matched_name)+'</strong>'; }},
        {k:'case_id',     label:'Case ID',     cell:function(r){ return '<span class="cid">'+esc(r.case_id)+'</span>'; }},
        {k:'caption',     label:'Caption',     cell:function(r){ return '<span class="caption">'+esc(r.caption)+'</span>'; }},
        {k:'roles',       label:'Role',        cell:function(r){ return '<span class="role">'+esc(r.roles)+'</span>'; }},
        {k:'filing_date', label:'Filed',       cell:function(r){ return esc(r.filing_date)||'—'; }}
      ]
    }
  };

  // Party Watch payload: [{label, rows:[...]}]. Flatten for search/count, keep
  // groups for grouped rendering.
  var PW_GROUPS = (P.partywatch && P.partywatch.labels) ? P.partywatch.labels : [];
  PW_GROUPS.forEach(function(g){ (g.rows||[]).forEach(function(r){ r._label = g.label; TABS.partywatch.data.push(r); }); });

  var activeTab = 'complaints';
  var sortState = {complaints:{k:'filing_date',d:-1}, watchlist:{k:'case_id',d:-1}, partywatch:{k:'filing_date',d:-1}};
  var searchQ = {complaints:'', watchlist:'', partywatch:''};

  function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,function(m){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]; }); }

  function filtered(tab){
    var q = searchQ[tab].toLowerCase().trim();
    var rows = TABS[tab].data.slice();
    if (q) rows = rows.filter(function(r){
      return (r.caption+' '+r.case_id+' '+(r.note||'')+' '+(r.case_type||'')+' '+
              (r.matched_name||'')+' '+(r.roles||'')+' '+(r._label||'')).toLowerCase().indexOf(q) !== -1;
    });
    var ss = sortState[tab];
    rows.sort(function(a,b){
      var x=(a[ss.k]||'').toString(), y=(b[ss.k]||'').toString();
      return x<y ? -ss.d : x>y ? ss.d : 0;
    });
    return rows;
  }

  function buildHead(tab){
    var ss = sortState[tab];
    var tr = document.getElementById('thead-row'); tr.innerHTML = '';
    TABS[tab].cols.forEach(function(col){
      var th = document.createElement('th'); th.textContent = col.label;
      if (tab === 'watchlist') {
        th.setAttribute('data-k', col.k);
        if (col.k === ss.k) th.setAttribute('aria-sort', ss.d===1?'ascending':'descending');
        th.addEventListener('click', function(){
          if (ss.k===col.k) ss.d=-ss.d; else {ss.k=col.k; ss.d=1;}
          render();
        });
      }
      tr.appendChild(th);
    });
  }

  // type ordering: sorted by count in the FULL dataset (stable as user types)
  var _typeOrder = (function(){
    var counts = {};
    P.complaints.forEach(function(r){ counts[r.case_type]=(counts[r.case_type]||0)+1; });
    return Object.keys(counts).sort(function(a,b){ return counts[b]-counts[a]||a<b?-1:a>b?1:0; });
  })();

  function renderRow(r, tab){
    var tr = document.createElement('tr');
    tr.innerHTML = TABS[tab].cols.map(function(col){
      return '<td data-label="'+col.label+'">'+col.cell(r)+'</td>';
    }).join('');
    // Party Watch rows come from the party-name index (no docket replica), so
    // they are informational only; complaints/watchlist rows open their replica.
    if (tab !== 'partywatch') {
      tr.addEventListener('click', function(){
        window.open('civil_dockets/'+r.case_id+'.html', '_blank');
      });
    } else {
      tr.style.cursor = 'default';
    }
    return tr;
  }

  function render(){
    var tab = activeTab;
    buildHead(tab);
    var rows = filtered(tab);
    var tb = document.getElementById('tbody'); tb.innerHTML = '';

    if (tab === 'complaints') {
      // group by case_type in stable type order
      var grouped = {};
      rows.forEach(function(r){
        if (!grouped[r.case_type]) grouped[r.case_type] = [];
        grouped[r.case_type].push(r);
      });
      var ncols = TABS[tab].cols.length;
      _typeOrder.forEach(function(ct){
        var group = grouped[ct];
        if (!group || !group.length) return;
        var htr = document.createElement('tr');
        htr.className = 'type-hdr';
        htr.innerHTML = '<td colspan="'+ncols+'">'+esc(ct)+
          ' <span class="tc">('+group.length+')</span></td>';
        tb.appendChild(htr);
        group.forEach(function(r){ tb.appendChild(renderRow(r, tab)); });
      });
      var total = P.complaints.length;
      document.getElementById('count').innerHTML =
        'Showing <b>'+rows.length+'</b>'+(rows.length!==total?' of '+total:'')+
        ' new case'+(rows.length!==1?'s':'')+(rows.length!==total?'':'.') +
        (rows.length!==total ? ' matching.' : '');
    } else if (tab === 'partywatch') {
      // group by watched label, in config order
      var ncols = TABS[tab].cols.length;
      var byLabel = {};
      rows.forEach(function(r){ (byLabel[r._label]=byLabel[r._label]||[]).push(r); });
      PW_GROUPS.forEach(function(g){
        var group = byLabel[g.label];
        if (!group || !group.length) return;
        var htr = document.createElement('tr');
        htr.className = 'type-hdr';
        htr.innerHTML = '<td colspan="'+ncols+'">'+esc(g.label)+
          ' <span class="tc">('+group.length+')</span></td>';
        tb.appendChild(htr);
        group.forEach(function(r){ tb.appendChild(renderRow(r, tab)); });
      });
      var total = TABS[tab].data.length;
      document.getElementById('count').innerHTML =
        'Showing <b>'+rows.length+'</b>'+(rows.length!==total?' of '+total:'')+
        ' match'+(rows.length!==1?'es':'')+(rows.length!==total?' matching.':'.');
    } else {
      rows.forEach(function(r){ tb.appendChild(renderRow(r, tab)); });
      var total = TABS[tab].data.length;
      var lbl = TABS[tab].label;
      document.getElementById('count').innerHTML =
        'Showing <b>'+rows.length+'</b>'+(rows.length!==total?' of '+total:'')+' '+lbl+(total!==1?'s':'.')+'.';
    }
  }

  function buildControls(tab){
    var c = document.getElementById('controls'); c.innerHTML = '';
    var wrap = document.createElement('div'); wrap.className = 'ctl';
    var lab = document.createElement('label'); lab.htmlFor = 'search'; lab.textContent = 'Search';
    var inp = document.createElement('input'); inp.id = 'search'; inp.type = 'search';
    inp.placeholder = 'Caption, case ID, type…'; inp.value = searchQ[tab];
    inp.addEventListener('input', function(){ searchQ[tab]=inp.value; render(); });
    wrap.appendChild(lab); wrap.appendChild(inp); c.appendChild(wrap);
    var sp = document.createElement('div'); sp.className='spacer'; c.appendChild(sp);
    var reset = document.createElement('button'); reset.className='ghost'; reset.textContent='Reset';
    reset.addEventListener('click', function(){ searchQ[tab]=''; sortState[tab]={k:'filing_date',d:-1}; buildControls(tab); render(); });
    c.appendChild(reset);
  }

  function switchTab(tab){
    activeTab = tab;
    document.querySelectorAll('.tab').forEach(function(b){
      b.setAttribute('aria-selected', b.getAttribute('data-tab')===tab?'true':'false');
    });
    var note = document.getElementById('exclusion-note');
    if (note) note.style.display = tab === 'complaints' ? 'block' : 'none';
    var pwnote = document.getElementById('partywatch-note');
    if (pwnote) pwnote.style.display = tab === 'partywatch' ? 'block' : 'none';
    buildControls(tab); render();
  }

  // set tab counts in buttons
  document.getElementById('cnt-complaints').textContent = P.complaints.length;
  document.getElementById('cnt-watchlist').textContent  = P.watchlist.length;
  document.getElementById('cnt-partywatch').textContent = TABS.partywatch.data.length;

  document.querySelectorAll('.tab').forEach(function(b){
    b.addEventListener('click', function(){ switchTab(b.getAttribute('data-tab')); });
  });

  switchTab('complaints');
})();
</script>
</body>
</html>"""


# ── Build summary row ──────────────────────────────────────────────────────────
def _summary_row(parsed, note=""):
    ps, ds = short_parties(parsed.get("plaintiffs", []), parsed.get("defendants", []))
    last = parsed.get("last_entry") or {}
    return {
        "case_id": parsed.get("case_id", ""),
        "note": note,
        "caption": parsed.get("caption", ""),
        "filing_date": parsed.get("filing_date", ""),
        "case_type": parsed.get("case_type", ""),
        "status": parsed.get("status", ""),
        "entry_count": parsed.get("entry_count", 0),
        "plaintiffs_str": ps,
        "defendants_str": ds,
        "last_entry_type": last.get("type", ""),
        "last_entry_date": last.get("date", ""),
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    complaints = load_json(DATA / "complaints.json", [])
    state_watchlist = load_json(DATA / "state_watchlist.json", {})
    watchlist_order = load_json(HERE / "watchlist.json", [])
    partywatch = load_json(DATA / "name_watch_view.json", {"labels": []})

    # Build watchlist rows in watchlist.json order
    watchlist_rows = []
    for item in watchlist_order:
        cid = item["case_id"]
        note = item.get("note", "")
        if cid in state_watchlist:
            parsed = state_watchlist[cid].get("docket", {})
            if parsed:
                watchlist_rows.append(_summary_row(parsed, note))

    # Build complaint summary rows
    complaint_rows = [_summary_row(c) for c in complaints]

    ET = timezone(timedelta(hours=-4))  # EDT (UTC-4)
    generated = datetime.now(ET).strftime("%Y-%m-%d %H:%M EDT")

    payload = {
        "complaints": complaint_rows,
        "watchlist": watchlist_rows,
        "partywatch": partywatch,
        "meta": {
            "generated_at": generated,
            "complaint_count": len(complaint_rows),
            "watchlist_count": len(watchlist_rows),
            "partywatch_count": sum(len(g.get("rows", []))
                                    for g in partywatch.get("labels", [])),
        },
    }

    # Write dashboard.html
    html = (
        _DASHBOARD_HTML
        .replace("__DATA__", json.dumps(payload, separators=(",", ":")))
        .replace("__GENERATED__", generated)
    )
    out = HERE / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    log.info("Wrote %s (%d complaints, %d watchlist)", out, len(complaint_rows), len(watchlist_rows))

    # Write docket replica pages for all cases
    all_cases = []
    for c in complaints:
        all_cases.append((c, ""))
    for item in watchlist_order:
        cid = item["case_id"]
        note = item.get("note", "")
        if cid in state_watchlist:
            d = state_watchlist[cid].get("docket", {})
            if d:
                all_cases.append((d, note))

    written = 0
    for parsed, note in all_cases:
        cid = parsed.get("case_id", "").strip()
        if not cid:
            continue
        page = _render_docket_page(parsed, note)
        (DOCKETS_DIR / f"{cid}.html").write_text(page, encoding="utf-8")
        written += 1

    log.info("Wrote %d docket replica pages to %s", written, DOCKETS_DIR)


if __name__ == "__main__":
    main()
