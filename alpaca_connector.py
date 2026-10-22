"""
KIBO Alpaca Connector
Handles all order execution for Futures, ORB, and Crypto agents
via Alpaca paper trading API.

Supports:
  - Stocks (SPY, QQQ, GLD, USO)
  - Crypto (BTC/USD, ETH/USD, SOL/USD etc)
  - ETF proxies for futures (GLD=MGC, USO=MCL, SPY=MES, QQQ=MNQ)
"""

import os, requests, uuid, logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv("_env")
ALPACA_KEY    = os.getenv("ALPACA_API_KEY",    "PKPRJSUV6BTTGY75UZ2JQFFJRW")
ALPACA_SECRET = os.getenv("ALPACA_API_SECRET", "7w8GYKdtCStJCZwh92Ww7aydwmqebctg9xpZYex4C4pb")
ALPACA_BASE   = os.getenv("ALPACA_BASE_URL",   "https://paper-api.alpaca.markets")

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json",
}

log = logging.getLogger(__name__)

# ── Symbol mapping ────────────────────────────────────────────────────────────
# Futures → ETF proxies (Alpaca stocks)
FUTURES_MAP = {
    "MGC":    "GLD",    # Micro Gold → SPDR Gold ETF
    "MCL":    "USO",    # Micro Crude → US Oil Fund
    "MES":    "SPY",    # Micro S&P → SPY ETF
    "MNQ":    "QQQ",    # Micro Nasdaq → QQQ ETF
    "XAUUSD": "GLD",    # Gold spot → GLD
    "GLD":    "GLD",
    "USO":    "USO",
    "SPY":    "SPY",
    "QQQ":    "QQQ",
}

# Crypto symbols for Alpaca (uses /USD pairs)
CRYPTO_MAP = {
    "BTC":  "BTC/USD",
    "ETH":  "ETH/USD",
    "SOL":  "SOL/USD",
    "AVAX": "AVAX/USD",
    "LINK": "LINK/USD",
    "LTC":  "LTC/USD",
    "DOGE": "DOGE/USD",
    "XRP":  "XRP/USD",
    "BNB":  "BNB/USD",
}

def get_account():
    """Get Alpaca paper account info."""
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/account", headers=HEADERS, timeout=8)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        log.error(f"Alpaca account error: {e}")
        return None

def place_order(symbol, side, qty, order_type="market", limit_price=None):
    """
    Place an order on Alpaca paper account.
    
    symbol: ticker (e.g. "BTC/USD", "GLD", "SPY")
    side: "buy" or "sell"
    qty: number of shares/units (use notional for crypto)
    """
    client_id = f"KIBO-A-{uuid.uuid4().hex[:8].upper()}"

    # Determine if crypto
    is_crypto = "/" in symbol

    payload = {
        "symbol":        symbol,
        "side":          side.lower(),
        "type":          order_type,
        "time_in_force": "gtc" if is_crypto else "day",
        "client_order_id": client_id,
    }

    if order_type == "market":
        if is_crypto:
            # Crypto uses notional (dollar amount) or qty
            payload["qty"] = str(round(qty, 6))
        else:
            payload["qty"] = str(int(qty))
    elif order_type == "limit" and limit_price:
        payload["qty"]         = str(int(qty))
        payload["limit_price"] = str(round(limit_price, 4))

    try:
        r = requests.post(
            f"{ALPACA_BASE}/v2/orders",
            headers=HEADERS, json=payload, timeout=10
        )
        if r.status_code in (200, 201):
            resp = r.json()
            log.info(f"[ALPACA] {side.upper()} {qty} {symbol} — order {resp.get('id','?')[:8]} status={resp.get('status','?')}")
            return True, resp
        else:
            log.error(f"[ALPACA] Order rejected {symbol}: {r.status_code} {r.text[:150]}")
            return False, r.text
    except Exception as e:
        log.error(f"[ALPACA] Order error {symbol}: {e}")
        return False, str(e)

def close_position(symbol):
    """Close entire position for a symbol."""
    try:
        r = requests.delete(
            f"{ALPACA_BASE}/v2/positions/{symbol.replace('/','%2F')}",
            headers=HEADERS, timeout=10
        )
        if r.status_code in (200, 201, 204):
            log.info(f"[ALPACA] Position closed: {symbol}")
            return True
        else:
            log.error(f"[ALPACA] Close failed {symbol}: {r.status_code} {r.text[:100]}")
            return False
    except Exception as e:
        log.error(f"[ALPACA] Close error {symbol}: {e}")
        return False

def get_positions():
    """Get all open positions."""
    try:
        r = requests.get(f"{ALPACA_BASE}/v2/positions", headers=HEADERS, timeout=8)
        if r.status_code == 200:
            return r.json()
        return []
    except:
        return []

def get_orders(status="open"):
    """Get orders."""
    try:
        r = requests.get(
            f"{ALPACA_BASE}/v2/orders",
            headers=HEADERS,
            params={"status": status, "limit": 50},
            timeout=8
        )
        if r.status_code == 200:
            return r.json()
        return []
    except:
        return []

def futures_buy(symbol, contracts=1, entry=None):
    """Buy futures via ETF proxy."""
    etf = FUTURES_MAP.get(symbol, symbol)
    shares = max(contracts * 5, 1)
    ok, resp = place_order(etf, "buy", shares)
    if ok:
        log.info(f"[ALPACA FUTURES] BUY {contracts}x {symbol} → {shares}x {etf}")
    return ok, resp

def futures_sell(symbol, contracts=1):
    """Sell/short futures via ETF proxy."""
    etf = FUTURES_MAP.get(symbol, symbol)
    shares = max(contracts * 5, 1)
    ok, resp = place_order(etf, "sell", shares)
    if ok:
        log.info(f"[ALPACA FUTURES] SELL {contracts}x {symbol} → {shares}x {etf}")
    return ok, resp

def crypto_buy(symbol, usd_amount=100):
    """Buy crypto — uses dollar amount."""
    alpaca_sym = CRYPTO_MAP.get(symbol, f"{symbol}/USD")
    # Convert USD to approximate quantity
    try:
        r = requests.get(
            f"https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes",
            headers=HEADERS,
            params={"symbols": alpaca_sym},
            timeout=6
        )
        if r.status_code == 200:
            quote = r.json().get("quotes",{}).get(alpaca_sym,{})
            price = float(quote.get("ap", 1))
            qty   = round(usd_amount / price, 6) if price > 0 else 0.001
        else:
            qty = 0.001
    except:
        qty = 0.001

    ok, resp = place_order(alpaca_sym, "buy", qty)
    if ok:
        log.info(f"[ALPACA CRYPTO] BUY {qty} {alpaca_sym}")
    return ok, resp

def crypto_sell(symbol, qty=None):
    """Sell/close crypto position."""
    alpaca_sym = CRYPTO_MAP.get(symbol, f"{symbol}/USD")
    if qty:
        ok, resp = place_order(alpaca_sym, "sell", qty)
    else:
        ok = close_position(alpaca_sym)
        resp = {}
    if ok:
        log.info(f"[ALPACA CRYPTO] SELL {alpaca_sym}")
    return ok, resp

def test_connection():
    """Test Alpaca connection and print account info."""
    print("Testing Alpaca connection...")
    acct = get_account()
    if acct:
        print(f"✅ Connected to Alpaca Paper Trading")
        print(f"   Account ID : {acct.get('id','?')[:12]}...")
        print(f"   Cash       : ${float(acct.get('cash',0)):,.2f}")
        print(f"   Buying Pwr : ${float(acct.get('buying_power',0)):,.2f}")
        print(f"   Status     : {acct.get('status','?')}")
        return True
    else:
        print("❌ Connection failed — check API keys")
        return False

if __name__ == "__main__":
    test_connection()

    # Test a small order
    print("\nTesting small stock order (1 share SPY)...")
    ok, resp = place_order("SPY", "buy", 1)
    if ok:
        print(f"✅ Order placed: {resp.get('id','?')[:12]}... status={resp.get('status','?')}")
    else:
        print(f"❌ Order failed: {resp}")
