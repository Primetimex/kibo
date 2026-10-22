"""
KIBO News Intelligence
Fetches and scores news for any symbol.
Used by all agents to boost or block trades based on news sentiment.

Usage:
    from news_intel import get_news_score, get_market_news
    
    score = get_news_score("SPY")
    # Returns: {"score": 25, "sentiment": "BULLISH", "headlines": [...], "boost": True}
"""

import requests, time, re
from datetime import datetime

HEADERS = {"User-Agent": "Mozilla/5.0"}

# Keywords that boost BULLISH score
BULLISH_KEYWORDS = [
    "beat","beats","surges","jumps","soars","rallies","upgrade","buy rating",
    "record high","all time high","strong earnings","revenue beat","raised guidance",
    "fda approved","fda approval","contract awarded","merger","acquisition",
    "partnership","buyout","dividend increase","share buyback","short squeeze",
    "breakout","bullish","positive","growth","expansion","ai","artificial intelligence",
]

# Keywords that boost BEARISH score
BEARISH_KEYWORDS = [
    "misses","miss","falls","drops","plunges","tumbles","downgrade","sell rating",
    "record low","weak earnings","revenue miss","lowered guidance","fda rejected",
    "fda rejection","lawsuit","investigation","sec probe","bankruptcy","debt",
    "layoffs","restructuring","downside","bearish","negative","decline","warning",
    "recession","inflation","rate hike","tariff","sanction",
]

# High-impact keywords that double the score boost
HIGH_IMPACT = [
    "fda approved","fda approval","merger","acquisition","buyout",
    "bankruptcy","sec","fraud","short squeeze","earnings beat","earnings miss",
]

_cache = {}
_cache_time = {}
CACHE_TTL = 300  # 5 minute cache

def fetch_news(symbol: str) -> list:
    """Fetch recent news for a symbol from Yahoo Finance."""
    now = time.time()
    if symbol in _cache and now - _cache_time.get(symbol, 0) < CACHE_TTL:
        return _cache[symbol]
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            headers=HEADERS,
            params={"q": symbol, "newsCount": 5, "quotesCount": 0},
            timeout=6
        )
        if r.status_code != 200:
            return []
        items = r.json().get("news", [])
        news = []
        for item in items[:5]:
            title = item.get("title", "")
            pub   = item.get("providerPublishTime", 0)
            src   = item.get("publisher", "")
            link  = item.get("link", "")
            if title:
                hrs_ago = round((time.time() - pub) / 3600, 1) if pub else 99
                news.append({
                    "title":   title,
                    "src":     src,
                    "hrs_ago": hrs_ago,
                    "age_str": f"{hrs_ago}h ago" if hrs_ago < 24 else f"{int(hrs_ago/24)}d ago",
                    "link":    link,
                    "fresh":   hrs_ago < 4,  # within 4 hours = fresh
                })
        _cache[symbol] = news
        _cache_time[symbol] = now
        return news
    except:
        return []

def score_headline(title: str) -> tuple[int, str]:
    """
    Score a single headline.
    Returns (score, direction) where direction is BULLISH/BEARISH/NEUTRAL
    """
    lower = title.lower()
    bull_score = 0
    bear_score = 0

    for kw in BULLISH_KEYWORDS:
        if kw in lower:
            pts = 20 if kw in HIGH_IMPACT else 10
            bull_score += pts

    for kw in BEARISH_KEYWORDS:
        if kw in lower:
            pts = 20 if kw in HIGH_IMPACT else 10
            bear_score += pts

    net = bull_score - bear_score
    if net > 0:   return net, "BULLISH"
    if net < 0:   return net, "BEARISH"
    return 0, "NEUTRAL"

def get_news_score(symbol: str, direction: str = None) -> dict:
    """
    Get news score for a symbol.
    
    Returns:
        score     : net sentiment score (-100 to +100)
        sentiment : BULLISH / BEARISH / NEUTRAL / NO_NEWS
        headlines : list of recent headlines
        boost     : True if news supports trade direction (speeds up entry)
        block     : True if news strongly opposes trade direction (blocks entry)
        fresh     : True if there's news in the last 4 hours
    """
    news = fetch_news(symbol)
    
    if not news:
        return {
            "score":     0,
            "sentiment": "NO_NEWS",
            "headlines": [],
            "boost":     False,
            "block":     False,
            "fresh":     False,
            "reason":    "No recent news found",
        }
    
    # Only score recent news (last 24 hours)
    recent = [n for n in news if n["hrs_ago"] < 24]
    if not recent:
        recent = news[:2]  # use latest even if older
    
    total_score = 0
    scored = []
    for n in recent:
        pts, sent = score_headline(n["title"])
        total_score += pts
        scored.append({**n, "score": pts, "headline_sentiment": sent})
    
    # Cap score
    total_score = max(-100, min(100, total_score))
    
    if total_score >= 15:   overall = "BULLISH"
    elif total_score <= -15: overall = "BEARISH"
    else:                    overall = "NEUTRAL"
    
    fresh = any(n["fresh"] for n in recent)
    
    # Boost/block logic relative to trade direction
    boost = False
    block = False
    if direction:
        if direction == "LONG"  and overall == "BULLISH" and total_score >= 15: boost = True
        if direction == "SHORT" and overall == "BEARISH" and total_score <= -15: boost = True
        if direction == "LONG"  and overall == "BEARISH" and total_score <= -30: block = True
        if direction == "SHORT" and overall == "BULLISH" and total_score >= 30:  block = True
    
    reason = f"News: {overall} (score {total_score:+d})"
    if fresh: reason += " · FRESH NEWS"
    if boost: reason += " · SUPPORTS TRADE"
    if block: reason += " · OPPOSES TRADE"
    
    return {
        "score":     total_score,
        "sentiment": overall,
        "headlines": scored,
        "boost":     boost,
        "block":     block,
        "fresh":     fresh,
        "reason":    reason,
    }

def get_market_news() -> list:
    """Get broad market news — macro events that affect all instruments."""
    symbols = ["SPY", "QQQ", "^VIX", "GLD", "BTC-USD"]
    all_news = []
    for sym in symbols:
        news = fetch_news(sym)
        for n in news[:2]:
            if n["hrs_ago"] < 6:  # only very fresh macro news
                all_news.append({**n, "symbol": sym})
        time.sleep(0.2)
    # Sort by freshness
    all_news.sort(key=lambda x: x["hrs_ago"])
    return all_news[:10]

if __name__ == "__main__":
    # Test
    print("Testing news_intel...\n")
    for sym in ["SPY", "BTC-USD", "NVDA"]:
        result = get_news_score(sym)
        print(f"{sym}: {result['sentiment']} (score {result['score']:+d})")
        for h in result['headlines'][:2]:
            print(f"  [{h['age_str']}] {h['title'][:70]}")
        print()
