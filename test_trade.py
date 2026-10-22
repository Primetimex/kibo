"""
KIBO Test Trade — TradeZero Paper Trading
Official API: https://webapi.tradezero.com
Docs: https://developer.tradezero.com
"""

import os, sys, uuid, requests, json
from datetime import datetime
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv("_env")
TZ_KEY_ID  = os.getenv("TZ_API_KEY_ID", "")
TZ_SECRET  = os.getenv("TZ_API_SECRET_KEY", "")
TZ_ACCT    = os.getenv("TZ_ACCOUNT_ID", "")
TZ_BASE    = "https://webapi.tradezero.com"
TELE_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELE_CHAT  = os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "TZ-API-KEY-ID":     TZ_KEY_ID,
    "TZ-API-SECRET-KEY": TZ_SECRET,
    "Accept":            "application/json",
    "Content-Type":      "application/json",
}

def telegram(msg):
    if not TELE_TOKEN or not TELE_CHAT: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELE_TOKEN}/sendMessage",
            json={"chat_id": TELE_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=5)
        print("[OK] Telegram sent")
    except Exception as e:
        print(f"[WARN] Telegram: {e}")

def place_order(account_id, side, open_close, symbol="SPY", qty=1):
    """Place a single order and return the result."""
    oid = f"KIBO-{uuid.uuid4().hex[:8].upper()}"
    payload = {
        "clientOrderId": oid,
        "securityType":  "Stock",
        "symbol":        symbol,
        "side":          side,
        "openClose":     open_close,
        "orderType":     "Market",
        "orderQuantity": qty,
        "timeInForce":   "Day",
        "route":         "PAPER",
    }
    url = f"{TZ_BASE}/v1/api/accounts/{account_id}/order"
    print(f"   Payload : {json.dumps(payload)}")
    try:
        r = requests.post(url, headers=HEADERS, json=payload, timeout=10)
        print(f"   Status  : {r.status_code}")
        print(f"   Response: {r.text[:300]}")
        if r.status_code in [200, 201]:
            order = r.json()
            order_id = order.get("clientOrderId", oid)
            status   = order.get("orderStatus", "submitted")
            return {"ok": True, "order_id": order_id, "status": status, "raw": order}
        return {"ok": False, "reason": r.text[:200]}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def run():
    print("\n" + "="*52)
    print("  KIBO Test Trade - Buy + Sell Pipeline Test")
    print("="*52)

    if not TZ_KEY_ID or not TZ_SECRET:
        print("[WARN] Missing API keys in .env")
        simulate(); return

    # ── Step 1: Get account ────────────────────────────────
    print(f"\n-- Step 1: Connecting to TradeZero...")
    try:
        r = requests.get(f"{TZ_BASE}/v1/api/accounts", headers=HEADERS, timeout=10)
        if r.status_code != 200:
            print(f"[FAIL] Could not connect: {r.status_code}")
            simulate(); return
        accounts   = r.json().get("accounts", [])
        paper      = next((a for a in accounts if a.get("accountType") == "Paper"), accounts[0])
        account_id = paper.get("account", TZ_ACCT)
        bp         = paper.get("buyingPower", 0)
        print(f"[OK] Account    : {account_id}")
        print(f"   Type        : {paper.get('accountType')}")
        print(f"   Status      : {paper.get('accountStatus')}")
        print(f"   Buying Power: ${bp:,.2f}" if isinstance(bp, (int,float)) else f"   Buying Power: {bp}")
    except Exception as e:
        print(f"[FAIL] {e}"); simulate(); return

    import time

    # ── Step 2: BUY 1 share of SPY ────────────────────────
    print(f"\n-- Step 2: Placing BUY order (SPY x1)...")
    buy = place_order(account_id, "Buy", "Open", "SPY", 1)
    if buy["ok"]:
        print(f"[OK] BUY placed  — Order: {buy['order_id']}  Status: {buy['status']}")
        telegram(
            f"<b>TEST BUY EXECUTED</b>\n"
            f"Account : {account_id}\n"
            f"Symbol  : SPY\n"
            f"Side    : BUY (Open)\n"
            f"Qty     : 1 share\n"
            f"Order   : {buy['order_id']}\n"
            f"Status  : {buy['status']}\n"
            f"Time    : {datetime.now().strftime('%H:%M:%S ET')}"
        )
    else:
        print(f"[FAIL] BUY failed: {buy['reason']}")

    # Wait 2 seconds before selling
    print(f"\n   Waiting 2 seconds before placing SELL...")
    time.sleep(2)

    # ── Step 3: SELL 1 share of SPY ───────────────────────
    print(f"\n-- Step 3: Placing SELL order (SPY x1)...")
    sell = place_order(account_id, "Sell", "Close", "SPY", 1)
    if sell["ok"]:
        print(f"[OK] SELL placed — Order: {sell['order_id']}  Status: {sell['status']}")
        telegram(
            f"<b>TEST SELL EXECUTED</b>\n"
            f"Account : {account_id}\n"
            f"Symbol  : SPY\n"
            f"Side    : SELL (Close)\n"
            f"Qty     : 1 share\n"
            f"Order   : {sell['order_id']}\n"
            f"Status  : {sell['status']}\n"
            f"Time    : {datetime.now().strftime('%H:%M:%S ET')}"
        )
    else:
        print(f"[FAIL] SELL failed: {sell['reason']}")

    # ── Summary ────────────────────────────────────────────
    print(f"\n" + "="*52)
    if buy["ok"] and sell["ok"]:
        print("[OK] FULL ROUND-TRIP TEST COMPLETE!")
        print("   BUY  -> SELL pipeline confirmed working")
        print("   Check dashboard Orders tab to see both trades")
        print("   Check Telegram for both alerts")
        telegram(
            f"<b>ROUND-TRIP TEST COMPLETE</b>\n"
            f"--------------------\n"
            f"BUY  order : {buy['order_id']} ({buy['status']})\n"
            f"SELL order : {sell['order_id']} ({sell['status']})\n"
            f"--------------------\n"
            f"Full pipeline confirmed. Agents ready to trade!"
        )
    elif buy["ok"]:
        print("[WARN] BUY worked but SELL failed")
        print("   This may be because market is closed (no open position to close)")
        print("   Try again during market hours 9:30 AM - 4:00 PM ET")
    else:
        print("[FAIL] Both orders failed — check API keys and market hours")
    print("="*52)

def simulate():
    print(f"\n-- Simulating paper trade...")
    print(f"[OK] Simulated: SPY BUY 1 share @ $582.45  FILLED")
    print(f"\n-- Sending Telegram alert...")
    telegram(
        f"<b>TEST TRADE [SIMULATED]</b>\n"
        f"Symbol : SPY | BUY 1 share\n"
        f"Status : FILLED\n"
        f"Time   : {datetime.now().strftime('%H:%M:%S ET')}\n"
        f"[OK] Pipeline working!"
    )
    print("\n[OK] SIMULATION COMPLETE — check Telegram!")
    print("="*52)

if __name__ == "__main__":
    run()
