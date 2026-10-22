"""
KIBO ORB Futures Agent v2 — Advanced Opening Range Breakout
Instruments : MGC (Micro Gold) + MCL (Micro Crude Oil)
              MES (Micro S&P 500) + MNQ (Micro Nasdaq-100)
Strategy    : Enhanced 5-minute Opening Range Breakout with:
              1. Higher Time Frame (HTF) trend filter — only trade WITH the trend
              2. Fakeout detection + re-entry — fade failed breakouts back inside range
              3. Structure breaks (BOS) — price breaks a recent swing high/low
              4. Liquidity sweeps — price wicks past a level then snaps back
              5. Confluence indicators — EMA, RSI, MACD, ATR, Volume
Broker      : TradeZero Developer API
Alerts      : Telegram push notifications

DISCLAIMER: Futures trading involves substantial risk of loss. Always paper
trade first. You are solely responsible for all trading decisions.
"""

import os
import time
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time as dtime
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
    from news_intel import get_news_score
except ImportError:
    def get_news_score(sym, direction=None): return {"score":0,"sentiment":"NO_NEWS","boost":False,"block":False,"fresh":False,"reason":"news_intel not available"}  # fallback if not present
from structure_rules import check_entry_rules, classify_trend, format_structure_alert, detect_reversal, detect_indication, is_in_session

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

# ── Risk ──────────────────────────────────────────────────────────────────────
RISK_PCT       = 0.015
ATR_PERIOD     = 14
ATR_MULTIPLIER = 1.5
RR_RATIO       = 2.0

# ── ORB settings ──────────────────────────────────────────────────────────────
MARKET_OPEN    = dtime(9, 30)
ORB_LOCK_TIME  = dtime(9, 35)
ORB_CUTOFF     = dtime(12, 0)

# ── HTF trend settings ────────────────────────────────────────────────────────
# Uses the 1-hour chart (12 x 5-min bars) to determine trend direction
# Price above HTF EMA50 = bullish trend, below = bearish trend
HTF_EMA_PERIOD = 50    # EMA period on the 1-hour equivalent
HTF_BARS       = 12    # number of 5-min bars = 1 hour

# ── Fakeout detection settings ────────────────────────────────────────────────
# A fakeout occurs when price breaks the ORB level but then closes back inside
# Re-entry trade is taken in the OPPOSITE direction of the failed breakout
FAKEOUT_CLOSE_INSIDE_PCT = 0.2   # price must close back inside range by at least 20% of range size

# ── Structure break settings ──────────────────────────────────────────────────
# A Break of Structure (BOS) is when price breaks a recent swing high or low
# confirming the trend direction before we enter
BOS_LOOKBACK = 5    # look back 5 bars to find the most recent swing high/low

# ── Liquidity sweep settings ──────────────────────────────────────────────────
# A liquidity sweep is when price wicks past a key level (taking out stop orders)
# then snaps back — a strong reversal signal
SWEEP_WICK_PCT = 0.3   # wick must be at least 30% of the bar's total range

# ── Breakout confirmation ─────────────────────────────────────────────────────
VOL_MULTIPLIER    = 1.5
MIN_RSI_BULL      = 48
MAX_RSI_BULL      = 75
MAX_RSI_BEAR      = 52
MIN_RSI_BEAR      = 25
MIN_RANGE_ATR_PCT = 0.3

# ── Contracts ─────────────────────────────────────────────────────────────────
CONTRACTS = {
    "MGC": {
        "name":           "Micro Gold",
        "polygon_ticker": "GLD",
        "polygon_feed":   "stocks",     # uses /v2/aggs/ticker/{ticker}
        "point_value":    10.0,
    },
    "MCL": {
        "name":           "Micro Crude Oil",
        "polygon_ticker": "USO",
        "polygon_feed":   "stocks",
        "point_value":    100.0,
    },
    "MES": {
        "name":           "Micro E-mini S&P 500",
        "polygon_ticker": "SPY",
        "polygon_feed":   "stocks",
        "point_value":    5.0,
    },
    "MNQ": {
        "name":           "Micro E-mini Nasdaq-100",
        "polygon_ticker": "QQQ",
        "polygon_feed":   "stocks",
        "point_value":    2.0,
    },
    "XAUUSD": {
        "name":           "Spot Gold (XAU/USD)",
        "polygon_ticker": "C:XAUUSD",  # Polygon forex feed — 24hr gold spot price
        "polygon_feed":   "forex",     # uses forex aggregates endpoint
        "point_value":    1.0,          # $1 per $1 move (size position in oz)
        "min_trade_oz":   1.0,          # minimum 1 troy oz
    },
}

ALL_SYMBOLS = list(CONTRACTS.keys())

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("orb_v2_agent.log"),
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
        f"📦 <b>KIBO ORB Agent v2 STARTED</b>\n"
        f"Mode       : {mode}\n"
        f"Strategy   : Advanced 5-min ORB\n"
        f"Instruments: MGC, MCL, MES, MNQ\n"
        f"Features   :\n"
        f"  • HTF trend filter (1-hr EMA50)\n"
        f"  • Fakeout + re-entry trades\n"
        f"  • Structure breaks (BOS)\n"
        f"  • Liquidity sweeps\n"
        f"  • RSI + EMA + MACD + Volume\n"
        f"Risk/trade : {RISK_PCT*100:.1f}% of ${ACCOUNT_SIZE:,.0f}\n"
        f"Time       : {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
    )


def alert_htf_trend(symbol: str, trend: str, htf_ema: float, price: float):
    emoji = "📈" if trend == "BULLISH" else "📉"
    send_telegram(
        f"{emoji} <b>HTF TREND — {symbol}</b>\n"
        f"Trend      : <b>{trend}</b>\n"
        f"Price      : {price:.2f}\n"
        f"1-hr EMA50 : {htf_ema:.2f}\n"
        f"Only taking <b>{'LONG' if trend == 'BULLISH' else 'SHORT'}</b> ORB trades today"
    )


def alert_orb_set(symbol: str, orb_high: float, orb_low: float,
                  orb_size: float, trend: str):
    trend_emoji = "📈" if trend == "BULLISH" else "📉" if trend == "BEARISH" else "↔️"
    send_telegram(
        f"📐 <b>ORB SET — {symbol}</b>\n"
        f"Name       : {CONTRACTS[symbol]['name']}\n"
        f"High       : {orb_high:.2f}\n"
        f"Low        : {orb_low:.2f}\n"
        f"Range      : {orb_size:.2f} pts\n"
        f"HTF Trend  : {trend_emoji} {trend}\n"
        f"Bias       : {'LONG only (with trend)' if trend == 'BULLISH' else 'SHORT only (with trend)' if trend == 'BEARISH' else 'Both directions'}"
    )


def alert_fakeout(symbol: str, failed_direction: str, price: float):
    send_telegram(
        f"⚠️ <b>FAKEOUT DETECTED — {symbol}</b>\n"
        f"Failed breakout : {failed_direction}\n"
        f"Price closed back inside range at {price:.2f}\n"
        f"Watching for re-entry in opposite direction..."
    )


def alert_signal(symbol: str, signal_type: str, direction: str,
                 price: float, reasons: list):
    emoji = "🚀" if direction == "LONG" else "📉"
    mode  = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    reasons_str = "\n  • ".join(reasons)
    send_telegram(
        f"{emoji} <b>{signal_type} SIGNAL</b> [{mode}]\n"
        f"Symbol     : <b>{symbol} — {CONTRACTS[symbol]['name']}</b>\n"
        f"Direction  : <b>{direction}</b>\n"
        f"Price      : {price:.2f}\n"
        f"Confirmed by:\n  • {reasons_str}\n"
        f"Time       : {datetime.now().strftime('%H:%M:%S ET')}"
    )


def alert_trade_opened(symbol: str, signal_type: str, direction: str,
                       entry: float, stop: float, target: float,
                       contracts: int, orb_high: float, orb_low: float):
    emoji    = "📈" if direction == "LONG" else "📉"
    mode     = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    risk_usd = abs(entry - stop) * CONTRACTS[symbol]["point_value"] * contracts
    send_telegram(
        f"{emoji} <b>ORB TRADE OPENED [{signal_type}]</b> [{mode}]\n"
        f"Symbol    : <b>{symbol} — {CONTRACTS[symbol]['name']}</b>\n"
        f"Direction : {direction}\n"
        f"Entry     : {entry:.2f}\n"
        f"ORB Range : {orb_low:.2f} – {orb_high:.2f}\n"
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


def alert_daily_summary(trades: list):
    date_str = datetime.now().strftime('%Y-%m-%d')
    if not trades:
        send_telegram(f"📊 <b>ORB v2 Daily Summary — {date_str}</b>\nNo trades today.")
        return
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    winners   = sum(1 for t in trades if t.get("pnl", 0) > 0)
    breakouts = sum(1 for t in trades if t.get("type") == "BREAKOUT")
    reentries = sum(1 for t in trades if t.get("type") == "REENTRY")
    send_telegram(
        f"📊 <b>ORB v2 Daily Summary — {date_str}</b>\n"
        f"Trades      : {len(trades)} "
        f"({breakouts} breakouts, {reentries} re-entries)\n"
        f"Winners     : {winners} / {len(trades)}\n"
        f"Total P&L   : ${total_pnl:+,.2f}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 2 — News Blackout
# ─────────────────────────────────────────────────────────────────────────────

def is_in_blackout(symbol: str) -> tuple[bool, str]:
    now = datetime.now()
    for event in HIGH_IMPACT_EVENTS:
        if symbol not in event["affects"]:
            continue
        event_dt = datetime.strptime(event["datetime"], "%Y-%m-%d %H:%M")
        if abs((now - event_dt).total_seconds() / 60) <= BLACKOUT_MINUTES:
            return True, event["name"]
    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 3 — Market Data
# ─────────────────────────────────────────────────────────────────────────────

def get_15min_bars(symbol: str, lookback_bars: int = 500) -> pd.DataFrame:
    """
    Fetch 5-minute bars from Polygon.io.
    - Stocks/ETFs  : uses standard aggregates endpoint
    - Forex/XAUUSD : uses forex aggregates endpoint (C:XAUUSD)
    Both supported on Polygon free tier.
    30-day window guarantees enough bars for indicator warmup.
    """
    ticker = CONTRACTS[symbol]["polygon_ticker"]
    feed   = CONTRACTS[symbol].get("polygon_feed", "stocks")
    end    = datetime.now()
    start  = end - timedelta(days=30)

    # Forex feed uses the same aggregates URL structure — ticker prefix C: handles routing
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/5/minute"
        f"/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        f"?adjusted=true&sort=asc&limit={lookback_bars}&apiKey={POLYGON_API_KEY}"
    )
    try:
        wait_for_slot(symbol)
        for _attempt in range(3):
            resp = requests.get(url, timeout=10)
            if resp.status_code == 429:
                log.warning(f"{symbol}: 429 rate limit — waiting 20s")
                time.sleep(20 * (_attempt + 1))
                continue
            resp.raise_for_status()
            break
        data = resp.json()
        if data.get("resultsCount", 0) == 0:
            log.warning(f"No data for {symbol} ({ticker}) [{feed}]")
            return pd.DataFrame()
        df = pd.DataFrame(data["results"])
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
        df.rename(columns={"o":"open","h":"high","l":"low",
                           "c":"close","v":"volume"}, inplace=True)
        # Forex volume may be 0 — fill with 1 to avoid division errors in vol_ratio
        if feed == "forex":
            df["volume"] = df["volume"].replace(0, 1)
        time.sleep(13)  # rate limit: free tier = 5 calls/min (12s min)
        log.info(f"{symbol}: fetched {len(df)} bars [{feed}]")
        return df[["timestamp","open","high","low",
                   "close","volume"]].reset_index(drop=True)
    except Exception as e:
        log.error(f"Data fetch failed for {symbol}: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 4 — Indicators
# ─────────────────────────────────────────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period-1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period-1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast    = series.ewm(span=fast, adjust=False).mean()
    ema_slow    = series.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


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
    df["rsi"]         = calc_rsi(df["close"])
    df["ema9"]        = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"]       = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"]       = df["close"].ewm(span=50, adjust=False).mean()
    df["macd"], df["macd_signal"], df["macd_hist"] = calc_macd(df["close"])
    df["atr"]         = calc_atr(df, ATR_PERIOD)
    df["vol_avg20"]   = df["volume"].rolling(20).mean()
    df["vol_ratio"]   = df["volume"] / df["vol_avg20"]
    # Swing highs and lows for BOS detection
    df["swing_high"]  = df["high"].rolling(BOS_LOOKBACK).max()
    df["swing_low"]   = df["low"].rolling(BOS_LOOKBACK).min()
    return df.dropna()


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 5 — HTF Trend Filter
# ─────────────────────────────────────────────────────────────────────────────

def get_htf_trend(df: pd.DataFrame) -> tuple[str, float]:
    """
    Determines the Higher Time Frame (HTF) trend using the 1-hour equivalent.

    Method:
    - Groups 15-min bars into 1-hour bars (every 4 bars)
    - Applies EMA50 to the 1-hour closes
    - Price above EMA50 = BULLISH trend
    - Price below EMA50 = BEARISH trend

    This ensures we only trade ORB breakouts IN THE DIRECTION OF THE TREND.
    A LONG breakout is only taken when HTF is BULLISH.
    A SHORT breakout is only taken when HTF is BEARISH.
    """
    if len(df) < HTF_EMA_PERIOD * HTF_BARS + 10:
        return "NEUTRAL", df["close"].iloc[-1]

    # Build 1-hour bars from 15-min bars
    df_copy = df.copy()
    df_copy = df_copy.set_index("timestamp")
    hourly  = df_copy["close"].resample("1h").last().dropna()

    if len(hourly) < HTF_EMA_PERIOD:
        return "NEUTRAL", df["close"].iloc[-1]

    # Calculate EMA50 on hourly closes
    htf_ema = hourly.ewm(span=HTF_EMA_PERIOD, adjust=False).mean()
    current_price = hourly.iloc[-1]
    current_ema   = htf_ema.iloc[-1]

    if current_price > current_ema:
        return "BULLISH", current_ema
    elif current_price < current_ema:
        return "BEARISH", current_ema
    else:
        return "NEUTRAL", current_ema


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 6 — Structure Break (BOS) Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_bos(df: pd.DataFrame, direction: str) -> tuple[bool, str]:
    """
    Break of Structure (BOS) — confirms trend direction.

    BULLISH BOS: current close breaks ABOVE the recent swing high
                 → buyers are in control, trend confirmed UP
    BEARISH BOS: current close breaks BELOW the recent swing low
                 → sellers are in control, trend confirmed DOWN

    This is a key Smart Money Concept (SMC) confirmation tool.
    """
    if len(df) < BOS_LOOKBACK + 2:
        return False, ""

    row       = df.iloc[-1]
    prev_row  = df.iloc[-2]

    # Swing high/low from the previous bars (exclude current bar)
    recent_swing_high = df.iloc[-(BOS_LOOKBACK+1):-1]["high"].max()
    recent_swing_low  = df.iloc[-(BOS_LOOKBACK+1):-1]["low"].min()

    if direction == "LONG":
        if row["close"] > recent_swing_high and prev_row["close"] <= recent_swing_high:
            return True, f"BOS above swing high {recent_swing_high:.2f}"

    elif direction == "SHORT":
        if row["close"] < recent_swing_low and prev_row["close"] >= recent_swing_low:
            return True, f"BOS below swing low {recent_swing_low:.2f}"

    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 7 — Liquidity Sweep Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_liquidity_sweep(df: pd.DataFrame, orb: dict) -> tuple[bool, str, str]:
    """
    Liquidity Sweep — price wicks past a key level then snaps back.

    This is one of the most powerful signals in Smart Money trading:
    - Price spikes ABOVE the ORB high (taking out buy-stop orders)
      then CLOSES BACK BELOW — this is bearish (shorts incoming)
    - Price spikes BELOW the ORB low (taking out sell-stop orders)
      then CLOSES BACK ABOVE — this is bullish (longs incoming)

    The sweep tells us large institutions grabbed liquidity at that level
    and are now driving price in the opposite direction.

    Returns: (sweep_detected, direction_to_trade, description)
    """
    if len(df) < 2:
        return False, "", ""

    row      = df.iloc[-1]
    bar_size = row["high"] - row["low"]

    if bar_size == 0:
        return False, "", ""

    # Bullish sweep: wick below ORB low, close back above it
    low_wick = row["low"] - orb["low"]
    if (row["low"] < orb["low"] and          # wicked below ORB low
        row["close"] > orb["low"] and         # closed back above ORB low
        abs(low_wick) >= bar_size * SWEEP_WICK_PCT):   # wick is significant
        desc = (f"Liquidity sweep below ORB low {orb['low']:.2f} "
                f"(wick: {abs(low_wick):.2f})")
        return True, "LONG", desc

    # Bearish sweep: wick above ORB high, close back below it
    high_wick = row["high"] - orb["high"]
    if (row["high"] > orb["high"] and         # wicked above ORB high
        row["close"] < orb["high"] and         # closed back below ORB high
        abs(high_wick) >= bar_size * SWEEP_WICK_PCT):  # wick is significant
        desc = (f"Liquidity sweep above ORB high {orb['high']:.2f} "
                f"(wick: {abs(high_wick):.2f})")
        return True, "SHORT", desc

    return False, "", ""


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 8 — Fakeout Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_fakeout(df: pd.DataFrame, orb: dict,
                   prev_breakout_direction: str) -> tuple[bool, str]:
    """
    Fakeout — price broke out of the ORB range but closed back inside.

    This is a failed breakout. When this happens we FADE it:
    - Price broke ABOVE ORB high but closed back BELOW it = SHORT re-entry
    - Price broke BELOW ORB low but closed back ABOVE it = LONG re-entry

    The re-entry trade is taken IN THE OPPOSITE DIRECTION of the failed break.

    We only detect this if we saw a previous breakout attempt that failed.
    """
    if not prev_breakout_direction or len(df) < 2:
        return False, ""

    row      = df.iloc[-1]
    orb_size = orb["high"] - orb["low"]

    if prev_breakout_direction == "LONG":
        # Was a bullish breakout — check if price closed back inside
        if (row["close"] < orb["high"] and
            row["close"] > orb["low"] and
            (orb["high"] - row["close"]) >= orb_size * FAKEOUT_CLOSE_INSIDE_PCT):
            return True, "SHORT"  # fade the failed bull breakout

    elif prev_breakout_direction == "SHORT":
        # Was a bearish breakout — check if price closed back inside
        if (row["close"] > orb["low"] and
            row["close"] < orb["high"] and
            (row["close"] - orb["low"]) >= orb_size * FAKEOUT_CLOSE_INSIDE_PCT):
            return True, "LONG"   # fade the failed bear breakout

    return False, ""


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 9 — ORB Core Logic
# ─────────────────────────────────────────────────────────────────────────────

def utc_to_et(ts_series):
    """
    Convert UTC timestamps to US Eastern Time without pytz.
    EDT (Mar-Nov): UTC-4  |  EST (Nov-Mar): UTC-5
    Uses pandas' built-in UTC offset handling.
    """
    import pandas as pd
    # Polygon returns millisecond UTC timestamps
    # Convert to ET by subtracting the UTC offset
    now = datetime.now()
    # Determine if we are in EDT (UTC-4) or EST (UTC-5)
    # DST runs from 2nd Sunday in March to 1st Sunday in November
    from datetime import timezone, timedelta
    year = now.year
    # 2nd Sunday in March
    march_1 = datetime(year, 3, 1)
    dst_start = march_1 + timedelta(days=(6 - march_1.weekday()) % 7 + 7)
    # 1st Sunday in November
    nov_1 = datetime(year, 11, 1)
    dst_end = nov_1 + timedelta(days=(6 - nov_1.weekday()) % 7)
    if dst_start <= now < dst_end:
        offset = pd.Timedelta(hours=-4)   # EDT
    else:
        offset = pd.Timedelta(hours=-5)   # EST
    return ts_series + offset


def get_opening_range(df: pd.DataFrame) -> dict | None:
    """
    Get the high and low of the 9:30 AM ET 5-min bar.
    Converts UTC timestamps to Eastern Time before searching.
    """
    df = df.copy()
    df["ts_et"]  = utc_to_et(df["timestamp"])
    today_et     = datetime.now().date()
    today_bars   = df[df["ts_et"].dt.date == today_et]

    if today_bars.empty:
        log.debug("No bars for today in ET timezone")
        return None

    # The 9:30 AM ET 5-min bar opens at 9:30 and closes at 9:35
    opening = today_bars[
        (today_bars["ts_et"].dt.hour   == 9) &
        (today_bars["ts_et"].dt.minute == 30)
    ]

    if opening.empty:
        # Log what times we DO have to help debug
        times = today_bars["ts_et"].dt.strftime("%H:%M").tolist()[:5]
        log.debug(f"No 9:30 ET bar found. Available times: {times}")
        return None

    bar = opening.iloc[0]
    return {
        "high":      bar["high"],
        "low":       bar["low"],
        "size":      bar["high"] - bar["low"],
        "timestamp": bar["ts_et"],
    }


def check_breakout_signal(df: pd.DataFrame, orb: dict,
                          htf_trend: str) -> tuple[str, list]:
    """
    Check for a confirmed ORB breakout WITH all confluence filters.

    Returns (direction, reasons_list) or ("NONE", [])

    Confluence required:
      1. Price closes above ORB High (LONG) or below ORB Low (SHORT)
      2. HTF trend aligns with breakout direction
      3. RSI in valid range
      4. EMA9 > EMA21 (bull) or EMA9 < EMA21 (bear)
      5. MACD histogram positive (bull) or negative (bear)
      6. Volume spike >= 1.5x
      7. ORB range is meaningful
    """
    if len(df) < 2:
        return "NONE", []

    row  = df.iloc[-1]
    atr  = row["atr"]
    reasons = []

    if orb["size"] < atr * MIN_RANGE_ATR_PCT:
        return "NONE", []

    vol_ok = row["vol_ratio"] >= VOL_MULTIPLIER

    # ── LONG breakout ─────────────────────────────────────────────────────────
    if (row["close"] > orb["high"] and
        (htf_trend == "BULLISH" or htf_trend == "NEUTRAL") and
        MIN_RSI_BULL <= row["rsi"] <= MAX_RSI_BULL and
        row["ema9"] > row["ema21"] and
        row["macd_hist"] > 0 and
        vol_ok):

        reasons.append(f"Price broke above ORB high {orb['high']:.2f}")
        if htf_trend == "BULLISH":
            reasons.append(f"HTF trend: BULLISH (1-hr EMA50 aligned)")
        reasons.append(f"RSI: {row['rsi']:.1f} (bullish range)")
        reasons.append(f"EMA9 > EMA21 (uptrend confirmed)")
        reasons.append(f"MACD histogram positive")
        reasons.append(f"Volume: {row['vol_ratio']:.1f}x average")

        # Check for BOS confirmation
        bos, bos_desc = detect_bos(df, "LONG")
        if bos:
            reasons.append(f"BOS: {bos_desc}")

        return "LONG", reasons

    # ── SHORT breakout ────────────────────────────────────────────────────────
    if (row["close"] < orb["low"] and
        (htf_trend == "BEARISH" or htf_trend == "NEUTRAL") and
        MIN_RSI_BEAR <= row["rsi"] <= MAX_RSI_BEAR and
        row["ema9"] < row["ema21"] and
        row["macd_hist"] < 0 and
        vol_ok):

        reasons.append(f"Price broke below ORB low {orb['low']:.2f}")
        if htf_trend == "BEARISH":
            reasons.append(f"HTF trend: BEARISH (1-hr EMA50 aligned)")
        reasons.append(f"RSI: {row['rsi']:.1f} (bearish range)")
        reasons.append(f"EMA9 < EMA21 (downtrend confirmed)")
        reasons.append(f"MACD histogram negative")
        reasons.append(f"Volume: {row['vol_ratio']:.1f}x average")

        bos, bos_desc = detect_bos(df, "SHORT")
        if bos:
            reasons.append(f"BOS: {bos_desc}")

        return "SHORT", reasons

    return "NONE", []


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 10 — Position Sizing
# ─────────────────────────────────────────────────────────────────────────────

def calc_position(symbol: str, direction: str,
                  entry: float, orb: dict, atr: float) -> dict:
    """
    Stop placed at ORB level (logical invalidation point).
    Minimum stop = ATR × 0.5 to avoid stops that are too tight.

    For XAUUSD: position sized in troy ounces instead of contracts.
    For futures (MGC, MCL, MES, MNQ): sized in contracts.
    """
    point_val = CONTRACTS[symbol]["point_value"]
    buffer    = atr * 0.1

    if direction == "LONG":
        stop_loss   = orb["low"] - buffer
        stop_dist   = abs(entry - stop_loss)
        if stop_dist < atr * 0.5:
            stop_loss = entry - (atr * ATR_MULTIPLIER)
            stop_dist = abs(entry - stop_loss)
        take_profit = entry + (stop_dist * RR_RATIO)
    else:
        stop_loss   = orb["high"] + buffer
        stop_dist   = abs(entry - stop_loss)
        if stop_dist < atr * 0.5:
            stop_loss = entry + (atr * ATR_MULTIPLIER)
            stop_dist = abs(entry - stop_loss)
        take_profit = entry - (stop_dist * RR_RATIO)

    max_risk_usd = ACCOUNT_SIZE * RISK_PCT

    # XAUUSD: size in ounces, risk = stop_dist × oz
    if symbol == "XAUUSD":
        min_oz        = CONTRACTS[symbol].get("min_trade_oz", 1.0)
        oz            = max(min_oz, round(max_risk_usd / stop_dist, 2))
        return {
            "entry":       entry,
            "stop_loss":   round(stop_loss,   2),
            "take_profit": round(take_profit, 2),
            "contracts":   oz,   # ounces for XAUUSD
            "risk_usd":    stop_dist * oz,
        }

    # Futures: size in contracts
    risk_per_contract = stop_dist * point_val
    contracts         = max(1, int(max_risk_usd / risk_per_contract))
    return {
        "entry":       entry,
        "stop_loss":   round(stop_loss,   2),
        "take_profit": round(take_profit, 2),
        "contracts":   contracts,
        "risk_usd":    risk_per_contract * contracts,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 11 — TradeZero Execution
# ─────────────────────────────────────────────────────────────────────────────

TZ_HEADERS = {
    "TZ-API-KEY-ID":     TZ_API_KEY_ID,
    "TZ-API-SECRET-KEY": TZ_API_SECRET_KEY,
    "Content-Type":      "application/json",
}


def place_order(symbol: str, direction: str, contracts: int,
                entry: float, stop: float, target: float) -> bool:
    action = "BUY" if direction == "LONG" else "SELL"
    log.info(
        f"[{'PAPER' if PAPER_MODE else 'LIVE'}] {action} {contracts}x {symbol} @ {entry:.2f} | "
        f"SL={stop:.2f}  TP={target:.2f}"
    )
    import uuid as _uuid
    client_id = f"KIBO-{_uuid.uuid4().hex[:8].upper()}"
    payload = {
        "clientOrderId": client_id,
        "securityType":  "Stock",
        "symbol":        symbol,
        "side":          "Buy" if action=="BUY" else "Sell",
        "openClose":     "Open",
        "orderType":     "Market",
        "orderQuantity": contracts,
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
        log.error(f"Order failed: {e}")
        send_telegram(f"⚠️ <b>ORDER FAILED — {symbol}</b>\n{str(e)[:200]}")
        return False


def close_position(symbol: str, direction: str, contracts: int) -> bool:
    close_action = "SELL" if direction == "LONG" else "BUY"
    log.info(f"[{'PAPER' if PAPER_MODE else 'LIVE'}] CLOSE {contracts}x {symbol}")
    import uuid as _uuid_c
    close_id = f"KIBO-C-{_uuid_c.uuid4().hex[:6].upper()}"
    payload = {
        "clientOrderId": close_id,
        "securityType":  "Stock",
        "symbol":        symbol,
        "side":          "Sell" if close_action=="SELL" else "Buy",
        "openClose":     "Close",
        "orderType":     "Market",
        "orderQuantity": contracts,
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
        log.error(f"Close failed: {e}")
        send_telegram(
            f"⚠️ <b>CLOSE FAILED — {symbol}</b>\n"
            f"Manual close may be needed!\n{str(e)[:200]}"
        )
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 12 — Position Tracker + Exit Management
# ─────────────────────────────────────────────────────────────────────────────

open_positions:      dict = {}
trades_today:        list = []
orb_levels:          dict = {}
orb_alerted:         set  = set()
htf_trends:          dict = {}
htf_alerted:         set  = set()
failed_breakouts:    dict = {}   # tracks failed breakout directions per symbol


def check_exits():
    for symbol, pos in list(open_positions.items()):
        try:
            df = get_15min_bars(symbol, lookback_bars=10)
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
                trades_today.append({"symbol": symbol, "pnl": pnl, "type": pos.get("trade_type", "BREAKOUT")})
                del open_positions[symbol]

            elif hit_stop:
                log.info(f"STOP HIT {symbol}: {current_price:.2f}")
                close_position(symbol, direction, contracts)
                pnl = -abs(stop - entry) * CONTRACTS[symbol]["point_value"] * contracts
                alert_trade_closed(symbol, "STOP LOSS 🛑", entry, current_price, contracts)
                trades_today.append({"symbol": symbol, "pnl": pnl, "type": pos.get("trade_type", "BREAKOUT")})
                del open_positions[symbol]

        except Exception as e:
            log.error(f"Exit check error {symbol}: {e}")


def force_close_all_eod():
    if not open_positions:
        return
    send_telegram(
        f"🔔 <b>ORB v2 EOD CLOSE</b>\n"
        f"Closing all positions: {', '.join(open_positions.keys())}"
    )
    for symbol, pos in list(open_positions.items()):
        close_position(symbol, pos["direction"], pos["contracts"])
        trades_today.append({"symbol": symbol, "pnl": 0, "type": pos.get("trade_type", "BREAKOUT")})
        del open_positions[symbol]


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 13 — Session Hours
# ─────────────────────────────────────────────────────────────────────────────

def is_market_day() -> bool:
    return datetime.now().weekday() < 5


def current_et() -> dtime:
    return datetime.now().time().replace(second=0, microsecond=0)


def is_eod_close_time() -> bool:
    now = datetime.now()
    return now.hour == 15 and now.minute >= 55


def is_end_of_day_summary() -> bool:
    now = datetime.now()
    return now.hour == 16 and now.minute >= 15


def should_reset_daily() -> bool:
    now = datetime.now()
    return now.hour == 9 and now.minute < 15


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 14 — Main Loop
# ─────────────────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info("KIBO ORB Agent v2 — STARTED")
    log.info(f"Mode        : {'PAPER TRADING' if PAPER_MODE else 'LIVE TRADING'}")
    log.info(f"Features    : HTF trend + Fakeout + BOS + Liquidity Sweep")
    log.info(f"Instruments : {', '.join(CONTRACTS.keys())}")
    log.info(f"Risk/trade  : {RISK_PCT*100:.1f}% of ${ACCOUNT_SIZE:,.0f}")
    log.info("=" * 60)

    log.info("Startup offset: waiting 5s...")
    time.sleep(5)

    scan_interval    = 5 * 60
    last_summary_day = None
    last_reset_day   = None
    eod_closed       = False

    while True:
        now = datetime.now()
        t   = current_et()

        # ── Daily reset ─────────────────────────────────────────────────────
        if is_market_day() and should_reset_daily() and last_reset_day != now.date():
            log.info("Daily reset — clearing state")
            orb_levels.clear()
            orb_alerted.clear()
            htf_trends.clear()
            htf_alerted.clear()
            failed_breakouts.clear()
            trades_today.clear()
            eod_closed     = False
            last_reset_day = now.date()

        # ── EOD force close ─────────────────────────────────────────────────
        if is_market_day() and is_eod_close_time() and not eod_closed:
            force_close_all_eod()
            eod_closed = True

        # ── Daily summary ───────────────────────────────────────────────────
        if is_market_day() and is_end_of_day_summary() and last_summary_day != now.date():
            alert_daily_summary(trades_today)
            last_summary_day = now.date()

        # ── Weekend ─────────────────────────────────────────────────────────
        if not is_market_day():
            log.info("Weekend — sleeping 1 hour...")
            time.sleep(3600)
            continue

        # ── Pre-market ──────────────────────────────────────────────────────
        if t < MARKET_OPEN:
            wait = (datetime.combine(now.date(), MARKET_OPEN) - now).seconds
            log.info(f"Pre-market — opens in {wait // 60} min...")
            time.sleep(min(wait, 300))
            continue

        # ── Past ORB cutoff ─────────────────────────────────────────────────
        if t > ORB_CUTOFF and not open_positions:
            log.info("Past ORB cutoff — sleeping until EOD...")
            time.sleep(300)
            continue

        log.info(f"--- ORB v2 scan: {', '.join(CONTRACTS.keys())} | {t.strftime('%H:%M ET')} ---")

        if open_positions:
            check_exits()

        for symbol in CONTRACTS:
            if symbol in open_positions:
                log.info(f"{symbol}: in position — monitoring")
                continue

            blacked_out, event_name = is_in_blackout(symbol)
            if blacked_out:
                log.info(f"{symbol}: BLACKOUT — {event_name}")
                continue

            # Skip if already had a completed trade today on this symbol
            symbol_trades = [tr for tr in trades_today if tr["symbol"] == symbol]
            if len(symbol_trades) >= 2:
                log.info(f"{symbol}: max trades reached today — skipping")
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

                # ── Step 1: HTF Trend ────────────────────────────────────────
                htf_trend, htf_ema = get_htf_trend(df)
                if symbol not in htf_trends:
                    htf_trends[symbol] = htf_trend
                    if symbol not in htf_alerted:
                        alert_htf_trend(symbol, htf_trend, htf_ema, last_row["close"])
                        htf_alerted.add(symbol)
                        log.info(f"{symbol}: HTF trend = {htf_trend} | EMA50={htf_ema:.2f}")

                # ── Step 2: Opening Range ────────────────────────────────────
                orb = get_opening_range(df)
                if orb is None:
                    log.info(f"{symbol}: waiting for 9:30 AM bar...")
                    continue

                if symbol not in orb_levels:
                    orb_levels[symbol] = orb
                    if symbol not in orb_alerted:
                        alert_orb_set(symbol, orb["high"], orb["low"],
                                      orb["size"], htf_trend)
                        orb_alerted.add(symbol)
                        log.info(
                            f"{symbol}: ORB SET — "
                            f"High={orb['high']:.2f} "
                            f"Low={orb['low']:.2f} "
                            f"HTF={htf_trend}"
                        )

                if t < ORB_LOCK_TIME:
                    log.info(f"{symbol}: waiting for ORB lock at 9:35...")
                    continue

                if t > ORB_CUTOFF:
                    log.info(f"{symbol}: past cutoff — no new entries")
                    continue

                orb = orb_levels[symbol]

                # ── Step 3: Liquidity Sweep check ────────────────────────────
                sweep, sweep_dir, sweep_desc = detect_liquidity_sweep(df, orb)
                if sweep and (htf_trend == sweep_dir or htf_trend == "NEUTRAL"):
                    reasons = [sweep_desc, f"HTF trend: {htf_trend}"]
                    bos, bos_desc = detect_bos(df, sweep_dir)
                    if bos:
                        reasons.append(f"BOS: {bos_desc}")

                    log.info(f"{symbol}: LIQUIDITY SWEEP {sweep_dir}")

                    pos = calc_position(symbol, sweep_dir,
                                        last_row["close"], orb, last_row["atr"])
                    success = place_order(
                        symbol, sweep_dir, pos["contracts"],
                        pos["entry"], pos["stop_loss"], pos["take_profit"]
                    )
                    if success:
                        open_positions[symbol] = {
                            "direction":   sweep_dir,
                            "entry":       pos["entry"],
                            "stop_loss":   pos["stop_loss"],
                            "take_profit": pos["take_profit"],
                            "contracts":   pos["contracts"],
                            "trade_type":  "SWEEP",
                            "entry_time":  now.isoformat(),
                            "orb":         orb,
                        }
                        alert_trade_opened(
                            symbol, "LIQUIDITY SWEEP", sweep_dir,
                            pos["entry"], pos["stop_loss"], pos["take_profit"],
                            pos["contracts"], orb["high"], orb["low"]
                        )
                    continue

                # ── Step 4: Fakeout re-entry check ───────────────────────────
                prev_break = failed_breakouts.get(symbol)
                is_fakeout, fakeout_dir = detect_fakeout(df, orb, prev_break)
                if is_fakeout and (htf_trend == fakeout_dir or htf_trend == "NEUTRAL"):
                    reasons = [
                        f"Fakeout: failed {prev_break} breakout",
                        f"Price closed back inside range",
                        f"HTF trend: {htf_trend}",
                        f"Re-entry {fakeout_dir} (fade the fakeout)",
                    ]
                    alert_fakeout(symbol, prev_break, last_row["close"])

                    pos = calc_position(symbol, fakeout_dir,
                                        last_row["close"], orb, last_row["atr"])
                    success = place_order(
                        symbol, fakeout_dir, pos["contracts"],
                        pos["entry"], pos["stop_loss"], pos["take_profit"]
                    )
                    if success:
                        open_positions[symbol] = {
                            "direction":   fakeout_dir,
                            "entry":       pos["entry"],
                            "stop_loss":   pos["stop_loss"],
                            "take_profit": pos["take_profit"],
                            "contracts":   pos["contracts"],
                            "trade_type":  "REENTRY",
                            "entry_time":  now.isoformat(),
                            "orb":         orb,
                        }
                        alert_trade_opened(
                            symbol, "FAKEOUT RE-ENTRY", fakeout_dir,
                            pos["entry"], pos["stop_loss"], pos["take_profit"],
                            pos["contracts"], orb["high"], orb["low"]
                        )
                        failed_breakouts.pop(symbol, None)
                    continue

                # ── Step 5: Standard breakout check ─────────────────────────
                direction, reasons = check_breakout_signal(df, orb, htf_trend)

                if direction == "NONE":
                    log.info(
                        f"{symbol}: no signal | "
                        f"Price={last_row['close']:.2f} "
                        f"ORB={orb['low']:.2f}–{orb['high']:.2f} "
                        f"RSI={last_row['rsi']:.1f} "
                        f"HTF={htf_trend}"
                    )
                    # Check if price is attempting a breakout that might fail
                    if last_row["close"] > orb["high"]:
                        failed_breakouts[symbol] = "LONG"
                    elif last_row["close"] < orb["low"]:
                        failed_breakouts[symbol] = "SHORT"
                    continue

                # Confirmed breakout
                log.info(f"{symbol}: {direction} BREAKOUT confirmed!")

                pos = calc_position(
                    symbol, direction,
                    last_row["close"], orb, last_row["atr"]
                )
                # ── ICC Full Rules (all 14 videos) ──────────────────
                structure_check = check_entry_rules(df, direction)
                if not structure_check["approved"]:
                    log.info(f"{symbol}: ICC BLOCKED — {structure_check['reason'][:100]}")
                    continue
                # Override stop/target with ICC structure levels if better
                if structure_check.get("stop_loss") and structure_check.get("target"):
                    pos["stop_loss"]   = structure_check["stop_loss"]
                    pos["take_profit"] = structure_check["target"]
                log.info(f"{symbol}: ICC APPROVED — {structure_check['reason'][:100]}")

                # ── News Intelligence ─────────────────────────────────────────
                news = get_news_score(symbol, signal)
                if news["block"]:
                    log.info(f"{symbol}: NEWS BLOCK — {news['reason']}")
                    continue
                if news["boost"]:
                    log.info(f"{symbol}: NEWS BOOST ✅ — {news['reason']}")
                elif news["sentiment"] != "NO_NEWS":
                    log.info(f"{symbol}: NEWS — {news['sentiment']} {news['score']:+d}")
                log.info(f"{symbol}: ICC Stop={structure_check.get('stop_loss','?')} Target={structure_check.get('target','?')} RR={structure_check.get('rr',0):.1f} Session={structure_check.get('session','?')}")

                success = place_order(
                    symbol, direction, pos["contracts"],
                    pos["entry"], pos["stop_loss"], pos["take_profit"]
                )
                if success:
                    open_positions[symbol] = {
                        "direction":   direction,
                        "entry":       pos["entry"],
                        "stop_loss":   pos["stop_loss"],
                        "take_profit": pos["take_profit"],
                        "contracts":   pos["contracts"],
                        "trade_type":  "BREAKOUT",
                        "entry_time":  now.isoformat(),
                        "orb":         orb,
                    }
                    alert_trade_opened(
                        symbol, "ORB BREAKOUT", direction,
                        pos["entry"], pos["stop_loss"], pos["take_profit"],
                        pos["contracts"], orb["high"], orb["low"]
                    )

            except Exception as e:
                log.error(f"{symbol}: error — {e}")
                send_telegram(f"⚠️ <b>ORB v2 error on {symbol}</b>\n{str(e)[:200]}")

        log.info(f"Open positions: {list(open_positions.keys()) or 'none'}")
        log.info(f"Next scan in {scan_interval // 60} min...")
        time.sleep(scan_interval)


if __name__ == "__main__":
    run()
