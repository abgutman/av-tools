"""
Generate a self-contained dashboard.html from data/dashboard_data.json.

Two tabs (separate datasets, filters and search so they don't collide):
  - Motion hearings (CM)
  - Trial dates certain (MJ)

Data embedded inline (works from disk, no server/CORS), vanilla JS only.
Re-run after build_dashboard_data.py to refresh.

Source line and "verify against the docket" reminder are required on every build.
"""

import json
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "data" / "dashboard_data.json"
OUT = HERE / "dashboard.html"

payload = json.loads(DATA.read_text(encoding="utf-8"))
meta = payload["meta"]
data_json = json.dumps(payload, separators=(",", ":"))
scraped_date = meta["scraped_at"][:10]


def pretty(iso):
    from datetime import date
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{months[m-1]} {d}, {y}"


range_text = f'{pretty(meta["date_range"]["start"])} – {pretty(meta["date_range"]["end"])}'

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>CCP Civil — Motion Hearings &amp; Trial Dates Certain</title>
<style>
  :root {
    --motion: #1b6ca8; --trial: #d1701a;
    --ink: #1a1a1a; --muted: #5a5a5a; --line: #d8d8d8; --bg: #f7f7f5; --card: #fff;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
         color: var(--ink); background: var(--bg); }
  header { padding: 20px 18px 12px; border-bottom: 2px solid var(--ink); background: var(--card); }
  h1 { font-size: 1.5rem; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: .9rem; margin: 0; }
  .notice { background: #fff8e6; border: 1px solid #e6c34d; border-radius: 6px;
            padding: 8px 12px; margin: 10px 18px 0; font-size: .85rem; color: #5a4a10; }
  .tabs { display: flex; gap: 4px; padding: 12px 18px 0; background: var(--card); }
  .tab { padding: 9px 16px; border: 1px solid var(--line); border-bottom: none;
         border-radius: 7px 7px 0 0; cursor: pointer; background: #ededea; font-size: .92rem;
         font-weight: 600; color: var(--muted); }
  .tab[aria-selected="true"] { background: var(--card); color: var(--ink); }
  .tab.CM[aria-selected="true"] { box-shadow: inset 0 3px 0 var(--motion); }
  .tab.MJ[aria-selected="true"] { box-shadow: inset 0 3px 0 var(--trial); }
  .controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: end;
              padding: 14px 18px; background: var(--card); border-bottom: 1px solid var(--line);
              border-top: 1px solid var(--line); position: sticky; top: 0; z-index: 5; }
  .ctl { display: flex; flex-direction: column; gap: 2px; }
  .ctl label { font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
  .ctl select, .ctl input { padding: 6px 8px; border: 1px solid var(--line); border-radius: 5px;
                            font-size: .9rem; background: #fff; min-height: 34px; max-width: 230px; }
  .ctl input[type=search] { min-width: 200px; }
  .spacer { flex: 1; }
  button { padding: 7px 12px; border: 1px solid var(--ink); background: var(--ink); color: #fff;
           border-radius: 5px; cursor: pointer; font-size: .85rem; min-height: 34px; }
  button.ghost { background: #fff; color: var(--ink); }
  .count { padding: 8px 18px; font-size: .85rem; color: var(--muted); }
  .count b { color: var(--ink); }
  .wrap { padding: 0 18px 60px; }
  table { width: 100%; border-collapse: collapse; background: var(--card); font-size: .88rem; }
  thead th { position: sticky; top: 64px; background: #efefec; text-align: left; padding: 8px 10px;
             border-bottom: 2px solid var(--line); cursor: pointer; white-space: nowrap; font-size: .76rem;
             text-transform: uppercase; letter-spacing: .03em; }
  thead th[aria-sort="ascending"]::after { content: " \\2191"; }
  thead th[aria-sort="descending"]::after { content: " \\2193"; }
  tbody td { padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
  tbody tr:hover { background: #f0f6fb; }
  .caption { font-weight: 600; }
  .cid { font-family: ui-monospace, Menlo, monospace; font-size: .82rem; color: var(--muted); }
  .atty { color: var(--muted); font-size: .8rem; }
  .nowrap { white-space: nowrap; }
  .hidden { display: none; }
  footer { padding: 18px; font-size: .8rem; color: var(--muted); border-top: 1px solid var(--line);
           background: var(--card); }
  @media (max-width: 760px) {
    thead { display: none; }
    table, tbody, tr, td { display: block; width: 100%; }
    tbody tr { border: 1px solid var(--line); border-radius: 6px; margin: 10px 0; background: #fff; }
    tbody td { border: none; padding: 4px 10px; }
    tbody td::before { content: attr(data-label); display: block; font-size: .68rem;
                       text-transform: uppercase; color: var(--muted); letter-spacing: .03em; }
    thead th { top: 0; }
  }
</style>
</head>
<body>
<header>
  <h1>Philadelphia Common Pleas — Civil Motion Hearings &amp; Trial Dates Certain</h1>
  <p class="sub">__COUNT_CM__ motion hearings and __COUNT_MJ__ trial dates certain scheduled
     __RANGE__. Source: FJD public e-filing hearing lists, captured __SCRAPED__.</p>
</header>

<div class="notice" role="note">
  <strong>This is a calendar listing, not an official record.</strong> Court schedules change
  constantly. Verify every hearing against the official docket before relying on or publishing it.
</div>

<div class="tabs" role="tablist">
  <button class="tab CM" role="tab" data-tab="CM" aria-selected="true">Motion hearings</button>
  <button class="tab MJ" role="tab" data-tab="MJ" aria-selected="false">Trial dates certain</button>
</div>

<div id="panel" role="tabpanel">
  <div class="controls" id="controls"></div>
  <p class="count" id="count" aria-live="polite"></p>
  <div class="wrap">
    <table id="table" aria-label="Scheduled hearings">
      <thead><tr id="head"></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</div>

<footer>
  Source: First Judicial District of Pennsylvania public e-filing hearing lists
  (fjdefile.phila.gov). Calendar snapshot captured __SCRAPED__; schedules change without notice.
  Always confirm against the official docket. Not an official court record.
</footer>

<script id="payload" type="application/json">__DATA__</script>
<script>
(function () {
  var payload = JSON.parse(document.getElementById('payload').textContent);
  var all = payload.hearings;
  var MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var WD = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];

  function fmtDate(iso) {
    if (!iso) return '';
    var p = iso.split('-');
    return WD[new Date(+p[0],+p[1]-1,+p[2]).getDay()] + ' ' + MONTHS[+p[1]-1] + ' ' + (+p[2]) + ', ' + p[0];
  }
  function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g, function(m){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]; }); }
  function uniq(a){ return Array.from(new Set(a.filter(Boolean))).sort(); }

  // Column descriptors per tab. render() returns inner HTML for the cell.
  var loc = function (r){ return esc((r.room||'') + (r.location ? ' \\u2014 ' + r.location : '')); };
  var COLS = {
    CM: [
      {k:'iso_date', label:'Date',       cell:function(r){return '<span class="nowrap">'+fmtDate(r.iso_date)+'</span>';}},
      {k:'time',     label:'Time',       cell:function(r){return '<span class="nowrap">'+esc(r.time)+'</span>';}},
      {k:'event_type', label:'Event type', cell:function(r){return esc(r.event_type);}},
      {k:'case_id',  label:'Case',       cell:function(r){return '<span class="cid">'+esc(r.case_id)+'</span>';}},
      {k:'caption',  label:'Caption',    cell:function(r){return '<span class="caption">'+esc(r.caption)+'</span>';}},
      {k:'court_type', label:'Court type', cell:function(r){return esc(r.court_type);}},
      {k:'case_type', label:'Case type', cell:function(r){return esc(r.case_type);}},
      {k:'judge',    label:'Judge',      cell:function(r){return esc(r.judge);}},
      {k:'room',     label:'Location',   cell:loc},
      {k:'attorneys', label:'Attorneys', cell:function(r){return '<span class="atty">'+esc(r.attorneys)+'</span>';}}
    ],
    MJ: [
      {k:'iso_date', label:'Date',       cell:function(r){return '<span class="nowrap">'+fmtDate(r.iso_date)+'</span>';}},
      {k:'time',     label:'Time',       cell:function(r){return '<span class="nowrap">'+esc(r.time)+'</span>';}},
      {k:'case_id',  label:'Case',       cell:function(r){return '<span class="cid">'+esc(r.case_id)+'</span>';}},
      {k:'caption',  label:'Caption',    cell:function(r){return '<span class="caption">'+esc(r.caption)+'</span>';}},
      {k:'court_type', label:'Court type', cell:function(r){return esc(r.court_type);}},
      {k:'case_type', label:'Case type', cell:function(r){return esc(r.case_type);}},
      {k:'trial_days', label:'Trial days', cell:function(r){return esc(r.trial_days);}},
      {k:'room',     label:'Location',   cell:loc},
      {k:'attorneys', label:'Attorneys', cell:function(r){return '<span class="atty">'+esc(r.attorneys)+'</span>';}}
    ]
  };

  // Filter descriptors per tab (dropdowns built from data).
  var FILTERS = {
    CM: [{f:'event_type',label:'Event type'},{f:'judge',label:'Judge'},
         {f:'court_type',label:'Court type'},{f:'case_type',label:'Case type'}],
    MJ: [{f:'court_type',label:'Court type'},{f:'case_type',label:'Case type'}]
  };

  var state = {
    CM: {sortKey:'iso_date', sortDir:1, vals:{}},
    MJ: {sortKey:'iso_date', sortDir:1, vals:{}}
  };
  var activeTab = 'CM';

  function rowsFor(tab) { return all.filter(function(r){ return r.program === tab; }); }

  function buildControls(tab) {
    var st = state[tab];
    var c = document.getElementById('controls');
    c.innerHTML = '';
    var base = rowsFor(tab);
    FILTERS[tab].forEach(function (fd) {
      var wrap = document.createElement('div'); wrap.className = 'ctl';
      var lab = document.createElement('label'); lab.textContent = fd.label; lab.htmlFor = 'f_'+fd.f;
      var sel = document.createElement('select'); sel.id = 'f_'+fd.f;
      sel.innerHTML = '<option value="">All</option>';
      uniq(base.map(function(r){return r[fd.f];})).forEach(function (v) {
        var o = document.createElement('option'); o.value = v; o.textContent = v;
        if (st.vals[fd.f] === v) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener('input', function(){ st.vals[fd.f] = sel.value; render(); });
      wrap.appendChild(lab); wrap.appendChild(sel); c.appendChild(wrap);
    });
    // date from / to + search
    [['from','From','date'],['to','To','date'],['q','Search caption / case ID','search']].forEach(function (d) {
      var wrap = document.createElement('div'); wrap.className = 'ctl';
      var lab = document.createElement('label'); lab.textContent = d[1]; lab.htmlFor = 'f_'+d[0];
      var inp = document.createElement('input'); inp.id = 'f_'+d[0]; inp.type = d[2];
      if (d[2] === 'date') { inp.min = '__MIN__'; inp.max = '__MAX__'; }
      if (d[2] === 'search') inp.placeholder = 'e.g. foreclosure, 2406T0193';
      if (st.vals[d[0]]) inp.value = st.vals[d[0]];
      inp.addEventListener('input', function(){ st.vals[d[0]] = inp.value; render(); });
      wrap.appendChild(lab); wrap.appendChild(inp); c.appendChild(wrap);
    });
    var sp = document.createElement('div'); sp.className = 'spacer'; c.appendChild(sp);
    var reset = document.createElement('button'); reset.className = 'ghost'; reset.textContent = 'Reset';
    reset.addEventListener('click', function(){ st.vals = {}; st.sortKey='iso_date'; st.sortDir=1; buildControls(tab); render(); });
    var csv = document.createElement('button'); csv.textContent = 'Download CSV';
    csv.addEventListener('click', function(){ downloadCsv(tab); });
    c.appendChild(reset); c.appendChild(csv);
  }

  function filtered(tab) {
    var st = state[tab], v = st.vals;
    var rows = rowsFor(tab).filter(function (r) {
      var ok = true;
      FILTERS[tab].forEach(function (fd) { if (v[fd.f] && r[fd.f] !== v[fd.f]) ok = false; });
      if (v.from && r.iso_date < v.from) ok = false;
      if (v.to && r.iso_date > v.to) ok = false;
      if (v.q) { var q = v.q.toLowerCase().trim();
        if ((r.caption+' '+r.case_id).toLowerCase().indexOf(q) === -1) ok = false; }
      return ok;
    });
    rows.sort(function (a, b) {
      var x=(a[st.sortKey]||'').toString(), y=(b[st.sortKey]||'').toString();
      return x<y ? -st.sortDir : x>y ? st.sortDir : 0;
    });
    return rows;
  }

  function buildHead(tab) {
    var st = state[tab];
    var head = document.getElementById('head'); head.innerHTML = '';
    COLS[tab].forEach(function (col) {
      var th = document.createElement('th'); th.textContent = col.label; th.setAttribute('data-key', col.k);
      if (col.k === st.sortKey) th.setAttribute('aria-sort', st.sortDir===1?'ascending':'descending');
      th.addEventListener('click', function () {
        if (st.sortKey === col.k) st.sortDir = -st.sortDir; else { st.sortKey = col.k; st.sortDir = 1; }
        render();
      });
      head.appendChild(th);
    });
  }

  function render() {
    var tab = activeTab;
    buildHead(tab);
    var rows = filtered(tab);
    var tb = document.getElementById('rows'); tb.innerHTML = '';
    rows.forEach(function (r) {
      var tr = document.createElement('tr');
      tr.innerHTML = COLS[tab].map(function (col) {
        return '<td data-label="'+col.label+'">'+col.cell(r)+'</td>';
      }).join('');
      tb.appendChild(tr);
    });
    document.getElementById('count').innerHTML =
      'Showing <b>'+rows.length+'</b> of '+rowsFor(tab).length+' '+
      (tab==='CM'?'motion hearings':'trial dates certain')+'.';
  }

  function downloadCsv(tab) {
    var rows = filtered(tab);
    var cols = COLS[tab].map(function(c){return c.k;});
    var lines = [COLS[tab].map(function(c){return c.label;}).join(',')];
    rows.forEach(function (r) {
      lines.push(cols.map(function (k) {
        var val = (r[k]==null?'':String(r[k])).replace(/"/g,'""');
        return /[",\\n]/.test(val) ? '"'+val+'"' : val;
      }).join(','));
    });
    var blob = new Blob([lines.join('\\n')], {type:'text/csv'});
    var a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = (tab==='CM'?'ccp_motion_hearings':'ccp_trial_dates')+'.csv'; a.click();
  }

  function switchTab(tab) {
    activeTab = tab;
    Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (b) {
      b.setAttribute('aria-selected', b.getAttribute('data-tab')===tab ? 'true':'false');
    });
    buildControls(tab); render();
  }
  Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (b) {
    b.addEventListener('click', function(){ switchTab(b.getAttribute('data-tab')); });
  });

  switchTab('CM');
})();
</script>
</body>
</html>
"""

html = (HTML
        .replace("__DATA__", data_json)
        .replace("__COUNT_CM__", str(meta["counts"]["CM"]))
        .replace("__COUNT_MJ__", str(meta["counts"]["MJ"]))
        .replace("__RANGE__", range_text)
        .replace("__SCRAPED__", scraped_date)
        .replace("__MIN__", meta["date_range"]["start"])
        .replace("__MAX__", meta["date_range"]["end"]))

OUT.write_text(html, encoding="utf-8")
print(f"Wrote {OUT} ({len(html):,} bytes)")
