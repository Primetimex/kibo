"""
KIBO Crypto Scanner — Full ICC Rules Engine
Data: Binance (free, no API key required)
Scans 30 pairs every 2 minutes, 24/7
Output: crypto_scanner_results.json
"""

import os, sys, time, logging, requests
import pandas as pd
import numpy as np
from datetime import datetime
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
SCAN_INTERVAL  = 120
MIN_RULES      = 6
BARS           = 80
RESULTS_FILE   = "crypto_scanner_results.json"

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Binance symbol map ─────────────────────────────────────────────────────────
# sym -> Binance trading pair
SYMBOLS = {
    "BTC":    "BTCUSDT",
    "ETH":    "ETHUSDT",
    "BNB":    "BNBUSDT",
    "SOL":    "SOLUSDT",
    "XRP":    "XRPUSDT",
    "DOGE":   "DOGEUSDT",
    "ADA":    "ADAUSDT",
    "AVAX":   "AVAXUSDT",
    "LINK":   "LINKUSDT",
    "DOT":    "DOTUSDT",
    "LTC":    "LTCUSDT",
    "MATIC":  "MATICUSDT",
    "UNI":    "UNIUSDT",
    "ATOM":   "ATOMUSDT",
    "NEAR":   "NEARUSDT",
    "APT":    "APTUSDT",
    "ARB":    "ARBUSDT",
    "OP":     "OPUSDT",
    "INJ":    "INJUSDT",
    "SUI":    "SUIUSDT",
    "FET":    "FETUSDT",
    "RENDER": "RENDERUSDT",
    "WIF":    "WIFUSDT",
    "PEPE":   "PEPEUSDT",
    "SHIB":   "SHIBUSDT",
    "FIL":    "FILUSDT",
    "TIA":    "TIAUSDT",
    "SEI":    "SEIUSDT",
    "WLD":    "WLDUSDT",
    "FLOKI":  "FLOKIUSDT",
}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("crypto_scanner.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("crypto_scanner")

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

# ── Fetch bars from Binance (no API key needed) ────────────────────────────────
def fetch_bars(binance_sym, bars=BARS):
    for endpoint in ["https://api.binance.us/api/v3/klines",
                     "https://api.binance.com/api/v3/klines"]:
        try:
            r = requests.get(endpoint, params={
                "symbol": binance_sym, "interval": "5m", "limit": bars
            }, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            if not data or len(data) < 20:
                return None
            df = pd.DataFrame(data, columns=[
                "timestamp","open","high","low","close","volume",
                "close_time","quote_vol","trades","taker_base","taker_quote","ignore"
            ])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            for col in ["open","high","low","close","volume"]:
                df[col] = df[col].astype(float)
            return df[["timestamp","open","high","low","close","volume"]].reset_index(drop=True)
        except Exception as e:
            log.debug(f"Binance fetch error {binance_sym}: {e}")
            continue
    return None

# ── Analyze one symbol ─────────────────────────────────────────────────────────
def analyze_symbol(sym, binance_sym):
    if not ICC_AVAILABLE:
        return None
    try:
        df = fetch_bars(binance_sym)
        if df is None:
            return None

        price     = float(df["close"].iloc[-1])
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
            "dir_match":  (direction == "LONG"  and trend["trend"] == "UPTREND") or
                          (direction == "SHORT" and trend["trend"] == "DOWNTREND"),
        }
        passed   = int(sum(rules.values()))
        approved = bool(best.get("approved", False))

        if passed < 3:
            return None

        if price >= 1000:   price_fmt = f"${price:,.0f}"
        elif price >= 1:    price_fmt = f"${price:,.2f}"
        else:               price_fmt = f"${price:.6f}"

        return {
            "sym":       sym,
            "ticker":    binance_sym,
            "price":     round(price, 8),
            "price_fmt": price_fmt,
            "trend":     trend["trend"],
            "conf":      int(round(float(trend["confidence"]) * 100)),
            "direction": direction,
            "rules":     {k: bool(v) for k, v in rules.items()},
            "passed":    passed,
            "approved":  approved,
            "stop":      round(float(best["stop_loss"]), 8) if best.get("stop_loss") else None,
            "target":    round(float(best["target"]), 8)    if best.get("target")    else None,
            "rr":        round(float(best.get("rr", 0)), 2),
            "reason":    str(best.get("reason", "")),
            "rev_warn":  rev.get("reason", "") if rev.get("reversal_likely") else "",
            "ts":        datetime.now().strftime("%H:%M:%S"),
            "error":     False,
        }
    except Exception as e:
        log.debug(f"Analyze error {sym}: {e}")
        return None

# ── Alert on new approvals ─────────────────────────────────────────────────────
_alerted = set()

def check_alerts(results):
    for r in results:
        if not r.get("approved"):
            continue
        key = f"{r['sym']}-{r['direction']}-{datetime.now().strftime('%H:%M')}"
        if key in _alerted:
            continue
        _alerted.add(key)
        emoji = "📈" if r["direction"] == "LONG" else "📉"
        msg = (
            f"{emoji} <b>CRYPTO SCANNER — {r['sym']}</b>\n"
            f"Direction : {r['direction']}\n"
            f"Price     : {r['price_fmt']}\n"
            f"Trend     : {r['trend']} {r['conf']}%\n"
            f"Rules     : {r['passed']}/9\n"
            f"Stop      : {r.get('stop')}\n"
            f"Target    : {r.get('target')}\n"
            f"R:R       : {r['rr']}:1\n"
            f"Time      : {r['ts']} UTC"
        )
        send_telegram(msg)
        log.info(f"APPROVED: {r['sym']} {r['direction']} @ {r['price_fmt']} — {r['passed']}/9")

# ── Save results for dashboard ─────────────────────────────────────────────────
def save_results(results, updated, scan_time):
    import json
    # Convert any numpy types
    def clean(obj):
        if isinstance(obj, dict):  return {k: clean(v) for k, v in obj.items()}
        if isinstance(obj, list):  return [clean(i) for i in obj]
        if isinstance(obj, bool):  return obj
        try:
            import numpy as np
            if isinstance(obj, np.integer): return int(obj)
            if isinstance(obj, np.floating): return float(obj)
            if isinstance(obj, np.bool_): return bool(obj)
        except: pass
        return obj

    data = clean({
        "results":  results,
        "updated":  updated,
        "scan_time": scan_time,
        "total":    len(results),
        "approved": len([r for r in results if r.get("approved")]),
        "strong":   len([r for r in results if r.get("passed", 0) >= MIN_RULES]),
        "market":   "CRYPTO — 24/7 via Binance",
    })
    Path(RESULTS_FILE).write_text(json.dumps(data))
    log.info(f"Saved {len(results)} results to {RESULTS_FILE}")

# ── Main scan loop ─────────────────────────────────────────────────────────────
def main():
    if not ICC_AVAILABLE:
        log.error("structure_rules.py not found — cannot run ICC analysis")
        return

    log.info("=" * 60)
    log.info("  KIBO Crypto Scanner — Binance Edition")
    log.info(f"  Pairs      : {len(SYMBOLS)}")
    log.info(f"  Data       : Binance (free, no key needed)")
    log.info(f"  Scan every : {SCAN_INTERVAL}s  |  24/7")
    log.info(f"  Min rules  : {MIN_RULES}/9 to alert")
    log.info("=" * 60)

    scan_count = 0
    while True:
        try:
            scan_start = time.time()
            scan_count += 1
            log.info(f"── Scan #{scan_count} — {len(SYMBOLS)} pairs ──")

            results = []
            for sym, binance_sym in SYMBOLS.items():
                try:
                    log.info(f"  {sym} ({binance_sym})...")
                    r = analyze_symbol(sym, binance_sym)
                    if r:
                        results.append(r)
                    time.sleep(0.3)   # Binance is generous — short delay is fine
                except Exception as e:
                    log.warning(f"  {sym} error: {e}")

            results.sort(
                key=lambda x: (x.get("approved", False), x.get("passed", 0), x.get("conf", 0)),
                reverse=True
            )

            elapsed      = round(time.time() - scan_start, 1)
            updated      = datetime.now().strftime("%H:%M:%S UTC")
            approved_cnt = len([r for r in results if r.get("approved")])
            strong_cnt   = len([r for r in results if r.get("passed", 0) >= MIN_RULES])

            log.info(f"Scan #{scan_count} done in {elapsed}s — "
                     f"{len(results)} results | {approved_cnt} approved | {strong_cnt} strong")

            for r in results[:5]:
                status = "APPROVED" if r["approved"] else f"{r['passed']}/9"
                log.info(f"  {r['sym']:<8} {r['direction']:<5} {r['price_fmt']:<14} "
                         f"{r['trend']:<14} {r['conf']}%  {status}")

            check_alerts(results)
            save_results(results, updated, elapsed)

            wait = max(5, SCAN_INTERVAL - elapsed)
            log.info(f"Next scan in {wait:.0f}s...")
            time.sleep(wait)

        except KeyboardInterrupt:
            log.info("Crypto Scanner stopped.")
            break
        except Exception as e:
            log.error(f"Scan loop error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
