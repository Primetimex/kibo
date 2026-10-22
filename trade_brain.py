"""
KIBO Brain — Central Trade Risk Gatekeeper
────────────────────────────────────────────────────────────────────────────
Single source of truth that EVERY agent (crypto, futures, penny, ORB, KIBO
strategy, explosion) checks before opening a trade, and reports back to
after closing one.

Why this exists:
  Right now each agent tracks its own open_positions/trades_today in
  isolation. There is no cross-agent view of total risk, no loss-streak
  cooldown, and no daily drawdown halt anywhere in KIBO. This module is
  that missing layer.

What it enforces:
  1. Daily drawdown halt   — if today's realized P&L across ALL agents
                              breaches -X% of account, everything stops.
  2. Loss-streak cooldown  — N consecutive losses (across all agents,
                              or you can scope per-agent) triggers a
                              cooldown window before new trades are allowed.
  3. Max trades/day        — global cap and per-agent cap.
  4. Duplicate-symbol guard— stops two agents from independently opening
                              the same symbol (double exposure you didn't
                              intend).
  5. POI re-entry limiter  — a specific price level (POI) gets blacklisted
                              after MAX_POI_REENTRIES failed attempts.

Integration pattern (per agent):

    from trade_brain import brain

    ok, reason = brain.approve_trade("crypto", symbol, direction,
                                      risk_amount=risk_dollars,
                                      poi_level=entry_zone_price)
    if not ok:
        log.info(f"BRAIN BLOCKED {symbol}: {reason}")
        continue   # or return, skip this signal

    ... place the order as normal ...

    # later, when the position closes:
    brain.record_close("crypto", symbol, pnl, poi_level=entry_zone_price)

State persists to kibo_brain_state.json so a dashboard restart doesn't
wipe today's loss streak / halt status.
"""

import os
import json
import threading
from pathlib import Path
from datetime import datetime, date, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

STATE_FILE = Path(os.getenv("KIBO_BRAIN_STATE_FILE", "kibo_brain_state.json"))
_lock = threading.RLock()

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG — tune these. All overridable via env vars so you don't have to
#  touch code to adjust risk limits.
# ─────────────────────────────────────────────────────────────────────────────
ACCOUNT_SIZE              = float(os.getenv("ACCOUNT_SIZE", "10000"))
DAILY_DRAWDOWN_HALT_PCT   = float(os.getenv("KIBO_DAILY_DD_PCT", "0.03"))    # 3%
LOSS_STREAK_LIMIT         = int(os.getenv("KIBO_LOSS_STREAK_LIMIT", "3"))
LOSS_STREAK_COOLDOWN_MIN  = int(os.getenv("KIBO_COOLDOWN_MIN", "60"))
MAX_TRADES_PER_DAY_GLOBAL = int(os.getenv("KIBO_MAX_TRADES_GLOBAL", "20"))
MAX_TRADES_PER_DAY_AGENT  = int(os.getenv("KIBO_MAX_TRADES_AGENT", "8"))
MAX_POI_REENTRIES         = int(os.getenv("KIBO_MAX_POI_REENTRIES", "2"))

DEFAULT_STATE = {
    "date": None,
    "daily_pnl": 0.0,
    "trades_today_global": 0,
    "trades_today_by_agent": {},
    "loss_streak": 0,
    "cooldown_until": None,        # ISO timestamp string, or None
    "halted": False,
    "halt_reason": None,
    "open_symbols": {},            # symbol -> agent that owns it
    "poi_attempts": {},            # "SYMBOL:LEVEL" -> failed-attempt count
    "blacklisted_pois": [],        # list of "SYMBOL:LEVEL" strings
    "log": [],                     # last 200 decisions, most recent last
}


class TradeBrain:
    def __init__(self):
        self._state = self._load()
        self._roll_day_if_needed()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            if STATE_FILE.exists():
                data = json.loads(STATE_FILE.read_text())
                merged = dict(DEFAULT_STATE)
                merged.update(data)
                return merged
        except Exception:
            pass
        return dict(DEFAULT_STATE)

    def _save(self):
        try:
            STATE_FILE.write_text(json.dumps(self._state, indent=2))
        except Exception as e:
            print(f"[BRAIN] WARNING: failed to persist state: {e}")

    # ── internal helpers ─────────────────────────────────────────────────
    def _roll_day_if_needed(self):
        today = date.today().isoformat()
        if self._state["date"] != today:
            self._state["date"] = today
            self._state["daily_pnl"] = 0.0
            self._state["trades_today_global"] = 0
            self._state["trades_today_by_agent"] = {}
            self._state["loss_streak"] = 0
            self._state["cooldown_until"] = None
            self._state["halted"] = False
            self._state["halt_reason"] = None
            # NOTE: open_symbols, poi_attempts, blacklisted_pois are NOT
            # reset daily — those track real open exposure and real price
            # levels, which don't care what day it is.
            self._log(f"New trading day started ({today}). Daily counters reset.")
            self._save()

    def _log(self, msg: str):
        entry = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — {msg}"
        self._state["log"].append(entry)
        self._state["log"] = self._state["log"][-200:]

    def _poi_key(self, symbol: str, poi_level: float) -> str:
        return f"{symbol.upper()}:{round(float(poi_level), 2)}"

    def _in_cooldown(self) -> tuple[bool, str]:
        cu = self._state.get("cooldown_until")
        if not cu:
            return False, ""
        until = datetime.fromisoformat(cu)
        if datetime.now() < until:
            remaining = int((until - datetime.now()).total_seconds() / 60) + 1
            return True, f"Loss-streak cooldown active ({remaining} min remaining)"
        return False, ""

    def _drawdown_breached(self) -> bool:
        limit = -abs(ACCOUNT_SIZE * DAILY_DRAWDOWN_HALT_PCT)
        return self._state["daily_pnl"] <= limit

    # ── PUBLIC API ───────────────────────────────────────────────────────
    def approve_trade(self, agent: str, symbol: str, direction: str,
                       risk_amount: float = 0.0,
                       poi_level: float = None) -> tuple[bool, str]:
        """
        Call this before placing any order. Returns (approved, reason).
        If approved, the symbol is reserved as open under this agent.
        """
        with _lock:
            self._roll_day_if_needed()
            symbol = symbol.upper()

            # 1. Already halted (drawdown)
            if self._state["halted"]:
                reason = f"HALTED: {self._state['halt_reason']}"
                self._log(f"BLOCKED {agent}/{symbol}: {reason}")
                return False, reason

            # 2. Loss-streak cooldown
            cooling, reason = self._in_cooldown()
            if cooling:
                self._log(f"BLOCKED {agent}/{symbol}: {reason}")
                return False, reason

            # NOTE: drawdown is only ever evaluated/set inside record_close()
            # and manual force_halt()/clear_halt(). approve_trade() just
            # trusts the persisted "halted" flag (checked in step 1 above).
            # This is deliberate: if it recomputed drawdown from daily_pnl
            # here too, a manual clear_halt() would be instantly re-halted
            # by this same check on the very next trade attempt, making the
            # override useless.

            # 3. Max trades per day (global + per-agent)
            if self._state["trades_today_global"] >= MAX_TRADES_PER_DAY_GLOBAL:
                reason = f"Global daily trade cap reached ({MAX_TRADES_PER_DAY_GLOBAL})"
                self._log(f"BLOCKED {agent}/{symbol}: {reason}")
                return False, reason
            agent_count = self._state["trades_today_by_agent"].get(agent, 0)
            if agent_count >= MAX_TRADES_PER_DAY_AGENT:
                reason = f"{agent} daily trade cap reached ({MAX_TRADES_PER_DAY_AGENT})"
                self._log(f"BLOCKED {agent}/{symbol}: {reason}")
                return False, reason

            # 4. Duplicate-symbol guard (another agent already holding this symbol)
            owner = self._state["open_symbols"].get(symbol)
            if owner and owner != agent:
                reason = f"{symbol} already open via {owner} — blocking duplicate exposure"
                self._log(f"BLOCKED {agent}/{symbol}: {reason}")
                return False, reason

            # 5. POI blacklist check
            if poi_level is not None:
                key = self._poi_key(symbol, poi_level)
                if key in self._state["blacklisted_pois"]:
                    reason = f"POI {key} blacklisted after {MAX_POI_REENTRIES} failed re-entries"
                    self._log(f"BLOCKED {agent}/{symbol}: {reason}")
                    return False, reason

            # ── APPROVED ──
            self._state["trades_today_global"] += 1
            self._state["trades_today_by_agent"][agent] = agent_count + 1
            self._state["open_symbols"][symbol] = agent
            self._log(f"APPROVED {agent}/{symbol} {direction} (risk=${risk_amount:.2f})")
            self._save()
            return True, "APPROVED"

        # unreachable

    def record_close(self, agent: str, symbol: str, pnl: float,
                      poi_level: float = None):
        """Call this when a position closes, win or lose."""
        with _lock:
            self._roll_day_if_needed()
            symbol = symbol.upper()

            self._state["daily_pnl"] = round(self._state["daily_pnl"] + pnl, 2)
            self._state["open_symbols"].pop(symbol, None)

            if pnl < 0:
                self._state["loss_streak"] += 1
                if self._state["loss_streak"] >= LOSS_STREAK_LIMIT:
                    until = datetime.now() + timedelta(minutes=LOSS_STREAK_COOLDOWN_MIN)
                    self._state["cooldown_until"] = until.isoformat()
                    self._log(
                        f"{self._state['loss_streak']} consecutive losses hit — "
                        f"cooldown until {until.strftime('%H:%M:%S')}"
                    )
            else:
                if self._state["loss_streak"] > 0:
                    self._log(f"Loss streak reset (was {self._state['loss_streak']}) after a win/scratch")
                self._state["loss_streak"] = 0

            # POI attempt tracking
            if poi_level is not None:
                key = self._poi_key(symbol, poi_level)
                if pnl < 0:
                    count = self._state["poi_attempts"].get(key, 0) + 1
                    self._state["poi_attempts"][key] = count
                    if count >= MAX_POI_REENTRIES and key not in self._state["blacklisted_pois"]:
                        self._state["blacklisted_pois"].append(key)
                        self._log(f"POI {key} blacklisted after {count} failed attempts")
                else:
                    # a win at this level clears its failed-attempt count
                    self._state["poi_attempts"].pop(key, None)

            # Re-check drawdown after this close
            if self._drawdown_breached() and not self._state["halted"]:
                self._state["halted"] = True
                self._state["halt_reason"] = (
                    f"Daily drawdown limit hit (${self._state['daily_pnl']:.2f} / "
                    f"-{DAILY_DRAWDOWN_HALT_PCT*100:.1f}% of ${ACCOUNT_SIZE:.0f})"
                )
                self._log(f"HALT TRIGGERED: {self._state['halt_reason']}")

            self._log(f"CLOSED {agent}/{symbol} pnl=${pnl:.2f} (daily_pnl=${self._state['daily_pnl']:.2f})")
            self._save()

    # ── manual overrides (wire these to dashboard buttons) ───────────────
    def force_halt(self, reason: str = "Manual halt"):
        with _lock:
            self._state["halted"] = True
            self._state["halt_reason"] = reason
            self._log(f"MANUAL HALT: {reason}")
            self._save()

    def clear_halt(self):
        with _lock:
            self._state["halted"] = False
            self._state["halt_reason"] = None
            self._log("Halt manually cleared")
            self._save()

    def reset_loss_streak(self):
        with _lock:
            self._state["loss_streak"] = 0
            self._state["cooldown_until"] = None
            self._log("Loss streak / cooldown manually cleared")
            self._save()

    def clear_poi_blacklist(self, symbol: str = None):
        with _lock:
            if symbol:
                symbol = symbol.upper()
                self._state["blacklisted_pois"] = [
                    k for k in self._state["blacklisted_pois"] if not k.startswith(f"{symbol}:")
                ]
                self._log(f"POI blacklist cleared for {symbol}")
            else:
                self._state["blacklisted_pois"] = []
                self._state["poi_attempts"] = {}
                self._log("POI blacklist cleared for all symbols")
            self._save()

    def set_account_size(self, size: float):
        global ACCOUNT_SIZE
        with _lock:
            ACCOUNT_SIZE = float(size)
            self._log(f"Account size set to ${ACCOUNT_SIZE:.2f}")

    # ── status for dashboard ─────────────────────────────────────────────
    def get_status(self) -> dict:
        with _lock:
            self._roll_day_if_needed()
            cooling, cooldown_reason = self._in_cooldown()
            return {
                "date": self._state["date"],
                "halted": self._state["halted"],
                "halt_reason": self._state["halt_reason"],
                "in_cooldown": cooling,
                "cooldown_reason": cooldown_reason,
                "cooldown_until": self._state["cooldown_until"],
                "daily_pnl": self._state["daily_pnl"],
                "daily_dd_limit": -round(ACCOUNT_SIZE * DAILY_DRAWDOWN_HALT_PCT, 2),
                "loss_streak": self._state["loss_streak"],
                "loss_streak_limit": LOSS_STREAK_LIMIT,
                "trades_today_global": self._state["trades_today_global"],
                "max_trades_global": MAX_TRADES_PER_DAY_GLOBAL,
                "trades_today_by_agent": dict(self._state["trades_today_by_agent"]),
                "open_symbols": dict(self._state["open_symbols"]),
                "blacklisted_pois": list(self._state["blacklisted_pois"]),
                "recent_log": self._state["log"][-25:],
            }


# Singleton — every agent imports this same instance
brain = TradeBrain()
