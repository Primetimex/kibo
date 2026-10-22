"""
KIBO ORB Futures Agent — Opening Range Breakout Strategy
Instruments : MGC (Micro Gold) + MCL (Micro Crude Oil)
              MES (Micro S&P 500) + MNQ (Micro Nasdaq-100)
Strategy    : 15-minute Opening Range Breakout
              - Captures the high/low of the first 15-min bar after open
              - Trades breakouts above (LONG) or below (SHORT) that range
              - Confirmed by RSI, EMA trend, and volume spike
Timeframe   : 15-minute bars
Session     : Trades only 9:30 AM – 12:00 PM ET (highest ORB success rate)
Broker      : TradeZero Developer API
Alerts      : Telegram push notifications

Run alongside futures_agent.py in a separate terminal:
  Terminal 1: python futures_agent.py   (indicator-based)
  Terminal 2: python orb_agent.py       (ORB breakout)

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
from datetime import datetime, timedelta, time as dtime
from dotenv import load_dotenv

# ── Load credentials ──────────────────────────────────────────────────────────
load_dotenv("_env")
POLYGON_API_KEY  = os.getenv("POLYGON_API_KEY")
TZ_API_KEY       = os.getenv("TZ_API_KEY")
TZ_BASE_URL      = "https://api.tradezero.com"
ACCOUNT_SIZE     = float(os.getenv("ACCOUNT_SIZE", "10000"))
PAPER_MODE       = os.getenv("PAPER_MODE", "true").lower() == "true"
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ── Risk settings ─────────────────────────────────────────────────────────────
RISK_PCT          = 0.015   # 1.5% of account per trade
ATR_MULTIPLIER    = 1.5     # stop = entry ± (ATR × multiplier)
RR_RATIO          = 2.0     # 2:1 reward to risk minimum
ATR_PERIOD        = 14

# ── ORB settings ──────────────────────────────────────────────────────────────
ORB_WINDOW_BARS   = 1       # 1 bar × 15 min = 15-minute opening range
MARKET_OPEN       = dtime(9, 30)    # 9:30 AM ET
ORB_LOCK_TIME     = dtime(9, 45)    # opening range is set after first 15-min bar
ORB_CUTOFF        = dtime(12, 0)    # stop taking new ORB trades after noon
                                     # (afternoon ORB trades have lower success)

# ── Breakout confirmation filters ─────────────────────────────────────────────
MIN_RSI_BULL      = 48      # RSI must be above this for LONG breakout
MAX_RSI_BULL      = 75      # RSI must be below this for LONG breakout
MAX_RSI_BEAR      = 52      # RSI must be below this for SHORT breakout
MIN_RSI_BEAR      = 25      # RSI must be above this for SHORT breakout
VOL_MULTIPLIER    = 1.5     # breakout bar volume must be >= 1.5x average
MIN_RANGE_ATR_PCT = 0.3     # opening range must be at least 30% of ATR
                             # (filters out tiny insignificant ranges)

# ── Futures contract specs ────────────────────────────────────────────────────
CONTRACTS = {
    "MGC": {
        "name":           "Micro Gold",
        "polygon_ticker": "GLD",
        "point_value":    10.0,
        "currency":       "USD",
    },
    "MCL": {
        "name":           "Micro Crude Oil",
        "polygon_ticker": "USO",
        "point_value":    100.0,
        "currency":       "USD",
    },
    "MES": {
        "name":           "Micro E-mini S&P 500",
        "polygon_ticker": "SPY",
        "point_value":    5.0,
        "currency":       "USD",
    },
    "MNQ": {
        "name":           "Micro E-mini Nasdaq-100",
        "polygon_ticker": "QQQ",
        "point_value":    2.0,
        "currency":       "USD",
    },
}

ALL_SYMBOLS = list(CONTRACTS.keys())

# ── High-impact event blackout ────────────────────────────────────────────────
HIGH_IMPACT_EVENTS = [
    {"name": "FOMC Rate Decision",   "datetime": "2026-06-11 14:00", "affects": ALL_SYMBOLS},
    {"name": "FOMC Rate Decision",   "datetime": "2026-07-30 14:00", "affects": ALL_SYMBOLS},
    {"name": "FOMC Rate Decision",   "datetime": "2026-09-17 14:00", "affects": ALL_SYMBOLS},
    {"name": "CPI Inflation Report", "datetime": "2026-07-15 08:30", "affects": ALL_SYMBOLS},
    {"name": "CPI Inflation Report", "datetime": "2026-08-12 08:30", "affects": ALL_SYMBOLS},
    {"name": "NFP Jobs Report",      "datetime": "2026-07-10 08:30", "affects": ALL_SYMBOLS},
    {"name": "NFP Jobs Report",      "datetime": "2026-08-07 08:30", "affects": ALL_SYMBOLS},
    {"name": "EIA Inventory Report", "datetime": "2026-06-11 10:30", "affects": ["MCL"]},
    {"name": "EIA Inventory Report", "datetime": "2026-06-18 10:30", "affects": ["MCL"]},
    {"name": "EIA Inventory Report", "datetime": "2026-06-25 10:30", "affects": ["MCL"]},
    {"name": "EIA Inventory Report", "datetime": "2026-07-09 10:30", "affects": ["MCL"]},
    {"name": "EIA Inventory Report", "datetime": "2026-07-16 10:30", "affects": ["MCL"]},
]

BLACKOUT_MINUTES = 30

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("orb_agent.log"),
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
        f"📦 <b>KIBO ORB Agent STARTED</b>\n"
        f"Mode       : {mode}\n"
        f"Strategy   : 15-min Opening Range Breakout\n"
        f"Instruments: MGC, MCL, MES, MNQ\n"
        f"Window     : 9:30–9:45 AM ET (first 15-min bar)\n"
        f"Trade until: 12:00 PM ET\n"
        f"Risk/trade : {RISK_PCT*100:.1f}% of ${ACCOUNT_SIZE:,.0f}\n"
        f"Time       : {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
    )


def alert_orb_set(symbol: str, orb_high: float, orb_low: float, orb_size: float):
    send_telegram(
        f"📐 <b>ORB SET — {symbol}</b>\n"
        f"Name   : {CONTRACTS[symbol]['name']}\n"
        f"High   : {orb_high:.2f}\n"
        f"Low    : {orb_low:.2f}\n"
        f"Range  : {orb_size:.2f} points\n"
        f"Watching for breakout above or below..."
    )


def alert_breakout(symbol: str, direction: str, price: float,
                   orb_high: float, orb_low: float, rsi: float):
    emoji = "🚀" if direction == "LONG" else "📉"
    mode  = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    level = orb_high if direction == "LONG" else orb_low
    send_telegram(
        f"{emoji} <b>ORB BREAKOUT</b> [{mode}]\n"
        f"Symbol    : <b>{symbol} — {CONTRACTS[symbol]['name']}</b>\n"
        f"Direction : <b>{direction}</b>\n"
        f"Price     : {price:.2f}\n"
        f"Broke {'above' if direction == 'LONG' else 'below'}: {level:.2f}\n"
        f"RSI       : {rsi:.1f}\n"
        f"Time      : {datetime.now().strftime('%H:%M:%S ET')}"
    )


def alert_trade_opened(symbol: str, direction: str, entry: float,
                       stop: float, target: float,
                       contracts: int, orb_high: float, orb_low: float):
    emoji    = "📈" if direction == "LONG" else "📉"
    mode     = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    risk_usd = abs(entry - stop) * CONTRACTS[symbol]["point_value"] * contracts
    send_telegram(
        f"{emoji} <b>ORB TRADE OPENED</b> [{mode}]\n"
        f"Symbol    : <b>{symbol} — {CONTRACTS[symbol]['name']}</b>\n"
        f"Direction : {direction}\n"
        f"Entry     : {entry:.2f}\n"
        f"ORB High  : {orb_high:.2f}  |  ORB Low: {orb_low:.2f}\n"
        f"Stop Loss : {stop:.2f}\n"
        f"Target    : {target:.2f}\n"
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
        f"{emoji} <b>ORB TRADE CLOSED — {reason}</b> [{mode}]\n"
        f"Symbol : <b>{symbol}</b>\n"
        f"Entry  : {entry:.2f}  →  Exit: {exit_price:.2f}\n"
        f"P&L    : ${pnl:+,.2f} ({pct:+.2f}%)\n"
        f"Time   : {datetime.now().strftime('%H:%M:%S ET')}"
    )


def alert_daily_summary(trades: list, orb_levels: dict):
    date_str = datetime.now().strftime('%Y-%m-%d')
    if not trades:
        send_telegram(
            f"📊 <b>ORB Daily Summary — {date_str}</b>\n"
            f"No trades taken today."
        )
        return
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    winners   = sum(1 for t in trades if t.get("pnl", 0) > 0)
    send_telegram(
        f"📊 <b>ORB Daily Summary — {date_str}</b>\n"
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

def get_15min_bars(symbol: str, lookback_bars: int = 500) -> pd.DataFrame:
    """
    Fetch 15-minute bars from Polygon.io using ETF proxies.
    30-day window guarantees enough bars for indicator warmup.
    """
    ticker = CONTRACTS[symbol]["polygon_ticker"]
    end    = datetime.now()
    start  = end - timedelta(days=30)

    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/15/minute"
        f"/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        f"?adjusted=true&sort=asc&limit={lookback_bars}&apiKey={POLYGON_API_KEY}"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("resultsCount", 0) == 0:
            log.warning(f"No data for {symbol} ({ticker})")
            return pd.DataFrame()
        df = pd.DataFrame(data["results"])
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
        df.rename(columns={"o":"open","h":"high","l":"low",
                           "c":"close","v":"volume"}, inplace=True)
        return df[["timestamp","open","high","low",
                   "close","volume"]].reset_index(drop=True)
    except Exception as e:
        log.error(f"Data fetch failed for {symbol}: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 4 — Indicator Calculations
# ─────────────────────────────────────────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"]       = calc_rsi(df["close"])
    df["ema9"]      = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"]     = df["close"].ewm(span=21, adjust=False).mean()
    df["atr"]       = calc_atr(df, ATR_PERIOD)
    df["vol_avg20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg20"]
    return df.dropna()


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 5 — ORB Logic
# ─────────────────────────────────────────────────────────────────────────────

def get_opening_range(df: pd.DataFrame) -> dict | None:
    """
    Find the first 15-minute bar of today's session (9:30–9:45 AM ET).
    Returns the high, low, and size of that bar as the opening range.
    Returns None if the opening bar hasn't printed yet or data is missing.

    The opening range is:
      - ORB High = high of the 9:30 AM bar
      - ORB Low  = low of the 9:30 AM bar
      - ORB Size = high - low

    A breakout above ORB High = LONG signal
    A breakout below ORB Low  = SHORT signal
    """
    today = datetime.now().date()

    # Filter to today's bars only
    today_bars = df[df["timestamp"].dt.date == today].copy()

    if today_bars.empty:
        return None

    # Find the 9:30 AM bar specifically
    opening_bars = today_bars[
        (today_bars["timestamp"].dt.hour == 9) &
        (today_bars["timestamp"].dt.minute == 30)
    ]

    if opening_bars.empty:
        log.debug("Opening bar (9:30 AM) not yet available")
        return None

    orb_bar  = opening_bars.iloc[0]
    orb_high = orb_bar["high"]
    orb_low  = orb_bar["low"]
    orb_size = orb_high - orb_low

    return {
        "high":      orb_high,
        "low":       orb_low,
        "size":      orb_size,
        "timestamp": orb_bar["timestamp"],
    }


def check_breakout(df: pd.DataFrame, orb: dict) -> str:
    """
    Check if the current bar has broken out of the opening range.
    Returns 'LONG', 'SHORT', or 'NONE'.

    Breakout confirmation requires ALL of:
      1. Close above ORB High (LONG) or below ORB Low (SHORT)
      2. RSI in valid range for direction
      3. EMA9 aligned with direction
      4. Volume spike on the breakout bar
      5. ORB range is meaningful (>= 30% of ATR — not a tiny range)
    """
    if len(df) < 2:
        return "NONE"

    row = df.iloc[-1]   # current bar
    atr = row["atr"]

    # Filter 1: ORB range must be meaningful
    if orb["size"] < atr * MIN_RANGE_ATR_PCT:
        log.debug(f"ORB range {orb['size']:.2f} too small vs ATR {atr:.2f} — skipping")
        return "NONE"

    vol_ok = row["vol_ratio"] >= VOL_MULTIPLIER

    # LONG breakout: close above ORB high
    if (row["close"] > orb["high"] and
        MIN_RSI_BULL <= row["rsi"] <= MAX_RSI_BULL and
        row["ema9"] > row["ema21"] and
        vol_ok):
        return "LONG"

    # SHORT breakout: close below ORB low
    if (row["close"] < orb["low"] and
        MIN_RSI_BEAR <= row["rsi"] <= MAX_RSI_BEAR and
        row["ema9"] < row["ema21"] and
        vol_ok):
        return "SHORT"

    return "NONE"


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 6 — Position Sizing (ATR-Based)
# ─────────────────────────────────────────────────────────────────────────────

def calc_position(symbol: str, direction: str,
                  entry: float, orb: dict, atr: float) -> dict:
    """
    ORB-specific stop placement:
    - LONG stop  = just below ORB Low (the range low acts as support)
    - SHORT stop = just above ORB High (the range high acts as resistance)

    This is tighter and more logical than a pure ATR stop for ORB trades
    because the ORB level itself is the key invalidation point.

    If ORB-based stop is too tight (< ATR × 0.5), fall back to ATR stop.
    """
    point_val = CONTRACTS[symbol]["point_value"]
    buffer    = atr * 0.1   # small buffer below/above the ORB level

    if direction == "LONG":
        orb_stop    = orb["low"] - buffer
        stop_loss   = orb_stop
        stop_dist   = abs(entry - stop_loss)
        # If stop is too tight, use ATR-based minimum
        if stop_dist < atr * 0.5:
            stop_loss = entry - (atr * ATR_MULTIPLIER)
            stop_dist = abs(entry - stop_loss)
        take_profit = entry + (stop_dist * RR_RATIO)

    else:  # SHORT
        orb_stop    = orb["high"] + buffer
        stop_loss   = orb_stop
        stop_dist   = abs(entry - stop_loss)
        if stop_dist < atr * 0.5:
            stop_loss = entry + (atr * ATR_MULTIPLIER)
            stop_dist = abs(entry - stop_loss)
        take_profit = entry - (stop_dist * RR_RATIO)

    max_risk_usd      = ACCOUNT_SIZE * RISK_PCT
    risk_per_contract = stop_dist * point_val
    contracts         = max(1, int(max_risk_usd / risk_per_contract))

    return {
        "entry":       entry,
        "stop_loss":   round(stop_loss,   2),
        "take_profit": round(take_profit, 2),
        "contracts":   contracts,
        "stop_dist":   stop_dist,
        "risk_usd":    risk_per_contract * contracts,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 7 — TradeZero API Execution
# ─────────────────────────────────────────────────────────────────────────────

TZ_HEADERS = {
    "Authorization": f"Bearer {TZ_API_KEY}",
    "Content-Type":  "application/json",
}


def place_order(symbol: str, direction: str, contracts: int,
                entry: float, stop: float, target: float) -> bool:
    action = "BUY" if direction == "LONG" else "SELL"

    if PAPER_MODE:
        log.info(
            f"[PAPER] {action} {contracts}x {symbol} @ {entry:.2f} | "
            f"SL={stop:.2f}  TP={target:.2f}"
        )
        return True

    payload = {
        "symbol":        symbol,
        "quantity":      contracts,
        "order_type":    "LIMIT",
        "limit_price":   round(entry, 2),
        "action":        action,
        "time_in_force": "GTC",
        "stop_loss":     round(stop,   2),
        "take_profit":   round(target, 2),
    }
    try:
        resp = requests.post(
            f"{TZ_BASE_URL}/v1/futures/orders",
            headers=TZ_HEADERS, json=payload, timeout=10
        )
        resp.raise_for_status()
        log.info(f"Order placed: {resp.json()}")
        return True
    except Exception as e:
        log.error(f"Order failed for {symbol}: {e}")
        send_telegram(f"⚠️ <b>ORB ORDER FAILED — {symbol}</b>\n{str(e)[:200]}")
        return False


def close_position(symbol: str, direction: str, contracts: int) -> bool:
    close_action = "SELL" if direction == "LONG" else "BUY"

    if PAPER_MODE:
        log.info(f"[PAPER] CLOSE {contracts}x {symbol}")
        return True

    payload = {
        "symbol":        symbol,
        "quantity":      contracts,
        "order_type":    "MARKET",
        "action":        close_action,
        "time_in_force": "DAY",
    }
    try:
        resp = requests.post(
            f"{TZ_BASE_URL}/v1/futures/orders",
            headers=TZ_HEADERS, json=payload, timeout=10
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Close failed for {symbol}: {e}")
        send_telegram(
            f"⚠️ <b>ORB CLOSE FAILED — {symbol}</b>\n"
            f"Manual close may be needed!\n{str(e)[:200]}"
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 8 — Position Tracker + Exit Management
# ─────────────────────────────────────────────────────────────────────────────

open_positions: dict = {}
trades_today:   list = []
orb_levels:     dict = {}      # stores today's ORB high/low per symbol
orb_alerted:    set  = set()   # tracks which symbols we've sent ORB set alerts


def check_exits():
    for symbol, pos in list(open_positions.items()):
        try:
            df = get_15min_bars(symbol, lookback_bars=5)
            if df.empty:
                continue

            df            = add_indicators(df)
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
                log.info(f"TARGET HIT {symbol}: {current_price:.2f}")
                close_position(symbol, direction, contracts)
                pnl = abs(target - entry) * CONTRACTS[symbol]["point_value"] * contracts
                alert_trade_closed(symbol, "TAKE PROFIT ✅", entry, current_price, contracts)
                trades_today.append({"symbol": symbol, "pnl": pnl})
                del open_positions[symbol]

            elif hit_stop:
                log.info(f"STOP HIT {symbol}: {current_price:.2f}")
                close_position(symbol, direction, contracts)
                pnl = -abs(stop - entry) * CONTRACTS[symbol]["point_value"] * contracts
                alert_trade_closed(symbol, "STOP LOSS 🛑", entry, current_price, contracts)
                trades_today.append({"symbol": symbol, "pnl": pnl})
                del open_positions[symbol]

        except Exception as e:
            log.error(f"Exit check failed for {symbol}: {e}")


def force_close_all_eod():
    """
    Force close all open positions at end of day (3:55 PM ET).
    ORB is an intraday strategy — never hold overnight.
    """
    if not open_positions:
        return
    log.info("EOD forced close — closing all ORB positions")
    send_telegram(
        f"🔔 <b>ORB EOD CLOSE</b>\n"
        f"Closing all open positions before market close.\n"
        f"Positions: {', '.join(open_positions.keys())}"
    )
    for symbol, pos in list(open_positions.items()):
        close_position(symbol, pos["direction"], pos["contracts"])
        pnl = 0   # exact P&L calculated from fill price — approximated as 0 here
        trades_today.append({"symbol": symbol, "pnl": pnl})
        del open_positions[symbol]


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 9 — Session Hours
# ─────────────────────────────────────────────────────────────────────────────

def is_market_day() -> bool:
    now = datetime.now()
    return now.weekday() < 5   # Monday–Friday only


def current_time() -> dtime:
    return datetime.now().time().replace(second=0, microsecond=0)


def is_orb_window() -> bool:
    """Returns True during the window when ORB trades are valid (9:45 AM – 12:00 PM ET)."""
    t = current_time()
    return ORB_LOCK_TIME <= t <= ORB_CUTOFF


def is_eod_close_time() -> bool:
    """Returns True at 3:55 PM ET — time to force close all positions."""
    now = datetime.now()
    return now.hour == 15 and now.minute >= 55


def is_end_of_day_summary() -> bool:
    now = datetime.now()
    return now.hour == 16 and now.minute >= 15


def should_reset_daily_state() -> bool:
    """Reset ORB levels and trade history at start of each new day."""
    now = datetime.now()
    return now.hour == 9 and now.minute < 15


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 10 — Main Loop
# ─────────────────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info("KIBO ORB Futures Agent — STARTED")
    log.info(f"Mode        : {'PAPER TRADING' if PAPER_MODE else 'LIVE TRADING'}")
    log.info(f"Strategy    : 15-min Opening Range Breakout")
    log.info(f"Instruments : {', '.join(CONTRACTS.keys())}")
    log.info(f"ORB window  : 9:30–9:45 AM ET (1st bar)")
    log.info(f"Trade until : 12:00 PM ET")
    log.info(f"Risk/trade  : {RISK_PCT*100:.1f}% of ${ACCOUNT_SIZE:,.0f}")
    log.info("=" * 60)

    alert_started()

    scan_interval     = 15 * 60   # scan every 15 minutes
    last_summary_day  = None
    last_reset_day    = None
    eod_closed        = False

    while True:
        now = datetime.now()
        t   = current_time()

        # ── Daily state reset at 9:00–9:15 AM ──────────────────────────────
        if is_market_day() and should_reset_daily_state() and last_reset_day != now.date():
            log.info("Daily reset — clearing ORB levels and trade history")
            orb_levels.clear()
            orb_alerted.clear()
            trades_today.clear()
            eod_closed        = False
            last_reset_day    = now.date()

        # ── EOD forced close at 3:55 PM ─────────────────────────────────────
        if is_market_day() and is_eod_close_time() and not eod_closed:
            force_close_all_eod()
            eod_closed = True

        # ── Daily summary at 4:15 PM ─────────────────────────────────────────
        if is_market_day() and is_end_of_day_summary() and last_summary_day != now.date():
            alert_daily_summary(trades_today, orb_levels)
            last_summary_day = now.date()

        # ── Outside market hours — sleep ────────────────────────────────────
        if not is_market_day():
            log.info("Weekend — sleeping 1 hour...")
            time.sleep(3600)
            continue

        if t < MARKET_OPEN:
            wait_secs = (datetime.combine(now.date(), MARKET_OPEN) - now).seconds
            log.info(f"Pre-market — market opens in {wait_secs // 60} min. Sleeping...")
            time.sleep(min(wait_secs, 300))
            continue

        if t > ORB_CUTOFF and not open_positions:
            log.info("Past ORB cutoff (12:00 PM) and no open positions — sleeping until EOD...")
            time.sleep(300)
            continue

        # ── Active session ───────────────────────────────────────────────────
        log.info(f"--- ORB scan: {', '.join(CONTRACTS.keys())} | {t.strftime('%H:%M ET')} ---")

        if open_positions:
            check_exits()

        for symbol in CONTRACTS:
            # Skip if already in position for this symbol today
            if symbol in open_positions:
                log.info(f"{symbol}: in position — monitoring")
                continue

            # Skip if we already traded this symbol today (1 trade per symbol per day)
            if any(t["symbol"] == symbol for t in trades_today):
                log.info(f"{symbol}: already traded today — skipping")
                continue

            # Blackout check
            blacked_out, event_name = is_in_blackout(symbol)
            if blacked_out:
                log.info(f"{symbol}: BLACKOUT — {event_name}")
                continue

            try:
                df = get_15min_bars(symbol)
                if df.empty:
                    continue

                df = add_indicators(df)
                if len(df) < 2:
                    log.warning(f"{symbol}: not enough bars — skipping")
                    continue

                last_row = df.iloc[-1]

                # ── Step 1: Get or update opening range ─────────────────────
                orb = get_opening_range(df)

                if orb is None:
                    log.info(f"{symbol}: waiting for 9:30 AM opening bar...")
                    continue

                # Store and alert when ORB is first set
                if symbol not in orb_levels:
                    orb_levels[symbol] = orb
                    log.info(
                        f"{symbol}: ORB SET — "
                        f"High={orb['high']:.2f}  "
                        f"Low={orb['low']:.2f}  "
                        f"Range={orb['size']:.2f}"
                    )
                    if symbol not in orb_alerted:
                        alert_orb_set(symbol, orb["high"], orb["low"], orb["size"])
                        orb_alerted.add(symbol)

                # ── Step 2: Check for breakout (only after ORB is locked) ───
                if t < ORB_LOCK_TIME:
                    log.info(f"{symbol}: waiting for ORB to lock at 9:45 AM...")
                    continue

                if t > ORB_CUTOFF:
                    log.info(f"{symbol}: past ORB cutoff — no new entries")
                    continue

                orb      = orb_levels[symbol]
                breakout = check_breakout(df, orb)

                if breakout == "NONE":
                    log.info(
                        f"{symbol}: no breakout | "
                        f"Price={last_row['close']:.2f} "
                        f"ORB={orb['low']:.2f}–{orb['high']:.2f} "
                        f"RSI={last_row['rsi']:.1f} "
                        f"Vol={last_row['vol_ratio']:.1f}x"
                    )
                    continue

                # ── Step 3: Confirmed breakout — size and place trade ────────
                log.info(f"{symbol}: {breakout} BREAKOUT confirmed!")
                alert_breakout(
                    symbol, breakout, last_row["close"],
                    orb["high"], orb["low"], last_row["rsi"]
                )

                pos = calc_position(
                    symbol, breakout,
                    entry=last_row["close"],
                    orb=orb,
                    atr=last_row["atr"]
                )

                log.info(
                    f"{symbol}: entry={pos['entry']:.2f} "
                    f"stop={pos['stop_loss']:.2f} "
                    f"target={pos['take_profit']:.2f} "
                    f"contracts={pos['contracts']} "
                    f"risk=${pos['risk_usd']:,.2f}"
                )

                success = place_order(
                    symbol, breakout, pos["contracts"],
                    pos["entry"], pos["stop_loss"], pos["take_profit"]
                )

                if success:
                    open_positions[symbol] = {
                        "direction":   breakout,
                        "entry":       pos["entry"],
                        "stop_loss":   pos["stop_loss"],
                        "take_profit": pos["take_profit"],
                        "contracts":   pos["contracts"],
                        "entry_time":  now.isoformat(),
                        "orb":         orb,
                    }
                    alert_trade_opened(
                        symbol, breakout, pos["entry"],
                        pos["stop_loss"], pos["take_profit"],
                        pos["contracts"], orb["high"], orb["low"]
                    )

            except Exception as e:
                log.error(f"{symbol}: unexpected error — {e}")
                send_telegram(
                    f"⚠️ <b>ORB agent error on {symbol}</b>\n{str(e)[:200]}"
                )

        log.info(f"Open positions: {list(open_positions.keys()) or 'none'}")
        log.info(f"Next scan in {scan_interval // 60} min...")
        time.sleep(scan_interval)


if __name__ == "__main__":
    run()
