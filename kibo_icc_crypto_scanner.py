"""
KIBO ICC Live Rule Checker — 24hr Crypto Markets
Scans all major crypto pairs against full ICC rules every 60s
"""

import os
import sys
import time
import requests
from datetime import datetime
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from structure_rules import (
    classify_trend, check_entry_rules, detect_indication,
    detect_reversal, is_in_session, find_swings
)

POLYGON_KEY = os.getenv("POLYGON_API_KEY", "")

SYMBOLS = {
    "BTC":  "X:BTCUSD",
    "ETH":  "X:ETHUSD",
    "SOL":  "X:SOLUSD",
    "BNB":  "X:BNBUSD",
    "XRP":  "X:XRPUSD",
    "DOGE": "X:DOGEUSD",
    "AVAX": "X:AVAXUSD",
    "LINK": "X:LINKUSD",
    "LTC":  "X:LTCUSD",
}

def get_bars(ticker, bars=80):
    today = datetime.now().strftime("%Y-%m-%d")
    url = (f"https://api.polygon.io/v2/aggs/ticker/{ticker}"
           f"/range/5/minute/2024-01-01/{today}")
    r = requests.get(url, params={
        "adjusted": True, "sort": "asc",
        "limit": bars, "apiKey": POLYGON_KEY
    }, timeout=10)
    data = r.json()
    if "results" not in data or len(data["results"]) < 20:
        return None
    df = pd.DataFrame(data["results"])
    df.rename(columns={"o":"open","h":"high","l":"low",
                        "c":"close","v":"volume","t":"timestamp"}, inplace=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.tail(bars)

def analyze(sym, ticker):
    df = get_bars(ticker)
    if df is None:
        return {"sym": sym, "error": True}

    price       = df["close"].iloc[-1]
    trend       = classify_trend(df)
    highs, lows = find_swings(df)
    ind         = detect_indication(df)
    rev         = detect_reversal(df, trend["trend"])

    # BUGFIX: direction used to be picked by whichever side (LONG/SHORT) had
    # the better R:R, ignoring trend entirely. Since a blocked side's R:R
    # defaults to 0 and ties resolve to LONG, this constantly produced
    # "LONG signal, DOWNTREND structure" rows where dir_match always failed —
    # pure noise. Direction now follows the trend directly.
    if trend["trend"] == "UPTREND":
        direction = "LONG"
        best      = check_entry_rules(df, "LONG")
    elif trend["trend"] == "DOWNTREND":
        direction = "SHORT"
        best      = check_entry_rules(df, "SHORT")
    else:
        direction = "NONE"
        best      = {"approved": False, "reason": "CONSOLIDATION — no-trade zone, ICC rule: don't trade sideways structure.",
                     "stop_loss": None, "target": None, "rr": 0}

    rules = {
        "trend_ok":   trend["trend"] in ("UPTREND", "DOWNTREND"),
        "not_cons":   trend["trend"] != "CONSOLIDATION",
        "indication": ind.get("indication", False),
        "session":    True,  # crypto 24/7
        "stop_ok":    bool(best.get("stop_loss")),
        "target_ok":  bool(best.get("target")),
        "rr_ok":      best.get("rr", 0) >= 1.5,
        "no_rev":     not rev["reversal_likely"],
        "dir_match":  (direction == "LONG" and trend["trend"] == "UPTREND") or
                      (direction == "SHORT" and trend["trend"] == "DOWNTREND"),
    }

    passed   = sum(rules.values())
    approved = best.get("approved", False)

    return {
        "sym":       sym,
        "price":     price,
        "trend":     trend["trend"],
        "conf":      trend["confidence"],
        "direction": direction,
        "rules":     rules,
        "passed":    passed,
        "total":     9,
        "approved":  approved,
        "stop":      best.get("stop_loss"),
        "target":    best.get("target"),
        "rr":        best.get("rr", 0),
        "reason":    best.get("reason", ""),
        "rev_warn":  rev["reason"] if rev["reversal_likely"] else "",
        "error":     False,
    }

RULE_LABELS = [
    ("trend_ok",   "Trend structure"),
    ("not_cons",   "Not consolidation"),
    ("indication", "Indication detected"),
    ("session",    "Session active (24/7)"),
    ("stop_ok",    "Stop loss placeable"),
    ("target_ok",  "Target identified"),
    ("rr_ok",      "R:R >= 1.5"),
    ("no_rev",     "No reversal warning"),
    ("dir_match",  "Trade with trend"),
]

def print_summary(results):
    os.system("cls" if os.name == "nt" else "clear")
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n  🟢  KIBO ICC Crypto Scanner  |  {now} ET  |  {len(results)} pairs\n")
    print(f"  {'─'*70}")
    print(f"  {'Pair':<6}  {'Price':>12}  {'Trend':<14}  {'Rules':>6}  {'Direction':<8}  {'R:R':>5}  Status")
    print(f"  {'─'*70}")

    results.sort(key=lambda x: (x.get("approved", False), x.get("passed", 0)), reverse=True)

    for r in results:
        if r.get("error"):
            print(f"  {r['sym']:<6}  {'no data':>12}")
            continue
        status = "🟢 APPROVED" if r["approved"] else f"🔴 {r['passed']}/9"
        trend_str = f"{r['trend'][:11]:<11} {r['conf']*100:.0f}%"
        rr_str = f"{r['rr']:.1f}" if r["rr"] > 0 else " n/a"
        price_str = f"${r['price']:,.2f}" if r["price"] < 10000 else f"${r['price']:,.0f}"
        print(f"  {r['sym']:<6}  {price_str:>12}  {trend_str:<14}  {r['passed']:>3}/9   {r['direction']:<8}  {rr_str:>5}  {status}")

    print(f"  {'─'*70}")

    top = [r for r in results if not r.get("error")]
    if not top:
        return

    detail = next((r for r in top if r["approved"]), top[0])

    print(f"\n  📋  Detail: {detail['sym']}  ${detail['price']:,.2f}  —  {'✅ TRADE READY' if detail['approved'] else 'Not ready yet'}\n")
    for key, label in RULE_LABELS:
        icon = "  ✅" if detail["rules"][key] else "  ❌"
        print(f"{icon}  {label}")

    if detail["approved"]:
        print(f"\n  Entry : ${detail['price']:,.2f}")
        print(f"  Stop  : ${detail['stop']:,.2f}")
        print(f"  Target: ${detail['target']:,.2f}")
        print(f"  R:R   : {detail['rr']:.1f}:1")
    else:
        print(f"\n  Blocked: {detail['reason'][:65]}")
        if detail["rev_warn"]:
            print(f"  Reversal watch: {detail['rev_warn'][:65]}")

    print(f"\n  Refreshing every 60s — Ctrl+C to stop\n")

def main():
    if not POLYGON_KEY:
        print("❌  Set POLYGON_API_KEY in your .env file first")
        return

    print("\n  Loading KIBO ICC Crypto Scanner...")

    while True:
        results = []
        for sym, ticker in SYMBOLS.items():
            try:
                print(f"  Scanning {sym}...", end="\r")
                results.append(analyze(sym, ticker))
                time.sleep(2)  # avoid rate limits
            except Exception:
                results.append({"sym": sym, "error": True})

        print_summary(results)
        time.sleep(60)

if __name__ == "__main__":
    main()
