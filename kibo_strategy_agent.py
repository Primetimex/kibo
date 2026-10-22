"""
KIBO Strategy Agent — Bread Trading Framework
=============================================
Implements all 3 strategies from the Bread Trading system:
  1. Trend Pullback       — HH/HL structure → demand zone → liquidity sweep → enter
  2. Breakout Retest      — consolidation box → momentum candle → FVG → enter at 50%
  3. Liq. Sweep Reversal  — equal H/L pool → sweep → V-shape reaction → enter

Runs on Alpaca paper account. Logs to kibo_strategy_agent.log.
Start via dashboard or: python kibo_strategy_agent.py
"""

import os, sys, time, logging, json
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("_env")

# ── Force UTF-8 console output (Windows cp1252 can't render emoji/arrows,
# which was spamming "Logging error" tracebacks on every alert) ─────────────
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("kibo_strategy_agent.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("KIBO")

# ── Config ────────────────────────────────────────────────────────────────────
ALPACA_KEY    = os.getenv("APCA_API_KEY_ID",    "PKPRJSUV6BTTGY75UZ2JQFFJRW")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY","7w8GYKdtCStJCZwh92Ww7aydwmqebctg9xpZYex4C4pb")
ALPACA_BASE   = os.getenv("APCA_BASE_URL",       "https://paper-api.alpaca.markets")
DATA_BASE     = "https://data.alpaca.markets/v1beta3/crypto/us"

# Trading universe — majors first per Bread framework rule
WATCHLIST = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD"]

# Risk rules (Bread framework)
RISK_PCT          = 0.01    # 1% of account per trade
MAX_TRADES_PER_DAY = 3
R_TARGET          = 3.0     # 3R take-profit
MIN_RR            = 2.0     # minimum reward:risk to enter
MAX_LEVERAGE      = 1       # spot only — no leverage on paper crypto
SCAN_INTERVAL_SEC = 60      # re-scan every 60 seconds

# Strategy timeframes
TF_TREND    = "1Day"     # high timeframe for trend direction
TF_ENTRY    = "15Min"    # entry timeframe

# ── Alpaca REST helper ────────────────────────────────────────────────────────
import urllib.request, urllib.error

def _alpaca_req(method, path, body=None, data_api=False):
    base = DATA_BASE if data_api else ALPACA_BASE
    url  = base + path
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(url, data=data, method=method)
    req.add_header("APCA-API-KEY-ID",     ALPACA_KEY)
    req.add_header("APCA-API-SECRET-KEY", ALPACA_SECRET)
    req.add_header("Content-Type",        "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        log.error(f"Alpaca HTTP {e.code} {method} {path}: {body_text[:200]}")
        return None
    except Exception as e:
        log.error(f"Alpaca request error {method} {path}: {e}")
        return None

def get_account():
    return _alpaca_req("GET", "/v2/account") or {}

def get_positions():
    return _alpaca_req("GET", "/v2/positions") or []

def get_orders(status="open"):
    return _alpaca_req("GET", f"/v2/orders?status={status}&limit=50") or []

def get_bars(symbol, timeframe, limit=100):
    """Fetch OHLCV bars from Alpaca v1beta3 crypto data API."""
    tf_map = {"1Hour": "1H", "15Min": "15T", "1Min": "1T",
              "5Min": "5T", "4Hour": "4H", "1Day": "1D"}
    tf = tf_map.get(timeframe, timeframe)
    sym_enc = symbol.replace("/", "%2F")
    path = f"/bars?symbols={sym_enc}&timeframe={tf}&limit={limit}&sort=asc"
    resp = _alpaca_req("GET", path, data_api=True)
    if not resp:
        return []
    bars_map = resp.get("bars", {})
    return bars_map.get(symbol, [])

def place_order(symbol, qty, side, order_type="market",
                limit_price=None, stop_price=None,
                take_profit=None, stop_loss=None):
    """Place an order with optional bracket TP/SL."""
    body = {
        "symbol":        symbol,
        "qty":           str(qty),
        "side":          side,
        "type":          order_type,
        "time_in_force": "gtc",
    }
    if limit_price:
        body["limit_price"] = str(round(limit_price, 4))
    if take_profit:
        body["take_profit"] = {"limit_price": str(round(take_profit, 4))}
    if stop_loss:
        body["stop_loss"] = {"stop_price": str(round(stop_loss, 4))}
    return _alpaca_req("POST", "/v2/orders", body)

def close_position(symbol):
    sym_enc = symbol.replace("/", "%2F")
    return _alpaca_req("DELETE", f"/v2/positions/{sym_enc}")

# ── Technical helpers ─────────────────────────────────────────────────────────

def closes(bars):
    return [float(b["c"]) for b in bars]

def highs(bars):
    return [float(b["h"]) for b in bars]

def lows(bars):
    return [float(b["l"]) for b in bars]

def volumes(bars):
    return [float(b["v"]) for b in bars]

def ema(data, period):
    if len(data) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(data[:period]) / period]
    for p in data[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result

def rsi(data, period=14):
    if len(data) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(data)):
        d = data[i] - data[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[-period:]) / period
    avg_l = sum(losses[-period:]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return round(100 - 100 / (1 + rs), 2)

def atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = float(bars[i]["h"]), float(bars[i]["l"]), float(bars[i-1]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

# ── Market structure ──────────────────────────────────────────────────────────

def detect_trend(bars):
    """
    Identify trend via Higher Highs / Higher Lows (bullish)
    or Lower Highs / Lower Lows (bearish).
    Returns: 'BULLISH', 'BEARISH', or 'CONSOLIDATION'
    """
    if len(bars) < 20:
        return "CONSOLIDATION"
    h = highs(bars[-20:])
    l = lows(bars[-20:])
    # Find swing highs and lows (local extremes over 3-bar windows)
    swing_highs = [h[i] for i in range(1, len(h)-1) if h[i] > h[i-1] and h[i] > h[i+1]]
    swing_lows  = [l[i] for i in range(1, len(l)-1) if l[i] < l[i-1] and l[i] < l[i+1]]
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "CONSOLIDATION"
    # Check last 2 swing highs and lows
    hh = swing_highs[-1] > swing_highs[-2]
    hl = swing_lows[-1]  > swing_lows[-2]
    lh = swing_highs[-1] < swing_highs[-2]
    ll = swing_lows[-1]  < swing_lows[-2]
    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "CONSOLIDATION"

def find_demand_zones(bars, trend):
    """
    Mark demand zones: price areas where buyers overwhelmed sellers,
    visible as the base of a bullish impulse move.
    Returns list of (zone_low, zone_high) tuples, most recent first.
    """
    zones = []
    if len(bars) < 10:
        return zones
    c = closes(bars)
    h = highs(bars)
    l = lows(bars)
    for i in range(2, len(bars) - 2):
        # Bullish demand: small body candle followed by strong up candle
        body_i   = abs(c[i] - float(bars[i]["o"]))
        body_i1  = abs(c[i+1] - float(bars[i+1]["o"]))
        is_basing = body_i < (h[i] - l[i]) * 0.4
        is_impulse = (c[i+1] > float(bars[i+1]["o"])) and body_i1 > body_i * 1.5
        if trend == "BULLISH" and is_basing and is_impulse:
            zones.append((l[i], h[i]))
    return list(reversed(zones))  # most recent first

def find_supply_zones(bars, trend):
    """Mirror of demand zones for bearish setups."""
    zones = []
    if len(bars) < 10:
        return zones
    c = closes(bars)
    h = highs(bars)
    l = lows(bars)
    for i in range(2, len(bars) - 2):
        body_i   = abs(c[i] - float(bars[i]["o"]))
        body_i1  = abs(c[i+1] - float(bars[i+1]["o"]))
        is_basing  = body_i < (h[i] - l[i]) * 0.4
        is_impulse = (c[i+1] < float(bars[i+1]["o"])) and body_i1 > body_i * 1.5
        if trend == "BEARISH" and is_basing and is_impulse:
            zones.append((l[i], h[i]))
    return list(reversed(zones))

def detect_liquidity_sweep(bars, zone_low, zone_high, direction="bull"):
    """
    Check if recent price action swept liquidity below a zone (bull)
    or above a zone (bear) and then reversed sharply (V-shape).
    Returns True/False.
    """
    if len(bars) < 5:
        return False
    recent = bars[-5:]
    if direction == "bull":
        swept = any(float(b["l"]) < zone_low for b in recent)
        recovered = float(recent[-1]["c"]) > zone_low
        return swept and recovered
    else:
        swept = any(float(b["h"]) > zone_high for b in recent)
        recovered = float(recent[-1]["c"]) < zone_high
        return swept and recovered

def find_consolidation_box(bars, lookback=20):
    """
    Find the most recent consolidation range.
    Returns (box_low, box_high) or None.
    """
    if len(bars) < lookback:
        return None
    segment = bars[-lookback:]
    box_high = max(float(b["h"]) for b in segment)
    box_low  = min(float(b["l"]) for b in segment)
    range_pct = (box_high - box_low) / box_low
    # Consolidation = range < 6% over the lookback
    if range_pct < 0.06:
        return (box_low, box_high)
    return None

def find_momentum_candle_and_fvg(bars, direction):
    """
    Breakout Retest strategy:
    Find the momentum candle that broke the consolidation box,
    then identify the Fair Value Gap (imbalance) it created.
    Returns (fvg_low, fvg_high) or None.
    """
    if len(bars) < 5:
        return None
    for i in range(len(bars)-3, max(len(bars)-15, 1), -1):
        prev_c = float(bars[i-1]["c"])
        curr_c = float(bars[i]["c"])
        curr_o = float(bars[i]["o"])
        body   = abs(curr_c - curr_o)
        candle_range = float(bars[i]["h"]) - float(bars[i]["l"])
        # Momentum candle: body > 60% of range, moves in direction
        if candle_range == 0:
            continue
        is_momentum = body / candle_range > 0.60
        if direction == "BEARISH" and is_momentum and curr_c < curr_o:
            # FVG = gap between low of prev candle and high of next candle
            if i + 1 < len(bars):
                fvg_high = float(bars[i-1]["l"])
                fvg_low  = float(bars[i+1]["h"])
                if fvg_high > fvg_low:
                    return (fvg_low, fvg_high)
        elif direction == "BULLISH" and is_momentum and curr_c > curr_o:
            if i + 1 < len(bars):
                fvg_low  = float(bars[i-1]["h"])
                fvg_high = float(bars[i+1]["l"])
                if fvg_high > fvg_low:
                    return (fvg_low, fvg_high)
    return None

def find_equal_highs_lows(bars, tolerance=0.002):
    """
    Liquidity Sweep Reversal:
    Find equal highs or equal lows (within tolerance %) — these are
    liquidity pools that price is likely to sweep.
    Returns ('high'|'low', level) or None.
    """
    if len(bars) < 10:
        return None
    recent = bars[-15:]
    h = highs(recent)
    l = lows(recent)
    # Check for equal highs (within tolerance)
    for i in range(len(h)-1):
        for j in range(i+1, len(h)):
            if abs(h[i] - h[j]) / h[i] < tolerance:
                return ("high", max(h[i], h[j]))
    # Check for equal lows
    for i in range(len(l)-1):
        for j in range(i+1, len(l)):
            if l[i] > 0 and abs(l[i] - l[j]) / l[i] < tolerance:
                return ("low", min(l[i], l[j]))
    return None

# ── Strategy signal generators ────────────────────────────────────────────────

def strategy_trend_pullback(symbol, bars_h, bars_e):
    """
    Strategy 1: Trend Pullback
    - Identify trend on higher TF
    - Find unmitigated demand/supply zone
    - Confirm liquidity sweep at zone
    - Return signal dict or None
    """
    trend = detect_trend(bars_h)
    if trend == "CONSOLIDATION":
        return None

    current_price = float(bars_e[-1]["c"])
    atr_val = atr(bars_e) or (current_price * 0.01)

    if trend == "BULLISH":
        zones = find_demand_zones(bars_e, trend)
        for zone_low, zone_high in zones[:3]:
            # Price must be at or near the zone
            if zone_low <= current_price <= zone_high * 1.005:
                swept = detect_liquidity_sweep(bars_e, zone_low, zone_high, "bull")
                if swept:
                    stop  = zone_low - atr_val * 0.5
                    risk  = current_price - stop
                    if risk <= 0:
                        continue
                    tp    = current_price + risk * R_TARGET
                    rr    = round((tp - current_price) / risk, 2)
                    if rr < MIN_RR:
                        continue
                    return {
                        "symbol":    symbol,
                        "strategy":  "Trend Pullback",
                        "side":      "buy",
                        "price":     current_price,
                        "stop":      round(stop, 4),
                        "tp":        round(tp, 4),
                        "rr":        rr,
                        "trend":     trend,
                        "zone":      (round(zone_low,4), round(zone_high,4)),
                    }
    elif trend == "BEARISH":
        zones = find_supply_zones(bars_e, trend)
        for zone_low, zone_high in zones[:3]:
            if zone_low * 0.995 <= current_price <= zone_high:
                swept = detect_liquidity_sweep(bars_e, zone_low, zone_high, "bear")
                if swept:
                    stop  = zone_high + atr_val * 0.5
                    risk  = stop - current_price
                    if risk <= 0:
                        continue
                    tp    = current_price - risk * R_TARGET
                    rr    = round((current_price - tp) / risk, 2)
                    if rr < MIN_RR:
                        continue
                    return {
                        "symbol":    symbol,
                        "strategy":  "Trend Pullback",
                        "side":      "sell",
                        "price":     current_price,
                        "stop":      round(stop, 4),
                        "tp":        round(tp, 4),
                        "rr":        rr,
                        "trend":     trend,
                        "zone":      (round(zone_low,4), round(zone_high,4)),
                    }
    return None


def strategy_breakout_retest(symbol, bars_e):
    """
    Strategy 2: Breakout Retest
    - Find consolidation box
    - Wait for momentum candle break
    - Enter on 50% FVG (imbalance) mitigation
    """
    box = find_consolidation_box(bars_e)
    if not box:
        return None

    box_low, box_high = box
    current_price = float(bars_e[-1]["c"])
    atr_val = atr(bars_e) or (current_price * 0.01)

    # Bearish breakout below box
    if current_price < box_low * 0.999:
        fvg = find_momentum_candle_and_fvg(bars_e, "BEARISH")
        if fvg:
            fvg_low, fvg_high = fvg
            fvg_mid = (fvg_low + fvg_high) / 2
            # Price should be retracing toward FVG mid
            if current_price >= fvg_mid * 0.998:
                stop = fvg_high + atr_val * 0.3
                risk = stop - current_price
                if risk <= 0:
                    return None
                tp   = current_price - risk * R_TARGET
                rr   = round((current_price - tp) / risk, 2)
                if rr < MIN_RR:
                    return None
                return {
                    "symbol":   symbol,
                    "strategy": "Breakout Retest",
                    "side":     "sell",
                    "price":    current_price,
                    "stop":     round(stop, 4),
                    "tp":       round(tp, 4),
                    "rr":       rr,
                    "trend":    "BEARISH",
                    "fvg":      (round(fvg_low,4), round(fvg_high,4)),
                }

    # Bullish breakout above box
    elif current_price > box_high * 1.001:
        fvg = find_momentum_candle_and_fvg(bars_e, "BULLISH")
        if fvg:
            fvg_low, fvg_high = fvg
            fvg_mid = (fvg_low + fvg_high) / 2
            if current_price <= fvg_mid * 1.002:
                stop = fvg_low - atr_val * 0.3
                risk = current_price - stop
                if risk <= 0:
                    return None
                tp   = current_price + risk * R_TARGET
                rr   = round((tp - current_price) / risk, 2)
                if rr < MIN_RR:
                    return None
                return {
                    "symbol":   symbol,
                    "strategy": "Breakout Retest",
                    "side":     "buy",
                    "price":    current_price,
                    "stop":     round(stop, 4),
                    "tp":       round(tp, 4),
                    "rr":       rr,
                    "trend":    "BULLISH",
                    "fvg":      (round(fvg_low,4), round(fvg_high,4)),
                }
    return None


def strategy_liq_sweep_reversal(symbol, bars_e):
    """
    Strategy 3: Liquidation Sweep Reversal
    - Find equal highs/lows (liquidity pool)
    - Confirm price swept them
    - Enter on V-shape reversal candle
    """
    eq = find_equal_highs_lows(bars_e)
    if not eq:
        return None

    direction, level = eq
    current_price = float(bars_e[-1]["c"])
    atr_val = atr(bars_e) or (current_price * 0.01)
    recent_bars = bars_e[-5:]

    if direction == "low":
        # Swept lows → expect bullish reversal
        swept_below = any(float(b["l"]) < level * 0.999 for b in recent_bars)
        recovered   = current_price > level
        # V-shape: last candle is strongly bullish
        last = bars_e[-1]
        v_shape = float(last["c"]) > float(last["o"]) and \
                  (float(last["c"]) - float(last["o"])) > atr_val * 0.4
        if swept_below and recovered and v_shape:
            stop  = min(float(b["l"]) for b in recent_bars) - atr_val * 0.2
            risk  = current_price - stop
            if risk <= 0:
                return None
            tp    = current_price + risk * R_TARGET
            rr    = round((tp - current_price) / risk, 2)
            if rr < MIN_RR:
                return None
            return {
                "symbol":   symbol,
                "strategy": "Liq. Sweep Reversal",
                "side":     "buy",
                "price":    current_price,
                "stop":     round(stop, 4),
                "tp":       round(tp, 4),
                "rr":       rr,
                "trend":    "BULLISH",
                "liq_level": round(level, 4),
            }

    elif direction == "high":
        # Swept highs → expect bearish reversal
        swept_above = any(float(b["h"]) > level * 1.001 for b in recent_bars)
        recovered   = current_price < level
        last = bars_e[-1]
        v_shape = float(last["c"]) < float(last["o"]) and \
                  (float(last["o"]) - float(last["c"])) > atr_val * 0.4
        if swept_above and recovered and v_shape:
            stop  = max(float(b["h"]) for b in recent_bars) + atr_val * 0.2
            risk  = stop - current_price
            if risk <= 0:
                return None
            tp    = current_price - risk * R_TARGET
            rr    = round((current_price - tp) / risk, 2)
            if rr < MIN_RR:
                return None
            return {
                "symbol":   symbol,
                "strategy": "Liq. Sweep Reversal",
                "side":     "sell",
                "price":    current_price,
                "stop":     round(stop, 4),
                "tp":       round(tp, 4),
                "rr":       rr,
                "trend":    "BEARISH",
                "liq_level": round(level, 4),
            }
    return None

# ── BTC filter ────────────────────────────────────────────────────────────────

def btc_is_supportive():
    """
    Bread rule: Never fight the king.
    Returns True if BTC is not in a sharp downtrend on the 1H TF.
    """
    bars = get_bars("BTC/USD", TF_TREND, limit=60)
    if not bars:
        return True  # if we can't fetch, don't block trades
    trend = detect_trend(bars)
    current = float(bars[-1]["c"])
    ema20   = ema(closes(bars), 20)
    if not ema20:
        return True
    # Block new longs if BTC is bearish and below EMA20
    if trend == "BEARISH" and current < ema20[-1] * 0.995:
        log.info("BTC filter: BTC bearish — blocking new LONG signals")
        return False
    return True

# ── Position sizing ───────────────────────────────────────────────────────────

def calc_qty(account, price, stop):
    """
    Size position so that if stopped out, we lose exactly RISK_PCT of account.
    """
    try:
        equity = float(account.get("equity", 0))
        if equity <= 0:
            return 0
        risk_dollars = equity * RISK_PCT
        risk_per_unit = abs(price - stop)
        if risk_per_unit <= 0:
            return 0
        qty = risk_dollars / risk_per_unit
        # Round to reasonable precision for crypto
        if price > 10000:
            qty = round(qty, 4)    # BTC — fractional
        elif price > 100:
            qty = round(qty, 3)
        else:
            qty = round(qty, 2)
        return max(qty, 0.001)
    except Exception as e:
        log.error(f"calc_qty error: {e}")
        return 0

# ── Daily trade counter ───────────────────────────────────────────────────────

_trades_today = []
_trade_date   = None

def trades_today_count():
    global _trade_date, _trades_today
    today = datetime.now(timezone.utc).date()
    if _trade_date != today:
        _trade_date   = today
        _trades_today = []
    return len(_trades_today)

def record_trade(symbol):
    global _trades_today, _trade_date
    today = datetime.now(timezone.utc).date()
    if _trade_date != today:
        _trade_date   = today
        _trades_today = []
    _trades_today.append({"symbol": symbol, "time": datetime.now(timezone.utc).isoformat()})

# ── Already in position check ─────────────────────────────────────────────────

def already_in_position(symbol):
    positions = get_positions()
    if not positions:
        return False
    sym_clean = symbol.replace("/", "")
    for p in positions:
        if p.get("symbol","").upper() == sym_clean.upper():
            return True
    return False

# ── Main scan loop ────────────────────────────────────────────────────────────

def scan_symbol(symbol, account):
    """Run all 3 strategies on a symbol. Return first valid signal."""
    if already_in_position(symbol):
        log.info(f"{symbol}: already in position — skipping")
        return None

    bars_h = get_bars(symbol, TF_TREND, limit=200)
    bars_e = get_bars(symbol, TF_ENTRY, limit=200)

    if len(bars_h) < 5 or len(bars_e) < 5:
        log.warning(f"{symbol}: insufficient bar data (H:{len(bars_h)} E:{len(bars_e)})")
        return None

    current = float(bars_e[-1]["c"])
    rsi_val = rsi(closes(bars_e))
    vol_avg = sum(volumes(bars_e)[-20:]) / 20
    vol_cur = volumes(bars_e)[-1]
    vol_ratio = round(vol_cur / vol_avg, 2) if vol_avg > 0 else 0

    log.info(f"{symbol}: Price={current} RSI={rsi_val} Vol={vol_ratio}x scanning all 3 strategies")

    # Strategy 1 — Trend Pullback
    sig = strategy_trend_pullback(symbol, bars_h, bars_e)
    if sig:
        log.info(f"{symbol}: Trend Pullback signal detected → {sig['side'].upper()} "
                 f"Entry={sig['price']} Stop={sig['stop']} TP={sig['tp']} RR={sig['rr']}")
        return sig

    # Strategy 2 — Breakout Retest
    sig = strategy_breakout_retest(symbol, bars_e)
    if sig:
        log.info(f"{symbol}: Breakout Retest signal detected → {sig['side'].upper()} "
                 f"Entry={sig['price']} Stop={sig['stop']} TP={sig['tp']} RR={sig['rr']}")
        return sig

    # Strategy 3 — Liquidation Sweep Reversal
    sig = strategy_liq_sweep_reversal(symbol, bars_e)
    if sig:
        log.info(f"{symbol}: Liq. Sweep Reversal signal detected → {sig['side'].upper()} "
                 f"Entry={sig['price']} Stop={sig['stop']} TP={sig['tp']} RR={sig['rr']}")
        return sig

    log.info(f"{symbol}: no signal — Price={current} RSI={rsi_val} Vol={vol_ratio}x")
    return None


def execute_signal(signal, account):
    """Size and place the order for a confirmed signal."""
    symbol = signal["symbol"]
    price  = signal["price"]
    stop   = signal["stop"]
    tp     = signal["tp"]
    side   = signal["side"]

    qty = calc_qty(account, price, stop)
    if qty <= 0:
        log.error(f"{symbol}: calc_qty returned 0 — skipping order")
        return False

    equity = float(account.get("equity", 0))
    risk_d = round(equity * RISK_PCT, 2)
    log.info(f"{symbol}: Placing {side.upper()} order "
             f"qty={qty} entry={price} stop={stop} tp={tp} "
             f"risk=${risk_d} strategy={signal['strategy']}")

    result = place_order(
        symbol     = symbol,
        qty        = qty,
        side       = side,
        order_type = "market",
        stop_loss  = {"stop_price": str(stop)},
        take_profit= {"limit_price": str(tp)},
    )

    if result and result.get("id"):
        log.info(f"{symbol}: [PAPER] Order placed — ID={result['id']} "
                 f"strategy={signal['strategy']} side={side.upper()} "
                 f"qty={qty} stop={stop} tp={tp} RR={signal['rr']}")
        record_trade(symbol)
        # Save signal to JSON log for dashboard
        _append_signal_log(signal, qty, result["id"])
        return True
    else:
        log.error(f"{symbol}: Order placement failed — {result}")
        return False


def _append_signal_log(signal, qty, order_id):
    """Append trade to kibo_trades.json for dashboard consumption."""
    log_file = Path("kibo_trades.json")
    try:
        trades = json.loads(log_file.read_text()) if log_file.exists() else []
    except Exception:
        trades = []
    trades.append({
        "ts":       datetime.now(timezone.utc).isoformat(),
        "symbol":   signal["symbol"],
        "strategy": signal["strategy"],
        "side":     signal["side"],
        "entry":    signal["price"],
        "stop":     signal["stop"],
        "tp":       signal["tp"],
        "rr":       signal["rr"],
        "qty":      qty,
        "order_id": order_id,
        "status":   "open",
    })
    # Keep last 100 trades
    log_file.write_text(json.dumps(trades[-100:], indent=2))



def run_diagnostics():
    """Test what bar data Alpaca actually returns before main loop."""
    log.info("Running Alpaca data diagnostics...")
    test_sym = "BTC/USD"
    for tf in ["1Day", "4Hour", "1Hour", "15Min", "5Min"]:
        bars = get_bars(test_sym, tf, limit=200)
        if bars:
            first_t = bars[0].get("t","?")[:10]
            last_t  = bars[-1].get("t","?")[:10]
            log.info(f"  {tf}: {len(bars)} bars | {first_t} → {last_t}")
        else:
            log.warning(f"  {tf}: 0 bars — endpoint may not support this timeframe")
    log.info("Diagnostics complete")

def run():
    run_diagnostics()
    log.info("=" * 60)
    log.info(" KIBO Strategy Agent — Bread Trading Framework")
    log.info(f" Account: {ALPACA_BASE}")
    log.info(f" Watchlist: {', '.join(WATCHLIST)}")
    log.info(f" Strategies: Trend Pullback | Breakout Retest | Liq. Sweep Reversal")
    log.info(f" Risk: {RISK_PCT*100:.1f}% per trade | Target: {R_TARGET}R | Max trades/day: {MAX_TRADES_PER_DAY}")
    log.info("=" * 60)

    while True:
        try:
            # ── Account check ──────────────────────────────────────────────
            account = get_account()
            if not account:
                log.warning("Could not fetch account — retrying in 30s")
                time.sleep(30)
                continue

            equity = float(account.get("equity", 0))
            bp     = float(account.get("buying_power", 0))
            log.info(f"Account: equity=${equity:,.2f} buying_power=${bp:,.2f} "
                     f"trades_today={trades_today_count()}/{MAX_TRADES_PER_DAY}")

            # ── Bullet check ───────────────────────────────────────────────
            if trades_today_count() >= MAX_TRADES_PER_DAY:
                log.info(f"Max {MAX_TRADES_PER_DAY} trades reached for today — waiting for next session")
                time.sleep(SCAN_INTERVAL_SEC * 5)
                continue

            # ── BTC macro filter ───────────────────────────────────────────
            btc_ok = btc_is_supportive()

            # ── Scan each symbol ───────────────────────────────────────────
            for symbol in WATCHLIST:
                if trades_today_count() >= MAX_TRADES_PER_DAY:
                    break

                signal = scan_symbol(symbol, account)
                if not signal:
                    continue

                # Block long signals if BTC is bearish
                if signal["side"] == "buy" and not btc_ok:
                    log.info(f"{symbol}: ICC BLOCKED — BTC macro filter active (bearish)")
                    continue

                # All checks passed — execute
                log.info(f"{symbol}: ICC APPROVED — executing {signal['strategy']} "
                         f"{signal['side'].upper()} RR={signal['rr']}")
                execute_signal(signal, account)

                # Small pause between orders
                time.sleep(2)

        except KeyboardInterrupt:
            log.info("KIBO Strategy Agent stopped by user")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}", exc_info=True)

        log.info(f"Scan complete — sleeping {SCAN_INTERVAL_SEC}s")
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    run()
