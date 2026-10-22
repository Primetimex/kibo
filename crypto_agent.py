"""
KIBO Crypto Agent v2 — 9 Pairs, ICC Scanner Engine
Instruments : BTC, ETH, SOL, BNB, XRP, DOGE, AVAX, LINK, LTC
Timeframe   : 5-minute bars
Signal      : Full ICC structure rules (all 14 videos)
Sizing      : Volatility-adjusted ATR per coin
Data        : Polygon.io crypto feed (24/7)
Broker      : TradeZero Developer API
Alerts      : Telegram push notifications
"""

import os, time, logging, requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
try:
    from alpaca_connector import (futures_buy, futures_sell,
                                   crypto_buy, crypto_sell,
                                   get_account as alpaca_account)
    ALPACA_ENABLED = True
except ImportError:
    ALPACA_ENABLED = False
try:
    from rate_limiter import wait_for_slot
except ImportError:
    def wait_for_slot(caller=""): pass
try:
    from trade_journal import log_entry as journal_entry, log_exit as journal_exit
except ImportError:
    def journal_entry(*a,**k): return None
    def journal_exit(*a,**k): pass
from structure_rules import (
    check_entry_rules, classify_trend, format_structure_alert,
    detect_reversal, detect_indication, is_in_session
)

load_dotenv("_env")
POLYGON_API_KEY   = os.getenv("POLYGON_API_KEY")
TZ_API_KEY_ID     = os.getenv("TZ_API_KEY_ID")
TZ_API_SECRET_KEY = os.getenv("TZ_API_SECRET_KEY")
TZ_ACCOUNT_ID     = os.getenv("TZ_ACCOUNT_ID")
TZ_BASE_URL       = "https://webapi.tradezero.com"
ACCOUNT_SIZE      = float(os.getenv("ACCOUNT_SIZE", "10000"))
PAPER_MODE        = os.getenv("PAPER_MODE", "true").lower() == "true"
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")

# ── Risk settings ─────────────────────────────────────────────────────────────
RISK_PCT       = 0.015   # 1.5% of account per trade
ATR_PERIOD     = 14
RR_RATIO       = 2.0

# ── All 9 crypto instruments ──────────────────────────────────────────────────
# ATR_MULT is wider for volatile low-cap coins, tighter for BTC/ETH
CONTRACTS = {
    "BTC":  {"name": "Bitcoin",   "polygon": "X:BTCUSD",  "min_size": 0.0001, "atr_mult": 2.0, "risk_pct": 0.015},
    "ETH":  {"name": "Ethereum",  "polygon": "X:ETHUSD",  "min_size": 0.001,  "atr_mult": 2.0, "risk_pct": 0.015},
    "SOL":  {"name": "Solana",    "polygon": "X:SOLUSD",  "min_size": 0.01,   "atr_mult": 2.5, "risk_pct": 0.012},
    "BNB":  {"name": "BNB",       "polygon": "X:BNBUSD",  "min_size": 0.01,   "atr_mult": 2.5, "risk_pct": 0.012},
    "XRP":  {"name": "Ripple",    "polygon": "X:XRPUSD",  "min_size": 1.0,    "atr_mult": 2.5, "risk_pct": 0.010},
    "DOGE": {"name": "Dogecoin",  "polygon": "X:DOGEUSD", "min_size": 10.0,   "atr_mult": 3.0, "risk_pct": 0.010},
    "AVAX": {"name": "Avalanche", "polygon": "X:AVAXUSD", "min_size": 0.1,    "atr_mult": 2.5, "risk_pct": 0.010},
    "LINK": {"name": "Chainlink", "polygon": "X:LINKUSD", "min_size": 0.1,    "atr_mult": 2.5, "risk_pct": 0.010},
    "LTC":  {"name": "Litecoin",  "polygon": "X:LTCUSD",  "min_size": 0.01,   "atr_mult": 2.0, "risk_pct": 0.012},
}

# Sci's bias — override auto-signal direction if we have strong conviction
SCI_BIAS = {
    "BTC": "SHORT",   # Sci bearish — targets $50K
    "SOL": "SHORT",   # Sci bearish — targets $155
}

HIGH_IMPACT_EVENTS = [
    {"name": "FOMC Rate Decision",   "datetime": "2026-06-11 14:00"},
    {"name": "FOMC Rate Decision",   "datetime": "2026-07-30 14:00"},
    {"name": "CPI Inflation Report", "datetime": "2026-07-15 08:30"},
    {"name": "CPI Inflation Report", "datetime": "2026-08-12 08:30"},
    {"name": "NFP Jobs Report",      "datetime": "2026-07-10 08:30"},
]
BLACKOUT_MINUTES = 30

# Startup delay between coins — avoid 429s on Polygon free tier
SCAN_DELAY = 13  # seconds between each coin fetch (Polygon free tier = 5 calls/min)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("crypto_agent.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

TZ_HEADERS = {
    "TZ-API-KEY-ID":     TZ_API_KEY_ID,
    "TZ-API-SECRET-KEY": TZ_API_SECRET_KEY,
    "Content-Type":      "application/json",
}

# ─────────────────────────────────────────────────────────────────────────────
#  Telegram
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        ).raise_for_status()
    except Exception as e:
        log.warning(f"Telegram failed: {e}")

def alert_started():
    mode = "📄 PAPER" if PAPER_MODE else "🔴 LIVE"
    pairs = ", ".join(CONTRACTS.keys())
    send_telegram(
        f"🪙 <b>KIBO Crypto Agent v2 STARTED</b>\n"
        f"Mode   : {mode}\n"
        f"Pairs  : {pairs}\n"
        f"Risk   : {RISK_PCT*100:.1f}% per trade\n"
        f"Engine : ICC Full Rules (14 videos)\n"
        f"Sci bias: BTC SHORT (target $50K), SOL SHORT (target $155)\n"
        f"Time   : {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
    )

def alert_trade_opened(sym, direction, entry, stop, target, size):
    emoji = "📈" if direction == "LONG" else "📉"
    mode  = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    name  = CONTRACTS[sym]["name"]
    risk  = abs(entry - stop) * size
    rr    = abs(target - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0
    send_telegram(
        f"{emoji} <b>CRYPTO TRADE OPENED</b> [{mode}]\n"
        f"Pair      : <b>{sym} — {name}</b>\n"
        f"Direction : {direction}\n"
        f"Entry     : ${entry:,.4f}\n"
        f"Stop      : ${stop:,.4f}\n"
        f"Target    : ${target:,.4f}\n"
        f"Size      : {size} {sym}\n"
        f"Risk $    : ${risk:,.2f}\n"
        f"R:R       : {rr:.1f}:1\n"
        f"Time      : {datetime.now().strftime('%H:%M:%S ET')}"
    )

def alert_trade_closed(sym, reason, entry, exit_price, size):
    pnl   = (exit_price - entry) * size
    pct   = (exit_price - entry) / entry * 100
    emoji = "✅" if pnl >= 0 else "🛑"
    mode  = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    send_telegram(
        f"{emoji} <b>CRYPTO CLOSED — {reason}</b> [{mode}]\n"
        f"Pair  : <b>{sym}</b>\n"
        f"Entry : ${entry:,.4f}  →  Exit: ${exit_price:,.4f}\n"
        f"P&L   : ${pnl:+,.2f} ({pct:+.2f}%)\n"
        f"Time  : {datetime.now().strftime('%H:%M:%S ET')}"
    )

def alert_icc_scan(sym, sc, direction):
    pass

def alert_daily_summary(trades):
    date_str  = datetime.now().strftime('%Y-%m-%d')
    if not trades:
        send_telegram(f"📊 <b>Crypto Daily Summary — {date_str}</b>\nNo trades today.")
        return
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    winners   = sum(1 for t in trades if t.get("pnl", 0) > 0)
    symbols   = ", ".join(set(t["sym"] for t in trades))
    send_telegram(
        f"📊 <b>Crypto Daily Summary — {date_str}</b>\n"
        f"Pairs   : {symbols}\n"
        f"Trades  : {len(trades)}\n"
        f"Winners : {winners}/{len(trades)}\n"
        f"P&L     : ${total_pnl:+,.2f}"
    )

# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────

def utc_to_et(ts_series):
    now   = datetime.now()
    year  = now.year
    mar1  = datetime(year, 3, 1)
    nov1  = datetime(year, 11, 1)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    dst_end   = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    offset = pd.Timedelta(hours=-4) if dst_start <= now < dst_end else pd.Timedelta(hours=-5)
    return ts_series + offset

def is_in_blackout(symbol):
    now = datetime.now()
    for event in HIGH_IMPACT_EVENTS:
        event_dt = datetime.strptime(event["datetime"], "%Y-%m-%d %H:%M")
        delta    = abs((now - event_dt).total_seconds() / 60)
        if delta <= BLACKOUT_MINUTES:
            return True, event["name"]
    return False, ""

# ─────────────────────────────────────────────────────────────────────────────
#  Market Data
# ─────────────────────────────────────────────────────────────────────────────

BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "BNB": "BNBUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT",
    "AVAX": "AVAXUSDT", "LINK": "LINKUSDT", "LTC": "LTCUSDT",
}


def get_bars(symbol, lookback=100):
    """
    Fetch 5-min OHLCV bars from Binance public API.
    Completely free, no API key needed, 1200 calls/minute limit.
    Falls back to Polygon if Binance fails.
    """
    binance_sym = BINANCE_SYMBOLS.get(symbol)
    if binance_sym:
        try:
            url = "https://api.binance.us/api/v3/klines"  # US endpoint — avoids 451 geo-block
            resp = requests.get(url, params={
                "symbol": binance_sym, "interval": "5m", "limit": lookback
            }, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
            if not raw or len(raw) < 20:
                raise Exception("no data")
            df = pd.DataFrame(raw, columns=[
                "timestamp","open","high","low","close","volume",
                "close_time","quote_vol","trades","taker_buy_base",
                "taker_buy_quote","ignore"
            ])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            df["timestamp"] = utc_to_et(df["timestamp"])
            for col in ["open","high","low","close","volume"]:
                df[col] = pd.to_numeric(df[col])
            df = df[["timestamp","open","high","low","close","volume"]].reset_index(drop=True)
            log.info(f"{symbol}: fetched {len(df)} bars from Binance")
            time.sleep(0.5)  # Binance allows 1200 calls/min — barely need any delay
            return df
        except Exception as e:
            log.warning(f"{symbol}: Binance failed ({e}) — trying Kraken")

    # Kraken fallback for crypto (free, no key, works in US)
    kraken_map = {
        "BTC":"XBTUSD","ETH":"ETHUSD","SOL":"SOLUSD","XRP":"XRPUSD",
        "DOGE":"DOGEUSDT","LTC":"LTCUSD","LINK":"LINKUSD","AVAX":"AVAXUSD","BNB":"BNBUSD"
    }
    kraken_pair = kraken_map.get(symbol)
    if kraken_pair:
        try:
            r = requests.get(f"https://api.kraken.com/0/public/OHLC",
                params={"pair": kraken_pair, "interval": 5, "count": lookback}, timeout=10)
            r.raise_for_status()
            result = r.json().get("result", {})
            ohlc = result.get(kraken_pair) or result.get(list(result.keys())[0] if result else "", [])
            if ohlc and len(ohlc) >= 20:
                df = pd.DataFrame(ohlc, columns=["time","open","high","low","close","vwap","volume","count"])
                df["timestamp"] = pd.to_datetime(df["time"], unit="s")
                df["timestamp"] = utc_to_et(df["timestamp"])
                for col in ["open","high","low","close","volume"]:
                    df[col] = pd.to_numeric(df[col])
                df = df[["timestamp","open","high","low","close","volume"]].tail(lookback).reset_index(drop=True)
                log.info(f"{symbol}: fetched {len(df)} bars from Kraken")
                time.sleep(1)
                return df
        except Exception as ke:
            log.warning(f"{symbol}: Kraken failed ({ke}) — trying Polygon fallback")

    # Polygon fallback
    ticker = CONTRACTS[symbol]["polygon"]
    end    = datetime.now()
    start  = end - timedelta(days=14)
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}"
        f"/range/5/minute/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        f"?adjusted=true&sort=asc&limit={lookback}&apiKey={POLYGON_API_KEY}"
    )
    try:
        for attempt in range(3):
            resp = requests.get(url, timeout=10)
            if resp.status_code == 429:
                log.warning(f"{symbol}: Polygon 429 — waiting {20*(attempt+1)}s")
                time.sleep(20 * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            if not data.get("results"):
                return pd.DataFrame()
            df = pd.DataFrame(data["results"])
            df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
            df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"}, inplace=True)
            df["timestamp"] = utc_to_et(df["timestamp"])
            log.info(f"{symbol}: fetched {len(df)} bars from Polygon (fallback)")
            time.sleep(13)
            return df[["timestamp","open","high","low","close","volume"]].reset_index(drop=True)
        return pd.DataFrame()
    except Exception as e:
        log.error(f"{symbol}: all data sources failed — {e}")
        return pd.DataFrame()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, min_periods=period).mean()
    rs    = gain / loss.replace(0, float('nan'))
    return 100 - (100 / (1 + rs))

def calc_atr(df, period=14):
    prev  = df["close"].shift(1)
    tr    = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"]  - prev).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def add_indicators(df):
    df = df.copy()
    df["rsi"]       = calc_rsi(df["close"])
    df["ema9"]      = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"]     = df["close"].ewm(span=21, adjust=False).mean()
    df["atr"]       = calc_atr(df)
    df["vol_avg20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg20"]
    macd_fast       = df["close"].ewm(span=12, adjust=False).mean()
    macd_slow       = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]      = macd_fast - macd_slow
    df["macd_sig"]  = df["macd"].ewm(span=9, adjust=False).mean()
    return df.dropna()

# ─────────────────────────────────────────────────────────────────────────────
#  Signal Logic — ICC-first, momentum confirms
# ─────────────────────────────────────────────────────────────────────────────

def check_signal(df, symbol):
    """Score-based signal with Sci bias override."""
    if len(df) < 30:
        return "NONE"
    row  = df.iloc[-1]
    prev = df.iloc[-2]
    trend = classify_trend(df)
    if trend["trend"] == "CONSOLIDATION":
        return "NONE"
    
    # Sci bias override
    sci = SCI_BIAS.get(symbol, "NEUTRAL")
    vol_ok = row["vol_ratio"] >= 1.2 or row["vol_ratio"] == 0.0

    long_score = 0
    if trend["trend"] == "UPTREND":         long_score += 2
    if sci == "LONG":                        long_score += 1
    if 45 <= row["rsi"] <= 78:              long_score += 1
    if row["ema9"] > row["ema21"]:          long_score += 1
    if row["macd"] > row["macd_sig"]:       long_score += 1
    if prev["macd"] <= prev["macd_sig"] and row["macd"] > row["macd_sig"]: long_score += 1
    if vol_ok:                              long_score += 1

    short_score = 0
    if trend["trend"] == "DOWNTREND":       short_score += 2
    if sci == "SHORT":                       short_score += 1
    if 22 <= row["rsi"] <= 55:             short_score += 1
    if row["ema9"] < row["ema21"]:         short_score += 1
    if row["macd"] < row["macd_sig"]:      short_score += 1
    if prev["macd"] >= prev["macd_sig"] and row["macd"] < row["macd_sig"]: short_score += 1
    if vol_ok:                             short_score += 1

    if sci == "SHORT" and long_score > short_score:
        return "NONE"  # Sci bias blocks counter-trend
    if sci == "LONG" and short_score > long_score:
        return "NONE"

    if long_score >= 4 and long_score > short_score:
        return "LONG"
    if short_score >= 4 and short_score > long_score:
        return "SHORT"
    return "NONE"


def calc_position(symbol, direction, entry, atr):
    cfg          = CONTRACTS[symbol]
    atr_mult     = cfg["atr_mult"]
    risk_pct     = cfg["risk_pct"]
    min_size     = cfg["min_size"]
    stop_dist    = atr * atr_mult

    if direction == "LONG":
        stop_loss   = entry - stop_dist
        take_profit = entry + (stop_dist * RR_RATIO)
    else:
        stop_loss   = entry + stop_dist
        take_profit = entry - (stop_dist * RR_RATIO)

    max_risk = ACCOUNT_SIZE * risk_pct
    size     = max(min_size, round(max_risk / stop_dist, 6))

    # ── Hard caps to prevent runaway sizing ───────────────────────────────────
    MAX_NOTIONAL = 5000.0   # never risk more than $5,000 notional per trade
    MAX_SIZE_MAP = {         # per-coin max qty sanity check
        "BTC": 0.1, "ETH": 2.0, "SOL": 50.0, "BNB": 10.0,
        "XRP": 5000.0, "DOGE": 50000.0, "AVAX": 200.0,
        "LINK": 500.0, "LTC": 30.0,
    }
    if entry > 0:
        max_by_notional = MAX_NOTIONAL / entry
        size = min(size, max_by_notional)
    max_by_coin = MAX_SIZE_MAP.get(symbol, 100.0)
    size = min(size, max_by_coin)
    size = max(min_size, round(size, 6))

    return {
        "entry":       round(entry,       6),
        "stop_loss":   round(stop_loss,   6),
        "take_profit": round(take_profit, 6),
        "size":        size,
        "risk_usd":    round(stop_dist * size, 2),
    }

# ─────────────────────────────────────────────────────────────────────────────
#  TradeZero Execution
# ─────────────────────────────────────────────────────────────────────────────

def place_order(symbol, direction, size, entry, stop, target):
    action = "Buy" if direction == "LONG" else "Sell"
    import uuid
    client_id = f"KIBO-{uuid.uuid4().hex[:8].upper()}"

    # ── Alpaca Paper Trading (crypto) ────────────────────────────────
    if ALPACA_ENABLED:
        action_str = "BUY" if direction == "LONG" else "SELL"
        if action_str == "BUY":
            ok, resp = crypto_buy(symbol, usd_amount=float(size)*entry if entry>0 else 100)
        else:
            ok, resp = crypto_sell(symbol, qty=float(size))
        log.info(f"[ALPACA CRYPTO] {'✅' if ok else '❌'} {action_str} {size} {symbol} — {str(resp)[:80]}")
        return ok
    # ── Fallback: log only ────────────────────────────────────────────
    log.info(f"[{'PAPER' if PAPER_MODE else 'LIVE'}] {action} {size} {symbol} @ {entry:.4f} | SL={stop:.4f} TP={target:.4f}")

    payload = {
        "clientOrderId": client_id,
        "securityType":  "Stock",   # TZ paper uses Stock for all symbols
        "symbol":        symbol,
        "side":          action,
        "openClose":     "Open",
        "orderType":     "Market",
        "orderQuantity": float(size),
        "timeInForce":   "Day",
        "route":         "PAPER",
    }
    try:
        resp = requests.post(
            f"{TZ_BASE_URL}/v1/api/accounts/{TZ_ACCOUNT_ID}/order",
            headers=TZ_HEADERS, json=payload, timeout=10
        )
        resp.raise_for_status()
        log.info(f"Order placed: {resp.json()}")
        return True
    except Exception as e:
        log.error(f"Order failed {symbol}: {e}")
        send_telegram(f"⚠️ <b>ORDER FAILED — {symbol}</b>\n{str(e)[:200]}")
        return False

def close_position(symbol, direction, size):
    action = "Sell" if direction == "LONG" else "Buy"
    if PAPER_MODE:
        log.info(f"[PAPER] CLOSE {size} {symbol} ({action})")
        return True
    payload = {
        "clientOrderId": f"KIBO-CLOSE-{symbol}",
        "securityType":  "Crypto",
        "symbol":        symbol,
        "side":          action,
        "openClose":     "Close",
        "orderType":     "Market",
        "orderQuantity": size,
        "timeInForce":   "Day",
        "route":         "PAPER",
    }
    try:
        resp = requests.post(
            f"{TZ_BASE_URL}/v1/api/accounts/{TZ_ACCOUNT_ID}/order",
            headers=TZ_HEADERS, json=payload, timeout=10
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Close failed {symbol}: {e}")
        send_telegram(f"⚠️ <b>CLOSE FAILED — {symbol}</b>\nManual close needed!\n{str(e)[:200]}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
#  Position Tracker
# ─────────────────────────────────────────────────────────────────────────────

open_positions = {}
trades_today   = []

def check_exits():
    for symbol, pos in list(open_positions.items()):
        try:
            df = get_bars(symbol, lookback=5)
            if df.empty:
                continue
            price  = df.iloc[-1]["close"]
            d      = pos["direction"]
            hit_tp = (d == "LONG" and price >= pos["take_profit"]) or \
                     (d == "SHORT" and price <= pos["take_profit"])
            hit_sl = (d == "LONG" and price <= pos["stop_loss"]) or \
                     (d == "SHORT" and price >= pos["stop_loss"])
            if hit_tp:
                log.info(f"TARGET HIT {symbol} @ {price:.4f}")
                close_position(symbol, d, pos["size"])
                pnl = abs(pos["take_profit"] - pos["entry"]) * pos["size"]
                alert_trade_closed(symbol, "TAKE PROFIT ✅", pos["entry"], price, pos["size"])
                trades_today.append({"sym": symbol, "pnl": pnl})
                del open_positions[symbol]
            elif hit_sl:
                log.info(f"STOP HIT {symbol} @ {price:.4f}")
                close_position(symbol, d, pos["size"])
                pnl = -abs(pos["stop_loss"] - pos["entry"]) * pos["size"]
                alert_trade_closed(symbol, "STOP LOSS 🛑", pos["entry"], price, pos["size"])
                trades_today.append({"sym": symbol, "pnl": pnl})
                del open_positions[symbol]
        except Exception as e:
            log.error(f"Exit check error {symbol}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  Main Loop
# ─────────────────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 62)
    log.info("KIBO Crypto Agent v2 — 9 PAIRS — STARTED")
    log.info(f"Mode   : {'PAPER' if PAPER_MODE else 'LIVE'}")
    log.info(f"Pairs  : {', '.join(CONTRACTS.keys())}")
    log.info(f"Engine : ICC Full Rules (all 14 videos)")
    log.info(f"Sci bias: BTC→SHORT (target $50K), SOL→SHORT (target $155)")
    log.info("=" * 62)
    log.info("Startup offset: 5s...")
    time.sleep(5)

    scan_interval    = 5 * 60
    last_summary_day = None
    blackout_alerted = set()

    while True:
        now = datetime.now()

        # Daily summary at 11:45 PM
        if now.hour == 23 and now.minute >= 45 and last_summary_day != now.date():
            alert_daily_summary(trades_today)
            trades_today.clear()
            last_summary_day = now.date()
            blackout_alerted.clear()

        pairs_str = ", ".join(CONTRACTS.keys())
        log.info(f"--- Scanning 9 pairs: {pairs_str} ---")

        if open_positions:
            check_exits()

        for symbol, cfg in CONTRACTS.items():
            if symbol in open_positions:
                log.info(f"{symbol}: in position — skipping")
                continue

            blacked, event = is_in_blackout(symbol)
            if blacked:
                log.info(f"{symbol}: BLACKOUT — {event}")
                key = f"{symbol}-{event}-{now.date()}"
                if key not in blackout_alerted:
                    blackout_alerted.add(key)
                continue

            try:
                df = get_bars(symbol)
                if df.empty or len(df) < 30:
                    time.sleep(SCAN_DELAY)
                    continue

                df = add_indicators(df)
                if len(df) < 2:
                    time.sleep(SCAN_DELAY)
                    continue

                signal   = check_signal(df, symbol)
                last_row = df.iloc[-1]

                # ── Always log ICC structure on every scan ────────────
                _st  = classify_trend(df)
                # Crypto trades 24/7 — always in session
                _ses = {"in_session": True, "session": "CRYPTO_24_7", "reason": "Crypto never closes"}
                _rv  = detect_reversal(df, _st["trend"])
                _ind = detect_indication(df)

                if signal == "NONE":
                    log.info(
                        f"{symbol}: no signal | "
                        f"${last_row['close']:,.4f} "
                        f"RSI={last_row['rsi']:.1f} "
                        f"EMA={'>' if last_row['ema9'] > last_row['ema21'] else '<'} "
                        f"ATR={last_row['atr']:.4f} | "
                        f"ICC={_st['trend']}({_st['confidence']*100:.0f}%) "
                        f"Sess={_ses['session']}"
                        + (f" | REV: {_rv['reason'][:45]}" if _rv["reversal_likely"] else "")
                        + (f" | IND: {_ind['reason'][:45]}" if _ind.get("indication") else "")
                    )
                    time.sleep(SCAN_DELAY)
                    continue

                log.info(f"{symbol}: {signal} signal!")

                # ── Position sizing ───────────────────────────────────
                pos = calc_position(symbol, signal, last_row["close"], last_row["atr"])

                log.info(
                    f"{symbol}: entry={pos['entry']:.4f} "
                    f"stop={pos['stop_loss']:.4f} "
                    f"target={pos['take_profit']:.4f} "
                    f"size={pos['size']} "
                    f"risk=${pos['risk_usd']:.2f}"
                )

                # ── ICC Full Rules (all 14 videos) ────────────────────
                sc = check_entry_rules(df, signal)
                if not sc["approved"]:
                    log.info(f"{symbol}: ICC BLOCKED — {sc['reason'][:100]}")
                    time.sleep(SCAN_DELAY)
                    continue

                # Override stop/target with ICC structure levels
                if sc.get("stop_loss") and sc.get("target"):
                    pos["stop_loss"]   = sc["stop_loss"]
                    pos["take_profit"] = sc["target"]

                log.info(f"{symbol}: ICC APPROVED ✅ — {sc['reason'][:100]}")
                log.info(
                    f"{symbol}: Stop={pos['stop_loss']:.4f} "
                    f"Target={pos['take_profit']:.4f} "
                    f"RR={sc.get('rr', 0):.1f}"
                )

                success = place_order(
                    symbol, signal, pos["size"],
                    pos["entry"], pos["stop_loss"], pos["take_profit"]
                )

                if success:
                    trade_id = journal_entry(
                        symbol, signal, pos["entry"], pos["stop_loss"],
                        pos["take_profit"], pos["size"],
                        icc_score=sc.get("passed",0), icc_trend=trend_data.get("trend","—") if "trend_data" in dir() else "—",
                        rr=sc.get("rr",0), agent="crypto_agent"
                    )
                    open_positions[symbol] = {
                        "direction":   signal,
                        "entry":       pos["entry"],
                        "stop_loss":   pos["stop_loss"],
                        "take_profit": pos["take_profit"],
                        "size":        pos["size"],
                        "entry_time":  now.isoformat(),
                        "journal_id":  trade_id,
                    }
                    alert_trade_opened(
                        symbol, signal,
                        pos["entry"], pos["stop_loss"],
                        pos["take_profit"], pos["size"]
                    )

            except Exception as e:
                log.error(f"{symbol}: error — {e}")

            time.sleep(SCAN_DELAY)  # stagger each coin fetch

        log.info(f"Open: {list(open_positions.keys()) or 'none'} | Next scan in 5 min")
        time.sleep(scan_interval)


if __name__ == "__main__":
    run()
