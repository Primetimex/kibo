# KIBO Trading Agent — Complete Setup Guide

A momentum breakout options agent that scans your watchlist every 15 minutes,
fires trades via TradeZero API when all 5 signals align, and pushes instant
Telegram alerts to your phone for every signal, trade open, trade close, and
daily summary.

---

## SECTION 1 — Install Python

### Windows
1. Go to https://python.org/downloads
2. Download Python 3.11 or newer
3. Run the installer
4. ⚠️ On the first screen, CHECK the box that says "Add Python to PATH"
5. Click "Install Now"
6. When done, open Command Prompt and type:
       python --version
   You should see something like: Python 3.11.x

### Mac
1. Go to https://python.org/downloads
2. Download Python 3.11 or newer
3. Run the .pkg installer
4. Open Terminal and type:
       python3 --version
   You should see: Python 3.11.x

---

## SECTION 2 — Download & Set Up the Project

1. Create a folder on your Desktop called "trading-agent"
2. Move these 4 files into that folder:
       trading_agent.py
       .env.example
       requirements.txt
       README.md

3. Open Terminal (Mac) or Command Prompt (Windows) and navigate there:
   - Windows:   cd C:\Users\YourName\Desktop\trading-agent
   - Mac:       cd ~/Desktop/trading-agent

4. Install the required Python packages:
       pip install -r requirements.txt
   (Mac users may need: pip3 install -r requirements.txt)

---

## SECTION 3 — Get Your Polygon.io API Key (Market Data)

1. Go to: https://polygon.io/dashboard/signup
2. Enter your email and create a password — click "Sign Up"
3. Verify your email (check inbox for a confirmation link)
4. Log in and go to your Dashboard
5. Scroll down to the "API Keys" section
6. Copy the key shown (it looks like: abc123XYZetc)
7. Save it — you'll paste it into your .env file shortly

Note: Free tier gives you 15-min delayed data which is fine for paper trading.
      For live trading, the Starter plan ($29/mo) gives real-time data.

---

## SECTION 4 — Set Up Your Telegram Bot (Free Alerts to Your Phone)

### Step 4a — Create the bot
1. Open Telegram on your phone or desktop
2. Search for:  @BotFather  (official Telegram bot, blue checkmark)
3. Tap "Start" or type /start
4. Type:  /newbot
5. BotFather asks for a name — type anything, e.g.:  KIBO Trading Alerts
6. BotFather asks for a username — must end in "bot", e.g.:  KIBOTradesBot
   (If taken, try variations like KIBOTrader2Bot)
7. BotFather replies with your API token — it looks like:
       123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
   Copy this — this is your TELEGRAM_BOT_TOKEN

### Step 4b — Get your Chat ID
1. In Telegram, search for:  @userinfobot
2. Tap "Start" or type /start
3. It instantly replies with your user info including your ID number
   e.g.:  Id: 987654321
   Copy that number — this is your TELEGRAM_CHAT_ID

### Step 4c — Activate your bot
1. Search for your bot by its username (e.g. @KIBOTradesBot)
2. Tap "Start" — this is required before the bot can message you
   (Telegram bots cannot send the first message — you must initiate)

---

## SECTION 5 — Apply for TradeZero Developer API

1. Make sure you have an active TradeZero brokerage account
2. Go to: https://developer.tradezero.com
3. Log in with your TradeZero credentials
4. Apply for API access (may require approval — usually 1-3 business days)
5. Once approved, copy your API key from the developer dashboard
6. Verify the options chain endpoint path in the docs — confirm it matches
   what is in trading_agent.py (section 6) before going live

---

## SECTION 6 — Configure Your .env File

1. In your trading-agent folder, make a copy of .env.example:
   - Windows:  copy .env.example .env
   - Mac:      cp .env.example .env

2. Open the new .env file with any text editor (Notepad, TextEdit, VS Code)

3. Fill in each value:
       POLYGON_API_KEY=abc123yourpolygonkey
       TZ_API_KEY=yourtradezeroapikey
       ACCOUNT_SIZE=10000          ← your real account balance
       PAPER_MODE=true             ← keep true until ready for live
       TELEGRAM_BOT_TOKEN=123456789:ABCdef...
       TELEGRAM_CHAT_ID=987654321

4. Save the file

---

## SECTION 7 — Run in Paper Mode (Required First Step)

With PAPER_MODE=true in your .env:

    python trading_agent.py

What you'll see:
- Console logs every 15 minutes showing which tickers were scanned
- "no signal" for most tickers most of the time (that's normal and good)
- When a signal fires: a detailed log entry AND a Telegram message on your phone
- Simulated trade entries and exits logged with [PAPER] prefix
- Daily summary Telegram message at 3:45 PM ET

Run this for at least 2 weeks before considering live trading.
Watch the log file (trading_agent.log) to review every decision the agent made.

To stop the agent: press Ctrl+C in the terminal

---

## SECTION 8 — Go Live (Only After Validating Paper Results)

1. Review trading_agent.log — are signals making sense?
2. Are the option contracts it finds appropriate for your tickers?
3. Is the position sizing reasonable for your account?
4. Only when satisfied: open .env and change:
       PAPER_MODE=false
5. Run again:
       python trading_agent.py
6. You'll receive a Telegram message: "KIBO Trading Agent STARTED 🔴 LIVE TRADING"
7. Monitor closely for the first week

---

## Telegram Alerts You'll Receive

| Alert | When |
|-------|------|
| 🤖 Agent Started | Every time you launch the script |
| 🟢/🔴 Signal Detected | When all 5 indicators align |
| 📈/📉 Trade Opened | When an order is placed |
| ✅ Take Profit | When +60% gain exit fires |
| 🛑 Stop Loss | When -50% loss exit fires |
| ⚠️ Error/Warning | If anything goes wrong |
| 📊 Daily Summary | Every day at 3:45 PM ET |

---

## Signal Logic

All 5 must be true simultaneously for a trade to fire:

| Indicator | Bullish (calls) | Bearish (puts) |
|-----------|----------------|----------------|
| RSI | 50 to 70 | 30 to 50 |
| EMA Cross | EMA9 above EMA21 | EMA9 below EMA21 |
| MACD | Fresh bullish crossover | Fresh bearish crossover |
| Bollinger | Close above upper band | Close below lower band |
| Volume | 1.5x the 20-bar average | 1.5x the 20-bar average |

---

## Customizing Your Watchlist

Open trading_agent.py and edit line near the top:

    WATCHLIST = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "AMD"]

Add or remove any ticker symbols you want the agent to watch.

---

## Risk Warning

Options trading involves substantial risk of loss and is not appropriate for
all investors. Past performance does not guarantee future results. This script
is for personal and educational use only. Always paper trade first and never
risk money you cannot afford to lose. You are solely responsible for all
trading decisions and outcomes.
