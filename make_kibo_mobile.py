"""
make_kibo_mobile.py — turns the KIBO dashboard into an installable mobile web app (PWA).

HOW TO USE:
  1. Put this file in your KIBO folder (same folder as dashboard.py),
     along with: icon-192.png, icon-512.png, apple-touch-icon.png
  2. Run:  python make_kibo_mobile.py
  3. Restart the dashboard.

It makes a backup (dashboard.py.bak_mobile) before touching anything,
and it's safe to re-run — it skips changes that are already applied.
"""
from pathlib import Path
import shutil, sys

DASH = Path("dashboard.py")
if not DASH.exists():
    sys.exit("ERROR: dashboard.py not found. Run this from your KIBO folder.")

src = DASH.read_text(encoding="utf-8")
changed = False

# ── 1. PWA tags in <head> ─────────────────────────────────────────────
HEAD_ANCHOR = '<title>KIBO Trading</title>'
HEAD_TAGS = '''<title>KIBO Trading</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" type="image/png" sizes="192x192" href="/icon-192.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="theme-color" content="#0a0a0f">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="KIBO">'''

if '/manifest.json' in src:
    print("[skip] PWA head tags already present")
elif HEAD_ANCHOR in src:
    src = src.replace(HEAD_ANCHOR, HEAD_TAGS, 1)
    changed = True
    print("[ok]   PWA head tags added")
else:
    sys.exit("ERROR: couldn't find <title>KIBO Trading</title> in dashboard.py")

# ── 2. Routes for manifest + icons ────────────────────────────────────
ROUTE_ANCHOR = "        elif parsed.path=='/data':"
ROUTES = """        elif parsed.path=='/manifest.json':
            try:
                manifest = {
                    "name": "KIBO Trading",
                    "short_name": "KIBO",
                    "start_url": "/",
                    "display": "standalone",
                    "background_color": "#0a0a0f",
                    "theme_color": "#0a0a0f",
                    "icons": [
                        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
                    ]
                }
                self.send_response(200); self.send_header('Content-Type','application/manifest+json'); self.end_headers()
                self.wfile.write(json.dumps(manifest).encode())
            except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
                pass
        elif parsed.path in ('/icon-192.png','/icon-512.png','/apple-touch-icon.png'):
            try:
                icon_file = Path(__file__).parent / parsed.path.lstrip('/')
                if icon_file.exists():
                    self.send_response(200); self.send_header('Content-Type','image/png'); self.end_headers()
                    self.wfile.write(icon_file.read_bytes())
                else:
                    self.send_response(404); self.end_headers()
            except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
                pass
        elif parsed.path=='/data':"""

if "parsed.path=='/manifest.json'" in src:
    print("[skip] manifest/icon routes already present")
elif ROUTE_ANCHOR in src:
    src = src.replace(ROUTE_ANCHOR, ROUTES, 1)
    changed = True
    print("[ok]   manifest + icon routes added")
else:
    sys.exit("ERROR: couldn't find the '/data' route anchor in do_GET")

# ── 3. Save ───────────────────────────────────────────────────────────
if changed:
    shutil.copy(DASH, DASH.with_suffix(".py.bak_mobile"))
    DASH.write_text(src, encoding="utf-8")
    print("[ok]   dashboard.py updated (backup: dashboard.py.bak_mobile)")

for f in ("icon-192.png", "icon-512.png", "apple-touch-icon.png"):
    print(f"[{'ok' if Path(f).exists() else 'MISSING'}]   {f}")

print()
print("Done. Restart the dashboard, then on your phone (same WiFi):")
print("  1. Open  http://<your-PC-IP>:5000  (the dashboard prints this IP on startup)")
print("  2. Android Chrome: menu (3 dots) -> 'Add to Home screen'")
print("     iPhone Safari:  Share button   -> 'Add to Home Screen'")
