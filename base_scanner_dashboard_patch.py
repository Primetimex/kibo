# -*- coding: utf-8 -*-
"""
Volume Spike Scanner -> KIBO dashboard merge (auto-patcher, v2)
===============================================================
Run once from your KIBO folder (the one with dashboard.py):

    python base_scanner_dashboard_patch.py

It merges the scanner INTO your main dashboard (http://localhost:5000):
  * a "🚀 Volume Spikes" tab next to your other tabs, showing the full scanner
    (rules, sector strength, alerts, watch list with sectors, alert history,
    Scan now / Start / Stop), with a live count of today's alerts on the tab
  * "Base Breakout Scanner" in your agent list (start/stop + log like the others)
  * the routes the tab needs (/base-scanner, /base-scanner/data, /scan, /start, /stop)

Works on a fresh dashboard.py AND on one you already patched with v1 -- each piece
is only added if it's missing, and v1's header button is replaced by the tab.
A timestamped backup is written first. If the result wouldn't compile, nothing changes.
Keep base_breakout_scanner.py and base_scanner_dashboard.html next to dashboard.py.
"""
import ast
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASH = HERE / "dashboard.py"
MARKER = "BASE_SCANNER_PATCH"

AGENT_LINE = ('    "base_breakout": {"label":"Base Breakout Scanner", "file":"base_breakout_scanner.py", '
              '"color":"#4fc3f7","log":"base_breakout_scanner.log"},  # ' + MARKER + '\n')
SYMBOL_LINE = '    "base_breakout": [],  # ' + MARKER + '\n'

ROUTE_FUNCS = r'''
# ── BASE_SCANNER_PATCH: Volume Spike Scanner page + API ──────────────────────
_BS_DIR = Path(__file__).resolve().parent

def _bs_send(h, body, ctype, status=200):
    try:
        h.send_response(status)
        h.send_header('Content-Type', ctype)
        h.send_header('Content-Length', str(len(body)))
        h.send_header('Cache-Control', 'no-store')
        h.end_headers()
        h.wfile.write(body)
    except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
        pass

def _bs_json(h, obj, status=200):
    _bs_send(h, json.dumps(obj, default=str).encode('utf-8'), 'application/json', status)

def _bs_log_tail(n=25):
    try:
        with open(_BS_DIR / 'base_breakout_scanner.log', 'rb') as fh:
            fh.seek(0, 2)
            fh.seek(max(fh.tell() - 16000, 0))
            return fh.read().decode('utf-8', errors='replace').splitlines()[-n:]
    except Exception:
        return []

def _base_scanner_route(h, path):
    path = path.rstrip('/') or '/'
    if path == '/base-scanner':
        f = _BS_DIR / 'base_scanner_dashboard.html'
        if f.exists():
            _bs_send(h, f.read_bytes(), 'text/html; charset=utf-8')
        else:
            _bs_send(h, b'base_scanner_dashboard.html is missing - put it next to dashboard.py',
                     'text/plain; charset=utf-8', 404)
        return
    if path == '/base-scanner/data':
        try:
            data = json.loads((_BS_DIR / 'base_scanner_results.json').read_text(encoding='utf-8'))
        except Exception:
            data = {}
        data['running'] = is_running('base_breakout')
        data['log_tail'] = _bs_log_tail()
        _bs_json(h, data)
        return
    if h.command != 'POST':
        _bs_json(h, {'ok': False, 'msg': 'Use POST for this action'}, 405)
        return
    if path == '/base-scanner/scan':
        if is_running('base_breakout'):
            try:
                (_BS_DIR / 'base_scanner_trigger').touch()
                res = {'ok': True, 'msg': 'Scan requested - results in about a minute'}
            except Exception as e:
                res = {'ok': False, 'msg': f'Could not request scan: {e}'}
        else:
            res = start_agent('base_breakout')
            if res.get('ok'):
                res['msg'] = 'Scanner started - first scan is running now'
        _bs_json(h, res)
        return
    if path == '/base-scanner/start':
        _bs_json(h, start_agent('base_breakout'))
        return
    if path == '/base-scanner/stop':
        _bs_json(h, stop_agent('base_breakout'))
        return
    _bs_json(h, {'ok': False, 'msg': 'Unknown base-scanner route'}, 404)

'''

ROUTE_HOOK = ("        if parsed.path.startswith('/base-scanner'):  # " + MARKER + "\n"
              "            _base_scanner_route(self, parsed.path); return\n")

TAB_BUTTON = ('      <button class="tab" id="tab-spikes" onclick="showTab(\'spikes\')" style="color:#4fc3f7;">'
              '&#128640; Volume Spikes <span class="tc" id="cnt-spikes" '
              'style="background:rgba(79,195,247,.15);color:#4fc3f7;">&mdash;</span></button>'
              '<!-- ' + MARKER + ' -->\n')

TAB_PANEL = ('    <div id="spikes-panel" style="display:none;"><!-- ' + MARKER + ' -->\n'
             '      <iframe id="spikes-frame" title="Volume spike scanner" '
             'style="width:100%;height:900px;border:0;display:block;background:transparent;"></iframe>\n'
             '    </div>\n')

TAB_SCRIPT = r'''<script>
/* BASE_SCANNER_PATCH: Volume Spikes tab -------------------------------------- */
(function(){
  if (typeof showTab !== 'function') return;
  const origShowTab = showTab;
  const origRenderTZ = (typeof renderTZ === 'function') ? renderTZ : null;
  const frame = () => document.getElementById('spikes-frame');
  showTab = function(t){
    const btn = document.getElementById('tab-spikes'), pnl = document.getElementById('spikes-panel');
    if (t === 'spikes'){
      origShowTab('__spikes__');                       // hide every other tab + panel
      curTab = 'spikes';
      const tz = document.getElementById('tz-body'); if (tz) tz.style.display = 'none';
      if (btn) btn.classList.add('on');
      if (pnl) pnl.style.display = 'block';
      const f = frame(); if (f && !f.src) f.src = '/base-scanner?embed=1';
      return;
    }
    if (btn) btn.classList.remove('on');
    if (pnl) pnl.style.display = 'none';
    return origShowTab(t);
  };
  if (origRenderTZ){                                  // keep positions table hidden on our tab
    renderTZ = function(d){
      const r = origRenderTZ(d);
      if (curTab === 'spikes'){ const tz = document.getElementById('tz-body'); if (tz) tz.style.display = 'none'; }
      return r;
    };
  }
  window.addEventListener('message', e => {           // size the frame to its content
    if (e.data && e.data.type === 'kibo-spikes-height' && frame()) frame().style.height = Math.max(e.data.h, 300) + 'px';
  });
  async function spikesCount(){                       // live alert count on the tab
    try{
      const d = await (await fetch('/base-scanner/data', {cache:'no-store'})).json();
      const n = (d.spikes || []).length, el = document.getElementById('cnt-spikes');
      if (el){
        el.textContent = d.updated_et ? n : '—';
        el.style.background = n ? 'rgba(0,230,118,.2)' : 'rgba(79,195,247,.15)';
        el.style.color = n ? '#00e676' : '#4fc3f7';
      }
    }catch(e){}
  }
  spikesCount(); setInterval(spikesCount, 60000);
})();
</script>
'''

V1_HEADER_LINK = re.compile(r'^[ \t]*<a href="/base-scanner"[^\n]*<!-- ' + MARKER + r' -->\n', re.M)


def insert_before_dict_close(src, dict_name, line):
    m = re.search(r"^" + dict_name + r"\s*=\s*\{\s*$", src, re.M)
    if not m:
        raise RuntimeError(f"Could not find `{dict_name} = {{` in dashboard.py")
    close = re.compile(r"^\}\s*$", re.M).search(src, m.end())
    if not close:
        raise RuntimeError(f"Could not find the end of {dict_name}")
    return src[:close.start()] + line + src[close.start():]


def insert_after_line(src, needle, text):
    i = src.find(needle)
    if i == -1:
        return None
    eol = src.find("\n", i) + 1
    return src[:eol] + text + src[eol:]


def main():
    if not DASH.exists():
        sys.exit(f"dashboard.py not found in {HERE}. Put this script in your KIBO folder and run it there.")
    for need in ("base_breakout_scanner.py", "base_scanner_dashboard.html"):
        if not (HERE / need).exists():
            print(f"WARNING: {need} is not in {HERE} yet - copy it there before starting the dashboard.")

    src = DASH.read_text(encoding="utf-8")
    orig = src
    done = []

    # 1. agent registry
    agents_block = src[src.find("AGENTS = {"):src.find("\n}\n", src.find("AGENTS = {"))]
    if '"base_breakout"' not in agents_block:
        src = insert_before_dict_close(src, "AGENTS", AGENT_LINE)
        src = insert_before_dict_close(src, "SYMBOLS", SYMBOL_LINE)
        done.append("agent list")

    # 2. routes
    if "def _base_scanner_route(" not in src:
        anchor = re.search(r"^class Handler\(BaseHTTPRequestHandler\):", src, re.M)
        if not anchor:
            raise RuntimeError("Could not find `class Handler(BaseHTTPRequestHandler):`")
        src = src[:anchor.start()] + ROUTE_FUNCS + src[anchor.start():]
        done.append("routes")
    if "parsed.path.startswith('/base-scanner')" not in src:
        m = re.search(r"(    def do_GET\(self\):\n\s*parsed\s*=\s*urlparse\(self\.path\)\n)", src)
        if not m:
            raise RuntimeError("Could not find the start of do_GET (`parsed=urlparse(self.path)`)")
        src = src[:m.end()] + ROUTE_HOOK + src[m.end():]
        done.append("route hook")

    # 3. v1 header button -> replaced by the tab
    src, n = V1_HEADER_LINK.subn("", src)
    if n:
        done.append("removed old header button")

    # 4. the tab
    if 'id="tab-spikes"' not in src:
        out = insert_after_line(src, 'id="tab-kibo"', TAB_BUTTON) or insert_after_line(src, '<div class="tabs">', TAB_BUTTON)
        if out is None:
            raise RuntimeError('Could not find the tab bar (`<div class="tabs">`)')
        src = out
        done.append("tab button")
    if 'id="spikes-panel"' not in src:
        out = insert_after_line(src, 'id="tz-body"', TAB_PANEL)
        if out is None:
            raise RuntimeError('Could not find the tab content area (`id="tz-body"`)')
        src = out
        done.append("tab panel")
    if "Volume Spikes tab ---" not in src:
        i = src.rfind("</body>")
        if i == -1:
            raise RuntimeError("Could not find </body> in the dashboard HTML")
        src = src[:i] + TAB_SCRIPT + src[i:]
        done.append("tab script")

    if src == orig:
        print("dashboard.py already has the Volume Spikes tab - nothing to do.")
        return
    try:
        ast.parse(src)
    except SyntaxError as e:
        sys.exit(f"Patch aborted - result would not compile ({e}). dashboard.py was NOT changed.")

    backup = DASH.with_name(f"dashboard.py.bak_spikes_{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(DASH, backup)
    DASH.write_text(src, encoding="utf-8")
    print(f"Backup written: {backup.name}")
    print("Added: " + ", ".join(done))
    print("Restart dashboard.py, open http://localhost:5000 and click the Volume Spikes tab.")


if __name__ == "__main__":
    main()
