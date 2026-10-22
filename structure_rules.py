"""
KIBO Structure Rules — ICC Trading Strategy (All 14 Videos)
Based on "Trades By Sci" — Best Simple Price Action Trading Course

Core Framework: ICC = Indication → Correction → Continuation

Rules extracted from all 14 transcripts:
  1. Trend identification (HH/HL = up, LH/LL = down, equal = consolidation)
  2. Indication — price breaks a swing high or low (DO NOT TRADE the breakout)
  3. Correction — liquidity grab after indication (wait for it to finish)
  4. Continuation — entry after correction ends, trend resumes
  5. Timeframe alignment — 4H > 1H > 15min must all agree
  6. No trade zone — high and low with no indication
  7. Volume sessions — only trade NY session (9:30 AM ET) or London
  8. Stop loss placement — above lower high (sells) or below higher low (buys)
  9. Target — the swing high/low that caused the indication
  10. Momentum — higher TF is highly respected, lower TF loses respect
"""

import pandas as pd
import numpy as np
import re


# ── Settings ──────────────────────────────────────────────────────────────────
SWING_LOOKBACK    = 3     # bars each side — reduced from 5 for faster detection
MIN_SWINGS        = 2     # minimum swings to determine trend
HIGHER_LOW_THRESH = 0.003 # 0.3% tolerance — slightly wider for faster signals
SESSION_OPEN_ET   = (9, 30)   # NY session open
SESSION_CLOSE_ET  = (16, 0)   # NY session close
LONDON_OPEN_ET    = (3, 0)    # London open in ET
LONDON_CLOSE_ET   = (8, 30)   # London close in ET


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 1 — Swing High/Low Detection
# ─────────────────────────────────────────────────────────────────────────────

def find_swings(df: pd.DataFrame) -> tuple:
    """
    Find all confirmed swing highs and lows.

    A swing HIGH: bar whose high is highest in N bars on each side.
    A swing LOW:  bar whose low  is lowest  in N bars on each side.

    These are the building blocks of ALL structure analysis.
    Per video 1: "A high is the highest point price can get before reversing."
    Per video 5: "Anything above a swing low = potential bullish. Anything below a swing high = potential bearish."
    """
    n      = SWING_LOOKBACK
    highs  = []
    lows   = []

    for i in range(n, len(df) - n):
        h_window = df["high"].iloc[i - n: i + n + 1]
        l_window = df["low"].iloc[i  - n: i + n + 1]
        if df["high"].iloc[i] == h_window.max():
            highs.append({"idx": i, "price": float(df["high"].iloc[i]),
                          "ts": df["timestamp"].iloc[i]})
        if df["low"].iloc[i] == l_window.min():
            lows.append({"idx": i, "price": float(df["low"].iloc[i]),
                         "ts": df["timestamp"].iloc[i]})
    return highs, lows


def are_equal(p1: float, p2: float) -> bool:
    """Two prices are 'equal' if within 0.2% — called equal highs/lows."""
    return abs(p1 - p2) / max(p1, p2) <= HIGHER_LOW_THRESH


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 2 — Trend Classification
# ─────────────────────────────────────────────────────────────────────────────

def classify_trend(df: pd.DataFrame) -> dict:
    """
    Classify market structure per ICC rules across all 14 videos.

    UPTREND   : Higher Highs + Higher Lows  → ONLY take LONG trades
    DOWNTREND : Lower Highs  + Lower Lows   → ONLY take SHORT trades
    CONSOLIDATION: Equal or mixed highs/lows → NO TRADE ZONE
    UNDEFINED : Not enough data

    Key rules from transcripts:
    - Video 1:  "Uptrend = HH + HL. Downtrend = LH + LL."
    - Video 1:  "Price will not break structure unless reversing."
    - Video 10: "A no-trade zone is when price has a high and low with no indication."
    - Video 14: "Higher time frame highly respected — structure mandatory there."
    - Video 9:  "Moment price breaks a lower high = trend potentially changing."
    - Video 11: "Higher lows = sign price is looking to go bullish."
    """
    if len(df) < SWING_LOOKBACK * 2 + MIN_SWINGS:
        return _undefined("Not enough bars")

    highs, lows = find_swings(df)
    if len(highs) < 2 or len(lows) < 2:
        return _undefined("Not enough swing points")

    r_highs = highs[-4:]
    r_lows  = lows[-4:]

    # Score each consecutive pair of highs
    hh = lh = eh = 0
    for i in range(1, len(r_highs)):
        if are_equal(r_highs[i]["price"], r_highs[i-1]["price"]):
            eh += 1
        elif r_highs[i]["price"] > r_highs[i-1]["price"]:
            hh += 1
        else:
            lh += 1

    # Score each consecutive pair of lows
    hl = ll = el = 0
    for i in range(1, len(r_lows)):
        if are_equal(r_lows[i]["price"], r_lows[i-1]["price"]):
            el += 1
        elif r_lows[i]["price"] > r_lows[i-1]["price"]:
            hl += 1
        else:
            ll += 1

    total_h = hh + lh + eh
    total_l = hl + ll + el
    hh_pct  = hh / total_h if total_h else 0
    hl_pct  = hl / total_l if total_l else 0
    lh_pct  = lh / total_h if total_h else 0
    ll_pct  = ll / total_l if total_l else 0

    last_high = r_highs[-1]["price"]
    last_low  = r_lows[-1]["price"]
    prev_high = r_highs[-2]["price"] if len(r_highs) > 1 else last_high
    prev_low  = r_lows[-2]["price"]  if len(r_lows)  > 1 else last_low

    # ── UPTREND ───────────────────────────────────────────────────────────────
    if hh_pct >= 0.6 and hl_pct >= 0.6:
        return {
            "trend":      "UPTREND",
            "direction":  "LONG",
            "confidence": round((hh_pct + hl_pct) / 2, 2),
            "last_hh":    last_high,
            "last_hl":    last_low,
            "last_lh":    None,
            "last_ll":    None,
            "reason":     f"HH:{hh} HL:{hl} | Last high={last_high:.2f}>{prev_high:.2f} | Last low={last_low:.2f}>{prev_low:.2f}",
            "swing_highs": r_highs,
            "swing_lows":  r_lows,
        }

    # ── DOWNTREND ─────────────────────────────────────────────────────────────
    if lh_pct >= 0.6 and ll_pct >= 0.6:
        return {
            "trend":      "DOWNTREND",
            "direction":  "SHORT",
            "confidence": round((lh_pct + ll_pct) / 2, 2),
            "last_hh":    None,
            "last_hl":    None,
            "last_lh":    last_high,
            "last_ll":    last_low,
            "reason":     f"LH:{lh} LL:{ll} | Last high={last_high:.2f}<{prev_high:.2f} | Last low={last_low:.2f}<{prev_low:.2f}",
            "swing_highs": r_highs,
            "swing_lows":  r_lows,
        }

    # ── CONSOLIDATION — DO NOT TRADE ──────────────────────────────────────────
    # Per video 1: "Don't trade consolidation — it breaks the rules."
    # Per video 10: "No trade zone = high and low with no indication."
    return {
        "trend":      "CONSOLIDATION",
        "direction":  "NONE",
        "confidence": 0.0,
        "last_hh":    None,
        "last_hl":    None,
        "last_lh":    None,
        "last_ll":    None,
        "reason":     f"Mixed — HH:{hh} LH:{lh} HL:{hl} LL:{ll} EH:{eh} EL:{el} — no clear structure",
        "swing_highs": r_highs,
        "swing_lows":  r_lows,
    }


def _undefined(reason: str) -> dict:
    return {"trend": "UNDEFINED", "direction": "NONE", "confidence": 0.0,
            "reason": reason, "last_hh": None, "last_hl": None,
            "last_lh": None, "last_ll": None, "swing_highs": [], "swing_lows": []}


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 3 — Indication Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_indication(df: pd.DataFrame) -> dict:
    """
    An INDICATION is when price breaks a swing high or swing low.

    Rules from videos 2, 3, 4, 10:
    - "Indications can ONLY happen when price breaks a swing high or low."
    - "DO NOT TRADE the indication — it is a breakout, not your entry."
    - "Indication tells you WHICH DIRECTION price is going."
    - "Indication always creates a new high or new low."
    - The indication gives you: entry level, TP target, direction bias.

    After indication: wait for CORRECTION before entering.
    """
    if len(df) < 3:
        return {"indication": False, "direction": "NONE"}

    highs, lows = find_swings(df)
    if not highs or not lows:
        return {"indication": False, "direction": "NONE"}

    current_price = float(df["close"].iloc[-1])
    last_swing_high = highs[-1]["price"] if highs else None
    last_swing_low  = lows[-1]["price"]  if lows  else None

    bullish_indication = (
        last_swing_high is not None and
        current_price > last_swing_high and
        float(df["close"].iloc[-2]) <= last_swing_high
    )

    bearish_indication = (
        last_swing_low is not None and
        current_price < last_swing_low and
        float(df["close"].iloc[-2]) >= last_swing_low
    )

    if bullish_indication:
        return {
            "indication":   True,
            "direction":    "LONG",
            "broken_level": last_swing_high,
            "target":       last_swing_low,   # TP = previous swing low area
            "reason":       f"Price {current_price:.2f} broke above swing high {last_swing_high:.2f}",
        }

    if bearish_indication:
        return {
            "indication":   True,
            "direction":    "SHORT",
            "broken_level": last_swing_low,
            "target":       last_swing_high,  # TP = previous swing high area
            "reason":       f"Price {current_price:.2f} broke below swing low {last_swing_low:.2f}",
        }

    return {"indication": False, "direction": "NONE"}


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 4 — Correction Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_correction_over(df: pd.DataFrame, indication_direction: str) -> dict:
    """
    Detect when the CORRECTION is over so we can enter on CONTINUATION.

    Rules from videos 3, 4, 6:
    - "Correction happens AFTER the indication — it grabs liquidity."
    - "Monitor the correction on the 15-minute time frame."
    - "The correction is over when price shows signs of reversing back
       in the direction of the indication."
    - "Look for lower highs failing (for bull) or higher lows failing (for bear)
       on the 15-minute time frame."
    - "Correction cannot break the level that caused the indication."
    - "Once correction ends: sell back under (or buy back above) the indication level."

    For BULLISH correction over:
      - Price was pulling back (lower highs on 15min)
      - Now price breaks a lower high on 15min = correction ending
      - Price back above the indication level

    For BEARISH correction over:
      - Price was bouncing up (higher lows on 15min)
      - Now price breaks a higher low = correction ending
      - Price back below the indication level
    """
    if len(df) < SWING_LOOKBACK * 2 + 2:
        return {"correction_over": False, "reason": "Not enough bars"}

    highs, lows = find_swings(df)
    if len(highs) < 2 or len(lows) < 2:
        return {"correction_over": False, "reason": "Not enough swings"}

    current = float(df["close"].iloc[-1])
    prev    = float(df["close"].iloc[-2])

    if indication_direction == "LONG":
        # Correction was pulling price down — look for lower highs stopping
        # and price starting to push back up
        r_highs = highs[-3:]
        lower_highs = all(
            r_highs[i]["price"] < r_highs[i-1]["price"]
            for i in range(1, len(r_highs))
        )
        bouncing_up = current > prev

        if not lower_highs and bouncing_up:
            return {
                "correction_over": True,
                "reason": f"Lower highs stopped, price bouncing at {current:.2f}",
                "entry_zone": lows[-1]["price"],
            }

    elif indication_direction == "SHORT":
        # Correction was bouncing price up — look for higher lows stopping
        # and price starting to push back down
        r_lows = lows[-3:]
        higher_lows = all(
            r_lows[i]["price"] > r_lows[i-1]["price"]
            for i in range(1, len(r_lows))
        )
        pushing_down = current < prev

        if not higher_lows and pushing_down:
            return {
                "correction_over": True,
                "reason": f"Higher lows stopped, price pushing down at {current:.2f}",
                "entry_zone": highs[-1]["price"],
            }

    return {"correction_over": False, "reason": "Correction still in progress"}


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 5 — Timeframe Alignment Check
# ─────────────────────────────────────────────────────────────────────────────

def check_timeframe_alignment(df: pd.DataFrame, signal_direction: str) -> dict:
    """
    Check that structure on the current timeframe aligns with the signal direction.

    Rules from videos 6, 7, 8, 11, 12, 14:
    - "4H > 1H > 15min must all align — everything has to correlate."
    - "If 1H is bullish but 15min is bearish, you CANNOT take that trade."
    - "The higher the time frame, the more highly respected the structure."
    - "4 hour long-term. 1 hour day trades. 15 minute for entries only."
    - "Mark up on 1H/4H. Scale down to 15min/5min for entry only."
    - "Lower TF must be going IN THE SAME DIRECTION as higher TF."

    This function verifies the current bars show structure aligned with signal.
    The agent calling this should pass its primary timeframe data (5-min bars).
    The classification is treated as the lower timeframe check.
    """
    structure = classify_trend(df)
    trend     = structure["trend"]
    direction = structure["direction"]

    # Consolidation = hard block regardless of signal
    if trend == "CONSOLIDATION":
        return {
            "aligned":  False,
            "reason":   "CONSOLIDATION detected — no trade zone. Price has no clear structure.",
            "structure": structure,
        }

    if trend == "UNDEFINED":
        return {
            "aligned":  False,
            "reason":   f"Structure undefined — {structure['reason']}",
            "structure": structure,
        }

    # Direction must match signal
    if signal_direction == "LONG" and direction != "LONG":
        return {
            "aligned":  False,
            "reason":   f"Signal is LONG but structure is {trend}. Only trade WITH the trend — video 1 rule.",
            "structure": structure,
        }

    if signal_direction == "SHORT" and direction != "SHORT":
        return {
            "aligned":  False,
            "reason":   f"Signal is SHORT but structure is {trend}. Only trade WITH the trend — video 1 rule.",
            "structure": structure,
        }

    return {
        "aligned":   True,
        "reason":    f"{trend} confirmed. Direction={direction}. Confidence={structure['confidence']*100:.0f}%",
        "structure": structure,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 6 — Session / Volume Check
# ─────────────────────────────────────────────────────────────────────────────

def is_in_session(dt=None) -> dict:
    """
    Check if current time is in a tradeable session.

    Rules from videos 6, 8, 9, 10, 12:
    - "Trade during New York session — 9:30 AM ET when the bell opens."
    - "London session also good — roughly 3 AM to 8:30 AM ET."
    - "Fake volume out of session — stay away."
    - "Volume is what drives price. No volume = no trade."
    - "Trade gold and NASDAQ during New York session."
    - "You really just want to be in the markets at 9:30, maybe 8:00."
    """
    from datetime import datetime as dt_cls
    if dt is None:
        dt = dt_cls.now()

    h, m = dt.hour, dt.minute
    time_val = h * 60 + m

    ny_open    = SESSION_OPEN_ET[0]  * 60 + SESSION_OPEN_ET[1]
    ny_close   = SESSION_CLOSE_ET[0] * 60 + SESSION_CLOSE_ET[1]
    lon_open   = LONDON_OPEN_ET[0]   * 60 + LONDON_OPEN_ET[1]
    lon_close  = LONDON_CLOSE_ET[0]  * 60 + LONDON_CLOSE_ET[1]

    in_ny      = ny_open  <= time_val <= ny_close
    in_london  = lon_open <= time_val <= lon_close

    if in_ny:
        return {"in_session": True, "session": "NEW_YORK",
                "reason": "New York session — highest volume, best for trading"}
    if in_london:
        return {"in_session": True, "session": "LONDON",
                "reason": "London session — good volume for gold and forex"}

    return {"in_session": False, "session": "OUT_OF_SESSION",
            "reason": "Out of session — fake volume, avoid trading"}


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 7 — Stop Loss Placement
# ─────────────────────────────────────────────────────────────────────────────

def calculate_stop_loss(df: pd.DataFrame, direction: str,
                        entry: float, structure: dict) -> float:
    """
    Calculate stop loss based on ICC structure rules.

    Rules from videos 4, 7, 13:
    - "Stop loss for SELLS goes ABOVE the lower high."
    - "Stop loss for BUYS goes BELOW the higher low."
    - "Stop loss = the point where IF HIT, the trade is invalidated."
    - "Do not put stop loss where hitting it doesn't break structure."
    - "If price hits your stop and didn't break structure, you placed it wrong."
    - "You need room — especially on NASDAQ. Use the swing level."
    """
    buffer = abs(entry) * 0.002   # 0.2% buffer beyond structure

    if direction == "LONG":
        # Stop below the last higher low
        last_hl = structure.get("last_hl")
        if last_hl:
            return round(last_hl - buffer, 2)
        # Fallback: below recent swing low
        lows = structure.get("swing_lows", [])
        if lows:
            return round(lows[-1]["price"] - buffer, 2)
        return round(entry * 0.985, 2)   # 1.5% fallback

    elif direction == "SHORT":
        # Stop above the last lower high
        last_lh = structure.get("last_lh")
        if last_lh:
            return round(last_lh + buffer, 2)
        # Fallback: above recent swing high
        highs = structure.get("swing_highs", [])
        if highs:
            return round(highs[-1]["price"] + buffer, 2)
        return round(entry * 1.015, 2)   # 1.5% fallback

    return entry


def calculate_target(structure: dict, direction: str, entry: float) -> float:
    """
    Calculate take profit target based on ICC rules.

    Rules from videos 2, 3, 4, 9, 12:
    - "Your TP is where the indication started — the swing that was broken."
    - "When buying, target the swing HIGHS — that's where sellers are."
    - "When selling, target the swing LOWS — that's where buyers are."
    - "Take your buys to the sellers. Take your sells to the buyers."
    - "Target the last area where buyers (or sellers) kicked in."
    - "Use soft TPs — don't put limit orders right at the level."
    """
    buffer = abs(entry) * 0.001   # 0.1% buffer before the level

    if direction == "LONG":
        last_hh = structure.get("last_hh")
        if last_hh and last_hh > entry:
            return round(last_hh - buffer, 2)
        highs = structure.get("swing_highs", [])
        candidates = [h["price"] for h in highs if h["price"] > entry]
        if candidates:
            return round(min(candidates) - buffer, 2)
        return round(entry * 1.015, 2)

    elif direction == "SHORT":
        last_ll = structure.get("last_ll")
        if last_ll and last_ll < entry:
            return round(last_ll + buffer, 2)
        lows = structure.get("swing_lows", [])
        candidates = [l["price"] for l in lows if l["price"] < entry]
        if candidates:
            return round(max(candidates) + buffer, 2)
        return round(entry * 0.985, 2)

    return entry


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 8 — Master Entry Check (Full ICC Validation)
# ─────────────────────────────────────────────────────────────────────────────

def check_entry_rules(df: pd.DataFrame, signal_direction: str) -> dict:
    """
    Master ICC entry validation — applies ALL rules from all 14 videos.

    A trade is only APPROVED if ALL of these pass:

    1. TREND ALIGNED  — Structure confirms signal direction (HH/HL or LH/LL)
    2. NOT CONSOLIDATION — No no-trade zone (equal highs/lows, sideways)
    3. NO COUNTER-TREND — Never buy in downtrend, never sell in uptrend
    4. STRUCTURE INTACT — Price hasn't broken the invalidation level
    5. MOMENTUM CONFIRMS — Current bar moving in signal direction
    6. CORRECTION CHECK — Price is past the correction phase
    7. SESSION CHECK — In NY or London session (volume present)

    Returns: approved, reason, stop_loss, target, rr, structure
    """
    if len(df) < 3:
        return _blocked("Not enough data", None, None)

    # ── Step 1: Timeframe alignment ───────────────────────────────────────────
    alignment = check_timeframe_alignment(df, signal_direction)
    if not alignment["aligned"]:
        return _blocked(alignment["reason"], None, alignment["structure"])

    structure     = alignment["structure"]
    current_price = float(df["close"].iloc[-1])
    prev_price    = float(df["close"].iloc[-2])

    # ── Step 2: Structure intact check ───────────────────────────────────────
    # Per video 4, 9: "If price breaks the higher low, structure is invalidated."
    if signal_direction == "LONG":
        last_hl = structure.get("last_hl")
        if last_hl and current_price < last_hl:
            return _blocked(
                f"Structure invalidated — price {current_price:.2f} broke below higher low {last_hl:.2f}. Trend may be reversing.",
                None, structure
            )

    if signal_direction == "SHORT":
        last_lh = structure.get("last_lh")
        if last_lh and current_price > last_lh:
            return _blocked(
                f"Structure invalidated — price {current_price:.2f} broke above lower high {last_lh:.2f}. Trend may be reversing.",
                None, structure
            )

    # ── Step 3: Momentum confirmation ─────────────────────────────────────────
    # Per video 4, 9: "Current price must be moving in the direction of the signal."
    if signal_direction == "LONG" and current_price < prev_price:
        return _blocked(
            f"Momentum not confirmed — price still pulling back ({current_price:.2f} < {prev_price:.2f}). Wait for bounce.",
            None, structure
        )

    if signal_direction == "SHORT" and current_price > prev_price:
        return _blocked(
            f"Momentum not confirmed — price still bouncing ({current_price:.2f} > {prev_price:.2f}). Wait for rejection.",
            None, structure
        )

    # ── Step 4: Session check ─────────────────────────────────────────────────
    # Per video 6, 8, 9: "Only trade in session with volume."
    session = is_in_session()
    session_note = ""
    if not session["in_session"]:
        session_note = " [WARNING: Out of session — lower probability]"

    # ── Step 5: Calculate stop loss and target ────────────────────────────────
    stop   = calculate_stop_loss(df, signal_direction, current_price, structure)
    target = calculate_target(structure, signal_direction, current_price)

    # ── Step 6: Risk:Reward check ─────────────────────────────────────────────
    # Per video 13: "Look for 1:3 to 1:4 risk-to-reward."
    risk    = abs(current_price - stop)
    reward  = abs(target - current_price)
    rr      = round(reward / risk, 2) if risk > 0 else 0

    if rr < 1.5:
        return _blocked(
            f"Risk:Reward too low ({rr}:1). Need at least 1.5:1. Entry={current_price:.2f} Stop={stop:.2f} Target={target:.2f}",
            structure, structure
        )

    # ── APPROVED ──────────────────────────────────────────────────────────────
    trend  = structure["trend"]
    conf   = structure["confidence"]
    reason = (
        f"{trend} confirmed ({conf*100:.0f}% confidence). "
        f"Structure intact. Momentum aligned. R:R={rr}:1. "
        f"Stop={stop:.2f} Target={target:.2f}{session_note}"
    )

    return {
        "approved":  True,
        "reason":    reason,
        "structure": structure,
        "stop_loss": stop,
        "target":    target,
        "rr":        rr,
        "session":   session["session"],
    }


def _blocked(reason: str, structure, full_structure) -> dict:
    return {
        "approved":  False,
        "reason":    reason,
        "structure": full_structure or {},
        "stop_loss": None,
        "target":    None,
        "rr":        0,
        "session":   "UNKNOWN",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 9 — Trend Reversal Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_reversal(df: pd.DataFrame, current_trend: str) -> dict:
    """
    Detect potential trend reversal based on ICC rules.

    Rules from videos 1, 4, 9, 11, 12:
    - "The moment price breaks a lower high = trend potentially changing."
    - "Higher low forms when expecting a lower low = bullish sign."
    - "Price will not break structure unless reversing."
    - "Equal highs + lower lows = consolidation forming, wait."
    - "For reversal confirmation: need HH on downtrend or LL on uptrend."
    - "Watch for higher lows building in a downtrend = reversal signal."
    """
    if len(df) < SWING_LOOKBACK * 2 + 3:
        return {"reversal_likely": False, "reason": "Not enough data"}

    highs, lows = find_swings(df)
    if len(highs) < 2 or len(lows) < 2:
        return {"reversal_likely": False, "reason": "Not enough swings"}

    r_highs = highs[-3:]
    r_lows  = lows[-3:]

    if current_trend == "DOWNTREND":
        # Reversal signal: lower low followed by a HIGHER low (not making new lows)
        # Per video 9: "Higher low in a downtrend = first sign of reversal"
        if (r_lows[-1]["price"] > r_lows[-2]["price"] and
            r_highs[-1]["price"] > r_highs[-2]["price"]):
            return {
                "reversal_likely": True,
                "new_direction":   "LONG",
                "reason":          f"Higher low {r_lows[-1]['price']:.2f} > {r_lows[-2]['price']:.2f} forming in downtrend. Watch for higher high to confirm.",
            }

    elif current_trend == "UPTREND":
        # Reversal signal: higher high followed by a LOWER high
        # Per video 4: "Lower high forms = sign of trend change"
        if (r_highs[-1]["price"] < r_highs[-2]["price"] and
            r_lows[-1]["price"]  < r_lows[-2]["price"]):
            return {
                "reversal_likely": True,
                "new_direction":   "SHORT",
                "reason":          f"Lower high {r_highs[-1]['price']:.2f} < {r_highs[-2]['price']:.2f} forming in uptrend. Watch for lower low to confirm.",
            }

    return {"reversal_likely": False, "reason": "No reversal signals detected"}


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 10 — Telegram Alert Formatter
# ─────────────────────────────────────────────────────────────────────────────

def format_structure_alert(symbol: str, result: dict) -> str:
    """Format a clear Telegram alert showing structure analysis."""
    s      = result.get("structure", {})
    trend  = s.get("trend", "UNKNOWN")
    conf   = s.get("confidence", 0)
    emojis = {"UPTREND": "📈", "DOWNTREND": "📉",
               "CONSOLIDATION": "↔️", "UNDEFINED": "❓"}
    emoji  = emojis.get(trend, "❓")
    lines  = [
        f"{emoji} <b>ICC STRUCTURE — {symbol}</b>",
        f"Trend      : <b>{trend}</b> ({conf*100:.0f}% confidence)",
        f"Direction  : {s.get('direction', '—')}",
    ]
    if s.get("last_hh"): lines.append(f"Last HH    : {s['last_hh']:.2f}")
    if s.get("last_hl"): lines.append(f"Last HL    : {s['last_hl']:.2f}")
    if s.get("last_lh"): lines.append(f"Last LH    : {s['last_lh']:.2f}")
    if s.get("last_ll"): lines.append(f"Last LL    : {s['last_ll']:.2f}")
    lines.append(f"Decision   : {'✅ APPROVED' if result.get('approved') else '🚫 BLOCKED'}")
    lines.append(f"Reason     : {result.get('reason', '—')[:100]}")
    if result.get("stop_loss"): lines.append(f"Stop Loss  : {result['stop_loss']:.2f}")
    if result.get("target"):    lines.append(f"Target     : {result['target']:.2f}")
    if result.get("rr"):        lines.append(f"R:R        : {result['rr']:.1f}:1")
    if result.get("session"):   lines.append(f"Session    : {result['session']}")
    return "\n".join(lines)
