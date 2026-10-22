"""Test Alpaca paper trading connection."""
import sys
sys.path.insert(0, '.')
from alpaca_connector import test_connection, place_order, crypto_buy, futures_buy

print("=" * 50)
test_connection()

print("\nTesting stock order (1 share GLD)...")
ok, resp = place_order("GLD", "buy", 1)
print(f"{'✅' if ok else '❌'} GLD: {resp.get('status','?') if isinstance(resp,dict) else resp}")

print("\nTesting crypto order ($10 BTC)...")
ok, resp = crypto_buy("BTC", usd_amount=10)
print(f"{'✅' if ok else '❌'} BTC: {resp.get('status','?') if isinstance(resp,dict) else resp}")

print("\nDone — check app.alpaca.markets/paper to see orders")
