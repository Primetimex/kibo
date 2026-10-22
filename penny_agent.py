"""
KIBO Penny & Small Cap Agent v2
Price Range : $0.10 – $5.00 (pennies + small caps)
Scanner     : Auto top gainers + manual watchlist you control
Data        : Yahoo Finance (free, no rate limits) + Polygon fallback
Timeframe   : 5-minute bars
Signal      : Full ICC structure rules (all 14 videos)
Hours       : NY session only 9:45 AM – 3:55 PM ET
Broker      : TradeZero Developer API
Alerts      : Telegram
"""

import os, time, logging, requests, uuid
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
from structure_rules import (
    check_entry_rules, classify_trend, format_structure_alert,
    detect_reversal, detect_indication, is_in_session
)

load_dotenv("_env")
try:
    from news_intel import get_news_score
except ImportError:
    def get_news_score(sym, direction=None): return {"score":0,"sentiment":"NO_NEWS","boost":False,"block":False,"fresh":False,"reason":"news_intel not available"}

POLYGON_API_KEY   = os.getenv("POLYGON_API_KEY")
TZ_API_KEY_ID     = os.getenv("TZ_API_KEY_ID")
TZ_API_SECRET_KEY = os.getenv("TZ_API_SECRET_KEY")
TZ_ACCOUNT_ID     = os.getenv("TZ_ACCOUNT_ID")
TZ_BASE_URL       = "https://webapi.tradezero.com"
ACCOUNT_SIZE      = float(os.getenv("ACCOUNT_SIZE", "1000000"))
PAPER_MODE        = os.getenv("PAPER_MODE", "true").lower() == "true"
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")

# ── Price range ───────────────────────────────────────────────────────────────
MIN_PRICE        = 0.10    # true pennies floor
MAX_PRICE        = 5.00    # expanded to include small caps
PENNY_MAX        = 1.00    # $0.10-$1.00 = penny tier (tighter risk)
SMALLCAP_MAX     = 5.00    # $1.00-$5.00 = small cap tier (slightly wider)

# ── Filters ───────────────────────────────────────────────────────────────────
MIN_VOLUME       = 200_000   # 200K shares minimum — lower for early morning
MIN_RELATIVE_VOL = 1.5       # 1.5x relative volume — catches early movers
MAX_POSITIONS    = 3
MAX_WATCHLIST    = 25

# ── Risk — tiered by price range ─────────────────────────────────────────────
RISK_PCT_PENNY   = 0.005   # 0.5% for true pennies ($0.10-$1.00)
RISK_PCT_SMALL   = 0.008   # 0.8% for small caps ($1.00-$5.00)
ATR_MULT_PENNY   = 1.5
ATR_MULT_SMALL   = 2.0
RR_RATIO         = 1.5     # 1.5:1 minimum — realistic for pennies
MAX_SHARES       = 50_000  # cap for pennies
MAX_SHARES_SMALL = 10_000  # cap for small caps

# ── Session ───────────────────────────────────────────────────────────────────
MARKET_OPEN      = (9, 30)
MARKET_CLOSE     = (16, 0)
TRADE_START      = (9, 30)   # start immediately at open
TRADE_END        = (15, 55)  # close all positions by 3:55 PM

# ─────────────────────────────────────────────────────────────────────────────
#  MANUAL WATCHLIST — add/remove tickers here anytime
#  These are ALWAYS scanned regardless of the auto-scanner results
#  Set to [] to use only auto-scanner
# ─────────────────────────────────────────────────────────────────────────────
MANUAL_WATCHLIST = [
    # Add your tickers here, e.g.:
    # "SNDL", "CLOV", "BBIG", "EXPR", "NAKD",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("penny_agent.log"),
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
    manual = f"\nManual: {', '.join(MANUAL_WATCHLIST)}" if MANUAL_WATCHLIST else ""
    send_telegram(
        f"💰 <b>KIBO Penny Agent v2 STARTED</b>\n"
        f"Mode    : {mode}\n"
        f"Range   : $0.10 – $5.00 (pennies + small caps)\n"
        f"Data    : Yahoo Finance + Polygon fallback\n"
        f"Risk    : 0.5% pennies / 0.8% small caps\n"
        f"Max pos : {MAX_POSITIONS}{manual}\n"
        f"Time    : {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
    )

def alert_watchlist(auto_syms, manual_syms):
    lines = [f"  • {s['sym']} ${s['price']:.2f} +{s['chg_pct']:.1f}% Vol:{s['vol']:,}"
             for s in auto_syms[:10]]
    if manual_syms:
        lines.append(f"\n📌 Manual: {', '.join(manual_syms)}")
    send_telegram(
        f"📋 <b>KIBO Penny Watchlist — {datetime.now().strftime('%Y-%m-%d')}</b>\n"
        f"Auto: {len(auto_syms)} movers\n" + "\n".join(lines)
    )

def alert_trade_opened(sym, direction, entry, stop, target, shares, tier):
    emoji = "📈" if direction == "LONG" else "📉"
    mode  = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    risk  = abs(entry - stop) * shares
    rr    = abs(target - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0
    send_telegram(
        f"{emoji} <b>PENNY TRADE OPENED</b> [{mode}]\n"
        f"Symbol    : <b>{sym}</b> [{tier}]\n"
        f"Direction : {direction}\n"
        f"Entry     : ${entry:.4f}\n"
        f"Stop      : ${stop:.4f}\n"
        f"Target    : ${target:.4f}\n"
        f"Shares    : {shares:,}\n"
        f"Risk $    : ${risk:,.2f}\n"
        f"R:R       : {rr:.1f}:1\n"
        f"Time      : {datetime.now().strftime('%H:%M:%S ET')}"
    )

def alert_trade_closed(sym, reason, entry, exit_price, shares):
    pnl   = (exit_price - entry) * shares
    pct   = (exit_price - entry) / entry * 100 if entry > 0 else 0
    emoji = "✅" if pnl >= 0 else "🛑"
    mode  = "📄 PAPER" if PAPER_MODE else "💰 LIVE"
    send_telegram(
        f"{emoji} <b>PENNY CLOSED — {reason}</b> [{mode}]\n"
        f"Symbol : <b>{sym}</b>\n"
        f"Entry  : ${entry:.4f}  →  Exit: ${exit_price:.4f}\n"
        f"P&L    : ${pnl:+,.2f} ({pct:+.2f}%)\n"
        f"Shares : {shares:,}\n"
        f"Time   : {datetime.now().strftime('%H:%M:%S ET')}"
    )

def alert_daily_summary(trades):
    date_str  = datetime.now().strftime('%Y-%m-%d')
    if not trades:
        send_telegram(f"📊 <b>Penny Daily Summary — {date_str}</b>\nNo trades today.")
        return
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    winners   = sum(1 for t in trades if t.get("pnl", 0) > 0)
    syms      = ", ".join(set(t["sym"] for t in trades))
    send_telegram(
        f"📊 <b>Penny Daily Summary — {date_str}</b>\n"
        f"Stocks  : {syms}\n"
        f"Trades  : {len(trades)}\n"
        f"Winners : {winners}/{len(trades)}\n"
        f"P&L     : ${total_pnl:+,.2f}"
    )

# ─────────────────────────────────────────────────────────────────────────────
#  Session
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

def is_market_open():
    now = datetime.now()
    if now.weekday() >= 5:
        return False, "Weekend"
    h, m = now.hour, now.minute
    t    = h * 60 + m
    # Pre-market scan starts at 7 AM — build watchlist early
    if t < 7*60:
        return False, "Overnight — pre-market starts 7:00 AM ET"
    if t >= MARKET_CLOSE[0]*60+MARKET_CLOSE[1]:
        return False, "Market closed"
    if t >= TRADE_END[0]*60+TRADE_END[1]:
        return False, "EOD zone — no new entries after 3:55 PM"
    return True, "Active"

# ─────────────────────────────────────────────────────────────────────────────
#  Scanner — Yahoo Finance top movers (free, no rate limits)
# ─────────────────────────────────────────────────────────────────────────────

def get_top_movers_yahoo():
    """
    Get top penny movers using multiple data sources with fallbacks.
    Source 1: Yahoo Finance chart API (individual tickers — reliable)
    Source 2: Yahoo Finance screener (sometimes blocked)
    Source 3: Finviz screener scrape
    Source 4: Polygon gainers
    """
    log.info("Scanning for penny movers...")
    candidates = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    # ── Source 1: Yahoo Finance screener (try multiple endpoints) ─────────────
    screener_urls = [
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=100",
        "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=100",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=100",
        "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=100",
    ]
    for url in screener_urls:
        if len(candidates) >= 10:
            break
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code != 200:
                continue
            quotes = r.json().get("finance",{}).get("result",[{}])[0].get("quotes",[])
            if not quotes:
                continue
            for q in quotes:
                sym   = q.get("symbol","")
                price = float(q.get("regularMarketPrice") or 0)
                vol   = int(q.get("regularMarketVolume") or 0)
                chg   = float(q.get("regularMarketChangePercent") or 0)
                avg   = float(q.get("averageDailyVolume3Month") or vol or 1)
                # Project full-day volume — at 10 AM we've only had ~30 min of trading
                # so raw vol looks low. Project to full 6.5hr day for accurate rel vol.
                from datetime import datetime as _dt
                _mins = max((_dt.now().hour - 9) * 60 + _dt.now().minute - 30, 1)
                _frac = min(_mins / 390, 1.0)
                proj_vol = vol / _frac if _frac > 0 else vol
                rel   = round(proj_vol / avg, 1) if avg > 0 else 1.0
                if not (MIN_PRICE <= price <= MAX_PRICE): continue
                if vol < 50_000: continue
                if rel < MIN_RELATIVE_VOL: continue
                if len(sym) > 5 or "." in sym: continue
                tier = "PENNY" if price <= PENNY_MAX else "SMALL_CAP"
                candidates.append({"sym":sym,"price":price,"vol":vol,
                    "rel_vol":rel,"chg_pct":round(chg,1),"tier":tier})
            if candidates:
                log.info(f"Yahoo screener: found {len(candidates)} candidates")
                break
        except Exception as e:
            log.warning(f"Yahoo screener ({url[:50]}): {e}")
        time.sleep(0.3)

    # ── Source 2: Yahoo Finance trending tickers ──────────────────────────────
    if len(candidates) < 5:
        try:
            r = requests.get(
                "https://query1.finance.yahoo.com/v1/finance/trending/US",
                headers=headers, timeout=8
            )
            if r.status_code == 200:
                tickers = r.json().get("finance",{}).get("result",[{}])[0].get("quotes",[])
                syms = [t.get("symbol","") for t in tickers if len(t.get("symbol",""))<=5][:20]
                for sym in syms:
                    try:
                        r2 = requests.get(
                            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                            headers=headers, params={"interval":"5m","range":"1d"}, timeout=6
                        )
                        if r2.status_code != 200: continue
                        meta = r2.json().get("chart",{}).get("result",[{}])[0].get("meta",{})
                        price = float(meta.get("regularMarketPrice") or 0)
                        vol   = int(meta.get("regularMarketVolume") or 0)
                        prev  = float(meta.get("chartPreviousClose") or price or 1)
                        chg   = ((price - prev) / prev * 100) if prev > 0 else 0
                        avg   = float(meta.get("averageDailyVolume10Day") or vol or 1)
                        rel   = round(vol/avg, 1) if avg > 0 else 1.0
                        if not (MIN_PRICE <= price <= MAX_PRICE): continue
                        if vol < MIN_VOLUME: continue
                        tier = "PENNY" if price <= PENNY_MAX else "SMALL_CAP"
                        candidates.append({"sym":sym,"price":price,"vol":vol,
                            "rel_vol":rel,"chg_pct":round(chg,1),"tier":tier})
                        time.sleep(0.2)
                    except: continue
        except Exception as e:
            log.warning(f"Yahoo trending: {e}")

    # ── Source 3: Polygon gainers fallback ────────────────────────────────────
    if len(candidates) < 5 and POLYGON_API_KEY:
        log.info("Trying Polygon gainers...")
        try:
            r = requests.get(
                f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/gainers"
                f"?apiKey={POLYGON_API_KEY}&include_otc=false",
                timeout=15
            )
            if r.status_code == 200:
                for t in r.json().get("tickers", []):
                    day  = t.get("day", {})
                    prev = t.get("prevDay", {})
                    price = float(day.get("c") or 0)
                    vol   = int(day.get("v") or 0)
                    prev_c = float(prev.get("c") or price or 1)
                    chg_pct = ((price-prev_c)/prev_c*100) if prev_c > 0 else 0
                    prev_vol = float(prev.get("v") or vol or 1)
                    rel = round(vol/prev_vol, 1) if prev_vol > 0 else 1.0
                    sym = t.get("ticker","")
                    if not (MIN_PRICE<=price<=MAX_PRICE): continue
                    if vol < MIN_VOLUME: continue
                    if len(sym)>5: continue
                    tier = "PENNY" if price<=PENNY_MAX else "SMALL_CAP"
                    candidates.append({"sym":sym,"price":price,"vol":vol,
                        "rel_vol":rel,"chg_pct":round(chg_pct,1),"tier":tier})
                log.info(f"Polygon: found {len(candidates)} total candidates")
        except Exception as e:
            log.error(f"Polygon fallback: {e}")

    # ── Deduplicate and sort ──────────────────────────────────────────────────
    seen = set()
    unique = []
    for c in candidates:
        if c["sym"] not in seen:
            seen.add(c["sym"])
            unique.append(c)

    unique.sort(key=lambda x: (x["rel_vol"], x["chg_pct"]), reverse=True)
    unique = unique[:MAX_WATCHLIST]

    log.info(f"Final watchlist: {len(unique)} movers — {[c['sym'] for c in unique[:10]]}")
    return unique

# ─────────────────────────────────────────────────────────────────────────────
#  Market Data — Yahoo Finance primary, Polygon fallback
# ─────────────────────────────────────────────────────────────────────────────

def get_bars(symbol, lookback=100):
    """Yahoo Finance 5-min bars — free, no API key needed."""
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        r = requests.get(url, headers=headers,
                         params={"interval":"5m","range":"2d"}, timeout=10)
        if r.status_code == 200:
            data  = r.json()
            res   = data.get("chart",{}).get("result",[])
            if res:
                chart = res[0]
                ts    = chart.get("timestamp",[])
                ohlcv = chart.get("indicators",{}).get("quote",[{}])[0]
                if ts and ohlcv.get("close"):
                    df = pd.DataFrame({
                        "timestamp": pd.to_datetime(ts, unit="s"),
                        "open":   ohlcv.get("open",[]),
                        "high":   ohlcv.get("high",[]),
                        "low":    ohlcv.get("low",[]),
                        "close":  ohlcv.get("close",[]),
                        "volume": ohlcv.get("volume",[]),
                    })
                    df = df.dropna(subset=["close"])
                    df["timestamp"] = utc_to_et(df["timestamp"])
                    df = df.tail(lookback).reset_index(drop=True)
                    log.info(f"{symbol}: fetched {len(df)} bars from Yahoo")
                    time.sleep(0.3)
                    return df
    except Exception as e:
        log.warning(f"{symbol}: Yahoo failed ({e}) — trying Polygon")

    # Polygon fallback
    if POLYGON_API_KEY:
        try:
            end   = datetime.now()
            start = end - timedelta(days=5)
            url2  = (
                f"https://api.polygon.io/v2/aggs/ticker/{symbol}"
                f"/range/5/minute/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
                f"?adjusted=true&sort=asc&limit={lookback}&apiKey={POLYGON_API_KEY}"
            )
            r2 = requests.get(url2, timeout=10)
            if r2.status_code == 429:
                log.warning(f"{symbol}: Polygon 429 — skipping")
                return pd.DataFrame()
            r2.raise_for_status()
            data2 = r2.json()
            if data2.get("results"):
                df2 = pd.DataFrame(data2["results"])
                df2["timestamp"] = pd.to_datetime(df2["t"], unit="ms")
                df2.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"}, inplace=True)
                df2["timestamp"] = utc_to_et(df2["timestamp"])
                log.info(f"{symbol}: fetched {len(df2)} bars from Polygon (fallback)")
                time.sleep(13)
                return df2[["timestamp","open","high","low","close","volume"]].tail(lookback).reset_index(drop=True)
        except Exception as e:
            log.error(f"{symbol}: all sources failed — {e}")

    return pd.DataFrame()

# ─────────────────────────────────────────────────────────────────────────────
#  Indicators
# ─────────────────────────────────────────────────────────────────────────────

def add_indicators(df):
    df = df.copy()
    # Fill zero volumes with small value to avoid division by zero
    df["volume"] = df["volume"].replace(0, 1)
    delta        = df["close"].diff()
    gain         = delta.clip(lower=0).ewm(com=13, min_periods=14).mean()
    loss         = (-delta.clip(upper=0)).ewm(com=13, min_periods=14).mean()
    rs           = gain / loss.replace(0, np.nan)
    df["rsi"]    = 100 - (100 / (1 + rs))
    df["ema9"]   = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema21"]  = df["close"].ewm(span=21, adjust=False).mean()
    prev         = df["close"].shift(1)
    tr           = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"]  - prev).abs()
    ], axis=1).max(axis=1)
    df["atr"]       = tr.ewm(span=14, adjust=False).mean()
    df["vol_avg20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_avg20"]
    macd_fast       = df["close"].ewm(span=12, adjust=False).mean()
    macd_slow       = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]      = macd_fast - macd_slow
    df["macd_sig"]  = df["macd"].ewm(span=9, adjust=False).mean()
    return df.dropna()

# ─────────────────────────────────────────────────────────────────────────────
#  Pre-filters
# ─────────────────────────────────────────────────────────────────────────────

def penny_prefilter(df, sym):
    if len(df) < 20:
        return False, "Not enough bars"
    last  = df.iloc[-1]
    price = last["close"]
    if not (MIN_PRICE <= price <= MAX_PRICE):
        return False, f"Price ${price:.4f} outside ${MIN_PRICE}–${MAX_PRICE} range"
    # Skip vol check if ratio is 0.0 (Yahoo didn't return intraday vol yet)
    # but raw volume must be at least 50K
    if last["vol_ratio"] > 0 and last["vol_ratio"] < 1.2:
        return False, f"Volume too low ({last['vol_ratio']:.1f}x avg)"
    if last["vol_ratio"] == 0 and last["volume"] < 50_000:
        return False, f"Volume too low (raw {int(last['volume']):,} shares)"
    atr_pct = last["atr"] / price * 100
    if atr_pct < 0.5:
        return False, f"ATR too small ({atr_pct:.1f}%) — stock not moving"
    day_range = (df["high"].max() - df["low"].min()) / df["low"].min() * 100
    if day_range > 300:
        return False, f"Extreme spike ({day_range:.0f}% range) — manipulation risk"
    return True, "Pre-filter passed"

# ─────────────────────────────────────────────────────────────────────────────
#  Signal Logic
# ─────────────────────────────────────────────────────────────────────────────

def check_signal(df):
    """Score-based signal — needs 3+ points. ICC does final filtering."""
    if len(df) < 20:
        return "NONE"
    row  = df.iloc[-1]
    prev = df.iloc[-2]
    trend = classify_trend(df)
    if trend["trend"] == "CONSOLIDATION":
        return "NONE"

    vol_ok = row["vol_ratio"] >= 1.2 or row["vol_ratio"] == 0.0

    # LONG score
    long_score = 0
    if trend["trend"] == "UPTREND":          long_score += 2
    if 45 <= row["rsi"] <= 80:               long_score += 1
    if row["ema9"] > row["ema21"]:           long_score += 1
    if row["macd"] > row["macd_sig"]:        long_score += 1
    if prev["macd"] <= prev["macd_sig"] and row["macd"] > row["macd_sig"]: long_score += 1  # fresh cross
    if vol_ok:                               long_score += 1

    # SHORT score
    short_score = 0
    if trend["trend"] == "DOWNTREND":        short_score += 2
    if 20 <= row["rsi"] <= 55:               short_score += 1
    if row["ema9"] < row["ema21"]:           short_score += 1
    if row["macd"] < row["macd_sig"]:        short_score += 1
    if prev["macd"] >= prev["macd_sig"] and row["macd"] < row["macd_sig"]: short_score += 1
    if vol_ok:                               short_score += 1

    if long_score >= 4 and long_score > short_score:
        return "LONG"
    if short_score >= 4 and short_score > long_score:
        return "SHORT"
    return "NONE"


def calc_position(direction, entry, atr):
    is_penny  = entry <= PENNY_MAX
    risk_pct  = RISK_PCT_PENNY   if is_penny else RISK_PCT_SMALL
    atr_mult  = ATR_MULT_PENNY   if is_penny else ATR_MULT_SMALL
    max_sh    = MAX_SHARES       if is_penny else MAX_SHARES_SMALL
    tier      = "PENNY"          if is_penny else "SMALL_CAP"

    stop_dist = max(atr * atr_mult, 0.02 if is_penny else 0.05)

    if direction == "LONG":
        stop_loss   = max(entry - stop_dist, 0.01)
        take_profit = entry + (stop_dist * RR_RATIO)
    else:
        stop_loss   = entry + stop_dist
        take_profit = max(entry - (stop_dist * RR_RATIO), 0.01)

    max_risk = ACCOUNT_SIZE * risk_pct
    shares   = int(min(max_risk / stop_dist, max_sh))
    shares   = max(shares, 100)

    return {
        "entry":       round(entry, 4),
        "stop_loss":   round(stop_loss, 4),
        "take_profit": round(take_profit, 4),
        "shares":      shares,
        "risk_usd":    round(stop_dist * shares, 2),
        "tier":        tier,
    }

# ─────────────────────────────────────────────────────────────────────────────
#  TradeZero Execution
# ─────────────────────────────────────────────────────────────────────────────

def _check_order_fill(client_id, sym, timeout=20):
    """
    Poll TradeZero until order is filled, rejected, or timeout.
    Returns (filled: bool, executed_qty: int)
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
                leaves   = int(o.get("leavesQuantity", 1) or 1)
                log.info(f"Order {client_id} | status={status} executed={executed} leaves={leaves}")
                if status in ("Filled", "PartiallyFilled") and executed > 0:
                    return True, executed
                if status in ("Rejected", "Canceled", "Cancelled", "Expired"):
                    log.warning(f"Order {client_id} {status} — not filled")
                    return False, 0
                # Still PendingNew or Working — keep waiting
            time.sleep(2)
        except Exception as e:
            log.warning(f"Fill check error: {e}")
            time.sleep(2)
    log.warning(f"Order {client_id} fill timeout after {timeout}s — treating as failed")
    return False, 0


def place_order(sym, direction, shares, entry, stop, target):
    # ── Guard: already have a pending order for this symbol ───────────────────
    if sym in pending_orders:
        placed_at = pending_orders[sym]["placed_at"]
        age = (datetime.now() - placed_at).total_seconds()
        if age < MAX_PENDING_AGE:
            log.warning(f"{sym}: Order already pending ({age:.0f}s old) — skipping duplicate")
            return False
        else:
            log.warning(f"{sym}: Stale pending order ({age:.0f}s) — clearing and retrying")
            del pending_orders[sym]

    # ── Guard: already in position ────────────────────────────────────────────
    if sym in open_positions:
        log.warning(f"{sym}: Already in open position — skipping")
        return False

    action    = "Buy" if direction == "LONG" else "Sell"
    client_id = f"KIBO-{uuid.uuid4().hex[:8].upper()}"
    log.info(f"[{'PAPER' if PAPER_MODE else 'LIVE'}] {action} {shares:,} {sym} @ ${entry:.4f} | SL=${stop:.4f} TP=${target:.4f} | id={client_id}")

    payload = {
        "clientOrderId": client_id,
        "securityType":  "Stock",
        "symbol":        sym,
        "side":          action,
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
        data = resp.json()
        log.info(f"Order submitted: {data}")

        # Track as pending immediately
        pending_orders[sym] = {
            "client_id":  client_id,
            "placed_at":  datetime.now(),
            "side":       action,
            "shares":     shares,
        }

        # Wait for fill confirmation — do NOT assume success from 200 OK
        filled, executed_qty = _check_order_fill(client_id, sym, timeout=25)

        if filled:
            log.info(f"✅ {sym} filled: {executed_qty} shares @ ~${entry:.4f}")
            del pending_orders[sym]
            return True
        else:
            log.warning(f"❌ {sym} order not filled (id={client_id}) — position NOT added")
            # Cancel the stale order if still open
            try:
                requests.delete(
                    f"{TZ_BASE_URL}/v1/api/accounts/{TZ_ACCOUNT_ID}/orders/{client_id}",
                    headers=TZ_HEADERS, timeout=8
                )
                log.info(f"Cancelled stale order {client_id}")
            except:
                pass
            if sym in pending_orders:
                del pending_orders[sym]
            send_telegram(f"⚠️ <b>ORDER NOT FILLED — {sym}</b>\nOrder {client_id} was not executed.")
            return False

    except Exception as e:
        log.error(f"Order failed {sym}: {e}")
        if sym in pending_orders:
            del pending_orders[sym]
        send_telegram(f"⚠️ <b>ORDER FAILED — {sym}</b>\n{str(e)[:200]}")
        return False

def close_position(sym, direction, shares):
    action = "Sell" if direction == "LONG" else "Buy"
    # NOTE: always send the close order — even in paper mode — so TradeZero
    # actually closes the position. The old early-return in paper mode was
    # causing the bot to think a position was closed when it wasn't.
    client_id = f"KIBO-CLOSE-{sym}-{uuid.uuid4().hex[:4].upper()}"
    payload = {
        "clientOrderId": client_id,
        "securityType":  "Stock",
        "symbol":        sym,
        "side":          action,
        "openClose":     "Close",
        "orderType":     "Market",
        "orderQuantity": shares,
        "timeInForce":   "Day",
        "route":         "PAPER",
    }
    log.info(f"[{'PAPER' if PAPER_MODE else 'LIVE'}] CLOSE {shares:,} {sym} — {action} | id={client_id}")
    try:
        resp = requests.post(
            f"{TZ_BASE_URL}/v1/api/accounts/{TZ_ACCOUNT_ID}/order",
            headers=TZ_HEADERS, json=payload, timeout=10
        )
        resp.raise_for_status()
        log.info(f"Close order submitted: {resp.json()}")
        return True
    except Exception as e:
        log.error(f"Close failed {sym}: {e}")
        send_telegram(f"⚠️ <b>CLOSE FAILED — {sym}</b>\nManual close needed!\n{str(e)[:200]}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
#  Exit Manager
# ─────────────────────────────────────────────────────────────────────────────

open_positions  = {}   # sym -> position dict
trades_today    = []
pending_orders  = {}   # sym -> {"client_id": ..., "placed_at": datetime, "side": ...}
MAX_PENDING_AGE = 30   # seconds before we consider a pending order stale/dead

def check_exits():
    now = datetime.now()
    force_close = now.hour == 15 and now.minute >= 55
    for sym, pos in list(open_positions.items()):
        try:
            df = get_bars(sym, lookback=5)
            if df.empty:
                continue
            price = df.iloc[-1]["close"]
            d     = pos["direction"]
            hit_tp = (d=="LONG" and price>=pos["take_profit"]) or (d=="SHORT" and price<=pos["take_profit"])
            hit_sl = (d=="LONG" and price<=pos["stop_loss"])   or (d=="SHORT" and price>=pos["stop_loss"])
            if hit_tp:
                log.info(f"TARGET HIT {sym} @ ${price:.4f}")
                close_position(sym, d, pos["shares"])
                pnl = abs(pos["take_profit"] - pos["entry"]) * pos["shares"]
                alert_trade_closed(sym, "TAKE PROFIT ✅", pos["entry"], price, pos["shares"])
                trades_today.append({"sym": sym, "pnl": pnl})
                del open_positions[sym]
            elif hit_sl:
                log.info(f"STOP HIT {sym} @ ${price:.4f}")
                close_position(sym, d, pos["shares"])
                pnl = -abs(pos["stop_loss"] - pos["entry"]) * pos["shares"]
                alert_trade_closed(sym, "STOP LOSS 🛑", pos["entry"], price, pos["shares"])
                trades_today.append({"sym": sym, "pnl": pnl})
                del open_positions[sym]
            elif force_close:
                log.info(f"EOD CLOSE {sym} @ ${price:.4f}")
                close_position(sym, d, pos["shares"])
                pnl = (price - pos["entry"]) * pos["shares"] * (1 if d=="LONG" else -1)
                alert_trade_closed(sym, "EOD CLOSE 🔔", pos["entry"], price, pos["shares"])
                trades_today.append({"sym": sym, "pnl": pnl})
                del open_positions[sym]
        except Exception as e:
            log.error(f"Exit check error {sym}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
#  Main Loop
# ─────────────────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 64)
    log.info("KIBO Penny & Small Cap Agent v2 — STARTED")
    log.info(f"Mode    : {'PAPER' if PAPER_MODE else 'LIVE'}")
    log.info(f"Range   : $0.10 – $5.00 (pennies + small caps)")
    log.info(f"Data    : Yahoo Finance primary + Polygon fallback")
    log.info(f"Manual  : {MANUAL_WATCHLIST or 'none'}")
    log.info(f"Risk    : {RISK_PCT_PENNY*100:.1f}% pennies / {RISK_PCT_SMALL*100:.1f}% small caps")
    log.info("=" * 64)

    scan_interval    = 2 * 60   # scan every 2 min
    auto_watchlist   = []
    last_scan_day    = None
    last_summary_day = None

    while True:
        now = datetime.now()

        # Daily summary at 4:05 PM
        if now.hour == 16 and now.minute >= 5 and last_summary_day != now.date():
            alert_daily_summary(trades_today)
            trades_today.clear()
            last_summary_day = now.date()
            auto_watchlist   = []   # reset daily

        # Market hours check
        mkt_open, mkt_reason = is_market_open()
        if not mkt_open:
            log.info(f"Market: {mkt_reason} — waiting 60s")
            time.sleep(60)
            continue

        # Build watchlist: at start of day OR if watchlist is empty
        should_scan = (last_scan_day != now.date()) or (not auto_watchlist and not MANUAL_WATCHLIST)
        if should_scan:
            log.info("Building today's penny watchlist...")
            auto_watchlist = get_top_movers_yahoo()
            last_scan_day  = now.date()
            if auto_watchlist:
                log.info(f"Watchlist ready: {[c['sym'] for c in auto_watchlist[:10]]}")
            else:
                log.warning("No movers found from any source — will retry next cycle")

        # Combine auto + manual watchlist
        manual_entries = [{"sym": s, "tier": "MANUAL", "chg_pct": 0, "vol": 0, "rel_vol": 0, "price": 0}
                         for s in MANUAL_WATCHLIST]
        watchlist = auto_watchlist + [e for e in manual_entries
                                      if e["sym"] not in {w["sym"] for w in auto_watchlist}]

        if not watchlist:
            log.info("Watchlist empty — retrying in 60s...")
            time.sleep(60)
            continue

        # Check exits on open positions
        if open_positions:
            check_exits()

        # Max positions check
        if len(open_positions) >= MAX_POSITIONS:
            log.info(f"Max positions ({MAX_POSITIONS}) reached")
            time.sleep(scan_interval)
            continue

        log.info(f"--- Scanning {len(watchlist)} stocks ({len(auto_watchlist)} auto + {len(MANUAL_WATCHLIST)} manual) ---")

        for stock in watchlist:
            sym = stock["sym"]
            if sym in open_positions:
                continue

            try:
                df = get_bars(sym)
                if df.empty or len(df) < 20:
                    time.sleep(1)
                    continue

                df = add_indicators(df)
                if len(df) < 2:
                    time.sleep(1)
                    continue

                last_row = df.iloc[-1]
                price    = last_row["close"]

                # Pre-filter
                ok, reason = penny_prefilter(df, sym)
                if not ok:
                    log.info(f"{sym}: PRE-FILTER — {reason}")
                    time.sleep(1)
                    continue

                # Determine tier
                tier = "PENNY" if price <= PENNY_MAX else "SMALL_CAP"

                # Signal check
                signal = check_signal(df)
                _st    = classify_trend(df)
                _rv    = detect_reversal(df, _st["trend"])
                _ind   = detect_indication(df)

                if signal == "NONE":
                    log.info(
                        f"{sym} [{tier}]: no signal | "
                        f"${price:.4f} RSI={last_row['rsi']:.1f} "
                        f"Vol={last_row['vol_ratio']:.1f}x | "
                        f"ICC={_st['trend']}({_st['confidence']*100:.0f}%)"
                        + (f" | REV: {_rv['reason'][:35]}" if _rv["reversal_likely"] else "")
                        + (f" | IND: {_ind['reason'][:35]}" if _ind.get("indication") else "")
                    )
                    time.sleep(1)
                    continue

                log.info(f"{sym} [{tier}]: {signal} signal!")

                # Position sizing
                pos = calc_position(signal, price, last_row["atr"])
                log.info(
                    f"{sym}: entry=${pos['entry']:.4f} stop=${pos['stop_loss']:.4f} "
                    f"target=${pos['take_profit']:.4f} shares={pos['shares']:,} "
                    f"risk=${pos['risk_usd']:.2f} tier={pos['tier']}"
                )

                # ICC Full Rules
                sc = check_entry_rules(df, signal)
                if not sc["approved"]:
                    log.info(f"{sym}: ICC BLOCKED — {sc['reason'][:100]}")
                    time.sleep(1)
                    continue

                if sc.get("stop_loss") and sc.get("target"):
                    pos["stop_loss"]   = sc["stop_loss"]
                    pos["take_profit"] = sc["target"]

                log.info(f"{sym}: ICC APPROVED ✅ RR={sc.get('rr',0):.1f}")

                # ── News Intelligence ─────────────────────────────────────────
                news = get_news_score(sym, signal)
                if news["block"]:
                    log.info(f"{sym}: NEWS BLOCK — {news['reason']}")
                    continue
                if news["boost"]:
                    log.info(f"{sym}: NEWS BOOST ✅ — {news['reason']}")
                elif news["sentiment"] != "NO_NEWS":
                    log.info(f"{sym}: NEWS — {news['sentiment']} {news['score']:+d}")
                success = place_order(sym, signal, pos["shares"],
                                      pos["entry"], pos["stop_loss"], pos["take_profit"])

                if success:
                    open_positions[sym] = {
                        "direction":   signal,
                        "entry":       pos["entry"],
                        "stop_loss":   pos["stop_loss"],
                        "take_profit": pos["take_profit"],
                        "shares":      pos["shares"],
                        "tier":        pos["tier"],
                        "entry_time":  now.isoformat(),
                    }
                    alert_trade_opened(sym, signal, pos["entry"],
                                       pos["stop_loss"], pos["take_profit"],
                                       pos["shares"], pos["tier"])
                    if len(open_positions) >= MAX_POSITIONS:
                        log.info("Max positions reached — pausing new entries")
                        break

            except Exception as e:
                log.error(f"{sym}: error — {e}")

            time.sleep(1)

        log.info(f"Open: {list(open_positions.keys()) or 'none'} | Next scan in 5 min")
        time.sleep(scan_interval)


if __name__ == "__main__":
    run()
