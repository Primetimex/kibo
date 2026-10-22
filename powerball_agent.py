#!/usr/bin/env python3
"""
Powerball Pick Generator + Telegram Notifier
=============================================

IMPORTANT REALITY CHECK:
Powerball is a certified random drawing. Every one of the 292,201,338 possible
combinations has EXACTLY the same odds every single time, regardless of what
has been drawn before. "Hot" and "cold" numbers are just historical noise --
over enough draws, every number converges to the same frequency (law of large
numbers). This script is for FUN, not prediction. Play responsibly, and never
spend more than you can afford to lose. If gambling ever feels out of control,
call the National Problem Gambling Helpline: 1-800-522-4700.

WHAT THIS SCRIPT DOES:
Generates a set of Powerball picks using 4 different "flavors" (hot-weighted,
cold-weighted, educated-guess blend, and pure random), then sends them to you
as a formatted Telegram message.

SETUP:
1. pip install requests
2. Fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID below (already done for you).
3. Run:  python3 powerball_agent.py
4. (Optional) Schedule it to run automatically before each draw
   (Powerball draws: Mon / Wed / Sat ~10:59 PM ET) using cron or Task Scheduler.
   Example cron (runs 6pm ET on draw days -- adjust for your timezone):
       0 18 * * 1,3,6 /usr/bin/python3 /path/to/powerball_agent.py
"""

import random
import requests
from datetime import datetime

# ── Telegram Alerts ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "8800696969:AAGTrZhZxlg65YQFdLPwrStcL0yXXatupao"
TELEGRAM_CHAT_ID = "7088539979"

# ── Powerball rules (current format since Oct 7, 2015) ──────────────────────
WHITE_BALL_RANGE = (1, 69)   # 5 numbers drawn from this range
POWERBALL_RANGE = (1, 26)    # 1 number drawn from this range

# ── Historical frequency data (current format, via public draw-history stats)─
# These are numbers that have appeared ABOVE average historically.
HOT_WHITE_BALLS = [21, 61, 64, 28, 23, 33, 27, 32, 69, 63, 36, 62, 53, 47, 39]
HOT_POWERBALLS = [18, 24, 4, 21, 2]  # 18 is the clear historical leader

# These have appeared BELOW average historically.
COLD_WHITE_BALLS = [13, 49, 29, 34, 46, 58, 1, 9, 52, 65]
COLD_POWERBALLS = [15, 25, 1, 13, 20]

ALL_WHITE_BALLS = list(range(WHITE_BALL_RANGE[0], WHITE_BALL_RANGE[1] + 1))
ALL_POWERBALLS = list(range(POWERBALL_RANGE[0], POWERBALL_RANGE[1] + 1))


def pick_white_balls(pool, count=5, fallback_pool=None):
    """Pick `count` unique numbers from pool, topping up from fallback if pool is short."""
    pool = list(set(pool))
    if len(pool) >= count:
        return sorted(random.sample(pool, count))
    picks = set(pool)
    fallback_pool = fallback_pool or ALL_WHITE_BALLS
    while len(picks) < count:
        picks.add(random.choice(fallback_pool))
    return sorted(picks)


def generate_hot_pick():
    """Weighted toward historically hot numbers."""
    whites = pick_white_balls(HOT_WHITE_BALLS, 5, ALL_WHITE_BALLS)
    powerball = random.choice(HOT_POWERBALLS)
    return whites, powerball, "🔥 Hot-weighted"


def generate_cold_pick():
    """Weighted toward historically cold / overdue numbers."""
    whites = pick_white_balls(COLD_WHITE_BALLS, 5, ALL_WHITE_BALLS)
    powerball = random.choice(COLD_POWERBALLS)
    return whites, powerball, "🧊 Cold-weighted"


def generate_educated_guess():
    """Blend: 3 hot + 2 cold white balls, powerball from the opposite temperature mix."""
    hot_part = random.sample(HOT_WHITE_BALLS, min(3, len(HOT_WHITE_BALLS)))
    cold_part = random.sample(COLD_WHITE_BALLS, min(2, len(COLD_WHITE_BALLS)))
    combined = list(set(hot_part + cold_part))
    while len(combined) < 5:
        combined.append(random.choice(ALL_WHITE_BALLS))
    whites = sorted(set(combined))[:5]
    while len(whites) < 5:
        extra = random.choice(ALL_WHITE_BALLS)
        if extra not in whites:
            whites.append(extra)
    whites = sorted(whites)
    powerball = random.choice(HOT_POWERBALLS + COLD_POWERBALLS)
    return whites, powerball, "🎯 Educated guess"


def generate_random_pick():
    """Pure uniform random -- statistically just as valid as any other method."""
    whites = sorted(random.sample(ALL_WHITE_BALLS, 5))
    powerball = random.choice(ALL_POWERBALLS)
    return whites, powerball, "🎲 Pure random"


def generate_all_picks():
    generators = [generate_hot_pick, generate_cold_pick, generate_educated_guess, generate_random_pick]
    return [gen() for gen in generators]


def format_message(picks):
    lines = [
        "🎱 *Powerball Picks*",
        f"_{datetime.now().strftime('%A, %B %d, %Y — %I:%M %p')}_",
        "",
    ]
    for whites, powerball, label in picks:
        white_str = " - ".join(f"{n:02d}" for n in whites)
        lines.append(f"*{label}*")
        lines.append(f"`{white_str}`  PB: `{powerball:02d}`")
        lines.append("")
    lines.append("⚠️ _For entertainment only. Every combination has equal odds — past frequency doesn't predict future draws._")
    return "\n".join(lines)


def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    response = requests.post(url, data=payload, timeout=10)
    if response.status_code != 200:
        raise RuntimeError(f"Telegram API error {response.status_code}: {response.text}")
    return response.json()


def main():
    picks = generate_all_picks()
    message = format_message(picks)
    print(message.replace("*", "").replace("_", "").replace("`", ""))
    print("\nSending to Telegram...")
    try:
        send_telegram_message(message)
        print("✅ Sent successfully!")
    except Exception as e:
        print(f"❌ Failed to send: {e}")
        print("Check your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")


if __name__ == "__main__":
    main()
