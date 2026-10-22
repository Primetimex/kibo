"""
KIBO Pre-Explosion Agent
========================
Scans for penny stocks that are about to make a big move BEFORE it happens.
Runs 24/7 — most valuable window is 7 AM - 9:30 AM ET pre-market.

Detection Formula:
  CATALYST + LOW FLOAT + VOLUME BUILDING + PRICE COMPRESSION = EXPLOSION IMMINENT

Signals sent via Telegram BEFORE the stock moves.
"""

import os, sys, time, requests, json, re, logging
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv("_env")

# ── Force UTF-8 console output (Windows cp1252 can't render emoji/arrows,
# which was spamming "Logging error" tracebacks on every alert) ─────────────
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLYGON_KEY      = os.getenv("POLYGON_API_KEY","")

# ── Settings ──────────────────────────────────────────────────────────────────
SCAN_INTERVAL    = 5 * 60    # scan every 5 min (2 min during market hours)
MIN_PRICE        = 0.10
MAX_PRICE        = 10.00     # wider range — some near-explosions are $5-10
MIN_SCORE        = 60        # minimum explosion score to alert
ALERT_COOLDOWN   = 60 * 60   # don't re-alert same stock within 1 hour
WATCHLIST_FILE   = "explosion_watchlist.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler("explosion_agent.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ── Catalyst Keywords (weighted by impact) ────────────────────────────────────
CATALYST_WEIGHTS = {
    # Highest impact — price can 2x-10x
    "fda approved":        50, "fda approval":         50,
    "breakthrough therapy":45, "emergency use":         45,
    "merger":              45, "acquisition":           45,
    "buyout":              45, "going private":         45,
    "short squeeze":       40, "gamma squeeze":         40,

    # High impact
    "liquidity":           35, "term loan":             35,
    "debt agreement":      35, "covenant waiver":       35,
    "lender agreement":    35, "financing":             30,
    "phase 3":             35, "phase 2":               25,
    "clinical trial":      25, "positive results":      30,
    "contract awarded":    35, "government contract":   35,
    "partnership":         25, "licensing":             25,

    # Medium impact
    "earnings beat":       25, "revenue beat":          25,
    "raised guidance":     20, "upgrade":               20,
    "insider buying":      20, "share buyback":         20,
    "dividend":            15, "special dividend":      25,

    # Sector/macro catalysts
    "ai":                  15, "artificial intelligence":15,
    "bitcoin":             15, "crypto":                15,
    "cannabis":            20, "marijuana":             20,
    "uranium":             20, "lithium":               15,
    "electric vehicle":    15, "ev":                    15,
}

# Negative keywords that REDUCE score
NEGATIVE_KEYWORDS = {
    "bankruptcy":          -30, "chapter 11":           -30,
    "delisting":           -25, "going concern":        -20,
    "fraud":               -30, "sec investigation":    -25,
    "class action":        -20, "revenue miss":         -15,
    "earnings miss":       -15, "lowered guidance":     -15,
    "downgrade":           -10,
}

# ── Alert cooldown tracker ─────────────────────────────────────────────────────
_alerted = {}  # sym -> timestamp

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=6
        ).raise_for_status()
    except Exception as e:
        log.warning(f"Telegram failed: {e}")

# ── Data Fetchers ─────────────────────────────────────────────────────────────

def fetch_quote(sym):
    """Get current price, volume, float from Yahoo Finance."""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
            headers=HEADERS,
            params={"interval": "5m", "range": "2d"},
            timeout=8
        )
        if r.status_code != 200:
            return None
        data = r.json()
        res  = data.get("chart", {}).get("result", [])
        if not res:
            return None
        meta = res[0].get("meta", {})
        price    = float(meta.get("regularMarketPrice") or 0)
        prev     = float(meta.get("chartPreviousClose") or price or 1)
        vol      = int(meta.get("regularMarketVolume") or 0)
        avg_vol  = int(meta.get("averageDailyVolume10Day") or vol or 1)
        pre_price= float(meta.get("preMarketPrice") or price)
        pre_chg  = ((pre_price - prev) / prev * 100) if prev > 0 else 0
        day_chg  = ((price - prev) / prev * 100) if prev > 0 else 0

        # Project full-day volume
        now = datetime.now()
        mins_elapsed = max((now.hour - 9) * 60 + now.minute - 30, 1)
        frac = min(mins_elapsed / 390, 1.0)
        proj_vol = vol / frac if frac > 0 and now.hour >= 9 else vol
        rel_vol  = round(proj_vol / avg_vol, 1) if avg_vol > 0 else 1.0

        # Get OHLCV bars
        quotes = res[0].get("indicators", {}).get("quote", [{}])[0]
        closes = [c for c in (quotes.get("close") or []) if c is not None]
        highs  = [h for h in (quotes.get("high") or []) if h is not None]
        lows   = [l for l in (quotes.get("low") or []) if l is not None]
        vols   = [v for v in (quotes.get("volume") or []) if v is not None]

        return {
            "sym":        sym,
            "price":      price,
            "prev_close": prev,
            "day_chg":    round(day_chg, 2),
            "pre_chg":    round(pre_chg, 2),
            "volume":     vol,
            "avg_vol":    avg_vol,
            "rel_vol":    rel_vol,
            "proj_vol":   int(proj_vol),
            "pre_price":  pre_price,
            "closes":     closes[-40:] if closes else [],
            "highs":      highs[-40:]  if highs  else [],
            "lows":       lows[-40:]   if lows   else [],
            "vols":       vols[-40:]   if vols   else [],
        }
    except Exception as e:
        log.debug(f"{sym} quote error: {e}")
        return None

def fetch_fundamentals(sym):
    """Get float, short interest from Yahoo Finance statistics."""
    try:
        r = requests.get(
            f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}",
            headers=HEADERS,
            params={"modules": "defaultKeyStatistics,summaryDetail"},
            timeout=8
        )
        if r.status_code != 200:
            return {}
        stats  = r.json().get("quoteSummary", {}).get("result", [{}])[0]
        ks     = stats.get("defaultKeyStatistics", {})
        sd     = stats.get("summaryDetail", {})
        return {
            "float":       ks.get("floatShares",    {}).get("raw", 0) or 0,
            "shares_out":  ks.get("sharesOutstanding",{}).get("raw",0) or 0,
            "short_pct":   ks.get("shortPercentOfFloat",{}).get("raw",0) or 0,
            "short_ratio": ks.get("shortRatio",    {}).get("raw", 0) or 0,
            "mktcap":      sd.get("marketCap",      {}).get("raw", 0) or 0,
        }
    except:
        return {}

def fetch_news(sym):
    """Get recent news headlines for a symbol."""
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            headers=HEADERS,
            params={"q": sym, "newsCount": 8, "quotesCount": 0},
            timeout=8
        )
        if r.status_code != 200:
            return []
        items = r.json().get("news", [])
        news  = []
        for item in items[:8]:
            title = item.get("title", "")
            pub   = item.get("providerPublishTime", 0)
            src   = item.get("publisher", "")
            hrs   = round((time.time() - pub) / 3600, 1) if pub else 99
            if title:
                news.append({"title": title, "hrs": hrs, "src": src})
        return news
    except:
        return []

# ── Scoring Engine ─────────────────────────────────────────────────────────────

def fetch_stocktwits(sym):
    """
    Fetch StockTwits sentiment and message volume.
    Free API, no key needed.
    """
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json",
            headers=HEADERS, timeout=8
        )
        if r.status_code != 200:
            return None
        data     = r.json()
        messages = data.get("messages", [])
        symbol   = data.get("symbol", {})

        # Count bullish/bearish sentiments
        bull = sum(1 for m in messages if m.get("entities",{}).get("sentiment",{}).get("basic") == "Bullish")
        bear = sum(1 for m in messages if m.get("entities",{}).get("sentiment",{}).get("basic") == "Bearish")
        total= len(messages)

        # Get watchlist count (how many people watching)
        watchers = symbol.get("watchlist_count", 0)

        # Get recent message texts
        texts = [m.get("body","")[:100] for m in messages[:5]]

        return {
            "total_msgs":  total,
            "bull":        bull,
            "bear":        bear,
            "watchers":    watchers,
            "sentiment":   "BULLISH" if bull > bear else "BEARISH" if bear > bull else "NEUTRAL",
            "bull_pct":    round(bull/total*100) if total > 0 else 0,
            "texts":       texts,
        }
    except Exception as e:
        log.debug(f"{sym} StockTwits error: {e}")
        return None

def fetch_reddit_mentions(sym):
    """
    Check Reddit for stock mentions using pushshift/reddit search.
    Looks at r/pennystocks, r/RobinhoodPennyStocks, r/Daytrading, r/wallstreetbets
    """
    try:
        # Search Reddit via their JSON API
        subreddits = "pennystocks+RobinhoodPennyStocks+Daytrading+wallstreetbets+stocks"
        r = requests.get(
            f"https://www.reddit.com/search.json",
            headers={**HEADERS, "User-Agent": "KIBO-Bot/1.0"},
            params={
                "q":       f"${sym} OR {sym}",
                "sort":    "new",
                "limit":   25,
                "t":       "day",  # last 24 hours
            },
            timeout=10
        )
        if r.status_code != 200:
            return None

        posts = r.json().get("data",{}).get("children",[])
        if not posts:
            return None

        now = time.time()
        recent = []
        hot    = []

        for post in posts:
            p = post.get("data",{})
            created = p.get("created_utc", 0)
            score   = p.get("score", 0)
            title   = p.get("title","")
            sub     = p.get("subreddit","")
            hrs_ago = (now - created) / 3600

            if hrs_ago < 24:
                recent.append({
                    "title":   title[:80],
                    "score":   score,
                    "sub":     sub,
                    "hrs_ago": round(hrs_ago, 1),
                })
                if score >= 10:
                    hot.append(title[:60])

        # Count mentions in last 1hr vs last 24hr
        last_1hr  = sum(1 for p in recent if p["hrs_ago"] < 1)
        last_6hr  = sum(1 for p in recent if p["hrs_ago"] < 6)
        total_ups  = sum(p["score"] for p in recent)

        if not recent:
            return None

        return {
            "total_posts":  len(recent),
            "last_1hr":     last_1hr,
            "last_6hr":     last_6hr,
            "total_ups":    total_ups,
            "hot_posts":    hot[:3],
            "velocity":     last_1hr,  # posts per hour right now
            "trending":     last_1hr >= 3 or total_ups >= 50,
        }
    except Exception as e:
        log.debug(f"{sym} Reddit error: {e}")
        return None

def fetch_twitter_mentions(sym):
    """
    Check for Twitter/X mentions via Nitter (free Twitter frontend).
    No API key needed.
    """
    try:
        r = requests.get(
            f"https://nitter.net/search",
            headers={**HEADERS, "Accept": "text/html"},
            params={"q": f"${sym}", "f": "tweets"},
            timeout=8
        )
        if r.status_code != 200:
            return None

        html  = r.text
        # Count tweet mentions
        count = html.count('class="tweet-content"')
        # Look for cashtag trend indicators
        trending = "$" + sym in html and count >= 5

        return {
            "mentions":  count,
            "trending":  trending,
        }
    except:
        return None

def score_social(sym):
    """
    Score social media activity for a symbol.
    Returns (score, reasons, social_data)
    """
    score   = 0
    reasons = []
    data    = {}

    # ── StockTwits ─────────────────────────────────────────────────────────────
    st = fetch_stocktwits(sym)
    if st:
        data["stocktwits"] = st
        msgs     = st["total_msgs"]
        bull_pct = st["bull_pct"]
        watchers = st["watchers"]

        # Volume of messages
        if msgs >= 20:   score += 25; reasons.append(f"StockTwits: {msgs} msgs (VERY ACTIVE)")
        elif msgs >= 10: score += 15; reasons.append(f"StockTwits: {msgs} msgs (active)")
        elif msgs >= 5:  score += 8;  reasons.append(f"StockTwits: {msgs} msgs")

        # Sentiment
        if bull_pct >= 80: score += 15; reasons.append(f"StockTwits: {bull_pct}% BULLISH")
        elif bull_pct >= 60: score += 8

        # Watchers (momentum indicator)
        if watchers >= 10000: score += 10; reasons.append(f"StockTwits: {watchers:,} watching")
        elif watchers >= 5000: score += 5

        time.sleep(0.5)

    # ── Reddit ─────────────────────────────────────────────────────────────────
    rd = fetch_reddit_mentions(sym)
    if rd:
        data["reddit"] = rd
        posts    = rd["total_posts"]
        last_1hr = rd["last_1hr"]
        trending = rd["trending"]

        if last_1hr >= 5:  score += 30; reasons.append(f"Reddit: {last_1hr} posts in LAST HOUR 🔥")
        elif last_1hr >= 3: score += 20; reasons.append(f"Reddit: {last_1hr} posts last hour")
        elif last_1hr >= 1: score += 10; reasons.append(f"Reddit: {last_1hr} post last hour")

        if posts >= 10:   score += 10; reasons.append(f"Reddit: {posts} posts today")
        elif posts >= 5:  score += 5

        if rd["hot_posts"]:
            reasons.append(f"Reddit hot: {rd['hot_posts'][0][:50]}")

        time.sleep(0.5)

    # ── Twitter/X ──────────────────────────────────────────────────────────────
    tw = fetch_twitter_mentions(sym)
    if tw and tw["mentions"] >= 5:
        data["twitter"] = tw
        if tw["trending"]:
            score += 20; reasons.append(f"Twitter: {sym} TRENDING ({tw['mentions']} tweets)")
        elif tw["mentions"] >= 10:
            score += 10; reasons.append(f"Twitter: {tw['mentions']} mentions")

    return min(score, 50), reasons, data

def score_catalyst(news_list):
    """Score news catalyst. Returns (score, reason, best_headline)."""
    if not news_list:
        return 0, "No recent news", ""

    best_score  = 0
    best_title  = ""
    total_score = 0

    for item in news_list:
        title  = item["title"]
        lower  = title.lower()
        hrs    = item["hrs"]
        score  = 0

        # Positive keywords
        for kw, weight in CATALYST_WEIGHTS.items():
            if kw in lower:
                score += weight

        # Negative keywords
        for kw, weight in NEGATIVE_KEYWORDS.items():
            if kw in lower:
                score += weight  # weight is negative

        # Freshness bonus — news under 4 hours gets a boost
        if hrs < 1:    score = int(score * 1.5)
        elif hrs < 4:  score = int(score * 1.2)
        elif hrs < 12: score = int(score * 1.0)
        elif hrs > 48: score = int(score * 0.3)

        if score > best_score:
            best_score = score
            best_title = title[:80]

        total_score += score

    return min(total_score, 100), best_title, best_title

def score_float(fundamentals):
    """Score float size. Smaller float = higher score."""
    f = fundamentals.get("float", 0)
    if f <= 0:
        return 0, "Unknown float"
    if f < 5_000_000:    return 40, f"Tiny float {f/1e6:.1f}M shares"
    if f < 10_000_000:   return 35, f"Very low float {f/1e6:.1f}M shares"
    if f < 20_000_000:   return 28, f"Low float {f/1e6:.1f}M shares"
    if f < 50_000_000:   return 20, f"Medium float {f/1e6:.0f}M shares"
    if f < 100_000_000:  return 10, f"High float {f/1e6:.0f}M shares"
    return 0, f"Too high float {f/1e6:.0f}M shares"

def score_short_interest(fundamentals):
    """Score short interest — high short + catalyst = squeeze."""
    pct   = fundamentals.get("short_pct", 0) * 100
    ratio = fundamentals.get("short_ratio", 0)
    if pct >= 30:  return 30, f"HIGH SHORT {pct:.0f}% — squeeze potential"
    if pct >= 20:  return 20, f"Elevated short {pct:.0f}%"
    if pct >= 10:  return 10, f"Moderate short {pct:.0f}%"
    return 0, ""

def score_volume(quote):
    """Score volume momentum."""
    rel  = quote.get("rel_vol", 0)
    proj = quote.get("proj_vol", 0)
    avg  = quote.get("avg_vol", 1)
    chg  = abs(quote.get("day_chg", 0))

    score = 0
    reason = ""

    if rel >= 10:   score += 35; reason = f"EXTREME vol {rel}x avg"
    elif rel >= 5:  score += 25; reason = f"Very high vol {rel}x avg"
    elif rel >= 3:  score += 15; reason = f"High vol {rel}x avg"
    elif rel >= 2:  score += 8;  reason = f"Above avg vol {rel}x avg"
    elif rel >= 1.5:score += 4;  reason = f"Slight vol {rel}x avg"

    # Pre-market volume bonus
    pre_chg = abs(quote.get("pre_chg", 0))
    if pre_chg >= 20: score += 20; reason += f" | Pre-mkt +{pre_chg:.0f}%"
    elif pre_chg >= 10: score += 12
    elif pre_chg >= 5:  score += 6

    return min(score, 50), reason

def score_price_action(quote):
    """
    Detect price compression (coiling) before explosion.
    Tight range + decreasing volume = spring loading.
    """
    closes = quote.get("closes", [])
    lows   = quote.get("lows", [])
    highs  = quote.get("highs", [])
    vols   = quote.get("vols", [])

    if len(closes) < 10:
        return 0, ""

    score  = 0
    reason = ""

    # Check for price compression in last 5 bars
    recent_high = max(highs[-5:]) if len(highs) >= 5 else 0
    recent_low  = min(lows[-5:])  if len(lows)  >= 5 else 0
    price       = closes[-1] if closes else 0

    if price > 0 and recent_low > 0:
        range_pct = (recent_high - recent_low) / recent_low * 100
        if range_pct < 3:
            score  += 20
            reason  = f"TIGHT COMPRESSION {range_pct:.1f}% range"
        elif range_pct < 5:
            score  += 10
            reason  = f"Coiling {range_pct:.1f}% range"

    # Check if volume is drying up (accumulation phase)
    if len(vols) >= 10:
        recent_vols = vols[-5:]
        older_vols  = vols[-10:-5]
        avg_recent  = sum(recent_vols) / len(recent_vols) if recent_vols else 0
        avg_older   = sum(older_vols)  / len(older_vols)  if older_vols  else 1
        if avg_recent < avg_older * 0.6:
            score  += 15
            reason += " | Vol drying up (accumulation)"

    # Check if price is near recent high (breakout imminent)
    if len(highs) >= 20:
        week_high = max(highs[-20:])
        if price > 0 and week_high > 0:
            near_high_pct = (price / week_high) * 100
            if near_high_pct >= 98:
                score += 20; reason += " | AT RESISTANCE — breakout imminent"
            elif near_high_pct >= 95:
                score += 10; reason += " | Near resistance"

    return min(score, 40), reason

def calculate_explosion_score(sym, quote, fundamentals, news):
    """
    Calculate overall pre-explosion score (0-100).
    Higher = more likely to explode soon.
    """
    cat_score,   cat_reason,  headline = score_catalyst(news)
    float_score, float_reason           = score_float(fundamentals)
    short_score, short_reason           = score_short_interest(fundamentals)
    vol_score,   vol_reason             = score_volume(quote)
    pa_score,    pa_reason              = score_price_action(quote)
    soc_score,   soc_reasons, soc_data  = score_social(sym)

    total = cat_score + float_score + short_score + vol_score + pa_score + soc_score

    # Bonus: all signals aligning together
    if cat_score >= 30 and vol_score >= 20 and float_score >= 20:
        total += 20
    # Social + catalyst alignment bonus
    if soc_score >= 20 and cat_score >= 20:
        total += 15

    total = min(total, 100)

    all_reasons = [r for r in [cat_reason, float_reason, short_reason, vol_reason, pa_reason] if r]
    all_reasons += soc_reasons

    return {
        "score":        total,
        "cat_score":    cat_score,
        "float_score":  float_score,
        "short_score":  short_score,
        "vol_score":    vol_score,
        "pa_score":     pa_score,
        "soc_score":    soc_score,
        "headline":     headline,
        "reasons":      all_reasons,
        "social":       soc_data,
        "float":        fundamentals.get("float", 0),
        "short_pct":    fundamentals.get("short_pct", 0) * 100,
        "mktcap":       fundamentals.get("mktcap", 0),
    }

# ── Alert System ──────────────────────────────────────────────────────────────

def alert_explosion(sym, quote, result):
    """Send Telegram alert when pre-explosion conditions detected."""
    now = time.time()

    # Cooldown check
    if sym in _alerted and now - _alerted[sym] < ALERT_COOLDOWN:
        return
    _alerted[sym] = now

    score   = result["score"]
    price   = quote["price"]
    chg     = quote["day_chg"]
    rel_vol = quote["rel_vol"]
    pre_chg = quote.get("pre_chg", 0)
    fl      = result["float"]
    short   = result["short_pct"]

    # Score emoji
    if score >= 85:   emoji = "🚨🚨🚨"
    elif score >= 75: emoji = "🚨🚨"
    elif score >= 65: emoji = "🚨"
    else:             emoji = "⚡"

    float_str  = f"{fl/1e6:.1f}M" if fl > 0 else "Unknown"
    short_str  = f"{short:.0f}%" if short > 0 else "—"
    mktcap_str = f"${result['mktcap']/1e6:.0f}M" if result['mktcap'] > 0 else "—"
    reason_str = "\n  ".join(f"• {r}" for r in result["reasons"][:4]) if result["reasons"] else "• Multiple signals aligning"

    # Social media summary
    social    = result.get("social", {})
    st_data   = social.get("stocktwits", {})
    rd_data   = social.get("reddit", {})
    soc_score = result.get("soc_score", 0)

    social_str = ""
    if st_data:
        social_str += f"StockTwits: {st_data.get('total_msgs',0)} msgs {st_data.get('bull_pct',0)}% bull\n"
    if rd_data:
        social_str += f"Reddit: {rd_data.get('total_posts',0)} posts · {rd_data.get('last_1hr',0)} last hr\n"
    if not social_str:
        social_str = "No social activity yet\n"

    msg = (
        f"{emoji} <b>PRE-EXPLOSION ALERT — {sym}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Score    : <b>{score}/100</b>\n"
        f"Price    : ${price:.4f}  ({'+' if chg>=0 else ''}{chg:.1f}% today)\n"
        f"Pre-Mkt  : {'+' if pre_chg>=0 else ''}{pre_chg:.1f}%\n"
        f"Volume   : {rel_vol}x avg daily\n"
        f"Float    : {float_str} shares\n"
        f"Short %  : {short_str}\n"
        f"Mkt Cap  : {mktcap_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Why it's about to move:</b>\n"
        f"  {reason_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Social Buzz (score {soc_score}):</b>\n"
        f"  {social_str}"
        f"<b>Catalyst:</b>\n"
        f"  {result['headline'][:100] if result['headline'] else 'See news'}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ This is a SIGNAL not a trade. Check ICC before entering."
    )

    send_telegram(msg)
    log.info(f"🚨 EXPLOSION ALERT sent for {sym} (score {score})")

# ── Scanner ───────────────────────────────────────────────────────────────────

def get_scan_universe():
    """
    Get list of stocks to scan.
    Combines: top movers + most active + existing watchlist.
    """
    syms = set()

    # Load saved watchlist
    try:
        if os.path.exists(WATCHLIST_FILE):
            saved = json.loads(open(WATCHLIST_FILE).read())
            syms.update(saved)
    except: pass

    # Yahoo Finance top movers
    for scrId in ["day_gainers", "most_actives"]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={scrId}&count=50",
                headers=HEADERS, timeout=8
            )
            if r.status_code != 200: continue
            quotes = r.json().get("finance",{}).get("result",[{}])[0].get("quotes",[])
            for q in quotes:
                sym   = q.get("symbol","")
                price = float(q.get("regularMarketPrice") or 0)
                if MIN_PRICE <= price <= MAX_PRICE and len(sym) <= 5 and "." not in sym:
                    syms.add(sym)
            time.sleep(0.5)
        except: continue

    # Also try query2
    try:
        r = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=100",
            headers=HEADERS, timeout=8
        )
        if r.status_code == 200:
            quotes = r.json().get("finance",{}).get("result",[{}])[0].get("quotes",[])
            for q in quotes:
                sym   = q.get("symbol","")
                price = float(q.get("regularMarketPrice") or 0)
                if MIN_PRICE <= price <= MAX_PRICE and len(sym) <= 5 and "." not in sym:
                    syms.add(sym)
    except: pass

    log.info(f"Scan universe: {len(syms)} stocks")
    return list(syms)[:60]  # cap at 60

def scan_once():
    """Run one full scan cycle."""
    log.info("=" * 50)
    log.info(f"Pre-Explosion Scan — {datetime.now().strftime('%H:%M:%S ET')}")

    universe = get_scan_universe()
    if not universe:
        log.warning("Empty universe — no stocks to scan")
        return []

    results = []
    alerts  = 0

    for sym in universe:
        try:
            # Fetch data
            quote = fetch_quote(sym)
            if not quote:
                time.sleep(0.3)
                continue

            price = quote["price"]
            if not (MIN_PRICE <= price <= MAX_PRICE):
                continue

            # Quick pre-filter — must have some movement
            if abs(quote["day_chg"]) < 3 and quote["rel_vol"] < 1.5:
                continue

            # Fetch fundamentals and news in parallel logic
            time.sleep(0.2)
            fundamentals = fetch_fundamentals(sym)
            time.sleep(0.2)
            news = fetch_news(sym)
            time.sleep(0.2)

            # Calculate explosion score
            result = calculate_explosion_score(sym, quote, fundamentals, news)
            score  = result["score"]

            log.info(
                f"{sym}: score={score} | "
                f"${price:.4f} {'+' if quote['day_chg']>=0 else ''}{quote['day_chg']:.1f}% | "
                f"vol={quote['rel_vol']}x | "
                f"float={result['float']/1e6:.1f}M | "
                f"cat={result['cat_score']} | "
                f"{result['reasons'][0][:40] if result['reasons'] else 'no catalyst'}"
            )

            results.append({**result, "sym": sym, "price": price,
                            "day_chg": quote["day_chg"], "rel_vol": quote["rel_vol"]})

            # Fire alert if score high enough
            if score >= MIN_SCORE:
                alert_explosion(sym, quote, result)
                alerts += 1

        except Exception as e:
            log.error(f"{sym}: scan error — {e}")
            time.sleep(0.5)
            continue

    # Sort by score and log top 5
    results.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"\nTop setups this scan:")
    for r in results[:5]:
        log.info(f"  #{results.index(r)+1} {r['sym']} score={r['score']} | ${r['price']:.4f} +{r['day_chg']:.1f}% | {r['reasons'][0][:50] if r['reasons'] else '—'}")

    log.info(f"Scan complete — {len(results)} stocks scored, {alerts} alerts sent\n")

    # Save top results for dashboard
    try:
        json.dump({
            "updated": datetime.now().isoformat(),
            "results": results[:20]
        }, open("explosion_results.json","w"), indent=2)
    except: pass

    return results

# ── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info("KIBO Pre-Explosion Agent — STARTED")
    log.info(f"Scanning $0.10–$10.00 range")
    log.info(f"Alert threshold: {MIN_SCORE}/100")
    log.info(f"Scan interval: {SCAN_INTERVAL//60} min")
    log.info("=" * 60)

    send_telegram(
        "🔥 <b>KIBO Pre-Explosion Agent STARTED</b>\n"
        f"Scanning penny + small cap universe 24/7\n"
        f"Alert when score ≥ {MIN_SCORE}/100\n"
        f"Formula: Catalyst + Float + Volume + Compression\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"
    )

    while True:
        try:
            now = datetime.now()

            # Faster scanning during key windows
            if 7 <= now.hour < 10:
                interval = 2 * 60   # every 2 min pre-market & opening
            elif 10 <= now.hour < 16:
                interval = 3 * 60   # every 3 min during market
            else:
                interval = 10 * 60  # every 10 min overnight

            scan_once()
            log.info(f"Next scan in {interval//60} min...")
            time.sleep(interval)

        except KeyboardInterrupt:
            log.info("Stopped by user")
            break
        except Exception as e:
            log.error(f"Main loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run()
