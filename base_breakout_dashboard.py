"""
KIBO Base Breakout Dashboard
============================
Live view of base_breakout_scanner.py -- breakouts (with a chart of the run, the base and the
volume spike), setups that are close, sector strength, and your Telegram alert history.

    python base_breakout_dashboard.py            ->  http://localhost:5055
    (phone on the same WiFi: http://<this-pc-ip>:5055)

It only reads base_scanner_results.json, so it works whether or not the scanner is running in
the same window. The "Scan now" button asks a running scanner to scan immediately.

Env overrides:  BASE_DASH_PORT (default 5055)   BASE_DASH_HOST (default 0.0.0.0)
"""

import os
import sys
import json
import socket
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
RESULTS_FILE = HERE / "base_scanner_results.json"
TRIGGER_FILE = HERE / "base_scanner_trigger"
PORT = int(os.getenv("BASE_DASH_PORT", "5055"))
HOST = os.getenv("BASE_DASH_HOST", "0.0.0.0")

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KIBO | Base breakout scanner</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0f;--s:#111118;--s2:#16161f;--b:#1e1e2e;--gold:#d4af37;--gold2:#f0c84a;--green:#00e676;--red:#ff4444;--blue:#4fc3f7;--amber:#ffb74d;--muted:#4a4a6a;--sub:#8888aa;--text:#e8e8f0;
--ui:'Space Grotesk',system-ui,-apple-system,'Segoe UI',sans-serif;--mono:'Space Mono',ui-monospace,Consolas,monospace;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--ui);font-size:14px;line-height:1.45;min-height:100vh}
button{font-family:inherit}
:focus-visible{outline:2px solid var(--gold2);outline-offset:2px}
.mono{font-family:var(--mono)}
.pos{color:var(--green)}.neg{color:var(--red)}

/* header */
.hdr{background:var(--s);border-bottom:1px solid var(--b);padding:12px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px;position:sticky;top:0;z-index:20;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px}
.logo-k{width:34px;height:34px;background:var(--gold);border-radius:50%;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-weight:700;font-size:14px;color:#0a0a0f;flex:none}
.brand h1{font-size:16px;font-weight:700}
.brand p{font-size:12px;color:var(--sub)}
.status{display:flex;align-items:center;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--sub)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block;margin-right:6px}
.dot.open{background:var(--green)}.dot.scanning{background:var(--amber);animation:pulse 1.2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.btn{background:transparent;border:1px solid var(--gold);color:var(--gold);padding:7px 14px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;transition:background .15s}
.btn:hover{background:rgba(212,175,55,.12)}.btn:disabled{opacity:.5;cursor:default}

.main{max-width:1400px;margin:0 auto;padding:22px 24px 60px;display:flex;flex-direction:column;gap:26px}
.banner{border:1px solid;border-radius:10px;padding:10px 14px;font-size:13px}
.banner.warn{border-color:rgba(255,183,77,.4);background:rgba(255,183,77,.07);color:var(--amber)}
.banner.err{border-color:rgba(255,68,68,.4);background:rgba(255,68,68,.07);color:var(--red)}
code{font-family:var(--mono);font-size:12px;background:var(--s2);padding:2px 6px;border-radius:5px;color:var(--text)}

h2{font-size:15px;font-weight:700;display:flex;align-items:baseline;gap:10px;margin-bottom:12px}
h2 small{font-size:12px;font-weight:400;color:var(--sub)}

/* breakout cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:14px}
.card{background:var(--s);border:1px solid var(--b);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:12px}
.card.hot{border-color:rgba(212,175,55,.45);box-shadow:0 0 0 1px rgba(212,175,55,.08) inset}
.card-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.sym{font-size:22px;font-weight:700;letter-spacing:.01em;line-height:1.1}
.nm{font-size:12px;color:var(--sub);max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.px{text-align:right}.px b{font-family:var(--mono);font-size:18px;display:block}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:11px;padding:2px 9px;border-radius:20px;border:1px solid var(--b);color:var(--sub);white-space:nowrap}
.chip.gold{color:var(--gold);border-color:rgba(212,175,55,.4);background:rgba(212,175,55,.08)}
.chip.amber{color:var(--amber);border-color:rgba(255,183,77,.35)}
.chip.green{color:var(--green);border-color:rgba(0,230,118,.3)}
.chip.red{color:var(--red);border-color:rgba(255,68,68,.35)}
svg.chart{width:100%;height:auto;display:block}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px 8px}
.m b{font-family:var(--mono);font-size:14px;display:block;font-weight:700}
.m span{font-size:11px;color:var(--sub)}
.empty{background:var(--s);border:1px dashed var(--b);border-radius:14px;padding:26px;color:var(--sub);text-align:center}
.empty b{color:var(--text)}

/* two-up panels */
.two{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:18px}
.two>*{min-width:0}
.panel{background:var(--s);border:1px solid var(--b);border-radius:14px;padding:16px}
.sec-row{display:grid;grid-template-columns:22px 1fr 64px;align-items:center;gap:10px;padding:5px 0;font-size:13px}
.sec-row .rk{font-family:var(--mono);font-size:11px;color:var(--muted)}
.sec-name{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:3px}
.sec-name span{color:var(--sub);font-size:11px}
.track{height:6px;background:var(--s2);border-radius:4px;overflow:hidden}
.fill{height:100%;border-radius:4px;background:var(--muted)}
.strong .fill{background:var(--gold)}.strong .sec-name b{color:var(--gold2)}
.sec-row .v{font-family:var(--mono);font-size:12px;text-align:right}

/* tables */
.tbl-wrap{overflow-x:auto;border:1px solid var(--b);border-radius:14px;background:var(--s)}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:720px}
th{text-align:left;font-weight:500;font-size:12px;color:var(--sub);padding:10px 12px;border-bottom:1px solid var(--b);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--b);white-space:nowrap}
tr:last-child td{border-bottom:none}
td.n,th.n{text-align:right;font-family:var(--mono)}
th.n{font-family:var(--ui)}
tr.row{cursor:pointer}tr.row:hover td,tr.row:focus-visible td{background:rgba(255,255,255,.025)}
tr.detail td{background:var(--s2);padding:14px 16px;white-space:normal}
.detail-inner{max-width:420px}
.hist{max-height:360px;overflow:auto}
.hist table{min-width:0}

details.crit{background:var(--s);border:1px solid var(--b);border-radius:14px;padding:14px 16px}
details.crit summary{cursor:pointer;font-weight:700;font-size:14px}
details.crit ul{margin:12px 0 0 18px;color:var(--sub);font-size:13px;display:grid;gap:5px}
details.crit li b{color:var(--text);font-weight:500}

@media (max-width:900px){.two{grid-template-columns:minmax(0,1fr)}.main{padding:16px 14px 50px}.hdr{padding:10px 14px}.cards{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){.dot.scanning{animation:none}}
</style>
</head>
<body>
<header class="hdr">
  <div class="brand">
    <div class="logo-k">K</div>
    <div><h1>Base breakout scanner</h1><p>Ran hard, rested on fading volume, broke out on a volume spike</p></div>
  </div>
  <div class="status">
    <span><i class="dot" id="dot"></i><span id="mkt">Loading</span></span>
    <span id="upd"></span>
    <button class="btn" id="scanBtn" type="button">Scan now</button>
  </div>
</header>

<div class="main">
  <div id="banners"></div>

  <section>
    <h2>Breakouts <small id="bo-sub"></small></h2>
    <div class="cards" id="cards"></div>
  </section>

  <div class="two">
    <section class="panel">
      <h2>Sector strength <small id="sec-sub"></small></h2>
      <div id="sectors"></div>
    </section>
    <section class="panel">
      <h2>Telegram alerts <small>latest first</small></h2>
      <div class="hist" id="history"></div>
    </section>
  </div>

  <section>
    <h2>Watchlist <small>passes every rule except the breakout or its volume</small></h2>
    <div class="tbl-wrap"><table id="watch"></table></div>
  </section>

  <details class="crit"><summary>Rules in use</summary><ul id="crit"></ul></details>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = x => x == null ? 'n/a' : x >= 1e9 ? '$' + (x/1e9).toFixed(2) + 'B' : '$' + Math.round(x/1e6) + 'M';
const sgn = (x, d=1) => (x >= 0 ? '+' : '') + Number(x).toFixed(d) + '%';
const cls = x => x >= 0 ? 'pos' : 'neg';
let D = null, openRow = null, scanRequested = false;

/* ---------- chart: run, base box, breakout, volume ---------- */
function chart(sp){
  const W = 340, H = 176, padL = 4, padR = 46, padT = 8, VH = 38, gap = 10;
  const ph = H - VH - gap - padT - 6, n = sp.c.length;
  const lo = Math.min(...sp.c, sp.bl) * 0.985, hi = Math.max(...sp.c, sp.bh) * 1.015;
  const x = i => padL + (W - padL - padR) * i / Math.max(n - 1, 1);
  const y = v => padT + ph * (1 - (v - lo) / (hi - lo));
  const path = sp.c.map((v, i) => (i ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(v).toFixed(1)).join('');
  const bx = x(sp.bs), bw = x(sp.be) - x(sp.bs);
  const vy0 = padT + ph + gap + VH, vmax = Math.max(...sp.v, sp.avgv) || 1;
  const bwid = Math.max((W - padL - padR) / n * 0.7, 1);
  const bars = sp.v.map((v, i) => {
    const h = Math.max(v / vmax * VH, 0.5);
    const c = i === n - 1 ? 'var(--gold2)' : (i >= sp.bs && i <= sp.be) ? '#3a4a66' : '#2a2a3e';
    const w = i === n - 1 ? Math.max(bwid * 1.8, 3.5) : bwid;
    return `<rect x="${(x(i) - w/2).toFixed(1)}" y="${(vy0 - h).toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" fill="${c}"/>`;
  }).join('');
  const avgY = vy0 - sp.avgv / vmax * VH;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Price and volume chart with the base highlighted">
    <rect x="${bx.toFixed(1)}" y="${y(sp.bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${(y(sp.bl) - y(sp.bh)).toFixed(1)}" fill="rgba(212,175,55,.10)" stroke="rgba(212,175,55,.45)" stroke-dasharray="3 3"/>
    <text x="${(bx + bw / 2).toFixed(1)}" y="${(y(sp.bl) + 13).toFixed(1)}" text-anchor="middle" fill="#d4af37" font-size="9" font-family="Space Grotesk,sans-serif">base, ${sp.be - sp.bs + 1} days</text>
    <line x1="${bx.toFixed(1)}" x2="${W - padR}" y1="${y(sp.bh).toFixed(1)}" y2="${y(sp.bh).toFixed(1)}" stroke="#d4af37" stroke-width="1" stroke-dasharray="4 3" opacity=".8"/>
    <text x="${W - padR + 4}" y="${(y(sp.bh) + 3).toFixed(1)}" fill="#d4af37" font-size="9" font-family="Space Mono,monospace">${sp.bh.toFixed(2)}</text>
    <path d="${path}" fill="none" stroke="#e8e8f0" stroke-width="1.5" stroke-linejoin="round"/>
    <circle cx="${x(n-1).toFixed(1)}" cy="${y(sp.c[n-1]).toFixed(1)}" r="3.5" fill="#f0c84a"/>
    ${bars}
    <line x1="${padL}" x2="${W - padR}" y1="${avgY.toFixed(1)}" y2="${avgY.toFixed(1)}" stroke="#8888aa" stroke-width="1" stroke-dasharray="2 3"/>
    <text x="${W - padR + 4}" y="${(avgY + 3).toFixed(1)}" fill="#8888aa" font-size="9" font-family="Space Grotesk,sans-serif">50d avg</text>
  </svg>`;
}

/* ---------- pieces ---------- */
function card(b){
  const alert = b.alerted ? `<span class="chip gold">Telegram sent ${esc(b.alert_time || '')}</span>` : `<span class="chip">No alert sent</span>`;
  const when = b.partial ? `<span class="chip amber">Intraday, volume so far</span>` : `<span class="chip green">Closed above base</span>`;
  const m = (v, l) => `<div class="m"><b>${v}</b><span>${l}</span></div>`;
  return `<article class="card hot">
    <div class="card-top">
      <div><div class="sym">${esc(b.symbol)}</div><div class="nm" title="${esc(b.name)}">${esc(b.name)}</div></div>
      <div class="px"><b>$${b.price.toFixed(2)}</b><span class="mono ${cls(b.day_chg)}" style="font-size:12px">${sgn(b.day_chg)} today</span></div>
    </div>
    <div class="chips"><span class="chip">${esc(b.sector)}</span>${when}${alert}</div>
    ${chart(b.spark)}
    <div class="metrics">
      ${m(b.rvol.toFixed(1) + 'x', 'volume vs 50d')}
      ${m(sgn(b.gain_3m, 0), '3-month gain')}
      ${m(b.volatility.toFixed(1) + '%', b.vol_metric_name === 'ATR' ? 'avg true range' : 'avg daily range')}
      ${m(b.score, 'score of 100')}
      ${m(b.base_days + 'd, ' + b.base_depth.toFixed(0) + '%', 'base / depth')}
      ${m(b.dryup_ratio.toFixed(2) + 'x', 'volume dry-up')}
      ${m(money(b.mcap), 'market cap')}
      ${m('#' + (b.sector_info?.rank ?? '?'), 'sector rank')}
    </div>
  </article>`;
}

function renderSectors(secs, spy){
  $('sec-sub').textContent = spy != null ? 'S&P 500 (SPY) 3-month ' + sgn(spy) : '';
  const max = Math.max(...secs.map(s => Math.abs(s.ret_3m)), 1);
  $('sectors').innerHTML = secs.map(s => {
    const w = Math.max(Math.abs(s.ret_3m) / max * 100, 2);
    const note = s.strong ? 'strong' : (!s.above_50dma && s.rank <= (D.criteria.strong_top_n || 4)) ? 'below 50-day average' : s.etf;
    return `<div class="sec-row ${s.strong ? 'strong' : ''}">
      <span class="rk">${s.rank}</span>
      <div><div class="sec-name"><b>${esc(s.sector)}</b><span>${esc(note)}</span></div><div class="track"><div class="fill" style="width:${w}%"></div></div></div>
      <span class="v ${cls(s.ret_3m)}">${sgn(s.ret_3m)}</span></div>`;
  }).join('');
}

function renderHistory(h){
  if (!h.length) { $('history').innerHTML = '<div class="empty" style="border:none">No alerts yet. A message goes to Telegram the moment a stock passes every rule.</div>'; return; }
  const chipFor = s => ({held:'<span class="chip green">Held at close</span>', faded:'<span class="chip red">Faded by close</span>',
                         intraday:'<span class="chip amber">Intraday</span>', closed:'<span class="chip">Closed</span>'}[s] || '');
  $('history').innerHTML = '<table><thead><tr><th>When</th><th>Symbol</th><th class="n">Price</th><th class="n">Volume</th><th>Result</th></tr></thead><tbody>' +
    h.map(a => `<tr><td>${esc(a.date.slice(5))} ${esc(a.time)}</td><td><b>${esc(a.symbol)}</b></td><td class="n">$${Number(a.price).toFixed(2)}</td><td class="n">${Number(a.rvol).toFixed(1)}x</td><td>${chipFor(a.status)}</td></tr>`).join('') + '</tbody></table>';
}

function renderWatch(d){
  const rows = [...(d.low_volume_breakouts || []).map(x => ({...x, kind: 'low'})), ...(d.setups || []).map(x => ({...x, kind: 'setup'}))];
  if (!rows.length) { $('watch').outerHTML = '<div id="watch" class="empty">Nothing close right now.</div>'; return; }
  const head = '<thead><tr><th>Symbol</th><th class="n">Price</th><th>Status</th><th class="n">Base</th><th class="n">Dry-up</th><th class="n">Range</th><th class="n">3M gain</th><th class="n">Volume today</th><th>Sector</th></tr></thead>';
  const body = rows.map((r, i) => {
    const status = r.kind === 'low' ? `<span class="chip amber">Broke out, volume only ${r.rvol.toFixed(1)}x</span>`
                                    : `<span class="chip">${r.gap_to_high.toFixed(1)}% below base high</span>`;
    return `<tr class="row" tabindex="0" data-i="${i}"><td><b>${esc(r.symbol)}</b></td><td class="n">$${r.price.toFixed(2)}</td><td>${status}</td>
      <td class="n">${r.base_days}d, ${r.base_depth.toFixed(0)}%</td><td class="n">${r.dryup_ratio.toFixed(2)}x</td><td class="n">${r.volatility.toFixed(1)}%</td>
      <td class="n ${cls(r.gain_3m)}">${sgn(r.gain_3m, 0)}</td><td class="n">${r.rvol.toFixed(1)}x</td><td>${esc(r.sector)}</td></tr>`;
  }).join('');
  $('watch').innerHTML = head + '<tbody>' + body + '</tbody>';
  const toggle = tr => {
    const i = +tr.dataset.i, next = tr.nextElementSibling;
    if (next && next.classList.contains('detail')) { next.remove(); openRow = null; return; }
    document.querySelectorAll('tr.detail').forEach(e => e.remove());
    const det = document.createElement('tr'); det.className = 'detail';
    det.innerHTML = `<td colspan="9"><div class="detail-inner">${chart(rows[i].spark)}</div></td>`;
    tr.after(det); openRow = rows[i].symbol;
  };
  document.querySelectorAll('tr.row').forEach(tr => {
    tr.addEventListener('click', () => toggle(tr));
    tr.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(tr); } });
    if (rows[+tr.dataset.i].symbol === openRow) toggle(tr);
  });
}

function renderCriteria(c){
  const li = (b, t) => `<li><b>${b}</b> ${t}</li>`;
  $('crit').innerHTML = [
    li('Market cap', `${money(c.mcap_min)} to ${money(c.mcap_max)}`),
    li('Volatility', `${c.volatility_metric === 'ATR' ? 'Average true range' : 'Average daily range'} at least ${c.min_volatility_pct}% over ${c.volatility_days} days`),
    li('Sector', `top ${c.strong_top_n} of 11 by 3-month return${c.sector_above_50dma ? ' and above its 50-day average' : ''}`),
    li('Big move', `up at least ${c.min_gain_3m_pct}% over ${c.bars_3m} trading days, with a run of ${c.min_run_pct}% or more into the peak`),
    li('Base', `starts right after the peak close, ${c.min_base_days} to ${c.max_base_days} days, no deeper than ${c.max_base_depth_pct}%, net drift under ${c.max_base_drift_pct}%, holds ${Math.round(c.hold_gain_frac*100)}% of the run, within ${c.near_high_pct}% of the peak`),
    li('Volume dry-up', `last third of the base at or below ${c.vol_dryup_ratio}x the first third, and trending down`),
    li('Breakout', `close above the base high on volume at least ${c.min_rvol}x the ${c.rvol_days}-day average`),
    li('Guard rails', `price at least $${c.min_price}, average daily dollar volume at least ${money(c.min_avg_dollar_vol)}`),
  ].join('');
}

/* ---------- main render ---------- */
function render(d){
  D = d;
  if (d.empty) {
    $('banners').innerHTML = `<div class="banner warn">Waiting for the first scan. Start the scanner with <code>python base_breakout_scanner.py</code> in this folder.</div>`;
    $('cards').innerHTML = ''; $('mkt').textContent = 'No data yet'; return;
  }
  const ms = d.market_state;
  $('dot').className = 'dot ' + (d.scanning ? 'scanning' : ms === 'open' ? 'open' : '');
  $('mkt').textContent = d.scanning ? 'Scanning' + (d.scan_started ? ' since ' + d.scan_started : '') : ({open:'Market open', premarket:'Pre-market', afterhours:'After hours', closed:'Market closed'}[ms] || ms);
  $('upd').textContent = 'Last scan ' + d.updated_et + (d.scan_seconds ? ' (' + d.scan_seconds + 's)' : '');
  if (!d.scanning) { scanRequested = false; $('scanBtn').disabled = false; $('scanBtn').textContent = 'Scan now'; }

  const b = [];
  const ageMin = (Date.now() - Date.parse(d.updated)) / 60000;
  if (ms === 'open' && ageMin > 3 * (d.interval_minutes || 10) && !d.scanning)
    b.push(`<div class="banner warn">No scan for ${ageMin < 120 ? Math.round(ageMin) + ' minutes' : ageMin < 2880 ? Math.round(ageMin / 60) + ' hours' : Math.round(ageMin / 1440) + ' days'}. The scanner may have stopped: run <code>python base_breakout_scanner.py</code>.</div>`);
  if (d.errors && d.errors.length) b.push(`<div class="banner err">${d.errors.map(esc).join('<br>')}</div>`);
  if (d.dry_run) b.push(`<div class="banner warn">Dry run: alerts are not sent to Telegram.</div>`);
  $('banners').innerHTML = b.join('');

  const bo = d.breakouts || [];
  $('bo-sub').textContent = `${d.analyzed} stocks checked in ${(d.sectors.filter(s => s.strong).length)} strong sectors` + (d.partial_bar ? ', today\'s volume is still building' : '');
  $('cards').innerHTML = bo.length ? bo.map(card).join('') :
    `<div class="empty" style="grid-column:1/-1"><b>No stock passes every rule right now.</b><br>${(d.setups||[]).length} setups are close to breaking out (see the watchlist). The scanner checks every ${d.interval_minutes || 10} minutes while the market is open.</div>`;
  renderSectors(d.sectors, d.spy_ret_3m);
  renderHistory(d.history || []);
  renderWatch(d);
  renderCriteria(d.criteria);
}

async function load(){
  try { render(await (await fetch('/api/data', {cache: 'no-store'})).json()); }
  catch (e) { $('mkt').textContent = 'Dashboard cannot reach its server'; }
}
$('scanBtn').addEventListener('click', async () => {
  $('scanBtn').disabled = true; $('scanBtn').textContent = 'Scan requested';
  try { await fetch('/api/scan-now', {method: 'POST'}); scanRequested = true; setTimeout(load, 4000); } catch (e) {}
});
load(); setInterval(load, 10000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/data":
            try:
                body = RESULTS_FILE.read_bytes()
                json.loads(body)                     # never serve a half-written file
            except Exception:
                body = json.dumps({"empty": True}).encode()
            self._send(200, body, "application/json")
        elif path == "/api/health":
            self._send(200, b'{"ok":true}', "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path.split("?")[0] == "/api/scan-now":
            try:
                TRIGGER_FILE.write_text("scan", encoding="utf-8")
                self._send(200, b'{"ok":true}', "application/json")
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "127.0.0.1"
    print("=" * 52)
    print(" KIBO Base Breakout Dashboard")
    print(f" http://localhost:{PORT}")
    print(f" http://{ip}:{PORT}   (phone on the same WiFi)")
    print(f" Reading: {RESULTS_FILE}")
    print("=" * 52)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Dashboard stopped.")


if __name__ == "__main__":
    main()
