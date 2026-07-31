#!/usr/bin/env python3
"""
Social Sentiment — Bluesky AT Protocol + Stocktwits Integration

Augments rss_watcher.py with two additional social sentiment sources:
  1. Bluesky AT Protocol — public firehose search (no auth needed)
  2. Stocktwits — public stream API (free tier, no API key)

Both sources use in-memory caching with a 10-minute TTL to avoid
rate-limiting during frequent watcher cycles.

Usage:
    from src.social_sentiment import (
        fetch_bluesky_sentiment,
        fetch_stocktwits_sentiment,
        discover_trending_tickers,
        match_sentiment_tickers,
        get_watchlist_tickers,
    )
"""

import re
import json
import time
import sys
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError
from collections import OrderedDict

# Shared sentiment keywords — single source of truth
from shared.sentiment_keywords import BULLISH_KEYWORDS, BEARISH_KEYWORDS

log = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
CACHE_TTL_SECONDS = 600  # 10 minutes
TRADER_DB = Path(__file__).resolve().parent / "shared" / "trader.db"

# ── in-memory cache ───────────────────────────────────────────────────────────
_cache: dict[str, tuple[float, dict]] = {}  # key -> (timestamp, data)
_last_bsky_request: float = 0.0          # rate-limit guard
_BSKY_MIN_INTERVAL: float = 1.0           # seconds between Bluesky API calls


def _cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, data = entry
    if time.time() - ts > CACHE_TTL_SECONDS:
        del _cache[key]
        return None
    return data


def _cache_set(key: str, data: dict) -> None:
    _cache[key] = (time.time(), data)


# ── helpers ───────────────────────────────────────────────────────────────────

def _simple_sentiment(text: str) -> float:
    """Score a single text string on a -1 (bearish) to +1 (bullish) scale."""
    text_lower = text.lower()
    bullish = sum(1 for kw in BULLISH_KEYWORDS if kw in text_lower)
    bearish = sum(1 for kw in BEARISH_KEYWORDS if kw in text_lower)
    total = bullish + bearish
    if total == 0:
        return 0.0
    return round((bullish - bearish) / total, 4)


def _is_valid_ticker(s: str) -> bool:
    """Return True if string looks like a valid US equity ticker."""
    return bool(re.match(r'^[A-Z]{1,5}$', s))


def _extract_tickers(text: str) -> set[str]:
    """Extract $TICKER patterns from text."""
    return {m.group(1) for m in re.finditer(r'\$([A-Z]{1,5})', text)}


def get_watchlist_tickers() -> set[str]:
    """Return Stonks's current watchlist + positions from trader.db."""
    tickers = set()
    if not TRADER_DB.exists():
        return tickers
    try:
        conn = sqlite3.connect(str(TRADER_DB))
        conn.execute("PRAGMA busy_timeout=5000")
        for row in conn.execute(
            "SELECT ticker FROM positions WHERE quantity > 0 AND status='open'"
        ):
            tickers.add(row[0].upper())
        for row in conn.execute(
            "SELECT ticker FROM watchlist WHERE agent_id='trader-stonks'"
        ):
            tickers.add(row[0].upper())
        conn.close()
    except Exception as e:
        log.warning("[social_sentiment] Warning: could not load tickers: {e}")
    return {t for t in tickers if _is_valid_ticker(t)}


# ── Bluesky AT Protocol ───────────────────────────────────────────────────────

BLUESKY_SEARCH_URL = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"


def fetch_bluesky_sentiment(ticker: str) -> dict:
    """
    Search Bluesky's public firehose for posts mentioning $TICKER.

    Returns:
        {
            "ticker": "COIN",
            "posts": 45,
            "bullish_pct": 0.72,
            "sentiment_score": 0.68,
            "top_posts": [...],       # up to 10
            "source": "bluesky",
            "cached": bool,
        }
    """
    cache_key = f"bluesky:{ticker.upper()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        cached["cached"] = True
        return cached

    ticker_upper = ticker.upper()
    url = f"{BLUESKY_SEARCH_URL}?q=%24{ticker_upper}&limit=25"

    # Rate-limit guard: ensure at least BSKY_MIN_INTERVAL between calls
    global _last_bsky_request
    elapsed = time.time() - _last_bsky_request
    if elapsed < _BSKY_MIN_INTERVAL:
        time.sleep(_BSKY_MIN_INTERVAL - elapsed)
    _last_bsky_request = time.time()

    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, json.JSONDecodeError, OSError) as e:
        log.error("[social_sentiment] Bluesky fetch error for {ticker_upper}: {e}")
        return {
            "ticker": ticker_upper,
            "posts": 0,
            "bullish_pct": 0.0,
            "sentiment_score": 0.0,
            "top_posts": [],
            "source": "bluesky",
            "cached": False,
            "error": str(e),
        }

    posts = data.get("posts", [])
    all_texts = []
    scored_posts = []

    for post in posts:
        record = post.get("record", {})
        text = record.get("text", "")
        if not text:
            continue

        author_handle = post.get("author", {}).get("handle", "unknown")
        like_count = post.get("likeCount", 0)
        repost_count = post.get("repostCount", 0)
        created_at = record.get("createdAt", "")

        score = _simple_sentiment(text)
        all_texts.append(text)

        scored_posts.append({
            "handle": author_handle,
            "text": text[:280],
            "sentiment_score": score,
            "likes": like_count,
            "reposts": repost_count,
            "created_at": created_at,
        })

    post_count = len(scored_posts)

    if post_count == 0:
        result = {
            "ticker": ticker_upper,
            "posts": 0,
            "bullish_pct": 0.0,
            "sentiment_score": 0.0,
            "top_posts": [],
            "source": "bluesky",
            "cached": False,
        }
        _cache_set(cache_key, result)
        return result

    # Aggregate scores: weighted by likes + reposts
    total_weight = 0.0
    weighted_sum = 0.0
    bullish_count = 0
    bearish_count = 0

    for p in scored_posts:
        weight = 1.0 + (p["likes"] * 0.01) + (p["reposts"] * 0.02)
        weighted_sum += p["sentiment_score"] * weight
        total_weight += weight
        if p["sentiment_score"] > 0:
            bullish_count += 1
        elif p["sentiment_score"] < 0:
            bearish_count += 1

    agg_score = round(weighted_sum / total_weight, 4) if total_weight > 0 else 0.0
    bullish_pct = round(bullish_count / post_count, 4) if post_count > 0 else 0.0

    # Sort by engagement (likes + reposts) and keep top 10
    scored_posts.sort(key=lambda p: p["likes"] + p["reposts"], reverse=True)
    top_posts = scored_posts[:10]

    result = {
        "ticker": ticker_upper,
        "posts": post_count,
        "bullish_pct": bullish_pct,
        "sentiment_score": agg_score,
        "top_posts": top_posts,
        "source": "bluesky",
        "cached": False,
    }
    _cache_set(cache_key, result)
    return result


# ── Stocktwits API ────────────────────────────────────────────────────────────

STOCKTWITS_STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{}.json"


def fetch_stocktwits_sentiment(ticker: str) -> dict:
    """
    Fetch Stocktwits public stream for a ticker. Stocktwits provides its own
    bullish/bearish classification on each message.

    Returns:
        {
            "ticker": "COIN",
            "messages": 120,
            "bullish_pct": 0.78,
            "bearish_pct": 0.22,
            "sentiment_score": 0.78,   # bullish_pct (Stocktwits native)
            "top_messages": [...],
            "source": "stocktwits",
            "cached": bool,
        }
    """
    cache_key = f"stocktwits:{ticker.upper()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        cached["cached"] = True
        return cached

    ticker_upper = ticker.upper()
    url = STOCKTWITS_STREAM_URL.format(ticker_upper)

    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, json.JSONDecodeError, OSError) as e:
        log.error("[social_sentiment] Stocktwits fetch error for {ticker_upper}: {e}")
        return {
            "ticker": ticker_upper,
            "messages": 0,
            "bullish_pct": 0.0,
            "bearish_pct": 0.0,
            "sentiment_score": 0.0,
            "top_messages": [],
            "source": "stocktwits",
            "cached": False,
            "error": str(e),
        }

    messages = data.get("messages", [])
    all_messages = []
    bullish_count = 0
    bearish_count = 0

    for msg in messages:
        body = msg.get("body", "")
        if not body:
            continue

        user = msg.get("user", {}).get("username", "unknown")
        created_at = msg.get("created_at", "")

        # Stocktwits provides its own sentiment classification
        st_sentiment = (msg.get("entities", {}) or {}).get("sentiment")
        if st_sentiment is not None and "basic" in st_sentiment:
            st_class = st_sentiment["basic"]
        else:
            st_class = None

        if st_class == "Bullish":
            bullish_count += 1
        elif st_class == "Bearish":
            bearish_count += 1

        all_messages.append({
            "user": user,
            "body": body[:280],
            "sentiment": st_class,
            "created_at": created_at,
        })

    msg_count = len(all_messages)

    if msg_count == 0:
        result = {
            "ticker": ticker_upper,
            "messages": 0,
            "bullish_pct": 0.0,
            "bearish_pct": 0.0,
            "sentiment_score": 0.0,
            "top_messages": [],
            "source": "stocktwits",
            "cached": False,
        }
        _cache_set(cache_key, result)
        return result

    classified = bullish_count + bearish_count
    bullish_pct = round(bullish_count / classified, 4) if classified > 0 else 0.5
    bearish_pct = round(bearish_count / classified, 4) if classified > 0 else 0.5

    # Sentiment score: use bullish_pct directly (Stocktwits native classification)
    sentiment_score = bullish_pct

    # Top messages: prioritize those with sentiment, then by body length (proxy for quality)
    top_messages = sorted(
        [m for m in all_messages if m["sentiment"]],
        key=lambda m: len(m["body"]),
        reverse=True,
    )[:10]

    result = {
        "ticker": ticker_upper,
        "messages": msg_count,
        "bullish_pct": bullish_pct,
        "bearish_pct": bearish_pct,
        "sentiment_score": sentiment_score,
        "top_messages": top_messages,
        "source": "stocktwits",
        "cached": False,
    }
    _cache_set(cache_key, result)
    return result


# ── Dynamic Ticker Discovery ──────────────────────────────────────────────────

BLUESKY_TRENDING_URL = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
STOCKTWITS_TRENDING_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"


def discover_trending_tickers(source: str = "bluesky", limit: int = 20) -> list[str]:
    """
    Scan social feeds for trending tickers that Stonks doesn't already track.

    Args:
        source: 'bluesky' or 'stocktwits'
        limit: max tickers to return

    Returns:
        List of ticker strings not already in Stonks's watchlist.
    """
    existing = get_watchlist_tickers()

    if source == "stocktwits":
        return _discover_stocktwits_trending(existing, limit)
    elif source == "bluesky":
        return _discover_bluesky_trending(existing, limit)
    else:
        return []


def _discover_stocktwits_trending(existing: set[str], limit: int) -> list[str]:
    """Discover trending tickers from Stocktwits trending endpoint."""
    try:
        req = Request(
            STOCKTWITS_TRENDING_URL,
            headers={"User-Agent": "paper-trading-social-sentiment/1.0"},
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, json.JSONDecodeError, OSError) as e:
        log.error("[social_sentiment] Stocktwits trending error: {e}")
        return []

    symbols = data.get("symbols", [])
    discovered = []
    for sym in symbols:
        ticker = sym.get("symbol", "").upper()
        if _is_valid_ticker(ticker) and ticker not in existing and ticker not in discovered:
            discovered.append(ticker)
            if len(discovered) >= limit:
                break

    return discovered


def _discover_bluesky_trending(existing: set[str], limit: int) -> list[str]:
    """
    Discover trending tickers from Bluesky by searching for $SYMBOL patterns
    using common crypto/stock prefix probes.
    """
    # Heuristic: search for "$" + common patterns to surface tickers
    # We search broad terms and extract $TICKER from results
    probes = [
        "$stock", "$crypto", "$btc", "$eth", "$trade", "$bull", "$bear",
        "$breakout", "$rally", "$moon", "$earnings",
    ]

    all_tickers: OrderedDict[str, int] = OrderedDict()  # ticker -> mention count
    seen_urls: set[str] = set()

    for probe in probes:
        url = f"{BLUESKY_TRENDING_URL}?q={probe}&limit=25"
        # Rate-limit guard
        global _last_bsky_request
        elapsed = time.time() - _last_bsky_request
        if elapsed < _BSKY_MIN_INTERVAL:
            time.sleep(_BSKY_MIN_INTERVAL - elapsed)
        _last_bsky_request = time.time()

        try:
            req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json",
        })
            with urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (URLError, json.JSONDecodeError, OSError):
            continue

        for post in data.get("posts", []):
            text = post.get("record", {}).get("text", "")
            uri = post.get("uri", "")
            if not text or uri in seen_urls:
                continue
            seen_urls.add(uri)

            for match in re.finditer(r'\$([A-Z]{1,5})', text):
                t = match.group(1)
                if not _is_valid_ticker(t):
                    continue
                if t in all_tickers:
                    all_tickers[t] += 1
                else:
                    all_tickers[t] = 1

    # Sort by mention frequency, exclude existing watchlist
    sorted_tickers = sorted(all_tickers.items(), key=lambda x: (-x[1], x[0]))
    discovered = []
    for ticker, count in sorted_tickers:
        if ticker not in existing and ticker not in discovered:
            discovered.append(ticker)
            if len(discovered) >= limit:
                break

    return discovered


# ── Integration helper for rss_watcher ────────────────────────────────────────

def match_sentiment_tickers(sentiment_result: dict, tickers: set[str]) -> list[dict]:
    """
    Given a sentiment result dict and a set of watched tickers, return any
    top posts/messages that mention a watched ticker.

    Returns list of dicts with keys: source, ticker, text, sentiment, engagement.
    """
    matches = []
    source = sentiment_result.get("source", "unknown")
    ticker = sentiment_result.get("ticker", "")

    if ticker.upper() in tickers:
        for post in sentiment_result.get("top_posts", []) or sentiment_result.get("top_messages", []):
            text = post.get("text", "") or post.get("body", "")
            sentiment = post.get("sentiment", "") or f"{post.get('sentiment_score', 0):.2f}"
            engagement = post.get("likes", 0) + post.get("reposts", 0)
            matches.append({
                "source": source,
                "ticker": ticker.upper(),
                "text": text[:120],
                "sentiment": sentiment,
                "engagement": engagement,
            })

    return matches


# ── Playwright Reddit Scraper ─────────────────────────────────────────────
# Uses Playwright's bundled Chromium with anti-detection init scripts.
# This reliably accesses www.reddit.com (new Reddit) without being blocked.
# Falls back to multiple selector strategies and URL patterns.

_PLAYWRIGHT_ANTI_DETECT: str = r"""
    Object.defineProperty(navigator, 'webdriver', {get: () => false});
    Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    window.chrome = {runtime: {}};
"""


def fetch_reddit_via_chrome(subreddit: str, limit: int = 25) -> list[dict]:
    """
    Scrape Reddit posts from a subreddit using Playwright's bundled Chromium.

    Uses anti-detection init scripts (navigator.webdriver=false, etc.) and
    navigates to sh.reddit.com (new Reddit layout) which has proven reliable
    against Reddit's anti-bot measures.

    Args:
        subreddit: Name of subreddit (e.g., 'wallstreetbets')
        limit: Max number of posts to return (default 25)

    Returns:
        List of dicts with keys: title, score, num_comments, url, flair,
        timestamp, subreddit, author

    This is used as a last-resort fallback when DuckDuckGo search proxy
    and RSS pipelines fail due to Reddit's anti-bot measures.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("[social_sentiment] playwright not installed")
        return []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            )
            context.add_init_script(_PLAYWRIGHT_ANTI_DETECT)

            page = context.new_page()

            # Try sh.reddit.com first (new Reddit layout, less aggressive blocking)
            url = f"https://sh.reddit.com/r/{subreddit}/hot/?limit={limit}"
            try:
                page.goto(url, timeout=20000, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
            except Exception as e:
                log.error("[social_sentiment] sh.reddit goto failed for {subreddit}: {e}")
                browser.close()
                return []

            # Check if blocked
            body_text = page.inner_text("body")
            if "blocked" in body_text[:500].lower():
                log.warning("[social_sentiment] Reddit blocked scraping for {subreddit}")
                browser.close()
                return []

            # Scrape using shreddit-post elements (new Reddit layout)
            posts = page.evaluate(f"""(limit) => {{
                const posts = [];
                document.querySelectorAll('shreddit-post').forEach(el => {{
                    const title = el.getAttribute('post-title') || '';
                    const score = parseInt(el.getAttribute('score') || '0', 10) || 0;
                    const comments = parseInt(el.getAttribute('comment-count') || '0', 10) || 0;
                    const permalink = el.getAttribute('permalink') || '';
                    const url = 'https://www.reddit.com' + permalink;
                    const timestamp = el.getAttribute('created-timestamp') || '';
                    const author = el.getAttribute('author') || '';
                    const flair = el.getAttribute('post-flair') || '';

                    if (title) {{
                        posts.push({{
                            title: title,
                            score: score,
                            num_comments: comments,
                            url: url,
                            flair: flair,
                            timestamp: timestamp,
                            subreddit: '{subreddit}',
                            author: author,
                        }});
                    }}
                }});
                return posts.slice(0, limit);
            }}""", limit)

            browser.close()
            return posts
    except Exception as e:
        log.error("[social_sentiment] Playwright scrape error for {subreddit}: {e}")
        return []

    # Fallback: try www.reddit.com as alternative
    # (reached only if playwright import failed, which is handled above)
    return []


# ── CLI ───────────────────────────────────────────────────────────────────────

# ── Reddit via DuckDuckGo Search Proxy ───────────────────────────────────────
# Reddit blocks direct API/RSS from this IP. DuckDuckGo HTML search bypasses it.
# Uses shared.sentiment_keywords for keyword lists (BULLISH_KEYWORDS, BEARISH_KEYWORDS).


def fetch_reddit_via_search(ticker: str, subreddits: list = None) -> dict:
    """Search Reddit for ticker mentions via DuckDuckGo HTML search (no-auth)."""
    if subreddits is None:
        subreddits = ["wallstreetbets", "stocks", "investing"]
    
    ticker_upper = ticker.upper()
    cache_key = f"reddit_search:{ticker_upper}"
    
    try:
        from shared.cache_util import cache
        cached = cache.get(cache_key)
        if cached:
            return cached
    except Exception:
        cached = None
    
    all_posts = []
    
    for sub in subreddits:
        try:
            from urllib.request import urlopen, Request as URLRequest
            from urllib.parse import quote
            
            query = f"site:reddit.com/r/{sub} {ticker_upper}"
            url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
            req = URLRequest(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            with urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            
            links = re.findall(r'uddg=(https?%3A%2F%2F[^&"]+)', html)
            snippets = re.findall(r'class="result__snippet">(.*?)</a>', html, re.DOTALL)
            
            from urllib.parse import unquote
            for i, link in enumerate(links[:5]):
                decoded = unquote(link)
                snippet = re.sub(r'<[^>]+>', '', snippets[i]) if i < len(snippets) else ""
                
                title_match = re.search(r'/comments/[^/]+/([^/]+)/?$', decoded)
                title = title_match.group(1).replace("_", " ") if title_match else decoded.split("/")[-1]
                
                text_lower = (title + " " + snippet).lower()
                bullish_hits = sum(1 for kw in BULLISH_KEYWORDS if kw in text_lower)
                bearish_hits = sum(1 for kw in BEARISH_KEYWORDS if kw in text_lower)
                
                if bullish_hits > bearish_hits:
                    post_sentiment = "bullish"
                elif bearish_hits > bullish_hits:
                    post_sentiment = "bearish"
                else:
                    post_sentiment = "neutral"
                
                all_posts.append({
                    "title": title[:120], "subreddit": sub,
                    "sentiment": post_sentiment, "snippet": snippet[:200],
                })
        except Exception as e:
            log.error("[social_sentiment] Reddit search error for {sub}: {e}")
    
    total = len(all_posts)
    if total == 0:
        result = {
            "ticker": ticker_upper, "source": "reddit_search",
            "posts": 0, "bullish": 50, "bearish": 25, "neutral": 25,
            "mention_count": 0, "sentiment": "neutral", "posts_data": [],
        }
    else:
        bullish = sum(1 for p in all_posts if p["sentiment"] == "bullish")
        bearish = sum(1 for p in all_posts if p["sentiment"] == "bearish")
        neutral = total - bullish - bearish
        overall = "bullish" if bullish > bearish else ("bearish" if bearish > bullish else "neutral")
        result = {
            "ticker": ticker_upper, "source": "reddit_search",
            "posts": total,
            "bullish": round(bullish / total * 100, 1),
            "bearish": round(bearish / total * 100, 1),
            "neutral": round(neutral / total * 100, 1),
            "mention_count": total, "sentiment": overall, "posts_data": all_posts,
        }
    
    try:
        from shared.cache_util import cache
        cache.set(cache_key, result, ttl=300)
    except Exception:
        pass
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Social sentiment CLI")
    parser.add_argument("--bluesky", type=str, help="Fetch Bluesky sentiment for ticker")
    parser.add_argument("--stocktwits", type=str, help="Fetch Stocktwits sentiment for ticker")
    parser.add_argument("--discover", choices=["bluesky", "stocktwits"], help="Discover trending tickers")
    parser.add_argument("--reddit", type=str, help="Fetch Reddit sentiment via DuckDuckGo")
    parser.add_argument("--reddit-chrome", type=str, help="Scrape Reddit via headless Chrome (subreddit name)")
    parser.add_argument("--limit", type=int, default=20, help="Trending ticker limit")
    args = parser.parse_args()

    if args.bluesky:
        result = fetch_bluesky_sentiment(args.bluesky)
        print(json.dumps(result, indent=2, default=str))
    elif args.stocktwits:
        result = fetch_stocktwits_sentiment(args.stocktwits)
        print(json.dumps(result, indent=2, default=str))
    elif args.reddit:
        result = fetch_reddit_via_search(args.reddit)
        print(json.dumps(result, indent=2, default=str))
    elif args.reddit_chrome:
        posts = fetch_reddit_via_chrome(args.reddit_chrome, args.limit)
        print(json.dumps(posts, indent=2, default=str))
    elif args.discover:
        tickers = discover_trending_tickers(args.discover, args.limit)
        print(f"Trending on {args.discover}: {tickers}")
    else:
        parser.print_help()
