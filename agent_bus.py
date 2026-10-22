"""
KIBO Agent Bus — Shared Coordination Layer
============================================
Lets the four trading agents (futures, crypto, penny, options/trading_agent)
"talk to each other" without needing a message broker — they're separate
processes on the same machine, so a small file-locked JSON blackboard is
the simplest thing that actually works here.

WHY THIS EXISTS (the money-safety problem it solves):
Each agent independently sizes trades against the FULL account balance —
e.g. RISK_PCT=1.5% of ACCOUNT_SIZE per trade, MAX_POSITIONS=3 per agent.
If all four agents are pointed at the same real Alpaca/TradeZero account
(which they are), and all four fire around the same time, actual combined
risk stacks silently: 4 agents x up to 3 positions x ~1.5% each can mean
far more than 1.5% of the account is on the line at once, with none of
them aware the others exist. Nobody sized for that.

What this module gives every agent:
  1. SHARED RISK BUDGET — a single account-wide risk cap that all agents
     draw from, instead of four separate ones that don't know about each
     other. New trades are rejected if the shared budget is already spent.
  2. CONFLICT DETECTION — block/flag one agent going LONG on a symbol
     while another agent is SHORT the same or a correlated symbol.
  3. REGIME BROADCAST — futures_agent watches MES/MNQ (S&P/Nasdaq proxies)
     every cycle; it can publish RISK-ON/RISK-OFF so equity-touching agents
     (trading_agent, penny_agent) get that context immediately instead of
     discovering it three signals too late.
  4. HEARTBEATS — dashboard can show which agents are alive and what
     they're currently seeing, in one place.

This is intentionally file-based (no new dependencies, no server to run) —
consistent with how open_positions is already persisted in each agent.
"""

import os
import json
import time
from pathlib import Path
from datetime import datetime

BUS_FILE  = Path("shared_state.json")
LOCK_FILE = Path("shared_state.lock")

# ── Shared account-wide risk cap ──────────────────────────────────────────────
# This is the ceiling ACROSS ALL AGENTS COMBINED, not per-agent. Override via
# .env if you want a looser/tighter shared cap. Default is intentionally
# conservative since it's guarding real (paper or live) account exposure.
MAX_TOTAL_RISK_PCT = float(os.getenv("AGENT_BUS_MAX_TOTAL_RISK_PCT", "0.05"))  # 5% of account, combined

# Manually curated correlation groups — symbols in the same group are treated
# as "the same bet" for conflict detection (e.g. don't let one agent go LONG
# SPY while another goes SHORT MES — that's the same trade fighting itself).
# Extend this as you add symbols/agents.
CORRELATION_GROUPS = [
    {"SPY", "MES"},
    {"QQQ", "MNQ"},
    {"GLD", "MGC"},
    {"USO", "MCL"},
]


def _correlated_symbols(sym: str) -> set:
    sym = sym.upper()
    for group in CORRELATION_GROUPS:
        if sym in group:
            return group
    return {sym}


# ─────────────────────────────────────────────────────────────────────────────
#  File locking — simple, cross-process, no extra dependencies
# ─────────────────────────────────────────────────────────────────────────────

def _acquire_lock(timeout=5.0):
    start = time.time()
    while True:
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(fd)
            return True
        except FileExistsError:
            if time.time() - start > timeout:
                # Stale lock protection — if it's been sitting for way too
                # long, another process probably crashed mid-write. Break it.
                try:
                    if time.time() - LOCK_FILE.stat().st_mtime > 10:
                        LOCK_FILE.unlink(missing_ok=True)
                        continue
                except Exception:
                    pass
                return False
            time.sleep(0.05)


def _release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _default_state() -> dict:
    return {
        "agents": {},          # agent_id -> {status, last_seen, regime_bias, watching}
        "positions": {},       # "agent_id:symbol" -> {agent, symbol, direction, risk_usd, entry, stop, target, opened}
        "market_regime": {"regime": "UNKNOWN", "updated": None, "source": None},
        "blocked_log": [],     # last 50 trades blocked by the bus, for dashboard visibility
    }


def _read_state() -> dict:
    if not BUS_FILE.exists():
        return _default_state()
    try:
        data = json.loads(BUS_FILE.read_text())
        for k, v in _default_state().items():
            data.setdefault(k, v)
        return data
    except Exception:
        return _default_state()


def _write_state(state: dict):
    tmp = BUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, default=str, indent=None))
    tmp.replace(BUS_FILE)  # atomic on POSIX


def _with_lock(fn):
    """Run fn(state) -> state under the file lock, then persist the result."""
    if not _acquire_lock():
        return None  # couldn't get the lock — caller should treat as "unknown, be conservative"
    try:
        state = _read_state()
        state = fn(state)
        _write_state(state)
        return state
    finally:
        _release_lock()


# ─────────────────────────────────────────────────────────────────────────────
#  Public API
# ─────────────────────────────────────────────────────────────────────────────

def heartbeat(agent_id: str, account_size: float, watching: list = None, extra: dict = None):
    """Call once per scan cycle so the dashboard can see the agent is alive."""
    def _mutate(state):
        entry = state["agents"].setdefault(agent_id, {})
        entry["status"]       = "running"
        entry["last_seen"]    = datetime.now().isoformat()
        entry["account_size"] = account_size
        entry["watching"]     = watching or entry.get("watching", [])
        if extra:
            entry.update(extra)
        return state
    _with_lock(_mutate)


def get_total_open_risk(exclude_agent: str = None) -> float:
    """Sum of risk_usd across all currently open positions, all agents."""
    state = _read_state()
    total = 0.0
    for key, pos in state["positions"].items():
        if exclude_agent and pos.get("agent") == exclude_agent:
            continue
        total += float(pos.get("risk_usd", 0) or 0)
    return total


def check_new_trade(agent_id: str, symbol: str, direction: str,
                     proposed_risk_usd: float, account_size: float) -> dict:
    """
    Call this BEFORE opening a position. Returns:
      {"allowed": bool, "reason": str}

    Checks, in order:
      1. Shared account-wide risk budget — would this trade push combined
         open risk (across ALL agents) past MAX_TOTAL_RISK_PCT of the account?
      2. Correlation conflict — is another agent already in the opposite
         direction on this symbol or a correlated one?
    """
    state = _read_state()

    # ── 1. Shared risk budget ──────────────────────────────────────────────
    current_risk = sum(float(p.get("risk_usd", 0) or 0) for p in state["positions"].values())
    cap = account_size * MAX_TOTAL_RISK_PCT
    if current_risk + proposed_risk_usd > cap:
        reason = (f"Shared risk budget exceeded — ${current_risk:,.0f} already at risk across all agents "
                  f"+ ${proposed_risk_usd:,.0f} proposed > ${cap:,.0f} cap ({MAX_TOTAL_RISK_PCT*100:.0f}% of account)")
        _log_block(agent_id, symbol, direction, reason)
        return {"allowed": False, "reason": reason}

    # ── 2. Correlation conflict ────────────────────────────────────────────
    group = _correlated_symbols(symbol)
    for key, pos in state["positions"].items():
        if pos.get("agent") == agent_id:
            continue
        if pos.get("symbol", "").upper() in group and pos.get("direction") != direction:
            reason = (f"Conflicts with {pos.get('agent')}'s {pos.get('direction')} position on "
                      f"{pos.get('symbol')} (correlated with {symbol}) — two agents fighting each other")
            _log_block(agent_id, symbol, direction, reason)
            return {"allowed": False, "reason": reason}

    return {"allowed": True, "reason": ""}


def _log_block(agent_id, symbol, direction, reason):
    def _mutate(state):
        state["blocked_log"].append({
            "agent": agent_id, "symbol": symbol, "direction": direction,
            "reason": reason, "ts": datetime.now().strftime("%H:%M:%S ET"),
        })
        state["blocked_log"] = state["blocked_log"][-50:]
        return state
    _with_lock(_mutate)


def register_position(agent_id: str, symbol: str, direction: str, risk_usd: float,
                       entry: float = None, stop: float = None, target: float = None):
    """Call AFTER a trade is successfully opened."""
    def _mutate(state):
        key = f"{agent_id}:{symbol}"
        state["positions"][key] = {
            "agent": agent_id, "symbol": symbol, "direction": direction,
            "risk_usd": risk_usd, "entry": entry, "stop": stop, "target": target,
            "opened": datetime.now().isoformat(),
        }
        return state
    _with_lock(_mutate)


def close_position(agent_id: str, symbol: str):
    """Call AFTER a position is closed (take profit, stop loss, or EOD)."""
    def _mutate(state):
        key = f"{agent_id}:{symbol}"
        state["positions"].pop(key, None)
        return state
    _with_lock(_mutate)


def publish_regime(agent_id: str, regime: str):
    """
    futures_agent calls this after classifying MES/MNQ trend each cycle —
    it's the agent best positioned to see broad market direction first.
    regime should be one of: RISK-ON, RISK-OFF, MIXED
    """
    def _mutate(state):
        state["market_regime"] = {
            "regime": regime, "updated": datetime.now().isoformat(), "source": agent_id,
        }
        return state
    _with_lock(_mutate)


def get_regime() -> dict:
    """Any agent can call this to get the latest broadcast regime."""
    state = _read_state()
    return state.get("market_regime", {"regime": "UNKNOWN", "updated": None, "source": None})


def get_full_state() -> dict:
    """Used by the dashboard to render the coordination panel."""
    return _read_state()
