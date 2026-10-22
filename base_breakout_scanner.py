"""
KIBO Volume Spike Scanner
=========================
Finds stocks that match ALL of your rules and alerts you on Telegram:

  1. Market cap        $500M - $2B
  2. Volatility        ADR (average daily range, last 20 days) above 6%
  3. Strong sector     sector ETF is top 4 of 11 by 3-month return AND above its 50-day average
  4. Big move          up 30%+ over the last 3 months (63 trading days)
  5. Volume spike      today's volume is at least 2x the 50-day average

Nothing else is filtered. Thresholds live in CFG below.

The dashboard also shows a "watch list": stocks passing rules 1-4 whose volume
hasn't reached the spike level yet, sorted by how close they are.

USAGE
    python base_breakout_scanner.py                 run forever (scans while market is open)
    python base_breakout_scanner.py --once          one scan, then exit
    python base_breakout_scanner.py --dry-run       scan but never send Telegram
    python base_breakout_scanner.py --test-telegram send a test message and exit
    python base_breakout_scanner.py --explain XYZ   show pass/fail for every rule on one ticker

SETUP
    pip install -U yfinance pandas numpy requests python-dotenv tzdata
    Uses the same _env file as your other agents:  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

INTRADAY VOLUME
    During market hours "today's volume" is the volume traded so far (no projection),
    so an intraday alert means the 2x spike has already happened. Early in the session
    fewer stocks qualify; the count grows as the day goes on. A final scan runs after
    the close (4:10 PM ET) and marks each of today's alerts "held" or "faded".
"""

import os
import sys
import json
import time
import html
import logging
import argparse
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv("_env")
load_dotenv(HERE / "_env")

# -- Windows cp1252 can't print emoji -> force UTF-8 (known KIBO gotcha) ---------
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# -- Time zone (Windows needs `pip install tzdata` for zoneinfo) ------------------
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    import pytz
    ET = pytz.timezone("America/New_York")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# =============================================================================
#  SETTINGS  -- everything you might want to tune is here
# =============================================================================
CFG = dict(
    # 1. market cap
    mcap_min=500e6,
    mcap_max=2e9,
    # 2. volatility. "ADR" = avg of (high/low - 1) over N days; "ATR" = avg true range / price
    volatility_metric="ADR",
    min_volatility_pct=6.0,
    volatility_days=20,
    # 3. strong sector (sector ETF ranked by 3-month return)
    strong_top_n=4,
    sector_above_50dma=True,
    # 4. big move
    min_gain_3m_pct=30.0,
    bars_3m=63,
    # 5. volume spike (today's volume vs 50-day average of prior days)
    min_rvol=2.0,
    rvol_days=50,
    # data needed to measure the rules (3 months + today)
    min_bars=70,
)

SECTOR_ETFS = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Communication Services": "XLC",
}
US_EXCHANGES = ["NMS", "NGM", "NCM", "NYQ", "ASE"]

SCAN_MINUTES = 10                       # while the market is open
OPEN_SCAN_START = dtime(9, 40)          # skip the opening chaos
FINAL_SCAN_AT = dtime(16, 10)           # complete daily bars
ALERT_HISTORY_MAX = 60

RESULTS_FILE = HERE / "base_scanner_results.json"
STATE_FILE = HERE / "base_scanner_state.json"
UNIVERSE_FILE = HERE / "base_scanner_universe.json"
TRIGGER_FILE = HERE / "base_scanner_trigger"      # dashboard "Scan now" drops this file
LOG_FILE = HERE / "base_breakout_scanner.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    # When the dashboard launches us, stdout is redirected into this same log file,
    # so a console handler would write every line twice. Only add it in a terminal.
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")]
             + ([logging.StreamHandler()] if sys.stdout.isatty() else []),
)
log = logging.getLogger("base_breakout")


# =============================================================================
#  Small utilities
# =============================================================================
def now_et():
    return datetime.now(ET)


def market_state(dt):
    """'closed' (weekend) | 'premarket' | 'open' | 'afterhours'. Holidays are not modelled."""
    if dt.weekday() >= 5:
        return "closed"
    t = dt.time()
    if t < dtime(9, 30):
        return "premarket"
    if t < dtime(16, 0):
        return "open"
    return "afterhours"


def atomic_write_json(path: Path, obj):
    """Write via temp file + replace so the dashboard never reads a half-written file.
    Retries because Windows refuses to replace a file another process has open."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.25 * (attempt + 1))
    log.warning(f"Could not replace {path.name} (file locked) -- will retry next scan")


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def fmt_money(x):
    if x is None:
        return "n/a"
    if x >= 1e9:
        return f"${x / 1e9:.2f}B"
    return f"${x / 1e6:.0f}M"


# =============================================================================
#  Telegram
# =============================================================================
def send_telegram(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in _env)")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if r.status_code != 200:
            log.warning(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception:
        log.exception("Telegram send failed")
        return False


def format_alert(item: dict) -> str:
    sym = html.escape(item["symbol"])
    name = html.escape(item.get("name") or "")
    when = "INTRADAY (volume so far)" if item.get("partial") else "Closed"
    sec = item.get("sector_info") or {}
    lines = [
        f"\U0001F680 <b>VOLUME SPIKE: ${sym}</b>  <i>{name}</i>",
        f"<b>${item['price']:.2f}</b>  ({item['day_chg']:+.1f}% today)  |  mkt cap {fmt_money(item['mcap'])}",
        f"Volume <b>{item['rvol']:.1f}x</b> the 50-day average  |  {when}",
        f"3-month gain <b>{item['gain_3m']:+.0f}%</b>  |  {item['vol_metric_name']} {item['volatility']:.1f}%",
        f"Sector: {html.escape(item['sector'] or '?')} "
        f"(#{sec.get('rank', '?')} of 11, 3M {sec.get('ret_3m', 0):+.1f}%)",
        f"Score {item['score']}/100  |  "
        f"<a href=\"https://www.tradingview.com/chart/?symbol={sym}\">chart</a>",
    ]
    return "\n".join(lines)


# =============================================================================
#  Data: sector strength, universe, daily bars
# =============================================================================
def _extract(data, sym):
    """Pull one ticker's OHLCV frame out of a yfinance download (any column layout)."""
    if data is None or len(data) == 0:
        return None
    try:
        if isinstance(data.columns, pd.MultiIndex):
            lvl0 = data.columns.get_level_values(0)
            lvl1 = data.columns.get_level_values(1)
            if sym in lvl0:
                df = data[sym]
            elif sym in lvl1:
                df = data.xs(sym, axis=1, level=1)
            else:
                return None
        else:
            df = data
        df = df.copy()
        df.columns = [str(c).lower() for c in df.columns]
        need = ["open", "high", "low", "close", "volume"]
        if not all(c in df.columns for c in need):
            return None
        df = df[need].dropna(subset=["open", "high", "low", "close"])
        df["volume"] = df["volume"].fillna(0)
        return df if len(df) else None
    except Exception:
        return None


def fetch_daily_bars(symbols, period="9mo", chunk=80):
    import yfinance as yf
    out = {}
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        try:
            data = yf.download(part, period=period, interval="1d", auto_adjust=True,
                               group_by="ticker", threads=True, progress=False)
        except Exception:
            log.exception(f"Bar download failed (chunk {i // chunk + 1})")
            continue
        for s in part:
            df = _extract(data, s)
            if df is not None:
                out[s] = df
        time.sleep(0.5)
    return out


def prepare_bars(df, dt):
    """Handle the still-forming candle. Returns (df, partial_flag).
    A zero-volume forming candle is dropped (Yahoo reports volume=0 for it -- known bug
    that corrupted volume ratios before). A forming candle WITH volume is kept and flagged."""
    if df is None or len(df) < 2:
        return df, False
    st = market_state(dt)
    if df.index[-1].date() == dt.date() and st in ("open", "premarket"):
        if st == "premarket" or df["volume"].iloc[-1] <= 0:
            return df.iloc[:-1], False
        return df, True
    return df, False


def compute_sector_strength(cfg=CFG):
    """Rank the 11 sector ETFs by 3-month return; 'strong' = top-N and above the 50-day MA."""
    bars = fetch_daily_bars(list(SECTOR_ETFS.values()) + ["SPY"], period="9mo", chunk=20)
    if "SPY" not in bars:
        raise RuntimeError("Could not download SPY -- data source unavailable")
    n3 = cfg["bars_3m"]

    def ret3m(df):
        c = df["close"].values
        return (c[-1] / c[-1 - n3] - 1) * 100 if len(c) > n3 else None

    spy_ret = ret3m(bars["SPY"])
    rows = []
    for sector, etf in SECTOR_ETFS.items():
        df = bars.get(etf)
        if df is None or len(df) < 60:
            continue
        r = ret3m(df)
        if r is None:
            continue
        c = df["close"].values
        rows.append(dict(sector=sector, etf=etf, ret_3m=round(r, 2),
                         vs_spy=round(r - spy_ret, 2) if spy_ret is not None else None,
                         above_50dma=bool(c[-1] > c[-50:].mean())))
    rows.sort(key=lambda x: x["ret_3m"], reverse=True)
    for i, row in enumerate(rows, 1):
        row["rank"] = i
        row["strong"] = bool(i <= cfg["strong_top_n"]
                             and (row["above_50dma"] or not cfg["sector_above_50dma"]))
    if not rows:
        raise RuntimeError("No sector ETF data -- cannot rank sectors")
    return rows, spy_ret


def _screen_sector(sector, cfg=CFG):
    """Ask Yahoo's screener for US stocks in one sector inside the market-cap band."""
    import yfinance as yf
    from yfinance import EquityQuery
    q = EquityQuery("and", [
        EquityQuery("eq", ["region", "us"]),
        EquityQuery("eq", ["sector", sector]),
        EquityQuery("btwn", ["intradaymarketcap", cfg["mcap_min"], cfg["mcap_max"]]),
        EquityQuery("is-in", ["exchange"] + US_EXCHANGES),
    ])
    stocks, offset = {}, 0
    while offset < 1500:
        resp = yf.screen(q, offset=offset, size=250)      # default sort (by ticker) pages reliably
        quotes = (resp or {}).get("quotes", [])
        if not quotes:
            break
        for qt in quotes:
            sym, mcap = qt.get("symbol"), qt.get("marketCap")
            if not sym or not mcap or any(ch in sym for ch in "^="):
                continue
            if cfg["mcap_min"] <= mcap <= cfg["mcap_max"]:
                stocks[sym] = dict(name=qt.get("shortName") or qt.get("longName") or sym,
                                   sector=sector, mcap=float(mcap))
        if len(quotes) < 250:
            break
        offset += 250
        time.sleep(0.5)
    return stocks


def get_universe(sectors, cfg=CFG):
    """Screen only the strong sectors; cache per sector per day."""
    today = now_et().strftime("%Y-%m-%d")
    cache = read_json(UNIVERSE_FILE, {})
    if cache.get("date") != today:
        cache = {"date": today, "by_sector": {}}
    for sector in sectors:
        if sector in cache["by_sector"]:
            continue
        try:
            cache["by_sector"][sector] = _screen_sector(sector, cfg)
            log.info(f"Universe: {sector} -> {len(cache['by_sector'][sector])} stocks")
        except Exception:
            log.exception(f"Screener failed for {sector}")
    atomic_write_json(UNIVERSE_FILE, cache)
    universe = {}
    for sector in sectors:
        universe.update(cache["by_sector"].get(sector, {}))
    return universe


# =============================================================================
#  The rules engine (pure function of a price frame -- easy to test)
# =============================================================================
def _volatility(H, L, C, cfg):
    n = cfg["volatility_days"]
    if cfg["volatility_metric"].upper() == "ATR":
        n = 14
        prev = C[-n - 1:-1]
        tr = np.maximum.reduce([H[-n:] - L[-n:], np.abs(H[-n:] - prev), np.abs(L[-n:] - prev)])
        return float(tr.mean() / C[-1] * 100), "ATR"
    return float(np.mean(H[-n:] / L[-n:] - 1) * 100), "ADR"


RULE_KEYS = ["market_cap", "strong_sector", "volatility", "gain_3m", "volume_spike"]


def evaluate(df, mcap, sector, sector_map, cfg=CFG):
    """Run your 5 rules on one stock. `df` = daily OHLCV whose LAST row is today
    (possibly still forming). Returns dict with stage/checks/metrics."""
    res = dict(stage=None, checks={}, metrics={})
    if df is None or len(df) < cfg["min_bars"]:
        res["checks"]["enough_history"] = False
        return res
    h, l, c, v = (df[k].values.astype(float) for k in ("high", "low", "close", "volume"))
    H, L, C, V = h[:-1], l[:-1], c[:-1], v[:-1]        # history = everything before today
    price, prev_close, t_vol = float(c[-1]), float(c[-2]), float(v[-1])

    vol_pct, vol_name = _volatility(H, L, C, cfg)
    gain3m = (price / c[-1 - cfg["bars_3m"]] - 1) * 100
    avg_vol = float(V[-cfg["rvol_days"]:].mean())
    rvol = t_vol / avg_vol if avg_vol > 0 else 0.0
    sec_row = sector_map.get(sector) or {}

    checks = dict(
        market_cap=bool(mcap and cfg["mcap_min"] <= mcap <= cfg["mcap_max"]),
        strong_sector=bool(sec_row.get("strong")),
        volatility=vol_pct >= cfg["min_volatility_pct"],
        gain_3m=gain3m >= cfg["min_gain_3m_pct"],
        volume_spike=rvol >= cfg["min_rvol"],
    )
    first_four = all(checks[k] for k in RULE_KEYS[:4])
    stage = "SPIKE" if first_four and checks["volume_spike"] else "WATCH" if first_four else None

    res.update(stage=stage, checks=checks)
    res["metrics"] = dict(
        price=price, day_chg=(price / prev_close - 1) * 100, volume=t_vol, avg_volume=avg_vol,
        rvol=rvol, volatility=vol_pct, vol_metric_name=vol_name, gain_3m=gain3m, mcap=mcap,
    )
    return res


def funnel_steps(cfg=CFG):
    """Your rules in order, as (label, [check keys]). Counts are cumulative: a stock
    only counts toward a step if it passed every step above it."""
    return [
        (f"Market cap ${cfg['mcap_min'] / 1e6:.0f}M-${cfg['mcap_max'] / 1e9:.0f}B", ["market_cap"]),
        ("Strong sector", ["strong_sector"]),
        (f"{cfg['volatility_metric'].upper()} above {cfg['min_volatility_pct']:.0f}%", ["volatility"]),
        (f"Up {cfg['min_gain_3m_pct']:.0f}%+ in 3 months", ["gain_3m"]),
        (f"Volume {cfg['min_rvol']:.0f}x+ the 50-day average today", ["volume_spike"]),
    ]


def score_item(m, sec_row, cfg=CFG):
    """0-100 ranking aid (NOT a win probability): volume spike, 3M gain, volatility, sector rank."""
    s = 0.0
    s += min(max(m["rvol"] - 1, 0) / 4, 1) * 40
    s += min(m["gain_3m"] / 100, 1) * 25
    s += min(max(m["volatility"] - cfg["min_volatility_pct"], 0) / 6, 1) * 15
    rank = sec_row.get("rank", cfg["strong_top_n"])
    s += max(1 - (rank - 1) / max(cfg["strong_top_n"], 1), 0) * 20
    return int(round(min(s, 100)))


def build_item(sym, info, df, ev, partial, sector_map, cfg=CFG):
    m = ev["metrics"]
    sec_row = sector_map.get(info["sector"], {})
    tail = df.iloc[-min(100, len(df)):]
    return dict(
        symbol=sym, name=info.get("name"), sector=info["sector"], sector_info=sec_row,
        stage=ev["stage"], mcap=info["mcap"], price=round(m["price"], 4),
        day_chg=round(m["day_chg"], 2), rvol=round(m["rvol"], 2),
        volume=int(m["volume"]), avg_volume=int(m["avg_volume"]),
        volatility=round(m["volatility"], 2), vol_metric_name=m["vol_metric_name"],
        gain_3m=round(m["gain_3m"], 1), score=score_item(m, sec_row, cfg), partial=partial,
        bar_date=str(df.index[-1].date()),
        spark=dict(d=[str(x.date()) for x in tail.index],
                   c=[round(float(x), 3) for x in tail["close"]],
                   v=[int(x) for x in tail["volume"]],
                   avgv=int(m["avg_volume"])),
    )


# =============================================================================
#  One full scan
# =============================================================================
def load_state():
    st = read_json(STATE_FILE, {})
    st.setdefault("alerted", {})
    st.setdefault("history", [])
    return st


def prune_state(st):
    cutoff = (now_et() - timedelta(days=30)).strftime("%Y-%m-%d")
    st["alerted"] = {k: v for k, v in st["alerted"].items() if k.split("|")[-1] >= cutoff}
    st["history"] = st["history"][:ALERT_HISTORY_MAX]


def set_scanning(flag):
    prev = read_json(RESULTS_FILE, {})
    prev["scanning"] = flag
    if flag:
        prev["scan_started"] = now_et().strftime("%H:%M:%S")
    atomic_write_json(RESULTS_FILE, prev)


def run_scan(dry_run=False, reason="interval"):
    t0 = time.time()
    dt = now_et()
    mstate = market_state(dt)
    log.info(f"=== Scan start ({reason}) | market {mstate} ===")
    set_scanning(True)
    errors = []
    try:
        sectors, spy_ret = compute_sector_strength()
        sector_map = {r["sector"]: r for r in sectors}
        strong = [r["sector"] for r in sectors if r["strong"]]
        log.info("Strong sectors: " + (", ".join(strong) if strong else "none"))

        universe = get_universe(strong) if strong else {}
        log.info(f"Universe: {len(universe)} stocks in strong sectors")
        if strong and not universe:
            errors.append("Stock screener returned nothing (Yahoo may be rate-limiting). See base_breakout_scanner.log.")
        bars = fetch_daily_bars(list(universe.keys())) if universe else {}
        log.info(f"Bars downloaded: {len(bars)} / {len(universe)}")

        spikes, watch = [], []
        analyzed = 0
        steps = funnel_steps()
        funnel_counts = [0] * len(steps)
        any_partial = False
        for sym, info in universe.items():
            df, partial = prepare_bars(bars.get(sym), dt)
            if df is None:
                continue
            try:
                ev = evaluate(df, info["mcap"], info["sector"], sector_map)
            except Exception:
                log.exception(f"Evaluate failed for {sym}")
                errors.append(f"{sym}: evaluate failed")
                continue
            analyzed += 1
            for i, (_, keys) in enumerate(steps):
                if not all(ev["checks"].get(k) for k in keys):
                    break
                funnel_counts[i] += 1
            if not ev["stage"]:
                continue
            item = build_item(sym, info, df, ev, partial, sector_map)
            any_partial = any_partial or partial
            (spikes if ev["stage"] == "SPIKE" else watch).append(item)

        spikes.sort(key=lambda x: x["score"], reverse=True)
        watch.sort(key=lambda x: x["rvol"], reverse=True)

        # -- alerts (once per symbol per bar date) --------------------------------
        st = load_state()
        final_scan = (mstate == "afterhours" and dt.time() >= FINAL_SCAN_AT) or mstate == "closed"
        for item in spikes:
            key = f"{item['symbol']}|{item['bar_date']}"
            item["alerted"], item["alert_time"] = False, None
            if key in st["alerted"]:
                item["alerted"], item["alert_time"] = True, st["alerted"][key]
                continue
            if dry_run:
                log.info(f"[dry-run] would alert {item['symbol']} (score {item['score']})")
                continue
            if send_telegram(format_alert(item)):
                stamp = dt.strftime("%H:%M")
                st["alerted"][key] = stamp
                item["alerted"], item["alert_time"] = True, stamp
                st["history"].insert(0, dict(symbol=item["symbol"], date=item["bar_date"], time=stamp,
                                             price=item["price"], score=item["score"],
                                             rvol=item["rvol"], status="intraday" if item["partial"] else "closed"))
                log.info(f"ALERT sent: {item['symbol']} score {item['score']} rvol {item['rvol']}x")

        # -- reconcile today's intraday alerts against the final close --------------
        if final_scan:
            live = {i["symbol"] for i in spikes}
            for h in st["history"]:
                if h["date"] == dt.strftime("%Y-%m-%d") and h["status"] == "intraday":
                    h["status"] = "held" if h["symbol"] in live else "faded"
        prune_state(st)
        atomic_write_json(STATE_FILE, st)

        out = dict(
            updated=dt.isoformat(timespec="seconds"), updated_et=dt.strftime("%b %d, %H:%M:%S ET"),
            scanning=False, scan_seconds=round(time.time() - t0, 1), reason=reason,
            market_state=mstate, partial_bar=any_partial, interval_minutes=SCAN_MINUTES,
            spy_ret_3m=round(spy_ret, 2) if spy_ret is not None else None,
            universe_size=len(universe), analyzed=analyzed,
            funnel=[dict(label=lbl, passed=n) for (lbl, _), n in zip(steps, funnel_counts)],
            sectors=sectors, spikes=spikes, watch=watch[:40],
            history=st["history"][:ALERT_HISTORY_MAX],
            criteria=CFG, errors=errors[:10], dry_run=dry_run,
        )
        atomic_write_json(RESULTS_FILE, out)
        log.info(f"=== Scan done in {out['scan_seconds']}s | spikes {len(spikes)} | "
                 f"watch list {len(watch)} ===")
        return out
    except Exception as e:
        log.exception("Scan failed")
        prev = read_json(RESULTS_FILE, {})
        prev.update(scanning=False, errors=[f"Scan failed: {e}"], updated_et=dt.strftime("%b %d, %H:%M:%S ET"))
        atomic_write_json(RESULTS_FILE, prev)
        return None


# =============================================================================
#  --explain : pass/fail on every rule for a single ticker
# =============================================================================
RULE_LABELS = [
    ("market_cap", "Market cap $500M-$2B"),
    ("strong_sector", "Sector is strong"),
    ("volatility", "ADR above 6%"),
    ("gain_3m", "Up 30%+ in 3 months"),
    ("volume_spike", "Volume 2x+ the 50-day average today"),
]


def explain(sym):
    import yfinance as yf
    sym = sym.upper()
    print(f"Explaining {sym} ...")
    sectors, _ = compute_sector_strength()
    sector_map = {r["sector"]: r for r in sectors}
    try:
        info = yf.Ticker(sym).info
    except Exception:
        info = {}
    sector, mcap = info.get("sector"), info.get("marketCap")
    bars = fetch_daily_bars([sym])
    df, partial = prepare_bars(bars.get(sym), now_et())
    if df is None:
        print("No price data returned.")
        return
    ev = evaluate(df, mcap, sector, sector_map)
    m = ev["metrics"]
    print(f"\n{sym} | sector: {sector} | market cap: {fmt_money(mcap)} | last bar {df.index[-1].date()}"
          f"{' (forming)' if partial else ''}")
    if m:
        print(f"price ${m['price']:.2f} ({m['day_chg']:+.1f}%) | 3M {m['gain_3m']:+.1f}% | "
              f"{m['vol_metric_name']} {m['volatility']:.1f}% | rvol {m['rvol']:.2f}x")
    if sector in sector_map:
        r = sector_map[sector]
        print(f"sector rank #{r['rank']} (3M {r['ret_3m']:+.1f}%, above 50DMA: {r['above_50dma']}, strong: {r['strong']})")
    print()
    for key, label in RULE_LABELS:
        ok = ev["checks"].get(key)
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\nResult: " + {"SPIKE": "ALERT - every rule passes",
                             "WATCH": "watch list - rules 1-4 pass, no volume spike yet"}.get(ev["stage"], "no match"))


# =============================================================================
#  Main loop
# =============================================================================
def sleep_watch(seconds):
    """Sleep in short slices so a dashboard 'Scan now' click is picked up quickly."""
    end = time.time() + seconds
    while time.time() < end and not TRIGGER_FILE.exists():
        time.sleep(2)


def run_forever(dry_run=False, initial=True):
    log.info("Volume Spike Scanner online | Telegram "
             + ("configured" if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID else "NOT configured")
             + (" | DRY RUN" if dry_run else ""))
    last_scan, last_final_date, started = None, None, not initial
    while True:
        dt = now_et()
        st = market_state(dt)
        reason = None
        if TRIGGER_FILE.exists():
            try:
                TRIGGER_FILE.unlink()
            except Exception:
                pass
            reason = "manual"
        elif not started:
            reason = "startup"
        elif st == "open" and dt.time() >= OPEN_SCAN_START and \
                (last_scan is None or (dt - last_scan).total_seconds() >= SCAN_MINUTES * 60):
            reason = "interval"
        elif st == "afterhours" and dt.time() >= FINAL_SCAN_AT and last_final_date != dt.date():
            reason = "final"
        if reason:
            started = True
            run_scan(dry_run=dry_run, reason=reason)
            last_scan = now_et()
            if st == "afterhours" and dt.time() >= FINAL_SCAN_AT:
                last_final_date = dt.date()
        sleep_watch(20)


def main():
    ap = argparse.ArgumentParser(description="KIBO Volume Spike Scanner")
    ap.add_argument("--once", action="store_true", help="run one scan and exit")
    ap.add_argument("--dry-run", action="store_true", help="scan but do not send Telegram")
    ap.add_argument("--no-initial", action="store_true", help="don't scan immediately on startup")
    ap.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    ap.add_argument("--explain", metavar="TICKER", help="show pass/fail for every rule on one ticker")
    args = ap.parse_args()

    if args.test_telegram:
        ok = send_telegram("\u2705 <b>KIBO Volume Spike Scanner</b> is connected to this chat.")
        print("Telegram test:", "sent" if ok else "FAILED (check _env and that you've messaged the bot)")
        sys.exit(0 if ok else 1)
    if args.explain:
        explain(args.explain)
        return
    if args.once:
        run_scan(dry_run=args.dry_run, reason="once")
        return
    try:
        run_forever(dry_run=args.dry_run, initial=not args.no_initial)
    except KeyboardInterrupt:
        log.info("Stopped by user")


if __name__ == "__main__":
    main()
