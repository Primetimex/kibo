# -*- coding: utf-8 -*-
"""
KIBO Doctor — automated bug detection, safe repair, and reporting
════════════════════════════════════════════════════════════════════════════
Continuously checks the KIBO app for bugs and problems, fixes what is SAFE
to fix automatically, and reports everything to Telegram + a JSON report
the dashboard can display.

WHAT IT CHECKS
  Code level (detect + report, never auto-edit):
    C1  Syntax errors in every .py file
    C2  Hardcoded secrets (API keys, tokens) in source
    C3  open() calls without encoding= (the Windows cp1252 mojibake trap)
    C4  Agents that place trades but never write to the trade journal
    C5  journal_entry without matching journal_exit usage in an agent
    C6  Bare `except:` clauses that can hide real errors (informational)

  Data level (detect + AUTO-REPAIR with timestamped backups):
    D1  Corrupt/unparseable JSON state files  -> backed up + reset
    D2  Orphaned journal trades (OPEN > N days) -> closed as EXPIRED
        (only with --repair-journal, since it mutates trade history)

  Runtime level (detect + report):
    R1  Stale scanner feeds (agent probably down)
    R2  Dashboard not responding on its port
    R3  Recent ERROR / Traceback lines in *.log files
    R4  Brain state sanity (halted with no reason, negative counters)

WHY NO AUTO CODE EDITS
  This is a trading system. An automated regex "fix" that subtly changes
  order logic is more dangerous than any bug it targets. Code findings
  come with exact file:line and a suggested fix; you (or Claude/JARVIS)
  apply them deliberately. Data files are different — a corrupt state
  JSON has one safe remedy (backup + reset), so those ARE automated.

USAGE
    python kibo_doctor.py                  # one full checkup, report + alert
    python kibo_doctor.py --watch          # run every 30 min forever
    python kibo_doctor.py --repair-journal # also close orphaned OPEN trades
    python kibo_doctor.py --quiet          # no Telegram, console/report only

OUTPUT
    kibo_doctor_report.json  — full findings, dashboard-readable
    Telegram summary         — only when something is found (no spam)
"""

import os
import re
import ast
import json
import time
import argparse
import logging
import requests
from pathlib import Path
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
KIBO_DIR         = Path(os.getenv("KIBO_DIR", "."))
REPORT_FILE      = Path("kibo_doctor_report.json")
DASHBOARD_URL    = os.getenv("KIBO_DASHBOARD_URL", "http://localhost:5000")
STALE_FEED_MIN   = float(os.getenv("DOCTOR_STALE_FEED_MIN", "60"))
ORPHAN_DAYS      = int(os.getenv("DOCTOR_ORPHAN_DAYS", "5"))
WATCH_MIN        = int(os.getenv("DOCTOR_WATCH_MIN", "30"))
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

STATE_JSONS = ["kibo_brain_state.json", "scanner_executor_state.json",
               "scanner_results.json", "crypto_scanner_results.json",
               "explosion_results.json", "penny_watchlist.json",
               "trade_journal.json"]

FEED_FILES = {"scanner_results.json": "market scanner",
              "crypto_scanner_results.json": "crypto scanner",
              "explosion_results.json": "explosion agent"}

# Files that place orders and therefore should journal their trades
TRADING_AGENTS = ["trading_agent.py", "crypto_agent.py", "futures_agent.py",
                  "penny_agent.py", "orb_agent.py", "orb_agent_v2.py",
                  "kibo_strategy_agent.py", "explosion_agent.py",
                  "scanner_executor.py"]

SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),               "OpenAI-style API key"),
    (re.compile(r"(?i)(api_key|apikey|secret|token)\s*=\s*[\"'][A-Za-z0-9_\-]{20,}[\"']"),
                                                            "hardcoded credential assignment"),
    (re.compile(r"\d{9,10}:[A-Za-z0-9_\-]{30,}"),          "Telegram bot token"),
]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [DOCTOR] %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("kibo_doctor")


def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                            "parse_mode": "HTML"}, timeout=8)
    except Exception:
        pass


def finding(severity: str, category: str, title: str, detail: str,
             file: str = None, line: int = None, fix: str = None,
             auto_fixed: bool = False) -> dict:
    return {"severity": severity, "category": category, "title": title,
            "detail": detail, "file": file, "line": line,
            "suggested_fix": fix, "auto_fixed": auto_fixed}


# ═════════════════════════════════════════════════════════════════════════════
#  CODE-LEVEL CHECKS
# ═════════════════════════════════════════════════════════════════════════════

def _py_files():
    return sorted(p for p in KIBO_DIR.glob("*.py")
                  if p.name != "kibo_doctor.py" and not p.name.startswith("test_"))


def check_syntax(findings: list):
    for p in _py_files():
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
            ast.parse(src)
        except SyntaxError as e:
            findings.append(finding(
                "CRITICAL", "C1-syntax",
                f"Syntax error in {p.name}",
                f"{e.msg}", file=p.name, line=e.lineno,
                fix="This file will crash on import — fix before any agent that imports it can run."))
        except Exception as e:
            findings.append(finding("WARNING", "C1-syntax",
                                     f"Could not parse {p.name}", str(e), file=p.name))


def check_secrets(findings: list):
    for p in _py_files():
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for i, ln in enumerate(lines, 1):
            if "os.getenv" in ln and '"' not in ln.split("os.getenv")[1][:60].replace("'", '"').split(")")[0][20:]:
                pass
            for pat, label in SECRET_PATTERNS:
                m = pat.search(ln)
                if m and "os.getenv" not in ln.split(m.group(0))[0][-40:] or (m and "or \"" in ln):
                    if m:
                        findings.append(finding(
                            "CRITICAL", "C2-secret",
                            f"Hardcoded secret in {p.name}",
                            f"Line {i}: {label} found in source ({m.group(0)[:14]}…). "
                            f"Anyone with this file can use the key.",
                            file=p.name, line=i,
                            fix="Move to .env, load with os.getenv(), REVOKE the exposed key, "
                                "and make the code hard-fail if the env var is missing."))
                    break


def check_encoding_traps(findings: list):
    """open() without encoding= — bit KIBO before via cp1252 mojibake."""
    pat = re.compile(r"\bopen\(\s*[^)]*\)")
    for p in _py_files():
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        hits = []
        for i, ln in enumerate(lines, 1):
            if ln.strip().startswith("#"):
                continue
            for m in pat.finditer(ln):
                call = m.group(0)
                if "encoding" in call or "'rb'" in call or '"rb"' in call \
                   or "'wb'" in call or '"wb"' in call:
                    continue
                hits.append(i)
        if hits:
            findings.append(finding(
                "WARNING", "C3-encoding",
                f"open() without encoding= in {p.name}",
                f"{len(hits)} call(s) at line(s) {hits[:8]}. On Windows these "
                f"default to cp1252 — the exact cause of KIBO's earlier mojibake bug.",
                file=p.name, line=hits[0],
                fix='Add encoding="utf-8" to each text-mode open() call.'))


def check_journal_wiring(findings: list):
    for name in TRADING_AGENTS:
        p = KIBO_DIR / name
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        has_entry = "journal_entry(" in src or "log_entry(" in src
        has_exit  = "journal_exit(" in src or "log_exit(" in src
        # only count real call sites, not import lines / stub defs
        entry_calls = len(re.findall(r"^\s*(?:trade_id\s*=\s*)?(?:journal_entry|log_entry)\(",
                                      src, re.M))
        exit_calls  = len(re.findall(r"^\s*(?:journal_exit|log_exit)\(", src, re.M))
        if not has_entry:
            findings.append(finding(
                "WARNING", "C4-journal",
                f"{name} never journals trades",
                "This agent can place orders but writes nothing to trade_journal — "
                "its performance is invisible.",
                file=name,
                fix="Import trade_journal and call log_entry() on open, log_exit() on close."))
        elif entry_calls > 0 and exit_calls == 0:
            findings.append(finding(
                "CRITICAL", "C5-journal-exit",
                f"{name} logs entries but never exits",
                "Trades from this agent stay OPEN in the journal forever with no P&L — "
                "win rate and profitability cannot be computed.",
                file=name,
                fix="Call journal_exit(trade_id, exit_price, reason) in every close path "
                    "(take-profit, stop-loss, manual close)."))


def check_bare_excepts(findings: list):
    total = {}
    for p in _py_files():
        src = p.read_text(encoding="utf-8", errors="replace")
        n = len(re.findall(r"^\s*except\s*:\s*(#.*)?$", src, re.M))
        if n:
            total[p.name] = n
    if total:
        worst = sorted(total.items(), key=lambda kv: -kv[1])[:5]
        findings.append(finding(
            "INFO", "C6-bare-except",
            f"Bare except: clauses in {len(total)} files",
            "These swallow ALL errors silently — bugs can hide here. Worst: "
            + ", ".join(f"{k} ({v})" for k, v in worst),
            fix="Catch specific exceptions, or at minimum `except Exception as e:` + log it."))


# ═════════════════════════════════════════════════════════════════════════════
#  DATA-LEVEL CHECKS (auto-repair with backup)
# ═════════════════════════════════════════════════════════════════════════════

def _backup(p: Path) -> str:
    bak = p.with_suffix(p.suffix + f".bak-{datetime.now():%Y%m%d-%H%M%S}")
    bak.write_bytes(p.read_bytes())
    return bak.name


def check_state_files(findings: list, repairs: list):
    for name in STATE_JSONS:
        p = KIBO_DIR / name
        if not p.exists():
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            bak = _backup(p)
            if name == "trade_journal.json":
                # never reset the journal automatically — too valuable
                findings.append(finding(
                    "CRITICAL", "D1-corrupt-json",
                    f"{name} is corrupt (NOT auto-reset)",
                    f"Parse error: {e}. Backed up to {bak}. The journal holds trade "
                    f"history so it was not reset — recover it manually.",
                    file=name, fix=f"Inspect {bak} and repair by hand."))
            else:
                p.write_text("{}" if name.endswith("state.json") else "[]",
                              encoding="utf-8")
                repairs.append(f"{name}: corrupt JSON — backed up to {bak} and reset")
                findings.append(finding(
                    "WARNING", "D1-corrupt-json",
                    f"{name} was corrupt — auto-repaired",
                    f"Parse error: {e}. Backed up to {bak}, file reset to empty. "
                    f"The owning agent will rebuild it.",
                    file=name, auto_fixed=True))


def check_orphan_trades(findings: list, repairs: list, repair_journal: bool):
    p = KIBO_DIR / "trade_journal.json"
    if not p.exists():
        return
    try:
        trades = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    cutoff = datetime.now() - timedelta(days=ORPHAN_DAYS)
    orphans = []
    for t in trades:
        if t.get("outcome") != "OPEN":
            continue
        try:
            opened = datetime.fromisoformat(t["open_time"])
        except Exception:
            continue
        if opened < cutoff:
            orphans.append(t)

    if not orphans:
        return

    if repair_journal:
        bak = _backup(p)
        for t in trades:
            if t in orphans:
                t["outcome"] = "EXPIRED"
                t["close_reason"] = "DOCTOR_ORPHAN_CLEANUP"
                t["close_time"] = datetime.now().isoformat()
        p.write_text(json.dumps(trades, indent=2), encoding="utf-8")
        repairs.append(f"trade_journal.json: {len(orphans)} orphaned OPEN trades "
                       f"marked EXPIRED (backup {bak})")
        findings.append(finding(
            "WARNING", "D2-orphans",
            f"{len(orphans)} orphaned trades cleaned up",
            f"OPEN for more than {ORPHAN_DAYS} days with no exit — marked EXPIRED. "
            f"Backup: {bak}. Root cause is almost certainly a missing journal_exit() "
            f"call in the owning agent (see C5 findings).",
            auto_fixed=True))
    else:
        agents = {}
        for t in orphans:
            agents[t.get("agent", "?")] = agents.get(t.get("agent", "?"), 0) + 1
        findings.append(finding(
            "WARNING", "D2-orphans",
            f"{len(orphans)} journal trades stuck OPEN > {ORPHAN_DAYS} days",
            f"By agent: {agents}. These poison your stats. Run with "
            f"--repair-journal to close them as EXPIRED (with backup).",
            fix="python kibo_doctor.py --repair-journal"))


# ═════════════════════════════════════════════════════════════════════════════
#  RUNTIME CHECKS
# ═════════════════════════════════════════════════════════════════════════════

def check_stale_feeds(findings: list):
    for fname, owner in FEED_FILES.items():
        p = KIBO_DIR / fname
        if not p.exists():
            continue
        age = (time.time() - p.stat().st_mtime) / 60
        if age > STALE_FEED_MIN:
            findings.append(finding(
                "WARNING", "R1-stale-feed",
                f"{owner} feed is stale",
                f"{fname} last written {age:.0f} min ago (limit {STALE_FEED_MIN:.0f}). "
                f"The {owner} process is probably not running.",
                file=fname, fix=f"Restart the {owner} process and check its log."))


def check_dashboard(findings: list):
    try:
        r = requests.get(DASHBOARD_URL, timeout=5)
        if r.status_code >= 500:
            findings.append(finding(
                "CRITICAL", "R2-dashboard",
                "Dashboard returning server errors",
                f"{DASHBOARD_URL} responded {r.status_code}.",
                fix="Check dashboard.py console for a traceback."))
    except Exception as e:
        findings.append(finding(
            "WARNING", "R2-dashboard",
            "Dashboard unreachable",
            f"Could not reach {DASHBOARD_URL}: {e}",
            fix="Start dashboard.py, or set KIBO_DASHBOARD_URL if the port differs."))


def check_logs(findings: list):
    for p in sorted(KIBO_DIR.glob("*.log")):
        try:
            tail = p.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        except Exception:
            continue
        errors = [ln for ln in tail if "Traceback" in ln or " ERROR " in ln or "ERROR:" in ln]
        if errors:
            findings.append(finding(
                "WARNING", "R3-log-errors",
                f"{p.name}: {len(errors)} recent error lines",
                f"Latest: {errors[-1][:160]}",
                file=p.name, fix=f"Inspect {p.name} around the errors."))


def check_brain_sanity(findings: list):
    p = KIBO_DIR / "kibo_brain_state.json"
    if not p.exists():
        return
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return   # D1 already handles corrupt files
    if s.get("halted") and not s.get("halt_reason"):
        findings.append(finding(
            "WARNING", "R4-brain",
            "Brain halted with no reason recorded",
            "Trading is blocked but halt_reason is empty — likely a manual "
            "state edit or a bug.",
            file=p.name, fix="Clear via brain.clear_halt() or the dashboard RESUME button."))
    for k in ("trades_today_global", "loss_streak"):
        if isinstance(s.get(k), (int, float)) and s[k] < 0:
            findings.append(finding(
                "WARNING", "R4-brain", f"Brain state has negative {k}",
                f"{k}={s[k]} — state corruption.", file=p.name,
                fix="Delete kibo_brain_state.json (it resets safely at day roll)."))


# ═════════════════════════════════════════════════════════════════════════════
#  RUN + REPORT
# ═════════════════════════════════════════════════════════════════════════════

SEV_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
SEV_ICON  = {"CRITICAL": "🔴", "WARNING": "🟡", "INFO": "🔵"}


def run_checkup(repair_journal: bool = False, quiet: bool = False) -> dict:
    findings, repairs = [], []

    check_syntax(findings)
    check_secrets(findings)
    check_encoding_traps(findings)
    check_journal_wiring(findings)
    check_bare_excepts(findings)
    check_state_files(findings, repairs)
    check_orphan_trades(findings, repairs, repair_journal)
    check_stale_feeds(findings)
    check_dashboard(findings)
    check_logs(findings)
    check_brain_sanity(findings)

    findings.sort(key=lambda f: SEV_ORDER.get(f["severity"], 9))
    counts = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1

    report = {
        "ts": datetime.now().isoformat(),
        "dir": str(KIBO_DIR.resolve()),
        "summary": counts,
        "auto_repairs": repairs,
        "findings": findings,
    }
    REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Console
    log.info(f"Checkup complete: {counts.get('CRITICAL',0)} critical, "
             f"{counts.get('WARNING',0)} warnings, {counts.get('INFO',0)} info, "
             f"{len(repairs)} auto-repairs")
    for f in findings:
        loc = f" [{f['file']}" + (f":{f['line']}" if f.get("line") else "") + "]" if f.get("file") else ""
        log.info(f"  {SEV_ICON[f['severity']]} {f['title']}{loc}")

    # Telegram — only when there's something to say
    if not quiet and (findings or repairs):
        lines = [f"🩺 <b>KIBO DOCTOR</b> — "
                 f"{counts.get('CRITICAL',0)}🔴 {counts.get('WARNING',0)}🟡 {counts.get('INFO',0)}🔵"]
        for f in findings[:6]:
            lines.append(f"{SEV_ICON[f['severity']]} {f['title']}")
        if len(findings) > 6:
            lines.append(f"…and {len(findings)-6} more (see dashboard)")
        if repairs:
            lines.append(f"🔧 Auto-repaired: {len(repairs)} item(s)")
        send_telegram("\n".join(lines))

    return report


def main():
    parser = argparse.ArgumentParser(description="KIBO Doctor — bug detection and repair")
    parser.add_argument("--watch", action="store_true", help=f"Run every {WATCH_MIN} min forever")
    parser.add_argument("--repair-journal", action="store_true",
                        help="Also close orphaned OPEN journal trades as EXPIRED (with backup)")
    parser.add_argument("--quiet", action="store_true", help="No Telegram alerts")
    args = parser.parse_args()

    if args.watch:
        log.info(f"KIBO Doctor watching — checkup every {WATCH_MIN} min")
        while True:
            try:
                run_checkup(args.repair_journal, args.quiet)
            except Exception as e:
                log.error(f"Checkup crashed: {e}")
            time.sleep(WATCH_MIN * 60)
    else:
        report = run_checkup(args.repair_journal, args.quiet)
        raise SystemExit(1 if report["summary"].get("CRITICAL") else 0)


if __name__ == "__main__":
    main()
