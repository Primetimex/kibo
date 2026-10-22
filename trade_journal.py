"""
KIBO Trade Journal
Logs every trade with full ICC context, entry reason, outcome.
All agents write to this journal — single source of truth.
"""
import json, os, threading
from datetime import datetime
from pathlib import Path

JOURNAL_FILE = Path("trade_journal.json")
_lock = threading.Lock()

def _load():
    try:
        if JOURNAL_FILE.exists():
            return json.loads(JOURNAL_FILE.read_text())
    except: pass
    return []

def _save(trades):
    JOURNAL_FILE.write_text(json.dumps(trades, indent=2))

def log_entry(symbol, direction, entry, stop, target, size, icc_score,
              icc_trend, rr, agent, session="—"):
    """Call when a trade opens."""
    with _lock:
        trades = _load()
        trade = {
            "id":        f"KIBO-{len(trades)+1:04d}",
            "symbol":    symbol,
            "direction": direction,
            "entry":     round(entry, 4),
            "stop":      round(stop, 4),
            "target":    round(target, 4),
            "size":      size,
            "icc_score": icc_score,
            "icc_trend": icc_trend,
            "rr":        round(rr, 2),
            "agent":     agent,
            "session":   session,
            "open_time": datetime.now().isoformat(),
            "close_time": None,
            "exit_price": None,
            "pnl":        None,
            "outcome":    "OPEN",
            "close_reason": None,
        }
        trades.append(trade)
        _save(trades)
        return trade["id"]

def log_exit(trade_id, exit_price, reason="MANUAL"):
    """Call when a trade closes."""
    with _lock:
        trades = _load()
        for t in trades:
            if t["id"] == trade_id and t["outcome"] == "OPEN":
                t["exit_price"]  = round(exit_price, 4)
                t["close_time"]  = datetime.now().isoformat()
                t["close_reason"] = reason
                pnl = (exit_price - t["entry"]) * t["size"]
                if t["direction"] == "SHORT":
                    pnl = -pnl
                t["pnl"] = round(pnl, 2)
                if reason == "TAKE_PROFIT":
                    t["outcome"] = "WIN"
                elif reason == "STOP_LOSS":
                    t["outcome"] = "LOSS"
                elif pnl > 0:
                    t["outcome"] = "WIN"
                elif pnl < 0:
                    t["outcome"] = "LOSS"
                else:
                    t["outcome"] = "BREAKEVEN"
                break
        _save(trades)

def get_all():
    with _lock:
        return _load()

def get_stats():
    trades = [t for t in get_all() if t["outcome"] != "OPEN"]
    if not trades:
        return {"total":0,"wins":0,"losses":0,"win_rate":0,"total_pnl":0,"avg_win":0,"avg_loss":0,"best":0,"worst":0}
    wins   = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    pnls   = [t["pnl"] for t in trades if t["pnl"] is not None]
    win_pnls  = [t["pnl"] for t in wins  if t["pnl"] is not None]
    loss_pnls = [t["pnl"] for t in losses if t["pnl"] is not None]
    return {
        "total":     len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  round(len(wins)/len(trades)*100, 1) if trades else 0,
        "total_pnl": round(sum(pnls), 2),
        "avg_win":   round(sum(win_pnls)/len(win_pnls), 2) if win_pnls else 0,
        "avg_loss":  round(sum(loss_pnls)/len(loss_pnls), 2) if loss_pnls else 0,
        "best":      round(max(pnls), 2) if pnls else 0,
        "worst":     round(min(pnls), 2) if pnls else 0,
    }
