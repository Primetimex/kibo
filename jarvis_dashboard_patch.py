# -*- coding: utf-8 -*-
"""
JARVIS Dashboard Integration — add-on for dashboard.py
════════════════════════════════════════════════════════════════════════════
Wires kibo_jarvis.py into the KIBO dashboard:

  POST /jarvis/chat      {"message": "...", "history": [...]}  -> AI answer
  GET  /jarvis/status    -> sentinel findings + brain status (no LLM cost)
  POST /jarvis/halt      {"reason": "..."}  -> manual halt via brain
  POST /jarvis/resume    -> manual resume

Plus a chat panel (HTML/CSS/JS) styled for the KIBO dark theme (#0a0a0f).

HOW TO APPLY (manual, 3 steps — kept explicit so nothing surprises you):

  STEP 1 — imports. Near the top of dashboard.py add:

      from kibo_jarvis import jarvis_chat, sentinel_check, sentinel_loop
      from trade_brain import brain
      import threading as _jarvis_threading
      _jarvis_threading.Thread(target=sentinel_loop, daemon=True).start()

  STEP 2 — routes. In your do_POST handler, alongside /manual-trade, add
  the JARVIS_POST_ROUTES block below. In do_GET, alongside /signals, add
  the JARVIS_GET_ROUTES block.

  STEP 3 — UI. Paste JARVIS_PANEL_HTML into the dashboard HTML where you
  want the panel (it self-contains its CSS + JS).
"""

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2a — do_POST additions
#  (match your existing pattern: read body, respond ONCE with _json_response
#   or however your helper is named — the duplicate-response crash from
#   before came from responding twice, so each branch returns immediately)
# ─────────────────────────────────────────────────────────────────────────────
JARVIS_POST_ROUTES = r'''
        if self.path == "/jarvis/chat":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = json.loads(self.rfile.read(length) or b"{}")
                msg     = (body.get("message") or "").strip()
                history = body.get("history") or []
                if not msg:
                    self._send_json({"error": "empty message"}); return
                answer = jarvis_chat(msg, history[-10:])
                self._send_json({"answer": answer})
            except Exception as e:
                self._send_json({"error": str(e)})
            return

        if self.path == "/jarvis/halt":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = json.loads(self.rfile.read(length) or b"{}")
                brain.force_halt(body.get("reason", "Manual halt from dashboard"))
                self._send_json({"ok": True, "halted": True})
            except Exception as e:
                self._send_json({"error": str(e)})
            return

        if self.path == "/jarvis/resume":
            try:
                brain.clear_halt()
                self._send_json({"ok": True, "halted": False})
            except Exception as e:
                self._send_json({"error": str(e)})
            return
'''

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2b — do_GET addition
# ─────────────────────────────────────────────────────────────────────────────
JARVIS_GET_ROUTES = r'''
        if self.path == "/jarvis/status":
            try:
                findings = sentinel_check(alert=False)
                status   = brain.get_status()
                self._send_json({"sentinel": findings, "brain": status})
            except Exception as e:
                self._send_json({"error": str(e)})
            return
'''

# NOTE: replace self._send_json with your actual JSON-response helper name
# if it differs (some KIBO routes use a local send(obj) pattern).

# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3 — the panel (drop into dashboard HTML)
# ─────────────────────────────────────────────────────────────────────────────
JARVIS_PANEL_HTML = r'''
<!-- ══════════ JARVIS PANEL ══════════ -->
<div class="panel" id="jarvis-panel" style="display:flex;flex-direction:column;min-height:340px;">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
    <div style="display:flex;align-items:center;gap:8px;">
      <span style="font-size:16px;">🧠</span>
      <span style="font-weight:700;letter-spacing:1px;">JARVIS</span>
      <span id="jv-health" style="font-size:9px;padding:2px 8px;border-radius:10px;
            background:rgba(0,255,136,.12);color:var(--green,#00ff88);">CHECKING…</span>
    </div>
    <div style="display:flex;gap:6px;">
      <button onclick="jvHalt()"   style="font-size:9px;padding:3px 10px;border-radius:6px;border:1px solid #ff4466;
              background:rgba(255,68,102,.1);color:#ff4466;cursor:pointer;">HALT ALL</button>
      <button onclick="jvResume()" style="font-size:9px;padding:3px 10px;border-radius:6px;border:1px solid var(--green,#00ff88);
              background:rgba(0,255,136,.08);color:var(--green,#00ff88);cursor:pointer;">RESUME</button>
    </div>
  </div>

  <div id="jv-messages" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:8px;
       padding:8px;background:rgba(255,255,255,.02);border-radius:8px;min-height:200px;max-height:380px;">
    <div class="jv-msg jv-ai">JARVIS online. Ask me anything — exposure, P&amp;L, why an agent is quiet, whether a setup is worth it.</div>
  </div>

  <div style="display:flex;gap:6px;margin-top:8px;">
    <input id="jv-input" type="text" placeholder="Ask JARVIS…"
           style="flex:1;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);
                  border-radius:8px;padding:8px 12px;color:#e8e8f0;font-size:12px;outline:none;"
           onkeydown="if(event.key==='Enter')jvSend()">
    <button onclick="jvSend()" id="jv-send"
            style="padding:8px 16px;border-radius:8px;border:none;background:var(--green,#00ff88);
                   color:#0a0a0f;font-weight:700;font-size:12px;cursor:pointer;">ASK</button>
  </div>
  <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;">
    <button class="jv-chip" onclick="jvQuick('Full system status — brain, positions, P&L')">Status</button>
    <button class="jv-chip" onclick="jvQuick('What is my current total exposure across all agents?')">Exposure</button>
    <button class="jv-chip" onclick="jvQuick('How am I performing today? Any patterns in the losses?')">Today's P&L</button>
    <button class="jv-chip" onclick="jvQuick('Run an end of day debrief')">Debrief</button>
  </div>
</div>

<style>
.jv-msg{font-size:11.5px;line-height:1.5;padding:8px 12px;border-radius:10px;max-width:88%;white-space:pre-wrap;word-break:break-word;}
.jv-ai{background:rgba(0,255,136,.06);border:1px solid rgba(0,255,136,.15);align-self:flex-start;}
.jv-user{background:rgba(120,120,255,.1);border:1px solid rgba(120,120,255,.2);align-self:flex-end;}
.jv-chip{font-size:9px;padding:3px 10px;border-radius:12px;border:1px solid rgba(255,255,255,.15);
         background:transparent;color:var(--muted,#888);cursor:pointer;}
.jv-chip:hover{border-color:var(--green,#00ff88);color:var(--green,#00ff88);}
.jv-typing{opacity:.6;font-style:italic;}
</style>

<script>
let _jvHistory = [];

function jvAppend(text, who){
  const box = document.getElementById('jv-messages');
  const div = document.createElement('div');
  div.className = 'jv-msg ' + (who==='user'?'jv-user':'jv-ai');
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  return div;
}

async function jvSend(){
  const inp = document.getElementById('jv-input');
  const msg = inp.value.trim();
  if(!msg) return;
  inp.value = '';
  jvAppend(msg, 'user');
  const typing = jvAppend('Analyzing…', 'ai');
  typing.classList.add('jv-typing');
  document.getElementById('jv-send').disabled = true;
  try{
    const r = await fetch('/jarvis/chat', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message: msg, history: _jvHistory})
    });
    const d = await r.json();
    typing.remove();
    const answer = d.answer || ('Error: ' + (d.error || 'unknown'));
    jvAppend(answer, 'ai');
    _jvHistory.push({role:'user', content: msg}, {role:'assistant', content: answer});
    _jvHistory = _jvHistory.slice(-10);
  }catch(e){
    typing.remove();
    jvAppend('Connection error: ' + e, 'ai');
  }
  document.getElementById('jv-send').disabled = false;
}

function jvQuick(q){ document.getElementById('jv-input').value = q; jvSend(); }

async function jvHalt(){
  if(!confirm('Halt ALL trading across every agent?')) return;
  await fetch('/jarvis/halt', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({reason:'Manual halt from dashboard'})});
  jvAppend('🛑 All trading halted.', 'ai'); jvHealth();
}
async function jvResume(){
  await fetch('/jarvis/resume', {method:'POST'});
  jvAppend('✅ Trading resumed.', 'ai'); jvHealth();
}

async function jvHealth(){
  try{
    const r = await fetch('/jarvis/status');
    const d = await r.json();
    const el = document.getElementById('jv-health');
    if(d.brain && d.brain.halted){
      el.textContent = 'HALTED';
      el.style.background = 'rgba(255,68,102,.15)'; el.style.color = '#ff4466';
    }else if(d.sentinel && !d.sentinel.ok){
      el.textContent = d.sentinel.issues.length + ' WARNING' + (d.sentinel.issues.length>1?'S':'');
      el.style.background = 'rgba(255,180,0,.15)'; el.style.color = '#ffb400';
      el.title = d.sentinel.issues.join('\n');
    }else{
      el.textContent = 'NOMINAL';
      el.style.background = 'rgba(0,255,136,.12)'; el.style.color = 'var(--green,#00ff88)';
    }
  }catch(e){}
}
jvHealth();
setInterval(jvHealth, 30000);
</script>
<!-- ══════════ END JARVIS PANEL ══════════ -->
'''

if __name__ == "__main__":
    print(__doc__)
    print("This file is a reference/patch source — copy the three blocks into dashboard.py.")
