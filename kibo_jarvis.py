# -*- coding: utf-8 -*-
"""
KIBO JARVIS — System-Wide AI Brain
════════════════════════════════════════════════════════════════════════════
A JARVIS-style AI layer that sits on top of the entire KIBO platform.
It can see EVERYTHING — brain state, open positions, trade journal,
scanner feeds, ICC signals, news, market regime — and it:

  1. CHATS      — natural-language interface in the dashboard
                  ("JARVIS, what's my exposure right now?"
                   "Why did the crypto agent stop trading?"
                   "How am I doing this week vs last week?")

  2. REVIEWS    — second-opinion on every trade AFTER trade_brain approves
                  it but BEFORE the order is placed. Returns
                  APPROVE / CAUTION / VETO with reasoning.

  3. WATCHES    — sentinel loop that runs every few minutes, checks for
                  anomalies (drawdown proximity, stale positions, agents
                  gone quiet, loss streaks building) and pushes Telegram
                  alerts before problems become losses.

  4. ACTS       — can halt/resume all trading via trade_brain, but ONLY
                  if JARVIS_ALLOW_ACTIONS=true in .env. Off by default.

════════════════════════════════════════════════════════════════════════════
CHAIN OF COMMAND (this is the important part):

    ICC structure_rules  ──►  trade_brain  ──►  JARVIS review  ──►  order
    (deterministic)          (deterministic)    (AI, advisory*)

  * JARVIS can VETO a trade (downgrade to skip) but can NEVER approve a
    trade that ICC or trade_brain blocked. The deterministic layers are
    authoritative — same principle as the existing GPT-4o intelligence
    panel guardrail. An LLM opinion is never allowed to override code.

  * VETO is only binding if JARVIS_VETO_ENABLED=true. Default is advisory:
    the verdict is logged and alerted but the trade proceeds.

════════════════════════════════════════════════════════════════════════════
.env settings:

    OPENAI_API_KEY=sk-...          (REQUIRED — hard-fails if missing.
                                    No embedded fallback key, ever.)
    JARVIS_MODEL=gpt-4o            (default gpt-4o)
    JARVIS_VETO_ENABLED=false      (true = VETO verdicts block trades)
    JARVIS_ALLOW_ACTIONS=false     (true = JARVIS may halt/resume trading)
    JARVIS_SENTINEL_MIN=5          (minutes between sentinel checks)

Usage:
    python kibo_jarvis.py                     # interactive chat
    python kibo_jarvis.py --ask "status?"     # one-shot question
    python kibo_jarvis.py --sentinel          # run the watch loop
    python kibo_jarvis.py --debrief           # end-of-day debrief

Agent integration (after brain approval):
    from kibo_jarvis import pre_trade_review
    verdict = pre_trade_review("crypto", symbol, "LONG", entry, stop,
                                target, icc_score=score, icc_trend=trend)
    if verdict["blocked"]:
        log.info(f"JARVIS VETO {symbol}: {verdict['reason']}")
        continue
"""

import os
import json
import time
import argparse
import threading
import requests
from pathlib import Path
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config — env only. NO embedded fallback keys. ────────────────────────────
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")
JARVIS_MODEL         = os.getenv("JARVIS_MODEL", "gpt-4o")
JARVIS_VETO_ENABLED  = os.getenv("JARVIS_VETO_ENABLED", "false").lower() == "true"
JARVIS_ALLOW_ACTIONS = os.getenv("JARVIS_ALLOW_ACTIONS", "false").lower() == "true"
SENTINEL_MIN         = int(os.getenv("JARVIS_SENTINEL_MIN", "5"))
TELEGRAM_TOKEN       = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")
HEADERS              = {"User-Agent": "Mozilla/5.0"}

_client = None
_client_lock = threading.Lock()


def _get_client():
    """Lazy OpenAI client. Hard-fails without an env key — by design."""
    global _client
    with _client_lock:
        if _client is None:
            if not OPENAI_API_KEY:
                raise RuntimeError(
                    "OPENAI_API_KEY not set in .env — JARVIS refuses to run "
                    "without it. (No embedded fallback keys, per KIBO "
                    "security policy.)"
                )
            from openai import OpenAI
            _client = OpenAI(api_key=OPENAI_API_KEY)
        return _client


# ═════════════════════════════════════════════════════════════════════════════
#  TOOLS — JARVIS's eyes into the KIBO system
#  Every tool returns a JSON string and degrades gracefully if a piece of
#  the system isn't available (e.g. running JARVIS standalone for testing).
# ═════════════════════════════════════════════════════════════════════════════

def tool_get_brain_status() -> str:
    """Risk-brain state: halt/cooldown status, daily P&L, loss streak,
    trades today, open symbols, POI blacklist."""
    try:
        from trade_brain import brain
        return json.dumps(brain.get_status())
    except Exception as e:
        # fall back to reading the state file directly
        try:
            p = Path("kibo_brain_state.json")
            if p.exists():
                return p.read_text()
        except Exception:
            pass
        return json.dumps({"error": f"trade_brain unavailable: {e}"})


def tool_get_positions() -> str:
    """Live Alpaca account + open positions + open orders."""
    out = {}
    try:
        from alpaca_connector import get_account, get_positions, get_orders
        out["account"]   = get_account()
        out["positions"] = get_positions()
        out["orders"]    = get_orders("open")
    except Exception as e:
        out["error"] = f"alpaca_connector unavailable: {e}"
    return json.dumps(out, default=str)


def tool_get_journal(last_n: int = 15) -> str:
    """Trade journal: overall stats plus the last N trades with full
    ICC context (score, trend, R:R, agent, outcome, P&L)."""
    try:
        from trade_journal import get_all, get_stats
        trades = get_all()
        return json.dumps({
            "stats":  get_stats(),
            "recent": trades[-int(last_n):],
        })
    except Exception as e:
        try:
            p = Path("trade_journal.json")
            if p.exists():
                trades = json.loads(p.read_text())
                return json.dumps({"recent": trades[-int(last_n):]})
        except Exception:
            pass
        return json.dumps({"error": f"trade_journal unavailable: {e}"})


def tool_get_scanner_feeds() -> str:
    """Latest output from the market/crypto/explosion scanners
    (what setups the system is currently watching)."""
    feeds = {}
    for name, fname in [("market", "scanner_results.json"),
                         ("crypto", "crypto_scanner_results.json"),
                         ("explosion", "explosion_results.json")]:
        try:
            p = Path(fname)
            if p.exists():
                data = json.loads(p.read_text())
                # keep it compact — cap list-like payloads
                if isinstance(data, list):
                    data = data[:10]
                feeds[name] = data
                feeds[f"{name}_age_min"] = round(
                    (time.time() - p.stat().st_mtime) / 60, 1)
        except Exception as e:
            feeds[name] = f"unreadable: {e}"
    return json.dumps(feeds, default=str)


def tool_get_market_context() -> str:
    """Broad market regime: SPY, QQQ, BTC direction → RISK-ON/OFF/MIXED."""
    context = {}
    for sym, label in [("SPY", "SP500"), ("QQQ", "Nasdaq"), ("BTC-USD", "Bitcoin")]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                headers=HEADERS, params={"interval": "5m", "range": "1d"},
                timeout=6)
            if r.status_code == 200:
                meta  = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
                price = meta.get("regularMarketPrice", 0)
                prev  = meta.get("chartPreviousClose", price)
                chg   = ((price - prev) / prev * 100) if prev > 0 else 0
                context[label] = {"price": round(price, 2),
                                   "change_pct": round(chg, 2),
                                   "trend": "UP" if chg > 0.3 else "DOWN" if chg < -0.3 else "FLAT"}
        except Exception:
            continue
    trends = [v["trend"] for v in context.values() if isinstance(v, dict)]
    context["regime"] = ("RISK-ON" if trends.count("UP") >= 2 else
                          "RISK-OFF" if trends.count("DOWN") >= 2 else "MIXED")
    context["timestamp"] = datetime.now().strftime("%H:%M ET")
    return json.dumps(context)


def tool_get_news(symbol: str) -> str:
    """News catalyst score for a symbol via KIBO's news_intel module."""
    try:
        from news_intel import get_news_score
        return json.dumps(get_news_score(symbol), default=str)
    except Exception as e:
        return json.dumps({"error": f"news_intel unavailable: {e}", "symbol": symbol})


def tool_get_icc_signal(symbol: str) -> str:
    """Current ICC market-structure verdict for a symbol from the KIBO
    structure engine (trend, approval, stop/target)."""
    try:
        from trading_intelligence import get_icc_signal
        return get_icc_signal(symbol)
    except Exception as e:
        return json.dumps({"error": f"ICC signal unavailable: {e}", "symbol": symbol})


def tool_halt_trading(reason: str) -> str:
    """EMERGENCY: halt ALL trading across every agent. Requires
    JARVIS_ALLOW_ACTIONS=true."""
    if not JARVIS_ALLOW_ACTIONS:
        return json.dumps({"error": "Actions disabled. Set JARVIS_ALLOW_ACTIONS=true in .env to allow JARVIS to halt trading."})
    try:
        from trade_brain import brain
        brain.force_halt(f"JARVIS: {reason}")
        _send_telegram(f"🛑 <b>JARVIS HALTED ALL TRADING</b>\n{reason}")
        return json.dumps({"ok": True, "halted": True, "reason": reason})
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_resume_trading(reason: str) -> str:
    """Resume trading after a halt. Requires JARVIS_ALLOW_ACTIONS=true."""
    if not JARVIS_ALLOW_ACTIONS:
        return json.dumps({"error": "Actions disabled. Set JARVIS_ALLOW_ACTIONS=true in .env."})
    try:
        from trade_brain import brain
        brain.clear_halt()
        _send_telegram(f"✅ <b>JARVIS RESUMED TRADING</b>\n{reason}")
        return json.dumps({"ok": True, "halted": False, "reason": reason})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _send_telegram(message: str) -> str:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return json.dumps({"error": "Telegram not configured"})
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                  "parse_mode": "HTML"}, timeout=8)
        return json.dumps({"ok": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


def tool_send_telegram(message: str) -> str:
    """Push an alert to Josh's Telegram."""
    return _send_telegram(message)


TOOL_FUNCTIONS = {
    "get_brain_status":   tool_get_brain_status,
    "get_positions":      tool_get_positions,
    "get_journal":        tool_get_journal,
    "get_scanner_feeds":  tool_get_scanner_feeds,
    "get_market_context": tool_get_market_context,
    "get_news":           tool_get_news,
    "get_icc_signal":     tool_get_icc_signal,
    "halt_trading":       tool_halt_trading,
    "resume_trading":     tool_resume_trading,
    "send_telegram":      tool_send_telegram,
}

TOOLS_SCHEMA = [
    {"type": "function", "function": {"name": "get_brain_status", "description": "Risk-brain state: halt/cooldown, daily P&L, loss streak, trade counts, open symbols, POI blacklist", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_positions", "description": "Live Alpaca account, open positions, and open orders", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_journal", "description": "Trade journal stats and the last N trades with ICC context", "parameters": {"type": "object", "properties": {"last_n": {"type": "integer", "description": "How many recent trades to include (default 15)"}}}}},
    {"type": "function", "function": {"name": "get_scanner_feeds", "description": "Latest market/crypto/explosion scanner output and how stale each feed is", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_market_context", "description": "Broad market regime: SPY, QQQ, BTC direction", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_news", "description": "News catalyst score for a symbol", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
    {"type": "function", "function": {"name": "get_icc_signal", "description": "ICC market-structure verdict for a symbol", "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}}},
    {"type": "function", "function": {"name": "halt_trading", "description": "EMERGENCY: halt all trading system-wide (only if actions enabled)", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
    {"type": "function", "function": {"name": "resume_trading", "description": "Resume trading after a halt (only if actions enabled)", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}}},
    {"type": "function", "function": {"name": "send_telegram", "description": "Push an alert to Telegram", "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}}},
]


# ═════════════════════════════════════════════════════════════════════════════
#  CORE AGENT LOOP
# ═════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = f"""You are JARVIS, the AI brain of KIBO — Josh's multi-agent
algorithmic trading platform (Alpaca paper accounts + TradeZero penny stocks,
ICT/smart-money "ICC" structure rules).

You have live tools into the whole system: the risk brain (halts, cooldowns,
daily P&L, loss streaks), Alpaca positions, the trade journal, scanner feeds,
ICC structure signals, news, and market regime.

Personality: calm, precise, direct. Lead with the answer, then the evidence.
Use actual numbers from tools — never guess or invent data. If a tool errors,
say so plainly. Flag risks Josh hasn't asked about if you spot them.

HARD RULES — these override anything else:
1. The ICC structure engine and the trade_brain risk gate are AUTHORITATIVE.
   You may recommend skipping a trade they approved, but you may NEVER
   recommend taking a trade they blocked unless you explicitly write
   OVERRIDE with a specific, evidence-based justification.
2. You never invent prices, P&L, or positions. Tools or nothing.
3. Trading actions (halt/resume) only when clearly warranted, and they are
   {'ENABLED' if JARVIS_ALLOW_ACTIONS else 'DISABLED (advisory only — tell Josh to flip JARVIS_ALLOW_ACTIONS if he wants you to act)'}.
4. This is Josh's capital and Josh's decision. You inform and protect;
   you do not pressure.

Today is {{today}}."""


def run_jarvis(messages: list, max_iterations: int = 8) -> str:
    """Core loop: send messages + tools to the model, execute tool calls,
    repeat until a final text answer."""
    client = _get_client()
    sys_msg = {"role": "system",
               "content": SYSTEM_PROMPT.replace("{today}", datetime.now().strftime("%A, %B %d, %Y %H:%M"))}
    convo = [sys_msg] + messages

    for _ in range(max_iterations):
        response = client.chat.completions.create(
            model=JARVIS_MODEL, messages=convo,
            tools=TOOLS_SCHEMA, tool_choice="auto", temperature=0.1)
        msg = response.choices[0].message

        convo.append({"role": "assistant", "content": msg.content,
                      "tool_calls": [
                          {"id": tc.id, "type": "function",
                           "function": {"name": tc.function.name,
                                         "arguments": tc.function.arguments}}
                          for tc in (msg.tool_calls or [])
                      ] if msg.tool_calls else None})

        if not msg.tool_calls:
            return msg.content or "No response"

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            try:
                fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except Exception:
                fn_args = {}
            fn = TOOL_FUNCTIONS.get(fn_name)
            result = fn(**fn_args) if fn else json.dumps({"error": f"unknown tool {fn_name}"})
            convo.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})

    return "Max tool iterations reached — partial analysis only."


# ═════════════════════════════════════════════════════════════════════════════
#  1. CHAT — dashboard / CLI interface
# ═════════════════════════════════════════════════════════════════════════════

def jarvis_chat(user_message: str, history: list = None) -> str:
    """Chat entry point. `history` is a list of {"role","content"} dicts
    from previous turns (the dashboard should send the last ~10)."""
    messages = list(history or []) + [{"role": "user", "content": user_message}]
    return run_jarvis(messages)


# ═════════════════════════════════════════════════════════════════════════════
#  2. PRE-TRADE REVIEW — AI second opinion after brain approval
# ═════════════════════════════════════════════════════════════════════════════

def _parse_verdict(text: str) -> str:
    """Pure function — extract APPROVE/CAUTION/VETO from model output.
    Defaults to CAUTION if the model didn't follow the format."""
    if not text:
        return "CAUTION"
    head = text.strip().upper()[:400]
    for v in ("VETO", "APPROVE", "CAUTION"):
        if head.startswith(v):
            return v
    # look for VERDICT: X pattern anywhere
    for v in ("VETO", "APPROVE", "CAUTION"):
        if f"VERDICT: {v}" in head or f"VERDICT:{v}" in head:
            return v
    return "CAUTION"


def pre_trade_review(agent: str, symbol: str, direction: str,
                      entry: float, stop: float, target: float,
                      icc_score=None, icc_trend=None,
                      extra_context: str = "") -> dict:
    """
    Ask JARVIS for a second opinion on a trade the deterministic layers
    already approved. Returns:
        {"verdict": "APPROVE|CAUTION|VETO",
         "reason": str,
         "blocked": bool}   # True only if VETO *and* JARVIS_VETO_ENABLED

    Fail-open: if the API errors, the trade proceeds (deterministic layers
    already approved it) and the error is logged in the reason.
    """
    rr = 0
    try:
        risk = abs(entry - stop)
        rr = round(abs(target - entry) / risk, 2) if risk > 0 else 0
    except Exception:
        pass

    prompt = f"""PRE-TRADE REVIEW REQUEST (ICC and trade_brain already APPROVED this).

Agent: {agent} | Symbol: {symbol} | Direction: {direction}
Entry: {entry} | Stop: {stop} | Target: {target} | R:R: {rr}:1
ICC score: {icc_score} | ICC trend: {icc_trend}
{extra_context}

Check whatever tools you need (brain status, news on {symbol}, market regime,
recent journal performance for this agent). Then respond in EXACTLY this format:

VERDICT: APPROVE or CAUTION or VETO
REASON: one or two sentences.

VETO only for concrete red flags (fresh contradicting news, halted/cooldown
brain state that somehow slipped through, extreme regime mismatch, agent on
a brutal losing run today). CAUTION for soft concerns. APPROVE otherwise.
You cannot upgrade this trade — only confirm or flag it."""

    try:
        text = run_jarvis([{"role": "user", "content": prompt}], max_iterations=6)
        verdict = _parse_verdict(text)
    except Exception as e:
        return {"verdict": "APPROVE",
                "reason": f"JARVIS unavailable ({e}) — proceeding on deterministic approval (fail-open)",
                "blocked": False}

    blocked = (verdict == "VETO") and JARVIS_VETO_ENABLED
    if verdict == "VETO":
        _send_telegram(f"🧠 <b>JARVIS {'VETO' if blocked else 'VETO (advisory)'} — {symbol}</b>\n"
                        f"{agent} {direction} @ {entry}\n{text[:400]}")
    return {"verdict": verdict, "reason": text, "blocked": blocked}


# ═════════════════════════════════════════════════════════════════════════════
#  3. SENTINEL — the always-on watcher
# ═════════════════════════════════════════════════════════════════════════════

_last_sentinel_alerts: dict = {}   # dedupe key -> timestamp

def _should_alert(key: str, min_gap_min: int = 30) -> bool:
    """Don't re-send the same sentinel alert more often than every N min."""
    now = time.time()
    last = _last_sentinel_alerts.get(key, 0)
    if now - last >= min_gap_min * 60:
        _last_sentinel_alerts[key] = now
        return True
    return False


def sentinel_check(alert: bool = True) -> dict:
    """
    One deterministic health pass (no LLM cost) + optional AI narration
    when something is actually wrong. Returns findings dict.
    """
    findings = {"ok": True, "issues": [], "ts": datetime.now().isoformat()}

    # Brain state checks
    try:
        status = json.loads(tool_get_brain_status())
        if "error" not in status:
            if status.get("halted"):
                findings["issues"].append(f"TRADING HALTED: {status.get('halt_reason')}")
            if status.get("in_cooldown"):
                findings["issues"].append(status.get("cooldown_reason", "In loss-streak cooldown"))
            dd_limit = status.get("daily_dd_limit", 0)
            pnl = status.get("daily_pnl", 0)
            if dd_limit and pnl <= dd_limit * 0.7 and not status.get("halted"):
                findings["issues"].append(
                    f"DRAWDOWN WARNING: daily P&L ${pnl:.2f} is at "
                    f"{abs(pnl/dd_limit)*100:.0f}% of the halt limit (${dd_limit:.2f})")
            streak = status.get("loss_streak", 0)
            limit  = status.get("loss_streak_limit", 3)
            if streak == limit - 1:
                findings["issues"].append(
                    f"LOSS STREAK BUILDING: {streak} consecutive losses — one more triggers cooldown")
    except Exception as e:
        findings["issues"].append(f"Brain status unreadable: {e}")

    # Scanner feed staleness
    try:
        feeds = json.loads(tool_get_scanner_feeds())
        for name in ("market", "crypto", "explosion"):
            age = feeds.get(f"{name}_age_min")
            if age is not None and age > 60:
                findings["issues"].append(
                    f"{name} scanner feed is stale ({age:.0f} min old) — agent may be down")
    except Exception:
        pass

    findings["ok"] = len(findings["issues"]) == 0

    if alert and findings["issues"]:
        for issue in findings["issues"]:
            if _should_alert(issue[:60]):
                _send_telegram(f"🧠 <b>JARVIS SENTINEL</b>\n{issue}")

    return findings


def sentinel_loop():
    """Run sentinel_check every JARVIS_SENTINEL_MIN minutes, forever.
    Launch as a thread from dashboard.py or run standalone."""
    print(f"[JARVIS] Sentinel running — checking every {SENTINEL_MIN} min "
          f"(actions {'ENABLED' if JARVIS_ALLOW_ACTIONS else 'disabled'})")
    while True:
        try:
            f = sentinel_check()
            stamp = datetime.now().strftime("%H:%M:%S")
            if f["ok"]:
                print(f"[JARVIS {stamp}] All systems nominal.")
            else:
                for i in f["issues"]:
                    print(f"[JARVIS {stamp}] ⚠ {i}")
        except Exception as e:
            print(f"[JARVIS] Sentinel error: {e}")
        time.sleep(SENTINEL_MIN * 60)


# ═════════════════════════════════════════════════════════════════════════════
#  4. DAILY DEBRIEF
# ═════════════════════════════════════════════════════════════════════════════

def daily_debrief() -> str:
    """End-of-day: JARVIS reviews the journal + brain log and tells you
    what actually happened today and what to change tomorrow."""
    prompt = """Give me today's end-of-day debrief. Pull the brain status and the
trade journal. Cover:
1. Today's P&L and how close we came to any risk limits.
2. Which agents traded, which won/lost, any patterns (e.g. all losses
   counter-regime, one agent bleeding).
3. Anything the sentinel or brain blocked, and whether the blocks look right.
4. ONE specific, concrete adjustment for tomorrow.
Keep it tight. Send the summary to Telegram when done."""
    return run_jarvis([{"role": "user", "content": prompt}])


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KIBO JARVIS — system-wide AI brain")
    parser.add_argument("--ask", type=str, help="One-shot question")
    parser.add_argument("--sentinel", action="store_true", help="Run the watch loop")
    parser.add_argument("--debrief", action="store_true", help="End-of-day debrief")
    parser.add_argument("--check", action="store_true", help="Single sentinel pass, no alerts")
    args = parser.parse_args()

    if args.check:
        print(json.dumps(sentinel_check(alert=False), indent=2))
    elif args.sentinel:
        sentinel_loop()
    elif args.debrief:
        print(daily_debrief())
    elif args.ask:
        print(jarvis_chat(args.ask))
    else:
        print("[JARVIS] Online. Ask me anything about the system. 'quit' to exit.\n")
        history = []
        while True:
            try:
                q = input("You> ").strip()
                if not q:
                    continue
                if q.lower() in ("quit", "exit", "q"):
                    break
                answer = jarvis_chat(q, history)
                print(f"\nJARVIS> {answer}\n")
                history += [{"role": "user", "content": q},
                            {"role": "assistant", "content": answer}]
                history = history[-10:]
            except KeyboardInterrupt:
                break
