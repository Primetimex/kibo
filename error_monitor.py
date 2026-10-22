"""
error_monitor.py
─────────────────────────────────────────────────────────────────────────────
KIBO Error Capture Agent

Purpose:
    Tails every agent's log file in real time, pulls out ERROR / CRITICAL
    lines and full tracebacks, and writes them to one central error store —
    without touching any of the existing 10 agents. Nothing about the
    existing chain of command (structure_rules -> trade_brain -> JARVIS)
    changes; this only reads log files that already exist.

Outputs (created in the working directory KIBO runs from):
    kibo_errors.jsonl        - append-only, one JSON object per captured error
    kibo_error_summary.json  - rolling summary for the dashboard (counts,
                                last error per agent, last 50 errors)
    error_monitor.log        - this agent's own log (not watched, to avoid
                                a feedback loop)

Run it:
    python error_monitor.py            # continuous, polls every 2s
    python error_monitor.py --once      # one pass over current logs, then exit
    python error_monitor.py --tail 20   # also print the last 20 errors found

Optional Telegram alerts (reuses the same _env vars as the other agents):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
─────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import sys
import json
import time
import hashlib
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import deque, OrderedDict

from dotenv import load_dotenv

# KIBO's config file is named `_env`, not `.env` — loading the wrong default
# file is a known cause of silent credential failures across this project.
load_dotenv("_env")

# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 0 — Config
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent

# Every log file currently produced by a KIBO agent/scanner. Add new ones
# here as new agents come online — nothing else needs to change.
WATCHED_LOGS = {
    "trading_agent":       "trading_agent.log",
    "crypto_agent":        "crypto_agent.log",
    "futures_agent":       "futures_agent.log",
    "penny_agent":         "penny_agent.log",
    "orb_agent_v2":        "orb_v2_agent.log",
    "orb_agent":           "orb_agent.log",
    "zone_scanner":        "zone_scanner.log",
    "explosion_agent":     "explosion_agent.log",
    "market_scanner":      "market_scanner.log",
    "crypto_scanner":      "crypto_scanner.log",
    "kibo_strategy_agent": "kibo_strategy_agent.log",
}

ERROR_STORE   = BASE_DIR / "kibo_errors.jsonl"
SUMMARY_STORE = BASE_DIR / "kibo_error_summary.json"
STATE_STORE   = BASE_DIR / "kibo_error_monitor_state.json"   # file offsets, survives restarts

POLL_SECONDS         = 2
DEDUP_WINDOW_MIN     = 10          # same error signature within this window = not re-alerted
TELEGRAM_MIN_GAP_S   = 20          # never send more than 1 telegram message per this many seconds
SUMMARY_MAX_RECENT   = 50
MAX_BYTES_PER_POLL   = 2_000_000   # cap how much new log data we read in one poll (bytes)
MAX_RAW_ERRORS_PER_POLL = 500      # above this, we're almost certainly in a crash loop

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Matches the standard KIBO log line format:
#   2026-07-19 09:41:03,221  ERROR     message text here
LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]?\d*)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?P<msg>.*)$"
)

TRACEBACK_START_RE = re.compile(r"^Traceback \(most recent call last\):")

# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 1 — This agent's own logging (kept separate from watched logs)
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "error_monitor.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("error_monitor")


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 2 — Telegram (best-effort, never fatal)
# ─────────────────────────────────────────────────────────────────────────────

_last_telegram_sent = 0.0

def send_telegram(message: str):
    global _last_telegram_sent
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    now = time.time()
    if now - _last_telegram_sent < TELEGRAM_MIN_GAP_S:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=5,
        ).raise_for_status()
        _last_telegram_sent = now
    except Exception as e:
        log.warning(f"Telegram alert failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 3 — Persistent state (file offsets so restarts don't re-scan everything)
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_STORE.exists():
        try:
            return json.loads(STATE_STORE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("State file unreadable, starting fresh")
    return {}

def save_state(state: dict):
    try:
        STATE_STORE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"Could not save state: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 4 — Parsing new lines out of one log file
# ─────────────────────────────────────────────────────────────────────────────

def read_new_lines(path: Path, offset: int, max_bytes: int = MAX_BYTES_PER_POLL) -> tuple[list[str], int]:
    """Read whatever's been appended since `offset`. Handles log rotation/
    truncation by resetting to 0 if the file got smaller than our offset.
    Caps how much is read in one go so a crash-looping agent that writes
    megabytes of log between polls can't stall this monitor — we just pick
    up the rest on the next poll(s)."""
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if size < offset:
        offset = 0  # file was rotated/truncated
    if size == offset:
        return [], offset
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read(max_bytes)
        new_offset = offset + len(raw)
    text = raw.decode("utf-8", errors="replace")
    # Avoid ending mid-line when we hit the byte cap
    if new_offset < size and "\n" in text:
        text, _, leftover = text.rpartition("\n")
        new_offset -= len(leftover.encode("utf-8", errors="replace"))
    return text.splitlines(), new_offset


def extract_errors(agent: str, lines: list[str]) -> list[dict]:
    """Turn raw new log lines into structured error records, stitching
    multi-line tracebacks onto the ERROR line that triggered them."""
    errors = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        m = LOG_LINE_RE.match(line)

        is_error_level = m and m.group("level") in ("ERROR", "CRITICAL")
        is_bare_traceback = (not m) and TRACEBACK_START_RE.match(line)

        if is_error_level or is_bare_traceback:
            ts = m.group("ts") if m else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            level = m.group("level") if m else "ERROR"
            message = m.group("msg") if m else line

            block = [line]
            j = i + 1

            # If the very next line kicks off a traceback, swallow the whole
            # thing: indented "File ..." / code lines, then exactly one more
            # unindented line, which is the "ExceptionType: message" summary.
            if j < n and TRACEBACK_START_RE.match(lines[j]):
                block.append(lines[j])
                j += 1
                while j < n and lines[j].startswith(("  ", "\t")):
                    block.append(lines[j])
                    j += 1
                if j < n and lines[j].strip() and not LOG_LINE_RE.match(lines[j]):
                    block.append(lines[j])
                    j += 1

            traceback_text = "\n".join(block[1:]) if len(block) > 1 else ""
            if traceback_text:
                # last block line is the exception summary if we captured one
                last_line = block[-1].strip()
                if last_line and not last_line.startswith(("  ", "\t")) and last_line != block[1].strip():
                    message = last_line

            errors.append({
                "agent": agent,
                "timestamp": ts,
                "level": level,
                "message": message.strip(),
                "traceback": traceback_text,
                "raw": "\n".join(block),
            })
            i = j
        else:
            i += 1
    return errors


def signature(err: dict) -> str:
    """Stable hash used for de-duplication — same agent + same error text
    within the dedup window counts as 'already seen'."""
    key = f"{err['agent']}::{err['message']}"
    return hashlib.sha1(key.encode("utf-8", errors="replace")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 5 — Central store + dashboard summary
# ─────────────────────────────────────────────────────────────────────────────

def append_error(err: dict):
    try:
        with open(ERROR_STORE, "a", encoding="utf-8") as f:
            f.write(json.dumps(err) + "\n")
    except Exception as e:
        log.error(f"Failed writing to {ERROR_STORE}: {e}")


def update_summary(err: dict, recent: deque):
    recent.appendleft(err)
    summary_path = SUMMARY_STORE
    try:
        summary = {}
        if summary_path.exists():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        summary = {}

    by_agent = summary.get("by_agent", {})
    agent_stats = by_agent.get(err["agent"], {"count": 0, "last_error": None, "last_seen": None})
    agent_stats["count"] += 1
    agent_stats["last_error"] = err["message"][:300]
    agent_stats["last_seen"] = err["timestamp"]
    by_agent[err["agent"]] = agent_stats

    summary["by_agent"] = by_agent
    summary["total_captured"] = summary.get("total_captured", 0) + 1
    summary["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary["recent"] = list(recent)[:SUMMARY_MAX_RECENT]

    try:
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    except Exception as e:
        log.error(f"Failed writing {summary_path}: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 6 — Main loop
# ─────────────────────────────────────────────────────────────────────────────

def run(once: bool = False, print_tail: int = 0):
    state = load_state()
    offsets = state.get("offsets", {})
    seen = {}  # signature -> last-seen datetime, for dedup / alert throttling

    # Load existing recent errors from summary so we don't lose history on restart
    recent = deque(maxlen=SUMMARY_MAX_RECENT)
    if SUMMARY_STORE.exists():
        try:
            existing = json.loads(SUMMARY_STORE.read_text(encoding="utf-8"))
            for e in reversed(existing.get("recent", [])):
                recent.appendleft(e)
        except Exception:
            pass

    log.info(f"Watching {len(WATCHED_LOGS)} log files: {', '.join(WATCHED_LOGS.values())}")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — captured errors will not be alerted, only stored")

    captured_this_run = []

    try:
        while True:
            any_new = False
            for agent, filename in WATCHED_LOGS.items():
                path = BASE_DIR / filename
                offset = offsets.get(filename, 0)
                lines, new_offset = read_new_lines(path, offset, max_bytes=MAX_BYTES_PER_POLL)
                if not lines:
                    continue
                offsets[filename] = new_offset
                errors = extract_errors(agent, lines)
                if len(errors) > MAX_RAW_ERRORS_PER_POLL:
                    log.warning(
                        f"[{agent}] {len(errors)} error lines in one poll — "
                        f"looks like a crash loop, collapsing duplicates"
                    )

                # Collapse identical errors seen within this same poll into a
                # single record with a count, instead of writing one line per
                # occurrence. This is what actually stops a tight crash loop
                # (same exception raised hundreds of times a second) from
                # flooding kibo_errors.jsonl / error_monitor.log — dedup on
                # its own only suppressed telegram alerts, not disk writes.
                batch = OrderedDict()
                for err in errors:
                    sig = signature(err)
                    if sig in batch:
                        batch[sig]["occurrences"] += 1
                        batch[sig]["last_timestamp"] = err["timestamp"]
                    else:
                        err["signature"] = sig
                        err["occurrences"] = 1
                        err["last_timestamp"] = err["timestamp"]
                        batch[sig] = err

                for sig, err in batch.items():
                    any_new = True
                    now = datetime.now()
                    last_seen = seen.get(sig)
                    is_new_signature = (
                        last_seen is None
                        or now - last_seen > timedelta(minutes=DEDUP_WINDOW_MIN)
                    )
                    seen[sig] = now
                    err["duplicate"] = not is_new_signature

                    append_error(err)
                    update_summary(err, recent)
                    captured_this_run.append(err)

                    tag = "🔴 CRITICAL" if err["level"] == "CRITICAL" else "🟠 ERROR"
                    suffix = f" (x{err['occurrences']} this poll)" if err["occurrences"] > 1 else ""
                    log.info(f"[{err['agent']}] {err['level']}: {err['message'][:200]}{suffix}")

                    if is_new_signature:
                        send_telegram(
                            f"{tag} — <b>{err['agent']}</b>\n{err['message'][:500]}{suffix}"
                        )

            if any_new:
                save_state({"offsets": offsets})

            if once:
                break
            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        log.info("Stopped by user")
    finally:
        save_state({"offsets": offsets})

    if print_tail:
        shown = captured_this_run[-print_tail:] if captured_this_run else list(recent)[:print_tail]
        print(f"\n--- last {len(shown)} errors ---")
        for e in shown:
            print(f"[{e.get('timestamp')}] {e.get('agent')} {e.get('level')}: {e.get('message')[:200]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KIBO central error capture agent")
    parser.add_argument("--once", action="store_true", help="scan current logs once and exit")
    parser.add_argument("--tail", type=int, default=0, help="print the last N captured errors before exiting")
    args = parser.parse_args()
    run(once=args.once, print_tail=args.tail)
