"""
DASHBOARD PATCH — Add Confirmation UI to dashboard.py
======================================================
Add these routes and the frontend HTML block to your existing dashboard.py.
No restructuring needed — just drop these in.
"""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Add to imports at the top of dashboard.py
# ══════════════════════════════════════════════════════════════════════════════

from trade_confirm import resolve_from_dashboard, get_pending


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Add these two Flask routes to dashboard.py
# ══════════════════════════════════════════════════════════════════════════════

# --- Paste inside your Flask app, alongside your existing routes ---

"""
@app.route("/api/pending_confirmations")
def api_pending_confirmations():
    \"\"\"Dashboard polls this every 3s to show pending trade approvals.\"\"\"
    return jsonify(get_pending())


@app.route("/api/confirm_trade", methods=["POST"])
def api_confirm_trade():
    \"\"\"Called when user clicks Approve or Reject in the dashboard UI.\"\"\"
    data = request.get_json()
    conf_id  = data.get("id")
    approved = data.get("approved", False)
    if not conf_id:
        return jsonify({"error": "missing id"}), 400
    resolve_from_dashboard(conf_id, approved)
    return jsonify({"status": "ok", "id": conf_id, "approved": approved})
"""


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Add this HTML block to your dashboard template / inline HTML
#           Paste it just after your existing stats cards section.
#           It self-polls and disappears when no confirmations are pending.
# ══════════════════════════════════════════════════════════════════════════════

CONFIRMATION_HTML_BLOCK = """
<!-- ── Trade Confirmation Gate UI ────────────────────────── -->
<div id="confirm-gate" style="display:none; margin: 12px 0;">
  <div style="
    background: #1a1a2e;
    border: 2px solid #f0b429;
    border-radius: 10px;
    padding: 16px 20px;
    font-family: monospace;
  ">
    <div style="color:#f0b429; font-size:13px; font-weight:700; letter-spacing:1px; margin-bottom:12px;">
      ⚠️ TRADE CONFIRMATION REQUIRED
    </div>
    <div id="confirm-list"></div>
  </div>
</div>

<script>
(function() {
  function fetchPending() {
    fetch('/api/pending_confirmations')
      .then(r => r.json())
      .then(items => {
        const gate = document.getElementById('confirm-gate');
        const list = document.getElementById('confirm-list');
        if (!items || items.length === 0) {
          gate.style.display = 'none';
          return;
        }
        gate.style.display = 'block';
        list.innerHTML = items.map(item => {
          const color = ['BUY','BUYTOCOVER'].includes(item.action) ? '#4caf50' : '#f44336';
          const elapsed = Math.round((Date.now() - new Date(item.created_at + 'Z').getTime()) / 1000);
          return `
            <div style="
              background:#0d0d1a;
              border-radius:8px;
              padding:12px 14px;
              margin-bottom:10px;
              border-left: 4px solid ${color};
            ">
              <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px;">
                <div>
                  <span style="color:#888; font-size:11px;">[${item.id}]</span>
                  <span style="color:${color}; font-weight:700; font-size:15px; margin-left:8px;">${item.action}</span>
                  <span style="color:#fff; font-weight:700; font-size:15px; margin-left:6px;">${item.qty.toLocaleString()}x ${item.symbol}</span>
                  <span style="color:#aaa; font-size:13px; margin-left:8px;">@ $${item.price.toFixed(2)}</span>
                </div>
                <div style="color:#888; font-size:11px;">⏱ ${elapsed}s ago · ${item.strategy}</div>
              </div>
              ${item.reason ? `<div style="color:#aaa; font-size:12px; margin-top:6px;">📝 ${item.reason}</div>` : ''}
              <div style="margin-top:10px; display:flex; gap:8px;">
                <button onclick="confirmTrade('${item.id}', true)" style="
                  background:#1b5e20; color:#69f0ae; border:1px solid #4caf50;
                  border-radius:6px; padding:6px 18px; cursor:pointer;
                  font-family:monospace; font-size:13px; font-weight:700;
                ">✅ APPROVE</button>
                <button onclick="confirmTrade('${item.id}', false)" style="
                  background:#4a0000; color:#ff8a80; border:1px solid #f44336;
                  border-radius:6px; padding:6px 18px; cursor:pointer;
                  font-family:monospace; font-size:13px; font-weight:700;
                ">❌ REJECT</button>
              </div>
            </div>
          `;
        }).join('');
      })
      .catch(() => {});
  }

  function confirmTrade(id, approved) {
    fetch('/api/confirm_trade', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, approved})
    }).then(() => fetchPending());
  }

  function playKiboAlert() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const m = ctx.createGain(); m.gain.setValueAtTime(0.55, ctx.currentTime); m.connect(ctx.destination);
      const t = (f,s,d,type,g,fade) => { const o=ctx.createOscillator(),gn=ctx.createGain(); o.type=type; o.frequency.setValueAtTime(f,ctx.currentTime+s); gn.gain.setValueAtTime(g,ctx.currentTime+s); if(fade) gn.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+s+d); o.connect(gn); gn.connect(m); o.start(ctx.currentTime+s); o.stop(ctx.currentTime+s+d+0.01); };
      const c = s => { const o=ctx.createOscillator(),g=ctx.createGain(); o.type='triangle'; o.frequency.setValueAtTime(2400,ctx.currentTime+s); o.frequency.exponentialRampToValueAtTime(1800,ctx.currentTime+s+0.04); g.gain.setValueAtTime(0.3,ctx.currentTime+s); g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+s+0.08); o.connect(g); g.connect(m); o.start(ctx.currentTime+s); o.stop(ctx.currentTime+s+0.1); };
      t(523,0,.12,'sine',.6,true); t(659,.1,.12,'sine',.6,true); t(784,.2,.12,'sine',.6,true); t(1047,.3,.25,'sine',.7,true);
      [.55,.63,.71,.78,.85].forEach(c);
      t(523,.6,.6,'sine',.4,true); t(659,.6,.6,'sine',.35,true); t(784,.6,.6,'sine',.3,true);
    } catch(e) {}
  }

  let _lastSeenIds = new Set();

  // Poll every 3 seconds
  fetchPending();
  setInterval(fetchPending, 3000);

  // Override fetchPending to play sound only on NEW confirmations
  const _origFetch = fetchPending;
  fetchPending = function() {
    fetch('/api/pending_confirmations')
      .then(r => r.json())
      .then(items => {
        const gate = document.getElementById('confirm-gate');
        const list = document.getElementById('confirm-list');
        if (!items || items.length === 0) {
          gate.style.display = 'none';
          _lastSeenIds = new Set();
          return;
        }
        const newItems = items.filter(i => !_lastSeenIds.has(i.id));
        if (newItems.length > 0) playKiboAlert();
        items.forEach(i => _lastSeenIds.add(i.id));
        gate.style.display = 'block';
        list.innerHTML = items.map(item => {
          const color = ['BUY','BUYTOCOVER'].includes(item.action) ? '#4caf50' : '#f44336';
          const elapsed = Math.round((Date.now() - new Date(item.created_at + 'Z').getTime()) / 1000);
          return `
            <div style="background:#0d0d1a;border-radius:8px;padding:12px 14px;margin-bottom:10px;border-left:4px solid ${color};">
              <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
                <div>
                  <span style="color:#888;font-size:11px;">[${item.id}]</span>
                  <span style="color:${color};font-weight:700;font-size:15px;margin-left:8px;">${item.action}</span>
                  <span style="color:#fff;font-weight:700;font-size:15px;margin-left:6px;">${item.qty.toLocaleString()}x ${item.symbol}</span>
                  <span style="color:#aaa;font-size:13px;margin-left:8px;">@ $${item.price.toFixed(2)}</span>
                </div>
                <div style="color:#888;font-size:11px;">⏱ ${elapsed}s ago · ${item.strategy}</div>
              </div>
              ${item.reason ? `<div style="color:#aaa;font-size:12px;margin-top:6px;">📝 ${item.reason}</div>` : ''}
              <div style="margin-top:10px;display:flex;gap:8px;">
                <button onclick="confirmTrade('${item.id}',true)" style="background:#1b5e20;color:#69f0ae;border:1px solid #4caf50;border-radius:6px;padding:6px 18px;cursor:pointer;font-family:monospace;font-size:13px;font-weight:700;">✅ APPROVE</button>
                <button onclick="confirmTrade('${item.id}',false)" style="background:#4a0000;color:#ff8a80;border:1px solid #f44336;border-radius:6px;padding:6px 18px;cursor:pointer;font-family:monospace;font-size:13px;font-weight:700;">❌ REJECT</button>
              </div>
            </div>`;
        }).join('');
      })
      .catch(() => {});
  };
})();
</script>
"""


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Patch each agent's order execution function
#           Find where each agent calls place_order / submit_order / etc.
#           Wrap it like this:
# ══════════════════════════════════════════════════════════════════════════════

AGENT_PATCH_TEMPLATE = """
# ── Add at top of agent file ──────────────────────────────────────────────────
from trade_confirm import request_trade_confirmation

# ── Wrap your existing order call like this ───────────────────────────────────

# BEFORE (example):
#   self.place_order(symbol, action, qty, price)

# AFTER:
approved = await request_trade_confirmation(
    symbol   = symbol,
    action   = action,       # "BUY", "SELL", "SELLSHORT", etc.
    qty      = qty,
    price    = price,
    strategy = "penny_agent",  # change per agent
    reason   = signal_reason,  # whatever signal description you already have
)
if not approved:
    logger.info(f"[{symbol}] Trade confirmation not received — holding for next signal.")
    return

# ... your existing place_order call here unchanged
"""


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 — Set environment variables (add to your .env or shell)
# ══════════════════════════════════════════════════════════════════════════════

ENV_EXAMPLE = """
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
TRADE_CONFIRM_TIMEOUT=120   # seconds before trade is held (default: 120)
"""


# ══════════════════════════════════════════════════════════════════════════════
# Agent-specific locations to patch
# ══════════════════════════════════════════════════════════════════════════════

PATCH_LOCATIONS = {
    "penny_agent.py":    "Look for place_order() or submit_order() call after signal confirmation",
    "orb_agent_v2.py":   "Look for order submission after ORB breakout confirmation block",
    "trading_agent.py":  "Look for execute_trade() or the TradeZero order dispatch function",
    "futures_agent.py":  "Look for ETF proxy order submission (GLD/SPY/QQQ/USO)",
    "crypto_agent.py":   "Look for Binance US order_market_buy / order_market_sell call",
}
