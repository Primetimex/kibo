"""
KIBO Zone Scanner — Supply & Demand + Support & Resistance
Scans your full watchlist every 5 minutes and identifies:
  - Supply zones   (price areas where sellers overwhelmed buyers)
  - Demand zones   (price areas where buyers overwhelmed sellers)
  - Support levels (price floors price bounces off repeatedly)
  - Resistance levels (price ceilings price gets rejected from repeatedly)
  - Zone strength  (how many times price has respected the level)
  - Zone freshness (has price already returned to this zone?)

Sends Telegram alerts when price is approaching or entering a zone.
Run: python zone_scanner.py
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
    from rate_limiter import wait_for_slot
except ImportError:
    def wait_for_slot(caller=""): pass  # fallback if not present

load_dotenv("_env")
POLYGON_API_KEY  = os.getenv("POLYGON_API_KEY")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ── Watchlist — all symbols across all agents ─────────────────────────────────
WATCHLIST = {
    # Stocks
    "SPY":    {"name": "S&P 500 ETF",        "type": "stock"},
    "QQQ":    {"name": "Nasdaq 100 ETF",      "type": "stock"},
    "AAPL":   {"name": "Apple",               "type": "stock"},
    "TSLA":   {"name": "Tesla",               "type": "stock"},
    "NVDA":   {"name": "Nvidia",              "type": "stock"},
    "AMD":    {"name": "AMD",                 "type": "stock"},
    # Futures proxies
    "GLD":    {"name": "Gold ETF (MGC)",      "type": "stock"},
    "USO":    {"name": "Crude Oil ETF (MCL)", "type": "stock"},
    # Crypto
    "X:BTCUSD": {"name": "Bitcoin",           "type": "crypto"},
    "X:ETHUSD": {"name": "Ethereum",          "type": "crypto"},
    # Forex/Gold spot
    "C:XAUUSD": {"name": "Gold Spot",         "type": "forex"},
}

# ── Zone detection settings ───────────────────────────────────────────────────
ZONE_LOOKBACK      = 200   # bars of history to find zones in
ZONE_TIMEFRAME     = "5"   # 5-minute bars
SWING_LOOKBACK     = 5     # bars each side to confirm a swing high/low
ZONE_BODY_PCT      = 0.3   # candle body must be >= 30% of its range (strong candle)
ZONE_PROXIMITY_PCT = 0.003 # alert when price is within 0.3% of a zone
SR_TOUCHES_MIN     = 2     # minimum touches to confirm support/resistance
ZONE_MERGE_PCT     = 0.005 # merge zones within 0.5% of each other

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("zone_scanner.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 1 — Telegram
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    pass  # Zone scanner alerts disabled


def alert_started():
    send_telegram(
        f"🗺 <b>KIBO Zone Scanner STARTED</b>\n"
        f"Scanning {len(WATCHLIST)} symbols\n"
        f"Looking for: Supply/Demand zones + Support/Resistance\n"
        f"Alert when price within 0.3% of any zone\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
    )


def alert_zone(symbol: str, name: str, zone_type: str, zone_top: float,
               zone_bot: float, current_price: float, strength: int,
               fresh: bool, action: str):
    emojis = {
        "SUPPLY":     "🔴",
        "DEMAND":     "🟢",
        "RESISTANCE": "🟠",
        "SUPPORT":    "🔵",
    }
    emoji     = emojis.get(zone_type, "⚪")
    fresh_tag = "🆕 FRESH (unmitigated)" if fresh else "♻️ Tested before"
    pct_away  = abs(current_price - (zone_top + zone_bot) / 2) / current_price * 100
    send_telegram(
        f"{emoji} <b>{zone_type} ZONE — {symbol}</b>\n"
        f"Name      : {name}\n"
        f"Zone      : {zone_bot:.2f} – {zone_top:.2f}\n"
        f"Price Now : {current_price:.2f} ({pct_away:.2f}% away)\n"
        f"Action    : <b>{action}</b>\n"
        f"Strength  : {'⭐' * min(strength, 5)} ({strength} touches)\n"
        f"Status    : {fresh_tag}\n"
        f"Time      : {datetime.now().strftime('%H:%M:%S ET')}"
    )


def alert_daily_summary(zone_counts: dict):
    lines = "\n".join([f"  {sym}: {cnt} zones" for sym, cnt in zone_counts.items()])
    send_telegram(
        f"📊 <b>Zone Scanner Daily Summary</b>\n"
        f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"Zones found:\n{lines}"
    )


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 2 — Market Data
# ─────────────────────────────────────────────────────────────────────────────

def get_bars(ticker: str, days: int = 30) -> pd.DataFrame:
    end   = datetime.now()
    start = end - timedelta(days=days)
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/{ZONE_TIMEFRAME}/minute"
        f"/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        f"?adjusted=true&sort=asc&limit={ZONE_LOOKBACK}&apiKey={POLYGON_API_KEY}"
    )
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 429:
                log.warning(f"{ticker}: rate limited — waiting 30s")
                time.sleep(30)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("resultsCount", 0) == 0:
                return pd.DataFrame()
            df = pd.DataFrame(data["results"])
            df.rename(columns={"o":"open","h":"high","l":"low","c":"close","v":"volume"}, inplace=True)
            df["timestamp"] = pd.to_datetime(df["t"], unit="ms")
            df["body"]      = abs(df["close"] - df["open"])
            df["range"]     = df["high"] - df["low"]
            df["body_pct"]  = df["body"] / df["range"].replace(0, np.nan)
            time.sleep(13)  # rate limit: free tier 5 calls/min
            return df[["timestamp","open","high","low","close","volume","body","range","body_pct"]].reset_index(drop=True)
        except requests.exceptions.ConnectionError:
            log.warning(f"{ticker}: connection error (attempt {attempt+1}/3) — retrying in 10s")
            time.sleep(10)
        except Exception as e:
            log.error(f"Data fetch failed for {ticker}: {e}")
            return pd.DataFrame()
    return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 3 — Supply & Demand Zone Detection
# ─────────────────────────────────────────────────────────────────────────────

def find_supply_demand_zones(df: pd.DataFrame) -> list:
    """
    Supply & Demand zones are identified by finding:

    DEMAND ZONE (buy zone):
      - A strong bullish candle (big green body)
      - Preceded by a base (small candles — consolidation)
      - Price rallied away sharply from this area
      - The base area = the demand zone
      - Institutional buyers are sitting here waiting to buy again

    SUPPLY ZONE (sell zone):
      - A strong bearish candle (big red body)
      - Preceded by a base (small candles — consolidation)
      - Price dropped away sharply from this area
      - The base area = the supply zone
      - Institutional sellers are sitting here waiting to sell again

    Returns list of zone dicts with type, top, bottom, strength, fresh
    """
    zones = []
    if len(df) < 10:
        return zones

    for i in range(2, len(df) - 2):
        row      = df.iloc[i]
        prev     = df.iloc[i - 1]
        next_bar = df.iloc[i + 1]

        # Skip weak candles
        if pd.isna(row["body_pct"]) or row["body_pct"] < ZONE_BODY_PCT:
            continue

        # ── Demand zone: strong bullish move up ──────────────────────────────
        if (row["close"] > row["open"] and              # bullish candle
            row["body_pct"] >= ZONE_BODY_PCT and        # strong body
            row["close"] > prev["high"]):               # broke above previous high

            zone_top = max(row["open"], prev["high"])
            zone_bot = min(row["open"], prev["low"])

            if zone_top > zone_bot:
                # Count how many times price returned to this zone
                touches = count_zone_touches(df, zone_top, zone_bot, i)
                # Check if zone is fresh (price hasn't returned since creation)
                fresh   = is_zone_fresh(df, zone_top, zone_bot, i)
                zones.append({
                    "type":    "DEMAND",
                    "top":     round(zone_top, 4),
                    "bot":     round(zone_bot, 4),
                    "mid":     round((zone_top + zone_bot) / 2, 4),
                    "strength": touches,
                    "fresh":   fresh,
                    "bar_idx": i,
                    "time":    row["timestamp"],
                })

        # ── Supply zone: strong bearish move down ────────────────────────────
        if (row["close"] < row["open"] and              # bearish candle
            row["body_pct"] >= ZONE_BODY_PCT and        # strong body
            row["close"] < prev["low"]):                # broke below previous low

            zone_top = max(row["open"], prev["high"])
            zone_bot = min(row["open"], prev["low"])

            if zone_top > zone_bot:
                touches = count_zone_touches(df, zone_top, zone_bot, i)
                fresh   = is_zone_fresh(df, zone_top, zone_bot, i)
                zones.append({
                    "type":    "SUPPLY",
                    "top":     round(zone_top, 4),
                    "bot":     round(zone_bot, 4),
                    "mid":     round((zone_top + zone_bot) / 2, 4),
                    "strength": touches,
                    "fresh":   fresh,
                    "bar_idx": i,
                    "time":    row["timestamp"],
                })

    return merge_zones(zones)


def count_zone_touches(df: pd.DataFrame, zone_top: float,
                       zone_bot: float, from_idx: int) -> int:
    """Count how many times price entered the zone after its creation."""
    touches = 0
    for i in range(from_idx + 1, len(df)):
        row = df.iloc[i]
        if row["low"] <= zone_top and row["high"] >= zone_bot:
            touches += 1
    return touches


def is_zone_fresh(df: pd.DataFrame, zone_top: float,
                  zone_bot: float, from_idx: int) -> bool:
    """
    A zone is FRESH if price has never returned to it since it was created.
    Fresh zones are more powerful — institutions haven't filled all their orders yet.
    """
    for i in range(from_idx + 1, len(df)):
        row = df.iloc[i]
        if row["low"] <= zone_top and row["high"] >= zone_bot:
            return False
    return True


def merge_zones(zones: list) -> list:
    """Merge zones that are very close to each other into one stronger zone."""
    if not zones:
        return zones
    merged = []
    used   = set()
    for i, z1 in enumerate(zones):
        if i in used:
            continue
        group = [z1]
        for j, z2 in enumerate(zones):
            if j <= i or j in used:
                continue
            if z1["type"] == z2["type"]:
                pct_diff = abs(z1["mid"] - z2["mid"]) / z1["mid"]
                if pct_diff <= ZONE_MERGE_PCT:
                    group.append(z2)
                    used.add(j)
        # Merge group into one zone
        merged.append({
            "type":     group[0]["type"],
            "top":      max(z["top"] for z in group),
            "bot":      min(z["bot"] for z in group),
            "mid":      sum(z["mid"] for z in group) / len(group),
            "strength": sum(z["strength"] for z in group),
            "fresh":    any(z["fresh"] for z in group),
            "time":     group[0]["time"],
        })
        used.add(i)
    return merged


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 4 — Support & Resistance Detection
# ─────────────────────────────────────────────────────────────────────────────

def find_support_resistance(df: pd.DataFrame) -> list:
    """
    Support & Resistance levels identified by finding swing highs and lows
    that have been touched multiple times.

    RESISTANCE: a swing high that price has failed to break through multiple times
    SUPPORT:    a swing low that price has bounced from multiple times

    The more times a level has held, the stronger it is.
    """
    levels = []
    if len(df) < SWING_LOOKBACK * 2 + 1:
        return levels

    swing_highs = []
    swing_lows  = []

    # Find all swing highs and lows
    for i in range(SWING_LOOKBACK, len(df) - SWING_LOOKBACK):
        window_h = df.iloc[i - SWING_LOOKBACK: i + SWING_LOOKBACK + 1]["high"]
        window_l = df.iloc[i - SWING_LOOKBACK: i + SWING_LOOKBACK + 1]["low"]

        if df.iloc[i]["high"] == window_h.max():
            swing_highs.append({"price": df.iloc[i]["high"], "idx": i, "time": df.iloc[i]["timestamp"]})

        if df.iloc[i]["low"] == window_l.min():
            swing_lows.append({"price": df.iloc[i]["low"],  "idx": i, "time": df.iloc[i]["timestamp"]})

    # Cluster swing highs into resistance levels
    resistance = cluster_levels(swing_highs, df)
    support    = cluster_levels(swing_lows,  df)

    for r in resistance:
        if r["touches"] >= SR_TOUCHES_MIN:
            levels.append({
                "type":     "RESISTANCE",
                "top":      round(r["price"] * 1.002, 4),   # small buffer above
                "bot":      round(r["price"] * 0.998, 4),   # small buffer below
                "mid":      round(r["price"], 4),
                "strength": r["touches"],
                "fresh":    r["fresh"],
                "time":     r["time"],
            })

    for s in support:
        if s["touches"] >= SR_TOUCHES_MIN:
            levels.append({
                "type":     "SUPPORT",
                "top":      round(s["price"] * 1.002, 4),
                "bot":      round(s["price"] * 0.998, 4),
                "mid":      round(s["price"], 4),
                "strength": s["touches"],
                "fresh":    s["fresh"],
                "time":     s["time"],
            })

    return levels


def cluster_levels(swings: list, df: pd.DataFrame) -> list:
    """Group nearby swing points and count how many times each cluster was touched."""
    if not swings:
        return []

    clusters = []
    used     = set()

    for i, sw in enumerate(swings):
        if i in used:
            continue
        group = [sw]
        for j, sw2 in enumerate(swings):
            if j <= i or j in used:
                continue
            if abs(sw["price"] - sw2["price"]) / sw["price"] <= ZONE_MERGE_PCT * 2:
                group.append(sw2)
                used.add(j)

        avg_price = sum(s["price"] for s in group) / len(group)
        # Count touches from entire dataframe
        touches = sum(
            1 for _, row in df.iterrows()
            if abs(row["high"] - avg_price) / avg_price < 0.002 or
               abs(row["low"]  - avg_price) / avg_price < 0.002
        )
        # Fresh = price hasn't touched it in last 10 bars
        last_idx  = max(s["idx"] for s in group)
        fresh     = last_idx < len(df) - 10

        clusters.append({
            "price":   avg_price,
            "touches": touches,
            "fresh":   fresh,
            "time":    group[0]["time"],
        })
        used.add(i)

    return clusters


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 5 — Zone Proximity Alert
# ─────────────────────────────────────────────────────────────────────────────

def check_proximity(symbol: str, name: str, current_price: float,
                    zones: list) -> list:
    """
    Check if current price is approaching or inside any zone.
    Returns list of triggered zones with action recommendation.
    """
    triggered = []
    for zone in zones:
        zone_mid  = (zone["top"] + zone["bot"]) / 2
        pct_away  = abs(current_price - zone_mid) / current_price

        if pct_away <= ZONE_PROXIMITY_PCT:
            # Determine action based on zone type and price position
            inside = zone["bot"] <= current_price <= zone["top"]

            if zone["type"] == "DEMAND" or zone["type"] == "SUPPORT":
                if inside:
                    action = "🟢 INSIDE ZONE — Watch for LONG entry"
                elif current_price < zone["bot"]:
                    action = "⚠️ Price BELOW zone — Zone broken"
                else:
                    action = "📍 Approaching from above — Potential LONG"

            else:  # SUPPLY or RESISTANCE
                if inside:
                    action = "🔴 INSIDE ZONE — Watch for SHORT entry"
                elif current_price > zone["top"]:
                    action = "⚠️ Price ABOVE zone — Zone broken (bullish)"
                else:
                    action = "📍 Approaching from below — Potential SHORT"

            triggered.append({**zone, "action": action, "pct_away": pct_away})

    return triggered


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 6 — Main Scanner Loop
# ─────────────────────────────────────────────────────────────────────────────

# Track which alerts we've sent to avoid spamming
alerted_zones:   dict = {}   # symbol -> set of zone mids already alerted
daily_zone_counts: dict = {}
last_summary_day = None


def run():
    global last_summary_day

    log.info("=" * 60)
    log.info("KIBO Zone Scanner — STARTED")
    log.info(f"Symbols    : {', '.join(WATCHLIST.keys())}")
    log.info(f"Timeframe  : {ZONE_TIMEFRAME}-minute bars")
    log.info(f"Alert zone : within {ZONE_PROXIMITY_PCT*100:.1f}% of price")
    log.info("=" * 60)
    alert_started()

    scan_interval = 10 * 60  # scan every 10 minutes (zone scanner is less time-sensitive)

    while True:
        now = datetime.now()

        # Daily summary at 4:15 PM ET
        if now.hour == 16 and now.minute >= 15 and last_summary_day != now.date():
            alert_daily_summary(daily_zone_counts)
            daily_zone_counts.clear()
            alerted_zones.clear()
            last_summary_day = now.date()

        log.info(f"--- Zone scan: {', '.join(WATCHLIST.keys())} ---")

        for ticker, info in WATCHLIST.items():
            try:
                df = get_bars(ticker)
                if df.empty or len(df) < 20:
                    log.warning(f"{ticker}: not enough data")
                    continue

                current_price = df.iloc[-1]["close"]
                name          = info["name"]

                # Find all zones
                sd_zones  = find_supply_demand_zones(df)
                sr_levels = find_support_resistance(df)
                all_zones = sd_zones + sr_levels

                log.info(
                    f"{ticker}: price={current_price:.2f} | "
                    f"demand={sum(1 for z in sd_zones if z['type']=='DEMAND')} "
                    f"supply={sum(1 for z in sd_zones if z['type']=='SUPPLY')} "
                    f"support={sum(1 for z in sr_levels if z['type']=='SUPPORT')} "
                    f"resistance={sum(1 for z in sr_levels if z['type']=='RESISTANCE')}"
                )

                # Update daily counts
                daily_zone_counts[ticker] = len(all_zones)

                # Check proximity alerts
                triggered = check_proximity(ticker, name, current_price, all_zones)

                for zone in triggered:
                    zone_key = f"{ticker}-{zone['type']}-{zone['mid']:.2f}"
                    if zone_key not in alerted_zones.get(ticker, set()):
                        log.info(
                            f"{ticker}: {zone['type']} zone alert! "
                            f"Zone={zone['bot']:.2f}-{zone['top']:.2f} "
                            f"Price={current_price:.2f} "
                            f"({zone['pct_away']*100:.2f}% away)"
                        )
                        alert_zone(
                            ticker, name, zone["type"],
                            zone["top"], zone["bot"],
                            current_price, zone["strength"],
                            zone["fresh"], zone["action"]
                        )
                        if ticker not in alerted_zones:
                            alerted_zones[ticker] = set()
                        alerted_zones[ticker].add(zone_key)

            except Exception as e:
                log.error(f"{ticker}: error — {e}")

        log.info(f"Next zone scan in {scan_interval // 60} min...")
        time.sleep(scan_interval)


if __name__ == "__main__":
    run()
