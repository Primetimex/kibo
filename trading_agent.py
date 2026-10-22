"""
KIBO Trading Agent — Momentum Breakout Strategy
Timeframe : 5-minute bars
Indicators : RSI, EMA(9/21), MACD, Bollinger Bands, Volume
Direction  : Both (calls + puts)
Broker     : TradeZero Developer API
Alerts     : Telegram bot (instant push notifications)
Data       : Polygon.io

DISCLAIMER: Options trading involves substantial risk of loss.
Always paper trade first. You are solely responsible for all
trading decisions and outcomes.
"""

import os
import json
import time
import logging
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
try:
    from rate_limiter import wait_for_slot
except ImportError:
    def wait_for_slot(caller=""): pass
try:
    from news_intel import get_news_score
except ImportError:
    def get_news_score(sym, direction=None): return {"score":0,"sentiment":"NO_NEWS","boost":False,"block":False,"fresh":False,"reason":"news_intel not available"}  # fallback if not present
from structure_rules import check_entry_rules, classify_trend, format_structure_alert, detect_reversal, detect_indication, is_in_session
from trade_brain import brain

# ── Load credentials from .env ───────────────────────────────────────────────
load_dotenv("_env")

POLYGON_API_KEY  = os.getenv("POLYGON_API_KEY")
TZ_API_KEY_ID     = os.getenv("TZ_API_KEY_ID")
TZ_API_SECRET_KEY = os.getenv("TZ_API_SECRET_KEY")
TZ_ACCOUNT_ID     = os.getenv("TZ_ACCOUNT_ID")
TZ_BASE_URL      = "https://webapi.tradezero.com"
ACCOUNT_SIZE     = float(os.getenv("ACCOUNT_SIZE", "10000"))
RISK_PCT         = 0.01   # 1% per trade
MAX_POSITIONS    = 3      # max simultaneous positions
MAX_SHARES       = 500    # hard cap shares per trade
MAX_NOTIONAL     = 10000  # max 10% of $100K account per trade
PAPER_MODE       = os.getenv("PAPER_MODE", "true").lower() == "true"

# Telegram
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ── Watchlist ─────────────────────────────────────────────────────────────────
# Dynamic — pulled live from Market Scanner's ICC-approved setups instead of a
# fixed list. See load_scanner_watchlist() in Section 8.
SCANNER_RESULTS_FILE = Path("scanner_results.json")
MAX_SCAN_SYMBOLS      = 15   # cap per cycle so we don't blow through Polygon's rate limit
SCANNER_MAX_AGE_MIN   = 15   # treat scanner_results.json as stale after this long

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("trading_agent.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 1 — Telegram Alerts
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    """
    Send a push notification to your Telegram bot.
    Fails silently so it never interrupts trade execution.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping alert")
        return
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=5)
        resp.raise_for_status()
        log.info("Telegram alert sent")
    except Exception as e:
        log.warning(f"Telegram alert failed (non-critical): {e}")


def alert_signal(ticker: str, signal: str, rsi: float, price: float):
    emoji = "🟢" if signal == "BULLISH" else "🔴"
    mode  = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    send_telegram(
        f"{emoji} <b>SIGNAL DETECTED</b> [{mode}]\n"
        f"Ticker : <b>{ticker}</b>\n"
        f"Signal : <b>{signal}</b>\n"
        f"Price  : ${price:.2f}\n"
        f"RSI    : {rsi:.1f}\n"
        f"Time   : {datetime.now().strftime('%H:%M:%S ET')}"
    )


def alert_trade_opened(ticker: str, option: dict, contracts: int):
    emoji = "📈" if option["type"] == "call" else "📉"
    mode  = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    cost  = option["premium"] * contracts * 100
    send_telegram(
        f"{emoji} <b>TRADE OPENED</b> [{mode}]\n"
        f"Ticker   : <b>{ticker}</b>\n"
        f"Type     : {option['type'].upper()}\n"
        f"Strike   : ${option['strike']}\n"
        f"Expiry   : {option['expiry']}\n"
        f"Contracts: {contracts}\n"
        f"Premium  : ${option['premium']:.2f}\n"
        f"Total Cost: ${cost:.2f}\n"
        f"Time     : {datetime.now().strftime('%H:%M:%S ET')}"
    )


def alert_trade_closed(ticker: str, reason: str, entry_px: float,
                       exit_px: float, contracts: int):
    gain_pct = (exit_px - entry_px) / entry_px * 100
    pnl      = (exit_px - entry_px) * contracts * 100
    emoji    = "✅" if pnl >= 0 else "🛑"
    mode     = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    send_telegram(
        f"{emoji} <b>TRADE CLOSED — {reason}</b> [{mode}]\n"
        f"Ticker   : <b>{ticker}</b>\n"
        f"Entry    : ${entry_px:.2f}  →  Exit: ${exit_px:.2f}\n"
        f"P&L      : ${pnl:+.2f} ({gain_pct:+.1f}%)\n"
        f"Time     : {datetime.now().strftime('%H:%M:%S ET')}"
    )


def alert_agent_started():
    mode = "📄 PAPER TRADING" if PAPER_MODE else "🔴 LIVE TRADING"
    send_telegram(
        f"🤖 <b>KIBO Trading Agent STARTED</b>\n"
        f"Mode      : {mode}\n"
        f"Watchlist : Dynamic (Market Scanner ICC-approved setups)\n"
        f"Timeframe : 5-minute bars\n"
        f"Risk/trade: {RISK_PCT*100:.0f}% of ${ACCOUNT_SIZE:,.0f}\n"
        f"Time      : {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
    )


def alert_daily_summary(trades_today: list):
    if not trades_today:
        send_telegram(
            f"📊 <b>Daily Summary</b>\n"
            f"No trades taken today.\n"
            f"Date: {datetime.now().strftime('%Y-%m-%d')}"
        )
        return
    total_pnl = sum(t.get("pnl", 0) for t in trades_today)
    winners   = sum(1 for t in trades_today if t.get("pnl", 0) > 0)
    send_telegram(
        f"📊 <b>Daily Summary — {datetime.now().strftime('%Y-%m-%d')}</b>\n"
        f"Trades    : {len(trades_today)}\n"
        f"Winners   : {winners} / {len(trades_today)}\n"
        f"Total P&L : ${total_pnl:+.2f}\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 2 — Market Data (Polygon.io)
# ─────────────────────────────────────────────────────────────────────────────

def get_15min_bars(ticker: str, lookback_bars: int = 500) -> pd.DataFrame:
    end   = datetime.now()
    start = end - timedelta(days=30)
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/5/minute"
        f"/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        f"?adjusted=true&sort=asc&limit={lookback_bars}&apiKey={POLYGON_API_KEY}"
    )
    try:
        wait_for_slot(ticker)
        for _attempt in range(3):
            resp = requests.get(url, timeout=10)
            if resp.status_code == 429:
                log.warning(f"{ticker}: 429 rate limit — waiting 20s")
                time.sleep(20 * (_attempt + 1))
                continue
            resp.raise_for_status()
            break
        data = resp.json()
        if data.get("resultsCount", 0) == 0:
            log.warning(f"No data returned for {ticker}")
            return pd.DataFrame()
        df = pd.DataFrame(data["results"])
        df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
        df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"}, inplace=True)
        time.sleep(13)  # rate limit: free tier = 5 calls/min (12s min)
        df = df[["timestamp","open","high","low","close","volume"]].reset_index(drop=True)
        df["timestamp"] = utc_to_et(df["timestamp"])   # convert UTC -> Eastern Time
        return df
    except Exception as e:
        log.error(f"Data fetch failed for {ticker}: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 3 — Indicator Calculations
# ─────────────────────────────────────────────────────────────────────────────


def utc_to_et(ts_series):
    """
    Convert UTC timestamps to US Eastern Time without pytz.
    EDT (Mar-Nov): UTC-4  |  EST (Nov-Mar): UTC-5
    """
    import pandas as pd
    now   = __import__('datetime').datetime.now()
    year  = now.year
    from datetime import timedelta
    march_1   = __import__('datetime').datetime(year, 3, 1)
    dst_start = march_1 + timedelta(days=(6 - march_1.weekday()) % 7 + 7)
    nov_1     = __import__('datetime').datetime(year, 11, 1)
    dst_end   = nov_1   + timedelta(days=(6 - nov_1.weekday())   % 7)
    offset    = pd.Timedelta(hours=-4) if dst_start <= now < dst_end else pd.Timedelta(hours=-5)
    return ts_series + offset

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(com=period - 1, min_periods=period).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=period - 1, min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def calc_macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast    = series.ewm(span=fast, adjust=False).mean()
    ema_slow    = series.ewm(span=slow, adjust=False).mean()
    macd_line   = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram

def calc_bollinger(series: pd.Series, period=20, std_dev=2):
    sma   = series.rolling(period).mean()
    std   = series.rolling(period).std()
    return sma + std_dev * std, sma, sma - std_dev * std

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"]  = calc_rsi(df["close"])
    df["ema9"]  = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["macd"], df["macd_signal"], df["macd_hist"] = calc_macd(df["close"])
    df["bb_upper"], df["bb_mid"], df["bb_lower"]   = calc_bollinger(df["close"])
    df["vol_avg20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg20"]
    df = df.dropna()
    return df


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 4 — Signal Logic
# ─────────────────────────────────────────────────────────────────────────────

def check_signal(df: pd.DataFrame) -> str:
    """
    Score-based signal detection — more flexible than requiring all conditions at once.
    Need 3+ out of 5 conditions for a signal.
    """
    if len(df) < 2:
        return "NONE"

    row  = df.iloc[-1]
    prev = df.iloc[-2]

    # ── BULLISH conditions ────────────────────────────────────────────────────
    bull_score = 0
    bull_reasons = []

    if 48 <= row["rsi"] <= 75:
        bull_score += 1; bull_reasons.append(f"RSI={row['rsi']:.0f}")
    if row["ema9"] > row["ema21"]:
        bull_score += 1; bull_reasons.append("EMA9>21")
    if row["macd"] > row["macd_signal"]:
        bull_score += 1; bull_reasons.append("MACD>sig")
    if prev["macd"] <= prev["macd_signal"] and row["macd"] > row["macd_signal"]:
        bull_score += 2; bull_reasons.append("MACD fresh cross UP")  # fresh cross = 2pts
    if row["vol_ratio"] >= 1.3:
        bull_score += 1; bull_reasons.append(f"Vol={row['vol_ratio']:.1f}x")
    if row.get("close",0) > row.get("bb_mid", row.get("close",0)):
        bull_score += 1; bull_reasons.append("Above BB mid")

    # ── BEARISH conditions ────────────────────────────────────────────────────
    bear_score = 0
    bear_reasons = []

    if 25 <= row["rsi"] <= 52:
        bear_score += 1; bear_reasons.append(f"RSI={row['rsi']:.0f}")
    if row["ema9"] < row["ema21"]:
        bear_score += 1; bear_reasons.append("EMA9<21")
    if row["macd"] < row["macd_signal"]:
        bear_score += 1; bear_reasons.append("MACD<sig")
    if prev["macd"] >= prev["macd_signal"] and row["macd"] < row["macd_signal"]:
        bear_score += 2; bear_reasons.append("MACD fresh cross DOWN")
    if row["vol_ratio"] >= 1.3:
        bear_score += 1; bear_reasons.append(f"Vol={row['vol_ratio']:.1f}x")
    if row.get("close",0) < row.get("bb_mid", row.get("close",0)):
        bear_score += 1; bear_reasons.append("Below BB mid")

    # Need 3+ points for a signal
    if bull_score >= 3 and bull_score > bear_score:
        log.debug(f"BULLISH signal ({bull_score}pts): {', '.join(bull_reasons)}")
        return "BULLISH"
    if bear_score >= 3 and bear_score > bull_score:
        log.debug(f"BEARISH signal ({bear_score}pts): {', '.join(bear_reasons)}")
        return "BEARISH"

    return "NONE"


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 5 — Position Sizing
# ─────────────────────────────────────────────────────────────────────────────

def calc_contracts(option_premium: float) -> int:
    max_risk  = ACCOUNT_SIZE * RISK_PCT
    contracts = int(max_risk / max(option_premium * 100, 0.01))
    contracts = max(1, min(contracts, MAX_SHARES))
    if option_premium > 0:
        notional = contracts * option_premium
        if notional > MAX_NOTIONAL:
            contracts = max(1, int(MAX_NOTIONAL / option_premium))
    return contracts


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 5b — Dynamic Watchlist (fed by Market Scanner)
# ─────────────────────────────────────────────────────────────────────────────

def load_scanner_watchlist(max_symbols: int = MAX_SCAN_SYMBOLS) -> list:
    """
    Pull this cycle's candidate tickers from Market Scanner's ICC-approved
    setups (scanner_results.json) instead of a fixed watchlist.

    Returns an empty list — meaning "nothing to trade this cycle" — if the
    file is missing, stale, or has no approved setups right now. This agent
    still runs its own signal engine (RSI/EMA/MACD) and its own ICC check
    on whatever symbols come back here; the scanner just decides which
    tickers are worth looking at.
    """
    if not SCANNER_RESULTS_FILE.exists():
        log.warning("scanner_results.json not found — no candidates this cycle")
        return []

    age_min = (time.time() - SCANNER_RESULTS_FILE.stat().st_mtime) / 60
    if age_min > SCANNER_MAX_AGE_MIN:
        log.warning(f"scanner_results.json is {age_min:.0f} min old — treating as stale, no candidates")
        return []

    try:
        data = json.loads(SCANNER_RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Failed to read scanner_results.json: {e}")
        return []

    approved = [r for r in data.get("results", []) if r.get("approved") and r.get("sym")]
    # Best risk:reward first — with a capped scan budget, spend it on the
    # strongest setups the scanner found, not just whatever's first in the file.
    approved.sort(key=lambda r: r.get("rr", 0), reverse=True)

    seen = set()
    symbols = []
    for r in approved:
        sym = r["sym"].upper()
        if sym in seen:
            continue
        seen.add(sym)
        symbols.append(sym)
        if len(symbols) >= max_symbols:
            break

    if symbols:
        log.info(f"Scanner candidates this cycle: {', '.join(symbols)}")
    else:
        log.info("No ICC-approved setups from scanner this cycle")
    return symbols


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 6 — TradeZero API Execution
# ─────────────────────────────────────────────────────────────────────────────

TZ_HEADERS = {
    "TZ-API-KEY-ID":     TZ_API_KEY_ID,
    "TZ-API-SECRET-KEY": TZ_API_SECRET_KEY,
    "Content-Type":      "application/json",
}

def get_option_chain(ticker: str, direction: str) -> dict | None:
    """
    Try TZ options API first.
    If that fails (TZ paper doesn't support options), trade the stock directly.
    """
    option_type = "call" if direction == "BULLISH" else "put"

    # Try TZ options API
    url = f"{TZ_BASE_URL}/v1/options/chain/{ticker}"
    params = {"type": option_type, "dte_min": 1, "dte_max": 21, "strikes": 3}
    try:
        resp = requests.get(url, headers=TZ_HEADERS, params=params, timeout=10)
        if resp.status_code == 200:
            chain = resp.json()
            if chain and len(chain) > 0:
                c = chain[0]
                return {
                    "symbol":     c["symbol"],
                    "premium":    c["ask"],
                    "strike":     c["strike"],
                    "expiry":     c["expiration"],
                    "type":       option_type,
                    "underlying": ticker,
                    "is_stock":   False,
                }
    except Exception as e:
        log.debug(f"Options API unavailable for {ticker}: {e}")

    # Fallback: trade the underlying stock directly
    # Get current price from cache
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            headers=headers, params={"interval":"1m","range":"1d"}, timeout=6
        )
        price = 0
        if r.status_code == 200:
            meta  = r.json().get("chart",{}).get("result",[{}])[0].get("meta",{})
            price = float(meta.get("regularMarketPrice") or 0)
    except:
        price = 0

    if price > 0:
        log.info(f"{ticker}: options unavailable — trading stock directly @ ${price:.2f}")
        return {
            "symbol":     ticker,
            "premium":    price,
            "strike":     price,
            "expiry":     "stock",
            "type":       option_type,
            "underlying": ticker,
            "is_stock":   True,
        }

    return None

def _check_order_fill(client_id: str, timeout: int = 25) -> tuple[bool, int]:
    """
    Poll TZ orders endpoint until the order is filled, rejected, or timeout.
    Returns (filled: bool, executed_qty: int).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(
                f"{TZ_BASE_URL}/v1/api/accounts/{TZ_ACCOUNT_ID}/orders",
                headers=TZ_HEADERS, timeout=8
            )
            if resp.status_code != 200:
                time.sleep(2)
                continue
            orders = resp.json() if isinstance(resp.json(), list) else []
            for o in orders:
                if o.get("clientOrderId") != client_id:
                    continue
                status   = o.get("orderStatus", "")
                executed = int(o.get("executed", 0) or 0)
                log.info(f"Order poll {client_id}: status={status} executed={executed}")
                if status in ("Filled", "PartiallyFilled") and executed > 0:
                    return True, executed
                if status in ("Rejected", "Canceled", "Cancelled", "Expired"):
                    log.warning(f"Order {client_id} terminal status: {status}")
                    return False, 0
        except Exception as e:
            log.warning(f"Fill check error: {e}")
        time.sleep(2)
    log.warning(f"Order {client_id} timed out after {timeout}s — treating as failed")
    return False, 0


def _cancel_order(client_id: str):
    """Best-effort cancel of a stale/unfilled order."""
    try:
        requests.delete(
            f"{TZ_BASE_URL}/v1/api/accounts/{TZ_ACCOUNT_ID}/orders/{client_id}",
            headers=TZ_HEADERS, timeout=8
        )
        log.info(f"Cancelled stale order {client_id}")
    except Exception as e:
        log.warning(f"Cancel attempt failed for {client_id}: {e}")


def place_order(option: dict, contracts: int, action: str = "BUY") -> bool:
    sym = option.get("underlying", option["symbol"])

    # ── Guard: duplicate pending order ───────────────────────────────────────
    if sym in pending_orders:
        age = (datetime.now() - pending_orders[sym]["placed_at"]).total_seconds()
        if age < MAX_PENDING_AGE:
            log.warning(f"{sym}: order already pending ({age:.0f}s) — skipping duplicate")
            return False
        else:
            log.warning(f"{sym}: stale pending order ({age:.0f}s) — clearing")
            _cancel_order(pending_orders[sym]["client_id"])
            del pending_orders[sym]

    # ── Guard: already in position ────────────────────────────────────────────
    if sym in open_positions and action in ("BUY", "SELLSHORT"):
        log.warning(f"{sym}: already in open position — skipping")
        return False

    # ── Map action to TZ side string ─────────────────────────────────────────
    # TZ accepts: "Buy", "Sell", "SellShort", "BuyToCover"
    side_map = {
        "BUY":        "Buy",
        "SELLSHORT":  "SellShort",
        "SELL":       "Sell",
        "BUY":        "Buy",
        "Buy":        "Buy",
        "Sell":       "Sell",
        "SellShort":  "SellShort",
    }
    tz_side = side_map.get(action, "Buy")

    import uuid as _uuid
    client_id  = f"KIBO-{_uuid.uuid4().hex[:8].upper()}"
    underlying = option.get("underlying", option["symbol"])

    if option.get("is_stock"):
        log.info(f"[{'PAPER' if PAPER_MODE else 'LIVE'}] {tz_side} {contracts}x {underlying} "
                 f"@ ${option['premium']:.2f} | STOCK | id={client_id}")
    else:
        log.info(f"[{'PAPER' if PAPER_MODE else 'LIVE'}] {tz_side} {contracts}x {underlying} "
                 f"@ ${option['premium']:.2f} | {option['type'].upper()} "
                 f"{option['strike']} {option['expiry']} | id={client_id}")

    payload = {
        "clientOrderId": client_id,
        "securityType":  "Stock",
        "symbol":        underlying,
        "side":          tz_side,
        "openClose":     "Open" if action in ("BUY", "SELLSHORT") else "Close",
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
        log.info(f"Order submitted: {resp.json()}")

        # Track as pending — do NOT assume success yet
        pending_orders[sym] = {"client_id": client_id, "placed_at": datetime.now()}

        # Wait for actual fill confirmation
        filled, executed_qty = _check_order_fill(client_id, timeout=25)

        if filled:
            log.info(f"✅ {sym} confirmed filled: {executed_qty} shares")
            del pending_orders[sym]
            return True
        else:
            log.warning(f"❌ {sym} order {client_id} not filled — aborting")
            _cancel_order(client_id)
            if sym in pending_orders:
                del pending_orders[sym]
            send_telegram(f"⚠️ <b>ORDER NOT FILLED — {sym}</b>\nid={client_id}")
            return False

    except Exception as e:
        log.error(f"Order placement failed for {sym}: {e}")
        if sym in pending_orders:
            del pending_orders[sym]
        send_telegram(f"⚠️ <b>ORDER FAILED — {sym}</b>\n{e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 7 — Position Tracker + Exit Management
# ─────────────────────────────────────────────────────────────────────────────

open_positions: dict = {}
trades_today:   list = []
pending_orders: dict = {}        # sym -> {"client_id": str, "placed_at": datetime}
MAX_PENDING_AGE = 30             # seconds before treating a pending order as dead

POSITIONS_FILE = Path("trading_agent_positions.json")

def load_positions():
    global open_positions
    try:
        if POSITIONS_FILE.exists():
            open_positions = json.loads(POSITIONS_FILE.read_text())
            if open_positions:
                log.info(f"Restored {len(open_positions)} positions: {list(open_positions.keys())}")
    except: pass

def save_positions():
    try:
        POSITIONS_FILE.write_text(json.dumps(open_positions, default=str))
    except: pass

def check_exits():
    for ticker, pos in list(open_positions.items()):
        option    = pos["option"]
        entry_px  = pos["entry_premium"]
        contracts = pos["contracts"]
        direction = pos.get("direction", "LONG")  # LONG or SHORT
        stop_loss   = pos.get("stop_loss")
        take_profit = pos.get("take_profit")

        current = get_option_chain(
            ticker,
            "BULLISH" if direction == "LONG" else "BEARISH"
        )
        if not current:
            continue

        current_px = current["premium"]

        # ── Direction-aware P&L ───────────────────────────────────────────
        if direction == "SHORT":
            # SHORT profits when price goes DOWN
            gain_pct = (entry_px - current_px) / entry_px  # positive = price fell = profit
        else:
            # LONG profits when price goes UP
            gain_pct = (current_px - entry_px) / entry_px

        # ── Check ICC structure stop/target if available ──────────────────
        hit_tp = False
        hit_sl = False
        if stop_loss and take_profit:
            if direction == "LONG":
                hit_tp = current_px >= take_profit
                hit_sl = current_px <= stop_loss
            else:  # SHORT
                hit_tp = current_px <= take_profit  # price fell to target
                hit_sl = current_px >= stop_loss    # price rose past stop
        else:
            # Fallback: percentage-based
            hit_tp = gain_pct >= 0.15   # 15% gain
            hit_sl = gain_pct <= -0.08  # 8% loss

        # ── EOD force close ───────────────────────────────────────────────
        now = datetime.now()
        force_close = now.hour == 15 and now.minute >= 55

        if hit_tp or force_close:
            reason = "TAKE PROFIT ✅" if hit_tp else "EOD CLOSE 🕐"
            log.info(f"{reason} {ticker} ({direction}): {gain_pct:.1%} | ${entry_px:.2f} → ${current_px:.2f}")
            close_action = "Buy" if direction == "SHORT" else "Sell"
            place_order(option, contracts, action=close_action)
            alert_trade_closed(ticker, reason, entry_px, current_px, contracts)
            pnl = gain_pct * entry_px * contracts
            trades_today.append({"ticker": ticker, "pnl": pnl})
            brain.record_close("options", ticker, pnl, poi_level=pos.get("poi_level"))
            del open_positions[ticker]
            save_positions()

        elif hit_sl:
            log.info(f"STOP LOSS 🛑 {ticker} ({direction}): {gain_pct:.1%} | ${entry_px:.2f} → ${current_px:.2f}")
            close_action = "Buy" if direction == "SHORT" else "Sell"
            place_order(option, contracts, action=close_action)
            alert_trade_closed(ticker, "STOP LOSS 🛑", entry_px, current_px, contracts)
            pnl = gain_pct * entry_px * contracts
            trades_today.append({"ticker": ticker, "pnl": pnl})
            brain.record_close("options", ticker, pnl, poi_level=pos.get("poi_level"))
            del open_positions[ticker]
            save_positions()


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 8 — Main Loop
# ─────────────────────────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= now <= close_t

def is_end_of_day() -> bool:
    now = datetime.now()
    return now.hour == 15 and now.minute >= 45

def run():
    log.info("=" * 60)
    log.info("KIBO Trading Agent — STARTED")
    log.info(f"Mode      : {'PAPER TRADING' if PAPER_MODE else 'LIVE TRADING'}")
    log.info(f"Watchlist : Dynamic (Market Scanner ICC-approved setups, max {MAX_SCAN_SYMBOLS}/cycle)")
    log.info(f"Caps      : Max {MAX_POSITIONS} positions | Max {MAX_SHARES} shares | Max ${MAX_NOTIONAL:,}/trade")
    log.info("=" * 60)

    load_positions()
    alert_agent_started()

    scan_interval   = 5 * 60
    last_summary_day = None

    while True:
        now = datetime.now()

        # Daily summary at 3:45 PM ET
        if is_end_of_day() and last_summary_day != now.date():
            alert_daily_summary(trades_today)
            trades_today.clear()
            last_summary_day = now.date()

        if not is_market_hours():
            log.info("Market closed — sleeping 5 minutes...")
            time.sleep(300)
            continue

        # ── Global risk gate: skip the whole cycle if trade_brain has halted
        # trading (daily drawdown breached) or we're in a loss-streak cooldown.
        # Saves API calls too — no point scanning if nothing can be approved.
        status = brain.get_status()
        if status["halted"]:
            log.info(f"BRAIN HALTED — {status['halt_reason']} — sleeping")
            time.sleep(scan_interval)
            continue
        if status["in_cooldown"]:
            log.info(f"BRAIN COOLDOWN — {status['cooldown_reason']} — sleeping")
            time.sleep(scan_interval)
            continue

        tickers = load_scanner_watchlist()
        if not tickers:
            log.info("No approved candidates from scanner this cycle — sleeping")
            time.sleep(scan_interval)
            continue

        log.info(f"--- Scanning {len(tickers)} tickers ---")

        if open_positions:
            check_exits()

        for ticker in tickers:
            if ticker in open_positions:
                log.info(f"{ticker}: already in position — skipping")
                continue
            if len(open_positions) >= MAX_POSITIONS:
                log.info(f"Max positions ({MAX_POSITIONS}) reached — skipping {ticker}")
                continue

            try:
                df = get_15min_bars(ticker)
                if df.empty:
                    continue

                df     = add_indicators(df)
                signal = check_signal(df)

                if signal == "NONE":
                    row = df.iloc[-1]
                    log.info(
                        f"{ticker}: no signal | "
                        f"${row['close']:.2f} RSI={row['rsi']:.0f} "
                        f"EMA={'>' if row['ema9']>row['ema21'] else '<'} "
                        f"MACD={'>' if row['macd']>row['macd_signal'] else '<'} "
                        f"Vol={row['vol_ratio']:.1f}x"
                    )
                    continue

                row = df.iloc[-1]
                log.info(f"{ticker}: {signal} signal!")

                option = get_option_chain(ticker, signal)
                if not option:
                    log.warning(f"{ticker}: no suitable option found")
                    continue

                # ── ICC Full Rules ────────────────────────────────────────────
                trade_direction = "LONG" if signal == "BULLISH" else "SHORT"
                sc = check_entry_rules(df, trade_direction)
                if not sc["approved"]:
                    log.info(f"{ticker}: ICC BLOCKED — {sc['reason'][:100]}")
                    continue
                log.info(f"{ticker}: ICC APPROVED — Stop=${sc.get('stop_loss',0):.2f} Target=${sc.get('target',0):.2f} RR={sc.get('rr',0):.1f}")

                # ── News Intelligence ─────────────────────────────────────────
                news = get_news_score(ticker, signal)
                if news["block"]:
                    log.info(f"{ticker}: NEWS BLOCK — {news['reason']}")
                    continue
                if news["boost"]:
                    log.info(f"{ticker}: NEWS BOOST ✅ — {news['reason']}")
                elif news["sentiment"] != "NO_NEWS":
                    log.info(f"{ticker}: NEWS — {news['sentiment']} {news['score']:+d}")

                buy_action = "BUY" if trade_direction == "LONG" else "SELLSHORT"

                # ── Position sizing (was previously undefined here — bug) ──────
                contracts = calc_contracts(option["premium"])

                # ── trade_brain: global risk gatekeeper ─────────────────────────
                poi_level = float(sc.get("stop_loss") or row["close"])
                risk_amount = ACCOUNT_SIZE * RISK_PCT
                ok, brain_reason = brain.approve_trade(
                    "options", ticker, trade_direction,
                    risk_amount=risk_amount, poi_level=poi_level
                )
                if not ok:
                    log.info(f"{ticker}: BRAIN BLOCKED — {brain_reason}")
                    continue

                success = place_order(option, contracts, action=buy_action)

                if success:
                    open_positions[ticker] = {
                        "option":        option,
                        "contracts":     contracts,
                        "entry_premium": option["premium"],
                        "entry_time":    now.isoformat(),
                        "signal":        signal,
                        "direction":     trade_direction,
                        "stop_loss":     sc.get("stop_loss"),
                        "take_profit":   sc.get("target"),
                        "poi_level":     poi_level,
                    }
                    save_positions()
                    alert_trade_opened(ticker, option, contracts)
                else:
                    # Order didn't fill — release trade_brain's symbol reservation
                    # so it doesn't stay falsely "open" forever. pnl=0.0 clears
                    # the lock without touching the loss streak.
                    brain.record_close("options", ticker, 0.0, poi_level=poi_level)

            except Exception as e:
                log.error(f"{ticker}: unexpected error — {e}")

        log.info(f"Open positions: {list(open_positions.keys()) or 'none'}")
        log.info(f"Next scan in {scan_interval // 60} min...")
        time.sleep(scan_interval)

if __name__ == "__main__":
    run()
