"""
KIBO Market Scanner — Full ICC Rules Engine
Scans top 500 stocks by volume every 2 minutes
Price range: $5 - $100
Signals: LONG (uptrend) + SHORT (downtrend)
Alerts: Dashboard + Telegram
"""

import os, sys, time, logging, requests, threading
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv("_env")

# ── Force UTF-8 console output (Windows cp1252 can't render emoji/arrows,
# which was spamming "Logging error" tracebacks on every alert) ─────────────
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
# ── Imports ────────────────────────────────────────────────────────────────────
try:
    from structure_rules import (
        classify_trend, check_entry_rules, detect_reversal,
        detect_indication, find_swings
    )
    ICC_AVAILABLE = True
except ImportError:
    ICC_AVAILABLE = False
    print("WARNING: structure_rules.py not found — ICC rules disabled")

# ── Config ─────────────────────────────────────────────────────────────────────
SCAN_INTERVAL   = 120          # scan every 2 minutes
PRICE_MIN       = 5.0          # min stock price
PRICE_MAX       = 100.0        # max stock price
MIN_VOLUME      = 500_000      # minimum daily volume
TOP_N_MOVERS    = 500          # how many stocks to pull from screener
MIN_RULES       = 6            # minimum ICC rules to pass for alert
BARS            = 80           # 5-min bars to fetch per stock
MAX_WORKERS     = 8            # parallel threads for fetching
RESULTS_FILE    = "scanner_results.json"

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("market_scanner.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("market_scanner")

# ── Telegram ───────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=8
        )
    except: pass

# ── Fetch top movers from Yahoo screener ──────────────────────────────────────
def fetch_universe():
    """Pull top stocks by volume from Yahoo Finance screeners."""
    symbols = set()
    headers = {"User-Agent": "Mozilla/5.0"}
    urls = [
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=250",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=250",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_losers&count=250",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
            for q in quotes:
                sym   = q.get("symbol", "")
                price = float(q.get("regularMarketPrice") or 0)
                vol   = int(q.get("regularMarketVolume") or 0)
                if not sym or "." in sym or len(sym) > 5:
                    continue
                if not (PRICE_MIN <= price <= PRICE_MAX):
                    continue
                if vol < MIN_VOLUME:
                    continue
                symbols.add(sym)
            time.sleep(0.5)
        except Exception as e:
            log.warning(f"Screener fetch failed: {e}")
    log.info(f"Universe: {len(symbols)} stocks in $${PRICE_MIN}-${PRICE_MAX} range")
    return list(symbols)

# ── Fetch 5-min bars from Yahoo Finance ───────────────────────────────────────
def fetch_bars(sym, bars=BARS):
    """Fetch 5-minute OHLCV bars from Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, headers=headers,
                         params={"interval": "5m", "range": "5d"}, timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        res    = result[0]
        ts     = res.get("timestamp", [])
        ohlcv  = res.get("indicators", {}).get("quote", [{}])[0]
        if not ts or len(ts) < 20:
            return None
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(ts, unit="s"),
            "open":   ohlcv.get("open",   []),
            "high":   ohlcv.get("high",   []),
            "low":    ohlcv.get("low",    []),
            "close":  ohlcv.get("close",  []),
            "volume": ohlcv.get("volume", []),
        })
        df = df.dropna(subset=["close", "open", "high", "low"])
        if len(df) < 20:
            return None
        return df.tail(bars).reset_index(drop=True)
    except:
        return None

# ── Run ICC analysis on a single stock ────────────────────────────────────────
def analyze_stock(sym):
    """Run full ICC rule check on a stock. Returns result dict."""
    if not ICC_AVAILABLE:
        return None
    try:
        df = fetch_bars(sym)
        if df is None:
            return None

        price     = float(df["close"].iloc[-1])
        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None

        trend     = classify_trend(df)
        rev       = detect_reversal(df, trend["trend"])
        ind       = detect_indication(df)
        sc_long   = check_entry_rules(df, "LONG")
        sc_short  = check_entry_rules(df, "SHORT")
        best      = sc_long if sc_long.get("rr", 0) >= sc_short.get("rr", 0) else sc_short
        direction = "LONG" if best == sc_long else "SHORT"

        rules = {
            "trend_ok":   trend["trend"] in ("UPTREND", "DOWNTREND"),
            "not_cons":   trend["trend"] != "CONSOLIDATION",
            "indication": ind.get("indication", False),
            "session":    True,
            "stop_ok":    bool(best.get("stop_loss")),
            "target_ok":  bool(best.get("target")),
            "rr_ok":      best.get("rr", 0) >= 1.5,
            "no_rev":     not rev["reversal_likely"],
            "dir_match":  (direction == "LONG" and trend["trend"] == "UPTREND") or
                          (direction == "SHORT" and trend["trend"] == "DOWNTREND"),
        }
        passed   = int(sum(rules.values()))
        approved = bool(best.get("approved", False))

        if passed < 3:  # skip low-score stocks to save memory
            return None

        return {
            "sym":       sym,
            "price":     round(float(price), 2),
            "trend":     trend["trend"],
            "conf":      int(round(float(trend["confidence"]) * 100)),
            "direction": direction,
            "rules":     {k: bool(v) for k, v in rules.items()},
            "passed":    passed,
            "approved":  approved,
            "stop":      round(float(best["stop_loss"]), 2) if best.get("stop_loss") else None,
            "target":    round(float(best["target"]), 2) if best.get("target") else None,
            "rr":        round(float(best.get("rr", 0)), 2),
            "reason":    str(best.get("reason", "")),
            "ts":        datetime.now().strftime("%H:%M:%S ET"),
            "error":     False,
        }
    except Exception as e:
        return None

# ── Parallel scanner ──────────────────────────────────────────────────────────
def scan_batch(symbols):
    """Scan a batch of symbols using thread pool."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(analyze_stock, sym): sym for sym in symbols}
        for future in as_completed(futures):
            try:
                r = future.result(timeout=15)
                if r:
                    results.append(r)
            except:
                pass
    return results

# ── Alert on new approvals ────────────────────────────────────────────────────
_alerted = set()

def check_alerts(results):
    """Send Telegram alert for newly approved setups."""
    for r in results:
        if not r.get("approved"):
            continue
        key = f"{r['sym']}-{r['direction']}-{datetime.now().strftime('%H:%M')}"
        if key in _alerted:
            continue
        _alerted.add(key)
        emoji = "📈" if r["direction"] == "LONG" else "📉"
        msg = (
            f"{emoji} <b>ICC APPROVED — {r['sym']}</b>\n"
            f"Direction : {r['direction']}\n"
            f"Price     : ${r['price']}\n"
            f"Trend     : {r['trend']} {r['conf']}%\n"
            f"Rules     : {r['passed']}/9\n"
            f"Stop      : ${r['stop']}\n"
            f"Target    : ${r['target']}\n"
            f"R:R       : {r['rr']}:1\n"
            f"Time      : {r['ts']}"
        )
        send_telegram(msg)
        log.info(f"🚨 APPROVED: {r['sym']} {r['direction']} @ ${r['price']} — {r['passed']}/9 rules")

# ── Save results to file for dashboard ────────────────────────────────────────
def save_results(results, updated, scan_time):
    import json
    data = {
        "results":   results,
        "updated":   updated,
        "scan_time": scan_time,
        "total":     len(results),
        "approved":  len([r for r in results if r.get("approved")]),
        "strong":    len([r for r in results if r.get("passed", 0) >= MIN_RULES]),
    }
    Path(RESULTS_FILE).write_text(json.dumps(data))

# ── Main scan loop ─────────────────────────────────────────────────────────────
def main():
    log.info("=" * 65)
    log.info("  KIBO Market Scanner — Full ICC Engine")
    log.info(f"  Price range : ${PRICE_MIN} – ${PRICE_MAX}")
    log.info(f"  Scan every  : {SCAN_INTERVAL}s")
    log.info(f"  Min rules   : {MIN_RULES}/9 to alert")
    log.info(f"  Threads     : {MAX_WORKERS} parallel")
    log.info("=" * 65)

    if not ICC_AVAILABLE:
        log.error("structure_rules.py not found — cannot run ICC analysis")
        return

    scan_count = 0
    while True:
        try:
            scan_start = time.time()
            scan_count += 1
            log.info(f"── Scan #{scan_count} starting ──────────────────────────────────")

            # 1. Fetch universe
            symbols = fetch_universe()
            if not symbols:
                log.warning("No symbols fetched — retrying in 30s")
                time.sleep(30)
                continue

            log.info(f"Scanning {len(symbols)} stocks...")

            # 2. Scan in parallel batches
            results = scan_batch(symbols)

            # 3. Sort: approved first, then by rules passed, then conf
            results.sort(
                key=lambda x: (x.get("approved", False), x.get("passed", 0), x.get("conf", 0)),
                reverse=True
            )

            # 4. Keep top 100 results
            results = results[:100]

            elapsed = round(time.time() - scan_start, 1)
            updated = datetime.now().strftime("%H:%M:%S ET")
            approved_count = len([r for r in results if r.get("approved")])
            strong_count   = len([r for r in results if r.get("passed", 0) >= MIN_RULES])

            log.info(f"Scan #{scan_count} complete in {elapsed}s — {len(results)} results, "
                     f"{approved_count} approved, {strong_count} strong ({MIN_RULES}+/9)")

            # 5. Alert on approvals
            check_alerts(results)

            # 6. Save for dashboard
            save_results(results, updated, elapsed)

            # 7. Log top 5
            for r in results[:5]:
                status = "✅ APPROVED" if r["approved"] else f"{r['passed']}/9"
                log.info(f"  {r['sym']:<6} {r['direction']:<5} ${r['price']:<8} "
                         f"{r['trend']:<14} {r['conf']}% conf  {status}")

            # 8. Wait for next scan
            wait = max(5, SCAN_INTERVAL - elapsed)
            log.info(f"Next scan in {wait:.0f}s...")
            time.sleep(wait)

        except KeyboardInterrupt:
            log.info("Scanner stopped.")
            break
        except Exception as e:
            log.error(f"Scan loop error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
