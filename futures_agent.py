"""
KIBO Futures Agent — Gold, Crude Oil, S&P 500, Nasdaq Momentum Strategy
Instruments : MGC (Micro Gold) + MCL (Micro Crude Oil)
              MES (Micro S&P 500) + MNQ (Micro Nasdaq-100)
Timeframe   : 5-minute bars
Signals     : RSI, EMA(9/21), MACD, ATR stop, Volume
Broker      : TradeZero Developer API
Alerts      : Telegram push notifications

DISCLAIMER: Futures trading involves substantial risk of loss. You can lose
more than your initial deposit. Always paper trade first. You are solely
responsible for all trading decisions and outcomes.
"""

import os
import time
import logging
import requests
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
    def wait_for_slot(caller=""): pass  # fallback if not present
from structure_rules import check_entry_rules, classify_trend, format_structure_alert, detect_reversal, detect_indication, is_in_session

# ── Load credentials ──────────────────────────────────────────────────────────
load_dotenv("_env")
POLYGON_API_KEY  = os.getenv("POLYGON_API_KEY")
TZ_API_KEY_ID     = os.getenv("TZ_API_KEY_ID")
TZ_API_SECRET_KEY = os.getenv("TZ_API_SECRET_KEY")
TZ_ACCOUNT_ID     = os.getenv("TZ_ACCOUNT_ID")
TZ_BASE_URL      = "https://webapi.tradezero.com"
ACCOUNT_SIZE     = float(os.getenv("ACCOUNT_SIZE", "10000"))
PAPER_MODE       = os.getenv("PAPER_MODE", "true").lower() == "true"
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ── Risk settings ─────────────────────────────────────────────────────────────
RISK_PCT       = 0.015   # 1.5% of account per trade
ATR_PERIOD     = 14
ATR_MULTIPLIER = 1.5     # stop = entry ± (ATR × multiplier)
RR_RATIO       = 2.0     # 2:1 reward to risk

# ── Futures contract specs ────────────────────────────────────────────────────
# ── Trading symbols ──────────────────────────────────────────────────────────
# We trade the ETF proxies on TradeZero paper (stocks) since TZ paper
# doesn't support futures contracts directly.
# The ICC structure is detected on the ETF price — which tracks the
# underlying futures very closely.
#
# ETF → Futures correlation:
#   GLD  → MGC/GC  (Gold)       ~0.99 correlation
#   USO  → MCL/CL  (Crude Oil)  ~0.95 correlation  
#   SPY  → MES/ES  (S&P 500)    ~0.99 correlation
#   QQQ  → MNQ/NQ  (Nasdaq)     ~0.99 correlation

CONTRACTS = {
    "GLD": {
        "name":           "SPDR Gold ETF (Gold proxy)",
        "polygon_ticker": "GLD",
        "tz_symbol":      "GLD",        # actual TZ order symbol
        "futures_equiv":  "MGC",        # what it tracks
        "point_value":    1.0,
        "currency":       "USD",
        "shares_per_trade": 10,         # 10 shares per signal
    },
    "USO": {
        "name":           "US Oil Fund ETF (Crude proxy)",
        "polygon_ticker": "USO",
        "tz_symbol":      "USO",
        "futures_equiv":  "MCL",
        "point_value":    1.0,
        "currency":       "USD",
        "shares_per_trade": 10,
    },
    "SPY": {
        "name":           "S&P 500 ETF (MES proxy)",
        "polygon_ticker": "SPY",
        "tz_symbol":      "SPY",
        "futures_equiv":  "MES",
        "point_value":    1.0,
        "currency":       "USD",
        "shares_per_trade": 5,
    },
    "QQQ": {
        "name":           "Nasdaq-100 ETF (MNQ proxy)",
        "polygon_ticker": "QQQ",
        "tz_symbol":      "QQQ",
        "futures_equiv":  "MNQ",
        "point_value":    1.0,
        "currency":       "USD",
        "shares_per_trade": 5,
    },
}

# Keep backward compat — agents reference ALL_SYMBOLS
ALL_SYMBOLS = list(CONTRACTS.keys())  # ["GLD","USO","SPY","QQQ"]

ALL_SYMBOLS = list(CONTRACTS.keys())

# ── News blackout schedule ────────────────────────────────────────────────────
# Agent pauses 30 min before and after these events.
# Update monthly:
#   FOMC  : https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
#   CPI   : https://www.bls.gov/schedule/news_release/cpi.htm
#   NFP   : https://www.bls.gov/schedule/news_release/empsit.htm
#   EIA   : https://www.eia.gov/petroleum/supply/weekly (every Wednesday 10:30 AM ET)
HIGH_IMPACT_EVENTS = [
    # FOMC — affects all
    {"name": "FOMC Rate Decision",   "datetime": "2026-06-11 14:00", "affects": ALL_SYMBOLS},
    {"name": "FOMC Rate Decision",   "datetime": "2026-07-30 14:00", "affects": ALL_SYMBOLS},
    {"name": "FOMC Rate Decision",   "datetime": "2026-09-17 14:00", "affects": ALL_SYMBOLS},
    # CPI — affects all
    {"name": "CPI Inflation Report", "datetime": "2026-07-15 08:30", "affects": ALL_SYMBOLS},
    {"name": "CPI Inflation Report", "datetime": "2026-08-12 08:30", "affects": ALL_SYMBOLS},
    # NFP Jobs Report — affects all (first Friday each month)
    {"name": "NFP Jobs Report",      "datetime": "2026-07-10 08:30", "affects": ALL_SYMBOLS},
    {"name": "NFP Jobs Report",      "datetime": "2026-08-07 08:30", "affects": ALL_SYMBOLS},
    # EIA Petroleum — affects MCL + indexes
    {"name": "EIA Inventory Report", "datetime": "2026-06-11 10:30", "affects": ["MCL", "MES", "MNQ"]},
    {"name": "EIA Inventory Report", "datetime": "2026-06-18 10:30", "affects": ["MCL", "MES", "MNQ"]},
    {"name": "EIA Inventory Report", "datetime": "2026-06-25 10:30", "affects": ["MCL", "MES", "MNQ"]},
    {"name": "EIA Inventory Report", "datetime": "2026-07-09 10:30", "affects": ["MCL", "MES", "MNQ"]},
    {"name": "EIA Inventory Report", "datetime": "2026-07-16 10:30", "affects": ["MCL", "MES", "MNQ"]},
]

BLACKOUT_MINUTES = 30

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("futures_agent.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 1 — Telegram Alerts
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping alert")
        return
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=5).raise_for_status()
        log.info("Telegram alert sent")
    except Exception as e:
        log.warning(f"Telegram alert failed: {e}")


def alert_started():
    mode = "📄 PAPER TRADING" if PAPER_MODE else "🔴 LIVE TRADING"
    send_telegram(
        f"⚡ <b>KIBO Futures Agent STARTED</b>\n"
        f"Mode       : {mode}\n"
        f"Instruments: MGC, MCL, MES, MNQ\n"
        f"Timeframe  : 5-minute bars\n"
        f"Risk/trade : {RISK_PCT*100:.1f}% of ${ACCOUNT_SIZE:,.0f}\n"
        f"ATR Stop   : {ATR_MULTIPLIER}x ATR  |  R:R: 1:{RR_RATIO}\n"
        f"Time       : {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
    )


def alert_blackout(symbol: str, event_name: str):
    send_telegram(
        f"⏸ <b>BLACKOUT — {symbol}</b>\n"
        f"Event  : {event_name}\n"
        f"Paused ±{BLACKOUT_MINUTES} min around event"
    )


def alert_signal(symbol: str, direction: str, price: float,
                 rsi: float, atr: float):
    emoji = "🟢" if direction == "LONG" else "🔴"
    mode  = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    send_telegram(
        f"{emoji} <b>FUTURES SIGNAL</b> [{mode}]\n"
        f"Symbol : <b>{symbol} — {CONTRACTS[symbol]['name']}</b>\n"
        f"Signal : <b>{direction}</b>\n"
        f"Price  : {price:,.2f}\n"
        f"RSI    : {rsi:.1f}\n"
        f"ATR    : {atr:.2f}\n"
        f"Time   : {datetime.now().strftime('%H:%M:%S ET')}"
    )


def alert_trade_opened(symbol: str, direction: str, entry: float,
                       stop: float, target: float, contracts: int):
    emoji    = "📈" if direction == "LONG" else "📉"
    mode     = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    risk_usd = abs(entry - stop) * CONTRACTS[symbol]["point_value"] * contracts
    send_telegram(
        f"{emoji} <b>FUTURES TRADE OPENED</b> [{mode}]\n"
        f"Symbol    : <b>{symbol} — {CONTRACTS[symbol]['name']}</b>\n"
        f"Direction : {direction}\n"
        f"Entry     : {entry:,.2f}\n"
        f"Stop Loss : {stop:,.2f}\n"
        f"Target    : {target:,.2f}\n"
        f"Contracts : {contracts}\n"
        f"Risk $    : ${risk_usd:,.2f}\n"
        f"Time      : {datetime.now().strftime('%H:%M:%S ET')}"
    )


def alert_trade_closed(symbol: str, reason: str, entry: float,
                       exit_price: float, contracts: int):
    point_val = CONTRACTS[symbol]["point_value"]
    pnl       = (exit_price - entry) * point_val * contracts
    pct       = (exit_price - entry) / entry * 100
    emoji     = "✅" if pnl >= 0 else "🛑"
    mode      = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    send_telegram(
        f"{emoji} <b>FUTURES TRADE CLOSED — {reason}</b> [{mode}]\n"
        f"Symbol : <b>{symbol}</b>\n"
        f"Entry  : {entry:,.2f}  →  Exit: {exit_price:,.2f}\n"
        f"P&L    : ${pnl:+,.2f} ({pct:+.2f}%)\n"
        f"Time   : {datetime.now().strftime('%H:%M:%S ET')}"
    )


def alert_daily_summary(trades: list):
    date_str = datetime.now().strftime('%Y-%m-%d')
    if not trades:
        send_telegram(
            f"📊 <b>Futures Daily Summary — {date_str}</b>\n"
            f"No trades taken today."
        )
        return
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    winners   = sum(1 for t in trades if t.get("pnl", 0) > 0)
    send_telegram(
        f"📊 <b>Futures Daily Summary — {date_str}</b>\n"
        f"Trades    : {len(trades)}\n"
        f"Winners   : {winners} / {len(trades)}\n"
        f"Total P&L : ${total_pnl:+,.2f}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 2 — News Blackout Filter
# ─────────────────────────────────────────────────────────────────────────────

def is_in_blackout(symbol: str) -> tuple[bool, str]:
    now = datetime.now()
    for event in HIGH_IMPACT_EVENTS:
        if symbol not in event["affects"]:
            continue
        event_dt = datetime.strptime(event["datetime"], "%Y-%m-%d %H:%M")
        delta    = abs((now - event_dt).total_seconds() / 60)
        if delta <= BLACKOUT_MINUTES:
            return True, event["name"]
    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 3 — Market Data (Polygon.io)
# ─────────────────────────────────────────────────────────────────────────────

def get_15min_bars(symbol: str, lookback_bars: int = 100) -> pd.DataFrame:
    """Fetch 5-min bars via Yahoo Finance — free, fast, no rate limits."""
    ticker = CONTRACTS.get(symbol, {}).get("polygon_ticker", symbol)
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            headers=headers,
            params={"interval": "5m", "range": "2d"},
            timeout=10
        )
        if r.status_code == 200:
            res = r.json().get("chart", {}).get("result", [])
            if res:
                chart = res[0]
                ts    = chart.get("timestamp", [])
                ohlcv = chart.get("indicators", {}).get("quote", [{}])[0]
                if ts and ohlcv.get("close"):
                    df = pd.DataFrame({
                        "timestamp": pd.to_datetime(ts, unit="s"),
                        "open":   ohlcv.get("open", []),
                        "high":   ohlcv.get("high", []),
                        "low":    ohlcv.get("low", []),
                        "close":  ohlcv.get("close", []),
                        "volume": ohlcv.get("volume", []),
                    })
                    df = df.dropna(subset=["close"])
                    df = df.tail(lookback_bars).reset_index(drop=True)
                    log.info(f"{symbol}: fetched {len(df)} bars from Yahoo ({ticker})")
                    time.sleep(0.3)
                    return df
    except Exception as e:
        log.warning(f"{symbol}: Yahoo failed ({e}) — trying Polygon fallback")

    # Polygon fallback
    if POLYGON_API_KEY:
        try:
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=5)
            url = (
                f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/5/minute"
                f"/{start_dt.strftime('%Y-%m-%d')}/{end_dt.strftime('%Y-%m-%d')}"
                f"?adjusted=true&sort=asc&limit={lookback_bars}&apiKey={POLYGON_API_KEY}"
            )
            wait_for_slot(symbol)
            resp = requests.get(url, timeout=10)
            if resp.status_code == 429:
                log.warning(f"{symbol}: Polygon 429 — skipping")
                return pd.DataFrame()
            resp.raise_for_status()
            data = resp.json()
            if data.get("results"):
                df2 = pd.DataFrame(data["results"])
                df2["timestamp"] = pd.to_datetime(df2["t"], unit="ms")
                df2.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"}, inplace=True)
                log.info(f"{symbol}: fetched {len(df2)} bars from Polygon (fallback)")
                time.sleep(13)
                return df2[["timestamp","open","high","low","close","volume"]].tail(lookback_bars).reset_index(drop=True)
        except Exception as e:
            log.error(f"{symbol}: all sources failed — {e}")
    return pd.DataFrame()


def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, min_periods=period).mean()
    rs    = gain / loss.replace(0, float('nan'))
    return 100 - (100 / (1 + rs))

def calc_atr(df, period=14):
    prev = df["close"].shift(1)
    tr   = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"]  - prev).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def calc_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd     = ema_fast - ema_slow
    sig      = macd.ewm(span=signal, adjust=False).mean()
    hist     = macd - sig
    return macd, sig, hist

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"]         = calc_rsi(df["close"])
    df["ema9"]        = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"]       = df["close"].ewm(span=21, adjust=False).mean()
    df["macd"], df["macd_signal"], df["macd_hist"] = calc_macd(df["close"])
    df["atr"]         = calc_atr(df, ATR_PERIOD)
    df["vol_avg20"]   = df["volume"].rolling(20).mean()
    df["vol_ratio"]   = df["volume"] / df["vol_avg20"]
    return df.dropna()


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 5 — Signal Logic
# ─────────────────────────────────────────────────────────────────────────────

def check_signal(df: pd.DataFrame) -> str:
    """Score-based signal — needs 4+ points. ICC does final filtering."""
    if len(df) < 2:
        return "NONE"
    row  = df.iloc[-1]
    prev = df.iloc[-2]
    from structure_rules import classify_trend
    trend = classify_trend(df)
    if trend["trend"] == "CONSOLIDATION":
        return "NONE"
    vol_ok = row["vol_ratio"] >= 1.0

    long_score = 0
    if trend["trend"] == "UPTREND":          long_score += 2
    if 45 <= row["rsi"] <= 78:               long_score += 1
    if row["ema9"] > row["ema21"]:           long_score += 1
    if row["macd"] > row["macd_signal"]:     long_score += 1
    if prev["macd"] <= prev["macd_signal"] and row["macd"] > row["macd_signal"]: long_score += 1
    if vol_ok:                               long_score += 1

    short_score = 0
    if trend["trend"] == "DOWNTREND":        short_score += 2
    if 22 <= row["rsi"] <= 55:               short_score += 1
    if row["ema9"] < row["ema21"]:           short_score += 1
    if row["macd"] < row["macd_signal"]:     short_score += 1
    if prev["macd"] >= prev["macd_signal"] and row["macd"] < row["macd_signal"]: short_score += 1
    if vol_ok:                               short_score += 1

    if long_score >= 4 and long_score > short_score:
        return "LONG"
    if short_score >= 4 and short_score > long_score:
        return "SHORT"
    return "NONE"


def calc_position(symbol: str, direction: str,
                  entry: float, atr: float) -> dict:
    """
    ATR-based stop and target. Contracts sized to risk exactly RISK_PCT.

    Example — MES (S&P) at 5,500, ATR = 12 points:
      stop_distance = 12 × 1.5 = 18 points
      LONG stop     = 5,500 - 18 = 5,482
      LONG target   = 5,500 + (18 × 2) = 5,536
      max_risk      = $1,000,000 × 1.5% = $15,000
      risk/contract = 18 × $5 = $90
      contracts     = $15,000 / $90 = 166 contracts
    """
    point_val     = CONTRACTS[symbol]["point_value"]
    stop_distance = atr * ATR_MULTIPLIER

    if direction == "LONG":
        stop_loss   = entry - stop_distance
        take_profit = entry + (stop_distance * RR_RATIO)
    else:
        stop_loss   = entry + stop_distance
        take_profit = entry - (stop_distance * RR_RATIO)

    max_risk_usd      = ACCOUNT_SIZE * RISK_PCT
    risk_per_contract = stop_distance * point_val
    contracts         = max(1, int(max_risk_usd / risk_per_contract))

    return {
        "entry":         entry,
        "stop_loss":     round(stop_loss,   2),
        "take_profit":   round(take_profit, 2),
        "contracts":     contracts,
        "stop_distance": stop_distance,
        "risk_usd":      risk_per_contract * contracts,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 7 — TradeZero API Execution
# ─────────────────────────────────────────────────────────────────────────────

TZ_HEADERS = {
    "TZ-API-KEY-ID":     TZ_API_KEY_ID,
    "TZ-API-SECRET-KEY": TZ_API_SECRET_KEY,
    "Content-Type":      "application/json",
}


def place_futures_order(symbol: str, direction: str, contracts: int,
                        entry: float, stop: float, target: float) -> bool:
    action = "BUY" if direction == "LONG" else "SELL"
    # ── Alpaca Paper Trading ─────────────────────────────────────────
    if ALPACA_ENABLED:
        action_str = "BUY" if direction == "LONG" else "SELL"
        ok, resp = futures_buy(symbol, contracts, entry) if action_str=="BUY" else futures_sell(symbol, contracts)
        log.info(f"[ALPACA] {'✅' if ok else '❌'} {action_str} {contracts}x {symbol} — {str(resp)[:80]}")
        return ok
    # ── Fallback: TradeZero ───────────────────────────────────────────
    log.info(
        f"[{'PAPER' if PAPER_MODE else 'LIVE'}] {action} {contracts}x {symbol} @ {entry:,.2f} | "
        f"SL={stop:,.2f}  TP={target:,.2f}"
    )
    import uuid as _uuid
    client_id = f"KIBO-{_uuid.uuid4().hex[:8].upper()}"
    # Use the TZ symbol (ETF proxy) for the actual order
    tz_sym = CONTRACTS.get(symbol, {}).get("tz_symbol", symbol)
    shares = CONTRACTS.get(symbol, {}).get("shares_per_trade", contracts)
    payload = {
        "clientOrderId": client_id,
        "securityType":  "Stock",
        "symbol":        tz_sym,
        "side":          "Buy" if action=="BUY" else "Sell",
        "openClose":     "Open",
        "orderType":     "Market",
        "orderQuantity": shares,
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
        log.error(f"Order failed for {symbol}: {e}")
        send_telegram(f"⚠️ <b>ORDER FAILED — {symbol}</b>\n{str(e)[:200]}")
        return False


def close_futures_position(symbol: str, direction: str,
                           contracts: int) -> bool:
    close_action = "SELL" if direction == "LONG" else "BUY"
    log.info(f"[{'PAPER' if PAPER_MODE else 'LIVE'}] CLOSE {contracts}x {symbol}")
    import uuid as _uuid2
    close_id = f"KIBO-C-{_uuid2.uuid4().hex[:6].upper()}"
    tz_sym_c = CONTRACTS.get(symbol, {}).get("tz_symbol", symbol)
    shares_c = CONTRACTS.get(symbol, {}).get("shares_per_trade", contracts)
    payload = {
        "clientOrderId": close_id,
        "securityType":  "Stock",
        "symbol":        tz_sym_c,
        "side":          "Sell" if close_action=="SELL" else "Buy",
        "openClose":     "Close",
        "orderType":     "Market",
        "orderQuantity": shares_c,
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
        log.error(f"Close failed for {symbol}: {e}")
        send_telegram(
            f"⚠️ <b>CLOSE FAILED — {symbol}</b>\n"
            f"Manual close may be needed!\n{str(e)[:200]}"
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 8 — Position Tracker + Exit Management
# ─────────────────────────────────────────────────────────────────────────────

open_positions: dict = {}
trades_today:   list = []


def check_exits():
    for symbol, pos in list(open_positions.items()):
        try:
            df = get_15min_bars(symbol, lookback_bars=5)
            if df.empty:
                continue

            current_price = df.iloc[-1]["close"]
            direction     = pos["direction"]
            stop          = pos["stop_loss"]
            target        = pos["take_profit"]
            entry         = pos["entry"]
            contracts     = pos["contracts"]

            hit_target = (direction == "LONG"  and current_price >= target) or \
                         (direction == "SHORT" and current_price <= target)
            hit_stop   = (direction == "LONG"  and current_price <= stop) or \
                         (direction == "SHORT" and current_price >= stop)

            if hit_target:
                log.info(f"TARGET HIT {symbol}: {current_price:,.2f}")
                close_futures_position(symbol, direction, contracts)
                pnl = abs(target - entry) * CONTRACTS[symbol]["point_value"] * contracts
                alert_trade_closed(symbol, "TAKE PROFIT ✅", entry, current_price, contracts)
                trades_today.append({"symbol": symbol, "pnl": pnl})
                del open_positions[symbol]

            elif hit_stop:
                log.info(f"STOP HIT {symbol}: {current_price:,.2f}")
                close_futures_position(symbol, direction, contracts)
                pnl = -abs(stop - entry) * CONTRACTS[symbol]["point_value"] * contracts
                alert_trade_closed(symbol, "STOP LOSS 🛑", entry, current_price, contracts)
                trades_today.append({"symbol": symbol, "pnl": pnl})
                del open_positions[symbol]

        except Exception as e:
            log.error(f"Exit check failed for {symbol}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 9 — Session Hours
# ─────────────────────────────────────────────────────────────────────────────

def is_trading_session() -> bool:
    """
    CME futures hours: Sun 6 PM – Fri 5 PM ET
    Daily maintenance break: 5–6 PM ET
    Skip low-liquidity overnight: 11 PM – 7 AM ET
    """
    now = datetime.now()

    if now.weekday() == 5:                        # full Saturday off
        return False
    if now.weekday() == 6 and now.hour < 18:      # Sunday before 6 PM ET
        return False
    if now.hour == 17:                            # CME maintenance 5-6 PM ET
        return False
    if now.hour >= 23 or now.hour < 7:            # low liquidity overnight
        return False

    return True


def is_end_of_day() -> bool:
    now = datetime.now()
    return now.hour == 16 and now.minute >= 45


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 10 — Main Loop
# ─────────────────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info("KIBO Futures Agent — STARTED")
    log.info(f"Mode        : {'PAPER TRADING' if PAPER_MODE else 'LIVE TRADING'}")
    log.info(f"Instruments : {', '.join(CONTRACTS.keys())}")
    log.info(f"Risk/trade  : {RISK_PCT*100:.1f}% of ${ACCOUNT_SIZE:,.0f}")
    log.info(f"ATR Stop    : {ATR_MULTIPLIER}x ATR  |  R:R: 1:{RR_RATIO}")
    log.info("=" * 60)

    log.info("Startup offset: waiting 10s...")
    time.sleep(10)

    scan_interval    = 3 * 60  # 3 min scans for futures
    last_summary_day = None
    blackout_alerted = set()

    while True:
        now = datetime.now()

        if is_end_of_day() and last_summary_day != now.date():
            alert_daily_summary(trades_today)
            trades_today.clear()
            last_summary_day = now.date()
            blackout_alerted.clear()

        if not is_trading_session():
            log.info("Outside trading session — sleeping 5 min...")
            time.sleep(300)
            continue

        log.info(f"--- Scanning: {', '.join(CONTRACTS.keys())} ---")

        if open_positions:
            check_exits()

        for symbol in CONTRACTS:
            if symbol in open_positions:
                log.info(f"{symbol}: already in position — skipping")
                continue

            blacked_out, event_name = is_in_blackout(symbol)
            if blacked_out:
                log.info(f"{symbol}: BLACKOUT — {event_name}")
                key = f"{symbol}-{event_name}-{now.date()}"
                if key not in blackout_alerted:
                    blackout_alerted.add(key)
                continue

            try:
                df = get_15min_bars(symbol)
                if df.empty:
                    continue

                df = add_indicators(df)

                if len(df) < 2:
                    log.warning(f"{symbol}: not enough bars — skipping")
                    continue

                signal   = check_signal(df)
                last_row = df.iloc[-1]

                if signal == "NONE":
                    # ICC structure scan on every bar
                    _st  = classify_trend(df)
                    _ses = is_in_session()
                    _rv  = detect_reversal(df, _st["trend"])
                    _ind = detect_indication(df)
                    log.info(
                        f"{symbol}: no signal | "
                        f"Price={last_row['close']:,.2f} "
                        f"RSI={last_row['rsi']:.1f} "
                        f"EMA={'>' if last_row['ema9'] > last_row['ema21'] else '<'} "
                        f"ATR={last_row['atr']:.2f} | "
                        f"ICC={_st['trend']}({_st['confidence']*100:.0f}%) "
                        f"Sess={_ses['session']}"
                        + (f" | REVERSAL: {_rv['reason'][:50]}" if _rv["reversal_likely"] else "")
                        + (f" | INDICATION: {_ind['reason'][:50]}" if _ind.get("indication") else "")
                    )
                    continue

                log.info(f"{symbol}: {signal} signal!")

                pos = calc_position(
                    symbol, signal,
                    entry=last_row["close"],
                    atr=last_row["atr"]
                )

                log.info(
                    f"{symbol}: entry={pos['entry']:,.2f} "
                    f"stop={pos['stop_loss']:,.2f} "
                    f"target={pos['take_profit']:,.2f} "
                    f"contracts={pos['contracts']} "
                    f"risk=${pos['risk_usd']:,.2f}"
                )

                # ── ICC Full Rules (all 14 videos) ──────────────────
                sc = check_entry_rules(df, direction)
                if not sc["approved"]:
                    log.info(f"{symbol}: ICC BLOCKED — {sc['reason'][:100]}")
                    continue
                if sc.get("stop_loss") and sc.get("target"):
                    pos["stop_loss"]   = sc["stop_loss"]
                    pos["take_profit"] = sc["target"]
                log.info(f"{symbol}: ICC APPROVED — {sc['reason'][:100]}")
                log.info(f"{symbol}: ICC Stop={sc.get('stop_loss','?')} Target={sc.get('target','?')} RR={sc.get('rr',0):.1f} Session={sc.get('session','?')}")
                # ── News Intelligence ─────────────────────────────────────────
                news = get_news_score(symbol, direction)
                if news["block"]:
                    log.info(f"{symbol}: NEWS BLOCK — {news['reason']}")
                    continue
                if news["boost"]:
                    log.info(f"{symbol}: NEWS BOOST ✅ — {news['reason']}")
                elif news["sentiment"] != "NO_NEWS":
                    log.info(f"{symbol}: NEWS — {news['sentiment']} {news['score']:+d}")
                success = place_futures_order(
                    symbol, signal, pos["contracts"],
                    pos["entry"], pos["stop_loss"], pos["take_profit"]
                )

                if success:
                    open_positions[symbol] = {
                        "direction":   signal,
                        "entry":       pos["entry"],
                        "stop_loss":   pos["stop_loss"],
                        "take_profit": pos["take_profit"],
                        "contracts":   pos["contracts"],
                        "entry_time":  now.isoformat(),
                    }
                    alert_trade_opened(
                        symbol, signal, pos["entry"],
                        pos["stop_loss"], pos["take_profit"],
                        pos["contracts"]
                    )

            except Exception as e:
                log.error(f"{symbol}: unexpected error — {e}")
                send_telegram(
                    f"⚠️ <b>Futures agent error on {symbol}</b>\n{str(e)[:200]}"
                )

        log.info(f"Open positions: {list(open_positions.keys()) or 'none'}")
        log.info(f"Next scan in {scan_interval // 60} min...")
        time.sleep(scan_interval)


if __name__ == "__main__":
    run()
