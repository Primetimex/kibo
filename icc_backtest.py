"""
KIBO ICC Backtest Engine
Tests all 14 ICC rules against 90 days of historical data.
Run: python icc_backtest.py
Outputs: backtest_results.json + prints summary
"""
import os, json, time, requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv("_env")
POLYGON_KEY = os.getenv("POLYGON_API_KEY","")

# Import ICC rules
import sys
sys.path.insert(0, os.path.dirname(__file__))
from structure_rules import check_entry_rules, classify_trend

# Symbols to backtest
SYMBOLS = {
    "crypto": {
        "BTC": "X:BTCUSD", "ETH": "X:ETHUSD", "SOL": "X:SOLUSD",
    },
    "futures": {
        "MGC": "GLD", "MES": "SPY", "MNQ": "QQQ",
    },
    "stocks": {
        "SPY": "SPY", "QQQ": "QQQ", "NVDA": "NVDA",
    }
}

LOOKBACK_DAYS   = 90   # backtest window
MIN_BARS_SIGNAL = 30   # need this many bars before taking a signal
RESULTS_FILE    = "backtest_results.json"

def get_historical_bars(ticker, days=90):
    end   = datetime.now()
    start = end - timedelta(days=days)
    url   = (f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/5/minute"
             f"/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
             f"?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_KEY}")
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 429:
                print(f"  Rate limited, waiting 20s...")
                time.sleep(20)
                continue
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            if not results:
                return pd.DataFrame()
            df = pd.DataFrame(results)
            df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
            df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"}, inplace=True)
            print(f"  {ticker}: {len(df)} bars fetched")
            time.sleep(13)  # rate limit
            return df[["timestamp","open","high","low","close","volume"]].reset_index(drop=True)
        except Exception as e:
            print(f"  Error fetching {ticker}: {e}")
            time.sleep(5)
    return pd.DataFrame()

def calc_indicators(df):
    df = df.copy()
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, min_periods=14).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"]    = 100 - (100 / (1 + rs))
    df["ema9"]   = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"]  = df["close"].ewm(span=21, adjust=False).mean()
    prev         = df["close"].shift(1)
    tr           = pd.concat([df["high"]-df["low"],(df["high"]-prev).abs(),(df["low"]-prev).abs()],axis=1).max(axis=1)
    df["atr"]    = tr.ewm(span=14, adjust=False).mean()
    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg"]
    macd_fast    = df["close"].ewm(span=12, adjust=False).mean()
    macd_slow    = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]   = macd_fast - macd_slow
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    return df.dropna()

def run_backtest_symbol(sym, ticker, category):
    print(f"\nBacktesting {sym} ({ticker})...")
    df_raw = get_historical_bars(ticker)
    if df_raw.empty or len(df_raw) < 100:
        print(f"  Skipping {sym} — insufficient data")
        return None

    df = calc_indicators(df_raw)
    trades = []

    for i in range(MIN_BARS_SIGNAL, len(df)):
        window = df.iloc[:i+1].copy()
        row    = window.iloc[-1]
        prev   = window.iloc[-2]

        # Basic momentum signal
        vol_ok = row["vol_ratio"] >= 1.3
        long_signal  = (50 <= row["rsi"] <= 72 and row["ema9"] > row["ema21"] and
                        row["macd"] > row["macd_sig"] and prev["macd"] <= prev["macd_sig"] and vol_ok)
        short_signal = (28 <= row["rsi"] <= 50 and row["ema9"] < row["ema21"] and
                        row["macd"] < row["macd_sig"] and prev["macd"] >= prev["macd_sig"] and vol_ok)

        if not long_signal and not short_signal:
            continue

        direction = "LONG" if long_signal else "SHORT"

        # Run full ICC rules
        try:
            sc = check_entry_rules(window, direction)
        except:
            continue

        if not sc.get("approved"):
            continue

        entry  = row["close"]
        stop   = sc.get("stop_loss") or (entry - row["atr"]*2 if direction=="LONG" else entry + row["atr"]*2)
        target = sc.get("target")    or (entry + row["atr"]*4 if direction=="LONG" else entry - row["atr"]*4)
        rr     = sc.get("rr", 0)

        if rr < 1.5:
            continue

        # Simulate trade outcome on next 60 bars
        future = df.iloc[i+1:i+61]
        if future.empty:
            continue

        outcome    = "OPEN"
        exit_price = entry
        exit_bar   = None

        for _, frow in future.iterrows():
            if direction == "LONG":
                if frow["low"] <= stop:
                    outcome    = "LOSS"
                    exit_price = stop
                    exit_bar   = frow["timestamp"]
                    break
                if frow["high"] >= target:
                    outcome    = "WIN"
                    exit_price = target
                    exit_bar   = frow["timestamp"]
                    break
            else:
                if frow["high"] >= stop:
                    outcome    = "LOSS"
                    exit_price = stop
                    exit_bar   = frow["timestamp"]
                    break
                if frow["low"] <= target:
                    outcome    = "WIN"
                    exit_price = target
                    exit_bar   = frow["timestamp"]
                    break

        if outcome == "OPEN":
            exit_price = future.iloc[-1]["close"]
            pnl_raw    = (exit_price - entry) if direction == "LONG" else (entry - exit_price)
            outcome    = "WIN" if pnl_raw > 0 else "LOSS"

        pnl_pct = ((exit_price - entry) / entry * 100) if direction == "LONG" else ((entry - exit_price) / entry * 100)

        trades.append({
            "symbol":    sym,
            "category":  category,
            "direction": direction,
            "entry":     round(entry, 4),
            "stop":      round(stop, 4),
            "target":    round(target, 4),
            "exit":      round(exit_price, 4),
            "rr":        round(rr, 2),
            "icc_score": sc.get("reason","")[:60],
            "outcome":   outcome,
            "pnl_pct":   round(pnl_pct, 2),
            "open_time": str(row["timestamp"]),
            "close_time": str(exit_bar) if exit_bar else None,
        })

    print(f"  {sym}: {len(trades)} ICC-approved trades found")
    return trades

def print_summary(all_trades):
    if not all_trades:
        print("\n❌ No trades found in backtest period")
        return

    wins   = [t for t in all_trades if t["outcome"] == "WIN"]
    losses = [t for t in all_trades if t["outcome"] == "LOSS"]
    pnls   = [t["pnl_pct"] for t in all_trades]

    print("\n" + "="*58)
    print("  KIBO ICC BACKTEST RESULTS")
    print("="*58)
    print(f"  Period      : Last {LOOKBACK_DAYS} days")
    print(f"  Symbols     : {len(SYMBOLS['crypto'])+len(SYMBOLS['futures'])+len(SYMBOLS['stocks'])} pairs")
    print(f"  Total trades: {len(all_trades)}")
    print(f"  Wins        : {len(wins)}  ({len(wins)/len(all_trades)*100:.1f}%)")
    print(f"  Losses      : {len(losses)}  ({len(losses)/len(all_trades)*100:.1f}%)")
    print(f"  Total P&L   : {sum(pnls):+.2f}%")
    print(f"  Avg Win     : +{sum(t['pnl_pct'] for t in wins)/len(wins):.2f}%" if wins else "  Avg Win     : —")
    print(f"  Avg Loss    : {sum(t['pnl_pct'] for t in losses)/len(losses):.2f}%" if losses else "  Avg Loss    : —")
    print(f"  Best trade  : +{max(pnls):.2f}%")
    print(f"  Worst trade : {min(pnls):.2f}%")
    print("="*58)

    # Per symbol breakdown
    syms = sorted(set(t["symbol"] for t in all_trades))
    print(f"\n  {'Symbol':<8} {'Trades':>6} {'Win%':>6} {'P&L':>8}")
    print(f"  {'─'*34}")
    for sym in syms:
        st = [t for t in all_trades if t["symbol"]==sym]
        sw = [t for t in st if t["outcome"]=="WIN"]
        sp = sum(t["pnl_pct"] for t in st)
        wr = len(sw)/len(st)*100 if st else 0
        print(f"  {sym:<8} {len(st):>6} {wr:>5.0f}% {sp:>+8.2f}%")
    print()

def main():
    print("KIBO ICC Backtest Engine")
    print(f"Testing last {LOOKBACK_DAYS} days across 9 symbols...\n")

    all_trades = []
    for category, symbols in SYMBOLS.items():
        for sym, ticker in symbols.items():
            result = run_backtest_symbol(sym, ticker, category)
            if result:
                all_trades.extend(result)

    # Save results
    with open(RESULTS_FILE, "w") as f:
        json.dump({
            "run_date": datetime.now().isoformat(),
            "days": LOOKBACK_DAYS,
            "trades": all_trades,
            "stats": {
                "total": len(all_trades),
                "wins": len([t for t in all_trades if t["outcome"]=="WIN"]),
                "losses": len([t for t in all_trades if t["outcome"]=="LOSS"]),
                "win_rate": round(len([t for t in all_trades if t["outcome"]=="WIN"])/len(all_trades)*100,1) if all_trades else 0,
                "total_pnl_pct": round(sum(t["pnl_pct"] for t in all_trades),2) if all_trades else 0,
            }
        }, f, indent=2)

    print_summary(all_trades)
    print(f"  Full results saved to {RESULTS_FILE}")
    print(f"  View in dashboard → Backtest tab")

if __name__ == "__main__":
    main()
