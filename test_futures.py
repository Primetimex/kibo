"""Test if TradeZero paper account accepts micro futures orders."""
import os, requests
from dotenv import load_dotenv
load_dotenv("_env")
KEY_ID     = os.getenv("TZ_API_KEY_ID")
SECRET_KEY = os.getenv("TZ_API_SECRET_KEY")
ACCOUNT_ID = os.getenv("TZ_ACCOUNT_ID")
BASE_URL   = "https://webapi.tradezero.com"

HEADERS = {
    "TZ-API-KEY-ID":     KEY_ID,
    "TZ-API-SECRET-KEY": SECRET_KEY,
    "Content-Type":      "application/json",
}

def test_futures(symbol, security_type="Future"):
    import uuid
    payload = {
        "clientOrderId": f"KIBO-TEST-{uuid.uuid4().hex[:6].upper()}",
        "securityType":  security_type,
        "symbol":        symbol,
        "side":          "Buy",
        "openClose":     "Open",
        "orderType":     "Market",
        "orderQuantity": 1,
        "timeInForce":   "Day",
        "route":         "PAPER",
    }
    print(f"\nTesting {symbol} ({security_type})...")
    print(f"Payload: {payload}")
    try:
        r = requests.post(
            f"{BASE_URL}/v1/api/accounts/{ACCOUNT_ID}/order",
            headers=HEADERS, json=payload, timeout=10
        )
        print(f"Status: {r.status_code}")
        print(f"Response: {r.text[:300]}")
        return r.status_code == 200
    except Exception as e:
        print(f"Error: {e}")
        return False

# Test each micro futures symbol with different security types
for sym, sec_type in [
    ("MGC", "Future"),
    ("MES", "Future"), 
    ("MNQ", "Future"),
    ("MCL", "Future"),
    ("MGC", "Stock"),   # fallback test
]:
    test_futures(sym, sec_type)

