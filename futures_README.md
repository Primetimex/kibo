# KIBO Futures Agent — Setup Guide

Trades Micro Gold (MGC) and Micro Crude Oil (MCL) futures on 15-minute bars.
Auto-pauses around high-impact reports (EIA, FOMC, CPI).
Sends Telegram alerts for every signal, trade, and daily summary.

---

## Prerequisites

Before running this agent you need:
1. Python installed (see options agent README Section 1)
2. Polygon.io API key (see options agent README Section 3)
3. Telegram bot configured (see options agent README Section 4)
4. TradeZero account with FUTURES TRADING ENABLED
5. The same .env file used by the options agent — just add futures to it

---

## IMPORTANT — Enable Futures on Your TradeZero Account

Futures trading requires separate approval from options/equities.
1. Log in to your TradeZero account
2. Go to Account Settings → Trading Permissions
3. Apply for Futures Trading access
4. TradeZero may require a margin agreement and suitability questionnaire
5. Call or chat TradeZero support if you need help: support.tradezero.com

---

## Start With Micro Contracts — Non-Negotiable for Beginners

This agent trades MGC and MCL by default — these are 1/10th the size
of full GC and CL contracts.

Full GC contract: 100 oz of gold — a $10 move = $1,000 profit or loss
Micro MGC:        10 oz of gold  — a $10 move = $100 profit or loss

Never change this to full contracts until you have months of experience.

---

## Contract Reference

| Symbol | Name | Point Value | What moves it |
|--------|------|-------------|---------------|
| MGC | Micro Gold | $10 per $1 move | Fed rates, USD, geopolitics |
| MCL | Micro Crude | $100 per $1 move | EIA reports, OPEC, demand |

---

## Running the Agent

Same process as the options agent:

    python futures_agent.py

Both agents can run simultaneously in separate terminal windows:

    Terminal 1: python trading_agent.py      ← options agent
    Terminal 2: python futures_agent.py      ← futures agent

Each writes to its own log file:
    trading_agent.log   ← options trades
    futures_agent.log   ← futures trades

---

## Updating the News Blackout Calendar

The agent pauses trading 30 minutes before and after high-impact events.
You need to update the HIGH_IMPACT_EVENTS list at the top of futures_agent.py
each month with upcoming dates.

Where to get dates:
  EIA Reports (every Wednesday): https://www.eia.gov/petroleum/supply/weekly
  FOMC Meetings: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  CPI Reports: https://www.bls.gov/schedule/news_release/cpi.htm

---

## Signal Logic

| Indicator | LONG (buy) | SHORT (sell) |
|-----------|-----------|--------------|
| RSI | 50 to 65 | 35 to 50 |
| EMA Cross | EMA9 above EMA21 | EMA9 below EMA21 |
| MACD | Fresh bullish cross | Fresh bearish cross |
| Volume | 1.3x 20-bar average | 1.3x 20-bar average |

## Exit Logic (ATR-Based)

Stop distance  = ATR × 1.5
Stop loss      = entry minus stop distance (long) or plus (short)
Take profit    = entry plus stop distance × 2.0 (2:1 reward to risk)

This means for every $1 you risk, you target $2 in profit.

---

## Risk Warning

Futures trading involves substantial risk of loss. Leverage means you can
lose more than your initial deposit. Micro contracts reduce but do not
eliminate this risk. Always paper trade first. Never trade with money
you cannot afford to lose. This script is for personal and educational
use only. You are solely responsible for all trading decisions.
