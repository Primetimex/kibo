# -*- coding: utf-8 -*-
"""
KIBO Scanner Executor — trades what the market scanner approves
════════════════════════════════════════════════════════════════════════════
THE MISSING LINK. market_scanner.py already scans ~750 symbols per cycle
(Yahoo most-actives + gainers + losers) and writes fully ICC-approved
setups — with direction, stop, target, and R:R — to scanner_results.json.
Until now, nothing traded them. This agent does.

Pipeline per cycle:
    scanner_results.json  →  quality filter + ranking  →  trade_brain
    risk gate  →  Alpaca paper order  →  trade_journal entry
    …then manages exits (stop/target) and logs journal exits + brain closes.

This replaces the hardcoded 6-ticker watchlist model: the universe is now
whatever the scanner finds across the whole market, refreshed every scan.

Design notes:
  - The scanner's ICC approval is the setup filter; trade_brain is the
    risk gate (drawdown halt, loss streak, caps, duplicate symbols).
    This agent adds NO new strategy logic — it's pure plumbing, on purpose.
  - Journal integration is built in from day one (log_entry + log_exit),
    so unlike the older agents, every trade here is measurable.
  - State persists to scanner_executor_state.json — restart-safe.

.env settings (all optional, sane defaults):
    SCANEXEC_MIN_CONF=70        minimum scanner confidence to trade
    SCANEXEC_MIN_RR=1.5         minimum reward:risk to trade
    SCANEXEC_MAX_POSITIONS=5    concurrent positions cap
    SCANEXEC_MAX_STALE_MIN=20   ignore scanner file older than this
    SCANEXEC_ALLOW_SHORTS=true  set false to trade longs only
    SCANEXEC_MIN_PRICE=3.0      skip sub-$3 names (penny agent's turf)
    SCANEXEC_POLL_SEC=60        loop interval
    RISK_PCT=0.01               fraction of equity risked per trade
    ACCOUNT_SIZE=10000          fallback equity if Alpaca unreachable

Run:  python scanner_executor.py
      python scanner_executor.py --dry     (log decisions, place no orders)
      python scanner_executor.py --once    (single cycle, then exit)
"""

import os
import json
import time
import logging
import argparse
import requests
from pathlib import Path
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
MIN_CONF       = float(os.getenv("SCANEXEC_MIN_CONF", "70"))
MIN_RR         = float(os.getenv("SCANEXEC_MIN_RR", "1.5"))
MAX_POSITIONS  = int(os.getenv("SCANEXEC_MAX_POSITIONS", "5"))
MAX_STALE_MIN  = float(os.getenv("SCANEXEC_MAX_STALE_MIN", "20"))
ALLOW_SHORTS   = os.getenv("SCANEXEC_ALLOW_SHORTS", "true").lower() == "true"
MIN_PRICE      = float(os.getenv("SCANEXEC_MIN_PRICE", "3.0"))
POLL_SEC       = int(os.getenv("SCANEXEC_POLL_SEC", "60"))
RISK_PCT       = float(os.getenv("RISK_PCT", "0.01"))
ACCOUNT_SIZE   = float(os.getenv("ACCOUNT_SIZE", "10000"))

SCANNER_FILE   = Path(os.getenv("SCANNER_RESULTS_FILE", "scanner_results.json"))
STATE_FILE     = Path("scanner_executor_state.json")
AGENT_NAME     = "scanner_executor"
HEADERS        = {"User-Agent": "Mozilla/5.0"}

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [SCAN-EXEC] %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(AGENT_NAME)

# ── Optional KIBO modules — degrade gracefully, but warn loudly ──────────────
try:
    from trade_brain import brain
    BRAIN_OK = True
except ImportError:
    BRAIN_OK = False
    log.warning("trade_brain.py not found — running WITHOUT the central risk "
                "gate. Strongly recommended to add it.")

try:
    from trade_journal import log_entry as journal_entry, log_exit as journal_exit
    JOURNAL_OK = True
except ImportError:
    JOURNAL_OK = False
    def journal_entry(*a, **k): return None
    def journal_exit(*a, **k): pass
    log.warning("trade_journal.py not found — trades will NOT be journaled.")

try:
    from alpaca_connector import get_account, place_order, close_position as alpaca_close
    ALPACA_OK = True
except ImportError:
    ALPACA_OK = False
    log.warning("alpaca_connector.py not found — forcing dry-run mode.")


def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                            "parse_mode": "HTML"}, timeout=8)
    except Exception:
        pass


# ── State (open positions) — restart-safe ─────────────────────────────────────
def load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {"positions": {}}   # sym -> position dict


def save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.error(f"Could not persist state: {e}")


# ── Scanner input ─────────────────────────────────────────────────────────────
def load_approved_setups() -> list:
    """Read scanner_results.json, return approved setups passing quality
    filters, best-first. Returns [] if the file is missing or stale."""
    if not SCANNER_FILE.exists():
        log.warning(f"{SCANNER_FILE} not found — is market_scanner.py running?")
        return []

    age_min = (time.time() - SCANNER_FILE.stat().st_mtime) / 60
    if age_min > MAX_STALE_MIN:
        log.info(f"Scanner feed is {age_min:.0f} min old (> {MAX_STALE_MIN:.0f}) — skipping cycle")
        return []

    try:
        data = json.loads(SCANNER_FILE.read_text())
        results = data.get("results", [])
    except Exception as e:
        log.error(f"Could not parse scanner results: {e}")
        return []

    setups = []
    for r in results:
        if not r.get("approved"):
            continue
        if r.get("error"):
            continue
        conf   = float(r.get("conf") or 0)
        rr     = float(r.get("rr") or 0)
        price  = float(r.get("price") or 0)
        stop   = float(r.get("stop") or 0)
        target = float(r.get("target") or 0)
        direction = (r.get("direction") or "").upper()

        if conf < MIN_CONF:                       continue
        if rr < MIN_RR:                           continue
        if price < MIN_PRICE:                     continue
        if direction not in ("LONG", "SHORT"):    continue
        if direction == "SHORT" and not ALLOW_SHORTS:
            continue
        if not stop or not target:                continue
        # sanity: stop must be on the losing side, target on the winning side
        if direction == "LONG"  and not (stop < price < target):  continue
        if direction == "SHORT" and not (target < price < stop):  continue

        setups.append(r)

    # best first: confidence weighted by R:R (capped so one crazy R:R
    # doesn't dominate)
    setups.sort(key=lambda r: float(r["conf"]) * min(float(r["rr"]), 4.0),
                reverse=True)
    return setups


# ── Sizing + prices ───────────────────────────────────────────────────────────
def get_equity() -> float:
    if ALPACA_OK:
        try:
            acct = get_account()
            eq = float(acct.get("equity") or acct.get("portfolio_value") or 0)
            if eq > 0:
                return eq
        except Exception as e:
            log.warning(f"Alpaca account fetch failed ({e}) — using ACCOUNT_SIZE")
    return ACCOUNT_SIZE


def size_position(equity: float, entry: float, stop: float) -> int:
    """Risk exactly RISK_PCT of equity if the stop is hit."""
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return 0
    shares = int((equity * RISK_PCT) / risk_per_share)
    # never let one position exceed 25% of equity notionally
    max_by_notional = int((equity * 0.25) / entry) if entry > 0 else 0
    return max(0, min(shares, max_by_notional))


def get_price(sym: str) -> float:
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                         headers=HEADERS,
                         params={"interval": "1m", "range": "1d"}, timeout=8)
        if r.status_code == 200:
            meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
            return float(meta.get("regularMarketPrice") or 0)
    except Exception:
        pass
    return 0.0


# ── Entry ─────────────────────────────────────────────────────────────────────
def try_enter(setup: dict, state: dict, equity: float, dry: bool) -> bool:
    sym       = setup["sym"].upper()
    direction = setup["direction"].upper()
    entry     = float(setup["price"])
    stop      = float(setup["stop"])
    target    = float(setup["target"])
    rr        = float(setup.get("rr") or 0)
    conf      = float(setup.get("conf") or 0)

    if sym in state["positions"]:
        return False

    shares = size_position(equity, entry, stop)
    if shares < 1:
        log.info(f"{sym}: sized to 0 shares (entry={entry}, stop={stop}) — skipping")
        return False
    risk_dollars = round(abs(entry - stop) * shares, 2)

    # Central risk gate
    if BRAIN_OK:
        ok, reason = brain.approve_trade(AGENT_NAME, sym, direction,
                                          risk_amount=risk_dollars,
                                          poi_level=entry)
        if not ok:
            log.info(f"{sym}: BRAIN BLOCKED — {reason}")
            return False

    if dry or not ALPACA_OK:
        log.info(f"[DRY] Would {'BUY' if direction=='LONG' else 'SELL SHORT'} "
                 f"{shares}x {sym} @ ~{entry} stop={stop} target={target} "
                 f"(conf={conf:.0f} rr={rr}:1 risk=${risk_dollars})")
        # roll back the brain reservation in dry mode
        if BRAIN_OK:
            brain.record_close(AGENT_NAME, sym, 0.0)
        return False

    side = "buy" if direction == "LONG" else "sell"
    try:
        order = place_order(sym, side, shares)
        if not order:
            raise RuntimeError("place_order returned falsy")
    except Exception as e:
        log.error(f"{sym}: order failed — {e}")
        if BRAIN_OK:
            brain.record_close(AGENT_NAME, sym, 0.0)   # release reservation
        return False

    trade_id = journal_entry(sym, direction, entry, stop, target, shares,
                              icc_score=conf, icc_trend=setup.get("trend", "—"),
                              rr=rr, agent=AGENT_NAME) if JOURNAL_OK else None

    state["positions"][sym] = {
        "direction": direction, "entry": entry, "stop": stop,
        "target": target, "shares": shares, "risk": risk_dollars,
        "trade_id": trade_id, "opened": datetime.now().isoformat(),
        "conf": conf, "rr": rr,
    }
    save_state(state)

    log.info(f"✅ ENTERED {direction} {shares}x {sym} @ {entry} "
             f"stop={stop} target={target} (conf={conf:.0f}, rr={rr}:1)")
    send_telegram(f"🎯 <b>SCANNER TRADE — {sym}</b>\n"
                  f"{direction} {shares} shares @ ${entry}\n"
                  f"Stop ${stop} · Target ${target} · R:R {rr}:1\n"
                  f"Scanner conf {conf:.0f}% · risk ${risk_dollars}")
    return True


# ── Exits ─────────────────────────────────────────────────────────────────────
def check_exits(state: dict, dry: bool):
    for sym, pos in list(state["positions"].items()):
        price = get_price(sym)
        if price <= 0:
            continue

        d = pos["direction"]
        hit_target = (d == "LONG" and price >= pos["target"]) or \
                     (d == "SHORT" and price <= pos["target"])
        hit_stop   = (d == "LONG" and price <= pos["stop"]) or \
                     (d == "SHORT" and price >= pos["stop"])
        if not (hit_target or hit_stop):
            continue

        reason = "TAKE_PROFIT" if hit_target else "STOP_LOSS"
        pnl = (price - pos["entry"]) * pos["shares"]
        if d == "SHORT":
            pnl = -pnl
        pnl = round(pnl, 2)

        if not dry and ALPACA_OK:
            try:
                alpaca_close(sym)
            except Exception as e:
                log.error(f"{sym}: close failed — {e}. Will retry next cycle.")
                send_telegram(f"⚠️ <b>CLOSE FAILED — {sym}</b>\nManual check may be needed.\n{str(e)[:150]}")
                continue

        if JOURNAL_OK and pos.get("trade_id"):
            journal_exit(pos["trade_id"], price, reason=reason)
        if BRAIN_OK:
            brain.record_close(AGENT_NAME, sym, pnl, poi_level=pos["entry"])

        icon = "✅" if pnl >= 0 else "🛑"
        log.info(f"{icon} CLOSED {sym} @ {price} ({reason}) pnl=${pnl}")
        send_telegram(f"{icon} <b>{reason.replace('_',' ')} — {sym}</b>\n"
                      f"Exit ${price} · P&L ${pnl}")
        del state["positions"][sym]
        save_state(state)


# ── Main loop ─────────────────────────────────────────────────────────────────
def run_cycle(state: dict, dry: bool):
    check_exits(state, dry)

    open_count = len(state["positions"])
    if open_count >= MAX_POSITIONS:
        log.info(f"At max positions ({open_count}/{MAX_POSITIONS}) — managing exits only")
        return

    setups = load_approved_setups()
    if not setups:
        return
    log.info(f"{len(setups)} approved setups pass quality filters "
             f"(conf≥{MIN_CONF:.0f}, rr≥{MIN_RR})")

    equity = get_equity()
    slots  = MAX_POSITIONS - open_count
    for setup in setups:
        if slots <= 0:
            break
        if try_enter(setup, state, equity, dry):
            slots -= 1


def main():
    parser = argparse.ArgumentParser(description="KIBO Scanner Executor")
    parser.add_argument("--dry",  action="store_true", help="Log decisions, place no orders")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()

    dry = args.dry or not ALPACA_OK
    state = load_state()

    log.info("KIBO Scanner Executor starting")
    log.info(f"  Universe   : whatever market_scanner.py approves (whole-market)")
    log.info(f"  Filters    : conf≥{MIN_CONF:.0f}%, R:R≥{MIN_RR}, price≥${MIN_PRICE}, "
             f"shorts={'ON' if ALLOW_SHORTS else 'OFF'}")
    log.info(f"  Risk       : {RISK_PCT*100:.1f}%/trade, max {MAX_POSITIONS} positions")
    log.info(f"  Gates      : brain={'ON' if BRAIN_OK else 'OFF'}, "
             f"journal={'ON' if JOURNAL_OK else 'OFF'}, "
             f"alpaca={'ON' if ALPACA_OK else 'OFF'}, dry_run={dry}")
    log.info(f"  Open now   : {list(state['positions'].keys()) or 'none'}")

    if args.once:
        run_cycle(state, dry)
        return

    while True:
        try:
            run_cycle(state, dry)
        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception as e:
            log.error(f"Cycle error: {e}")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
