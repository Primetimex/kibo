# -*- coding: utf-8 -*-
"""
KIBO Trading Intelligence Agent
================================
Uses LangChain + OpenAI to reason about market conditions,
news, and signals — then gives plain-English trading recommendations
with explanations for WHY.

Integrates directly with your existing KIBO trading system.

Requirements:
    pip install langchain langchain-openai openai requests python-dotenv

Usage:
    python trading_intelligence.py
    python trading_intelligence.py --sym AAPL
    python trading_intelligence.py --sym BTC --ask "Is this a good short?"
"""

import os, requests, json, argparse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv("_env")
# ── OpenAI / LangChain imports ────────────────────────────────────────────────
from openai import OpenAI
import json as _json

OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY") or "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"
POLYGON_KEY      = os.getenv("POLYGON_API_KEY", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── Tools — what the agent can do ─────────────────────────────────────────────

def get_price_and_stats(symbol: str) -> str:
    """
    Get current price, change %, volume, and 52-week range for a stock or crypto.
    Use this first for any symbol analysis.
    """
    try:
        # Try Yahoo Finance
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            headers=HEADERS,
            params={"interval": "5m", "range": "1d"},
            timeout=8
        )
        if r.status_code == 200:
            meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            price   = meta.get("regularMarketPrice", 0)
            prev    = meta.get("chartPreviousClose", price)
            vol     = meta.get("regularMarketVolume", 0)
            avg_vol = meta.get("averageDailyVolume10Day", 1)
            high52  = meta.get("fiftyTwoWeekHigh", 0)
            low52   = meta.get("fiftyTwoWeekLow", 0)
            chg_pct = ((price - prev) / prev * 100) if prev > 0 else 0
            rel_vol = round(vol / avg_vol, 1) if avg_vol > 0 else 0

            return json.dumps({
                "symbol":      symbol,
                "price":       round(price, 4),
                "change_pct":  round(chg_pct, 2),
                "volume":      int(vol),
                "rel_volume":  rel_vol,
                "avg_volume":  int(avg_vol),
                "52w_high":    round(high52, 2),
                "52w_low":     round(low52, 2),
                "near_52w_high": price > high52 * 0.95 if high52 > 0 else False,
                "timestamp":   datetime.now().strftime("%H:%M ET")
            })
    except Exception as e:
        return json.dumps({"error": str(e), "symbol": symbol})


def get_news_headlines(symbol: str) -> str:
    """
    Get the latest 5 news headlines for a stock or crypto symbol.
    Use this to understand what catalysts are driving price movement.
    """
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            headers=HEADERS,
            params={"q": symbol, "newsCount": 5, "quotesCount": 0},
            timeout=8
        )
        if r.status_code != 200:
            return json.dumps({"error": "News fetch failed", "symbol": symbol})

        items = r.json().get("news", [])
        news  = []
        for item in items[:5]:
            title = item.get("title", "")
            pub   = item.get("providerPublishTime", 0)
            src   = item.get("publisher", "")
            hrs   = round((datetime.now().timestamp() - pub) / 3600, 1) if pub else 99
            if title:
                news.append({
                    "headline": title,
                    "source":   src,
                    "hours_ago": hrs,
                    "fresh":    hrs < 6
                })

        return json.dumps({
            "symbol":    symbol,
            "headlines": news,
            "has_fresh": any(n["fresh"] for n in news)
        })
    except Exception as e:
        return json.dumps({"error": str(e), "symbol": symbol})


def get_market_context() -> str:
    """
    Get the current overall market context — SPY, QQQ, VIX direction.
    Use this to understand if the broad market supports the trade.
    """
    context = {}
    for sym, label in [("SPY", "SP500"), ("QQQ", "Nasdaq"), ("BTC-USD", "Bitcoin")]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                headers=HEADERS,
                params={"interval": "5m", "range": "1d"},
                timeout=6
            )
            if r.status_code == 200:
                meta  = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                price = meta.get("regularMarketPrice", 0)
                prev  = meta.get("chartPreviousClose", price)
                chg   = ((price - prev) / prev * 100) if prev > 0 else 0
                context[label] = {
                    "price":      round(price, 2),
                    "change_pct": round(chg, 2),
                    "trend":      "UP" if chg > 0.3 else "DOWN" if chg < -0.3 else "FLAT"
                }
        except:
            continue

    # Determine overall market regime
    trends = [v["trend"] for v in context.values()]
    regime = "RISK-ON" if trends.count("UP") >= 2 else \
             "RISK-OFF" if trends.count("DOWN") >= 2 else "MIXED"

    context["regime"] = regime
    context["timestamp"] = datetime.now().strftime("%H:%M ET")
    return json.dumps(context)


def get_social_sentiment(symbol: str) -> str:
    """
    Get StockTwits social sentiment for a symbol.
    Use this to gauge retail trader sentiment and message volume.
    """
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
            headers=HEADERS,
            timeout=8
        )
        if r.status_code != 200:
            return json.dumps({"error": "StockTwits unavailable", "symbol": symbol})

        data     = r.json()
        messages = data.get("messages", [])
        sym_data = data.get("symbol", {})

        bull = sum(1 for m in messages
                   if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
        bear = sum(1 for m in messages
                   if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish")
        total    = len(messages)
        watchers = sym_data.get("watchlist_count", 0)

        return json.dumps({
            "symbol":    symbol,
            "messages":  total,
            "bullish":   bull,
            "bearish":   bear,
            "bull_pct":  round(bull / total * 100) if total > 0 else 0,
            "sentiment": "BULLISH" if bull > bear else "BEARISH" if bear > bull else "NEUTRAL",
            "watchers":  watchers,
            "active":    total >= 10
        })
    except Exception as e:
        return json.dumps({"error": str(e), "symbol": symbol})


def get_icc_signal(symbol: str) -> str:
    """
    Get the current ICC (market structure) signal from the KIBO trading system
    for a symbol. Returns trend, confidence, and whether a trade is approved.
    Reads from the live signal pipeline.
    """
    try:
        # Try to read from the KIBO dashboard signal data
        r = requests.get("http://localhost:5000/icc", timeout=5)
        if r.status_code == 200:
            data = r.json()
            # Find the symbol
            for item in data.get("results", []):
                if item.get("sym", "").upper() == symbol.upper():
                    return json.dumps({
                        "symbol":     symbol,
                        "trend":      item.get("trend", "UNKNOWN"),
                        "confidence": item.get("conf", 0),
                        "approved":   item.get("approved", False),
                        "rules_passed": item.get("passed", 0),
                        "stop_loss":  item.get("stop_loss"),
                        "target":     item.get("target"),
                        "rr":         item.get("rr"),
                        "source":     "KIBO Dashboard"
                    })
        return json.dumps({
            "symbol": symbol,
            "source": "Dashboard offline",
            "note":   "Start KIBO dashboard on port 5000 for live ICC signals"
        })
    except:
        return json.dumps({
            "symbol": symbol,
            "source": "Dashboard offline",
            "note":   "Start KIBO dashboard on port 5000 for live ICC signals"
        })


def send_telegram_alert(message: str) -> str:
    """
    Send a trading intelligence alert to Telegram.
    Use this when you find a high-conviction setup worth alerting on.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return "Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       f"[KIBO] KIBO Intelligence\n\n{message}",
                "parse_mode": "HTML"
            },
            timeout=6
        )
        return "Alert sent to Telegram [OK]" if r.status_code == 200 else f"Failed: {r.text}"
    except Exception as e:
        return f"Telegram error: {e}"


# ── Agent Setup ───────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are KIBO Intelligence — an expert trading analyst AI for the KIBO Trading System.

Your job is to analyze stocks, crypto, and market conditions and give clear, actionable trading recommendations with full reasoning.

You have access to real-time data tools. Always:
1. Check the current price and stats first
2. Get recent news to understand catalysts
3. Check market context (is the broad market supporting this move?)
4. Check social sentiment
5. Check the ICC signal from the KIBO system if available

Then provide:
- RECOMMENDATION: BUY / SELL / SHORT / HOLD / AVOID
- CONVICTION: HIGH / MEDIUM / LOW
- ENTRY: suggested entry price or range
- STOP LOSS: where to exit if wrong
- TARGET: profit target
- R:R RATIO: reward to risk
- REASONING: plain English explanation of WHY
- RISKS: what could go wrong

Be direct and specific. Traders need actionable information, not vague analysis.
If the setup is not clean, say AVOID and explain why.
Only send a Telegram alert if conviction is HIGH and the setup is clearly actionable."""

# Tool registry for OpenAI function calling
TOOL_FUNCTIONS = {
    "get_price_and_stats":   get_price_and_stats,
    "get_news_headlines":    get_news_headlines,
    "get_market_context":    get_market_context,
    "get_social_sentiment":  get_social_sentiment,
    "get_icc_signal":        get_icc_signal,
    "send_telegram_alert":   send_telegram_alert,
}

TOOLS_SCHEMA = [
    {"type":"function","function":{"name":"get_price_and_stats","description":"Get current price, change %, volume and stats for a symbol","parameters":{"type":"object","properties":{"symbol":{"type":"string","description":"Stock or crypto symbol"}},"required":["symbol"]}}},
    {"type":"function","function":{"name":"get_news_headlines","description":"Get latest news headlines for a symbol","parameters":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}}},
    {"type":"function","function":{"name":"get_market_context","description":"Get overall market context - SPY, QQQ, BTC direction and regime","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"get_social_sentiment","description":"Get StockTwits social sentiment for a symbol","parameters":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}}},
    {"type":"function","function":{"name":"get_icc_signal","description":"Get ICC market structure signal from KIBO system","parameters":{"type":"object","properties":{"symbol":{"type":"string"}},"required":["symbol"]}}},
    {"type":"function","function":{"name":"send_telegram_alert","description":"Send trading alert to Telegram when high conviction setup found","parameters":{"type":"object","properties":{"message":{"type":"string"}},"required":["message"]}}},
]

def build_agent():
    if not OPENAI_API_KEY:
        print("[ERROR] OPENAI_API_KEY not set in .env file")
        print("   Get a free key at: https://platform.openai.com/api-keys")
        return None
    client = OpenAI(api_key=OPENAI_API_KEY)
    print("[OK] OpenAI client ready (GPT-4o with function calling)")
    return client

def run_agent(client, query: str) -> str:
    """Run the trading intelligence agent using OpenAI function calling directly."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": query}
    ]
    print(f"\n[KIBO] Running analysis...")
    max_iterations = 8
    for i in range(max_iterations):
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=TOOLS_SCHEMA,
            tool_choice="auto",
            temperature=0.1,
        )
        msg = response.choices[0].message
        messages.append({"role":"assistant","content":msg.content,"tool_calls":[
            {"id":tc.id,"type":"function","function":{"name":tc.function.name,"arguments":tc.function.arguments}}
            for tc in (msg.tool_calls or [])
        ] if msg.tool_calls else None})

        if not msg.tool_calls:
            return msg.content or "No response"

        # Execute tool calls
        for tc in msg.tool_calls:
            fn_name = tc.function.name
            fn_args = _json.loads(tc.function.arguments) if tc.function.arguments else {}
            print(f"  -> Calling {fn_name}({list(fn_args.values())[0] if fn_args else ''})")
            if fn_name in TOOL_FUNCTIONS:
                result = TOOL_FUNCTIONS[fn_name](**fn_args)
            else:
                result = _json.dumps({"error": f"Unknown tool {fn_name}"})
            messages.append({"role":"tool","tool_call_id":tc.id,"content":str(result)})

    return "Analysis complete — max iterations reached"


# ── Predefined analysis tasks ─────────────────────────────────────────────────

def analyze_symbol(agent, symbol: str, question: str = None):
    """Run full analysis on a symbol."""
    query = question or f"""
    Give me a complete trading analysis for {symbol} right now.
    Check price, news, market context, social sentiment, and ICC signal.
    Should I trade this? If yes, what are the exact entry, stop, and target levels?
    If conviction is HIGH, send a Telegram alert with the setup.
    """
    print(f"\n{'='*60}")
    print(f"[KIBO] KIBO Intelligence analyzing {symbol}...")
    print(f"{'='*60}\n")
    result = run_agent(agent, query)
    print(f"\n{'='*60}")
    print("[CHART] FINAL RECOMMENDATION:")
    print(f"{'='*60}")
    print(result)
    return result


def morning_briefing(agent):
    """Generate a morning market briefing for all watched symbols."""
    query = """
    Generate a morning trading briefing for the KIBO system.
    
    Check market context first (SPY, QQQ, BTC direction).
    Then analyze these symbols: SPY, QQQ, NVDA, TSLA, BTC-USD, SOL-USD
    
    For each symbol give a 1-2 sentence status and whether it's worth watching today.
    End with: TOP 3 SETUPS to watch today ranked by conviction.
    
    Send the top setup as a Telegram alert if conviction is HIGH.
    """
    print(f"\n{'='*60}")
    print(f"[BRIEFING] KIBO Morning Briefing — {datetime.now().strftime('%B %d, %Y %H:%M ET')}")
    print(f"{'='*60}\n")
    result = run_agent(agent, query)
    print(f"\n[REPORT] BRIEFING:")
    print(result)
    return result


def scan_for_explosions(agent):
    """Scan penny stocks for pre-explosion setups."""
    query = """
    Scan for pre-explosion penny stock setups right now.
    
    Check these symbols that have been showing activity: 
    SNBR, CLOV, OPEN, LCID, PLUG, FCEL, SNDL
    
    For each one check price stats and news.
    Identify which ones have:
    - A real catalyst (news in last 12 hours)
    - Volume above average
    - Positive price momentum
    
    Rank them by explosion potential and give your top pick.
    If any have HIGH conviction send a Telegram alert.
    """
    print(f"\n{'='*60}")
    print(f"🔥 Scanning for Pre-Explosion Setups...")
    print(f"{'='*60}\n")
    result = run_agent(agent, query)
    print(f"\n[SETUPS] TOP SETUPS:")
    print(result)
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KIBO Trading Intelligence Agent")
    parser.add_argument("--sym",   type=str, help="Symbol to analyze (e.g. AAPL, BTC-USD)")
    parser.add_argument("--ask",   type=str, help="Custom question about the symbol")
    parser.add_argument("--brief", action="store_true", help="Run morning briefing")
    parser.add_argument("--scan",  action="store_true", help="Scan for explosion setups")
    args = parser.parse_args()

    print("[KIBO] Starting KIBO Trading Intelligence Agent...")
    agent = build_agent()
    if not agent:
        exit(1)

    if args.brief:
        morning_briefing(agent)
    elif args.scan:
        scan_for_explosions(agent)
    elif args.sym:
        analyze_symbol(agent, args.sym.upper(), args.ask)
    else:
        # Default: interactive mode
        print("\n[OK] Agent ready. Commands:")
        print("  analyze TSLA")
        print("  briefing")
        print("  scan")
        print("  ask NVDA Is this overbought?")
        print("  quit\n")

        while True:
            try:
                user_input = input("KIBO> ").strip()
                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit", "q"):
                    break
                elif user_input.lower() == "briefing":
                    morning_briefing(agent)
                elif user_input.lower() == "scan":
                    scan_for_explosions(agent)
                elif user_input.lower().startswith("analyze "):
                    sym = user_input.split(" ", 1)[1].upper()
                    analyze_symbol(agent, sym)
                elif user_input.lower().startswith("ask "):
                    parts = user_input.split(" ", 2)
                    if len(parts) >= 3:
                        analyze_symbol(agent, parts[1].upper(), parts[2])
                    else:
                        print("Usage: ask SYMBOL your question here")
                else:
                    # Pass anything to the agent directly
                    result = run_agent(agent, user_input)
                    print(f"\n{result}\n")
            except KeyboardInterrupt:
                print("\nGoodbye!")
                break
            except Exception as e:
                print(f"Error: {e}")
