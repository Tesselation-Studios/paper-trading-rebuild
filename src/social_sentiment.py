#!/usr/bin/env python3
"""
Social sentiment module — Bluesky, StockTwits, and keyword fallback.

Provides the functions imported by data_bus.py for the /social and
/sentiment endpoints.  Uses public (no-auth) APIs where available and
falls back to a pure-Python keyword-based sentiment analyzer.

Exported:
    fetch_bluesky_sentiment(ticker)   — Bluesky AT Protocol search
    fetch_stocktwits_sentiment(ticker) — StockTwits API
    fetch_reddit_via_search(ticker)    — DuckDuckGo proxy -> Reddit
    fetch_reddit_via_chrome(sub, limit) — placeholder (Chrome scraper)
    _simple_sentiment(text)            — keyword-based fallback (-1..+1)
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("social_sentiment")

# ── Keyword-based sentiment (pure-Python VADER-like) ─────────────────────────

_BULLISH_WORDS: set[str] = {
    "moon", "rocket", "squeeze", "breakout", "bullish", "bull",
    "calls", "yolo", "long", "buy", "buying", "bought", "loaded",
    "mooning", "tendies", "gain", "gains", "profit", "profits",
    "breakthrough", "surge", "surged", "soaring", "soar", "skyrocket",
    "explosive", "explode", "blast", "blastoff", "rip", "ripping",
    "pump", "pumping", "moonbound", "lambo", "rich", "wealth",
    "upgrade", "upgraded", "outperform", "beating", "beat",
    "accumulate", "accumulation", "strong", "strength", "positive",
    "growth", "growing", "grow", "upside", "potential", "promising",
    "opportunity", "opportunities", "value", "undervalued", "oversold",
    "support", "supportive", "rally", "rallied", "rallying",
    "recovery", "recovering", "recovered", "rebound", "rebounding",
    "solid", "improving", "improve", "improved", "better",
    "confident", "confidence", "optimistic", "optimism",
    "dividend", "yield", "buyback", "growth_stock",
    "alpha", "returns", "outlook", "favorable", "bullrun",
    # General positive
    "good", "great", "excellent", "amazing", "awesome", "fantastic",
    "best", "better", "winning", "win", "wins",
    "success", "successful", "thrilled", "excited", "exciting",
    "nice", "happy", "pleased", "impressed", "impressive",
    "beautiful", "perfect", "outstanding", "superior",
}

_BEARISH_WORDS: set[str] = {
    "dump", "dumping", "bear", "bearish", "puts", "short",
    "crash", "crashing", "crashed", "bag", "bags", "bagholder",
    "red", "sell", "selling", "sold", "rekt", "wrecked",
    "rug", "rugpull", "collapse", "collapsing", "collapsed",
    "plunge", "plunging", "plunged", "tank", "tanking", "tanked",
    "liquidate", "liquidation", "margin_call", "stop_loss",
    "dead", "death", "doom", "doomsday", "apocalypse",
    "decline", "declining", "declined", "drop", "dropping",
    "dropped", "fall", "falling", "fell", "down", "downturn",
    "downgrade", "downgraded", "underperform", "weak", "weakness",
    "negative", "loss", "losses", "losing", "underwater",
    "volatile", "volatility", "risk", "risky", "danger", "dangerous",
    "warning", "caution", "cautious", "uncertain", "uncertainty",
    "overvalued", "overbought", "expensive", "bubble", "inflated",
    "overhyped", "fading", "fade", "resistance", "reject", "rejected",
    "guidance_down", "cut", "cuts", "layoff", "layoffs",
    # General negative
    "bad", "terrible", "awful", "horrible", "dreadful",
    "worst", "worse", "poor", "lousy", "mediocre",
    "hate", "hating", "disaster", "disastrous", "catastrophe",
    "disappointed", "disappointing", "frustrating", "frustrated",
    "ugly", "nasty", "toxic", "poison", "poisonous",
}

_AMPLIFIERS: set[str] = {
    "very", "extremely", "incredibly", "highly", "super", "mega",
    "ultra", "totally", "absolutely", "definitely", "surely",
    "massively", "significantly", "substantially", "remarkably",
}

_NEGATORS: set[str] = {
    "not", "no", "never", "neither", "nor", "don't", "doesn't",
    "didn't", "won't", "wouldn't", "couldn't", "shouldn't",
    "can't", "cannot", "isn't", "aren't", "wasn't", "weren't",
    "hasn't", "haven't", "hadn't", "without", "hardly", "barely",
}


def _simple_sentiment(text: str) -> float:
    """Keyword-based sentiment analysis, returns compound score -1.0 to +1.0.

    Uses financial-specific word lists with negation and amplification
    handling.  Pure Python — no external dependencies.
    """
    if not text or not text.strip():
        return 0.0

    text_lower = text.lower()
    tokens = re.findall(r"[a-z']+", text_lower)
    if not tokens:
        return 0.0

    score = 0.0
    word_count = len(tokens)

    for i, token in enumerate(tokens):
        if token in _BULLISH_WORDS:
            impact = 1.0
            window_start = max(0, i - 3)
            for j in range(window_start, i):
                if tokens[j] in _NEGATORS:
                    impact *= -0.5
                    break
            if i > 0 and tokens[i - 1] in _AMPLIFIERS:
                impact *= 1.5
            score += impact

        elif token in _BEARISH_WORDS:
            impact = -1.0
            window_start = max(0, i - 3)
            for j in range(window_start, i):
                if tokens[j] in _NEGATORS:
                    impact *= -0.5
                    break
            if i > 0 and tokens[i - 1] in _AMPLIFIERS:
                impact *= 1.5
            score += impact

    # Normalize to [-1, 1] using tanh-like scaling
    score = score / max(word_count * 1.5, 10)
    score = max(-1.0, min(1.0, score))

    return round(score, 4)


# ── Bluesky (AT Protocol — public API, no auth) ──────────────────────────────

BSKY_SEARCH_URL = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
_TICKER_PATTERN = re.compile(r"\$([A-Z]{1,5})(?:\b|(?=[.,!?;:\s)\]}]))")


def fetch_bluesky_sentiment(ticker: str) -> dict[str, Any]:
    """Fetch Bluesky posts mentioning a ticker via AT Protocol public API.

    Returns dict with:
        posts (int): count of matching posts
        top_posts (list): up to 10 most recent posts
            each: {handle, text, likes, reposts, created_at, sentiment_score}
        sentiment_score (float): aggregate -1 to +1
    """
    import json
    import urllib.request
    import urllib.error

    ticker_upper = ticker.upper()
    query = f"${ticker_upper}"
    url = f"{BSKY_SEARCH_URL}?q={urllib.request.quote(query)}&sort=latest&limit=15"

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "paper-trading-databus/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as e:
        log.debug("Bluesky API error for %s: %s", ticker, e)
        return {"posts": 0, "top_posts": [], "sentiment_score": 0.0}

    posts_raw = data.get("posts", [])
    if not posts_raw:
        return {"posts": 0, "top_posts": [], "sentiment_score": 0.0}

    top_posts: list[dict] = []
    total_score = 0.0
    total_weight = 0

    for post in posts_raw[:10]:
        record = post.get("record", {})
        text = (record.get("text") or record.get("content", "") or "")[:500]
        if not text:
            continue

        author = post.get("author", {})
        handle = author.get("handle", "unknown")

        like_count = post.get("likeCount", 0)
        repost_count = post.get("repostCount", 0)

        score = _simple_sentiment(text)
        weight = max(1, like_count + repost_count * 2)
        total_score += score * weight
        total_weight += weight

        top_posts.append({
            "handle": handle,
            "text": text,
            "sentiment_score": score,
            "likes": like_count,
            "reposts": repost_count,
            "created_at": record.get("createdAt", ""),
        })

    aggregate = round(total_score / total_weight, 4) if total_weight > 0 else 0.0

    return {
        "posts": len(posts_raw),
        "top_posts": top_posts,
        "sentiment_score": aggregate,
    }


# ── StockTwits (public API, no auth) ─────────────────────────────────────────

ST_BASE_URL = "https://api.stocktwits.com/api/2/streams/symbol"


def fetch_stocktwits_sentiment(ticker: str) -> dict[str, Any]:
    """Fetch StockTwits messages for a ticker via their public API.

    Returns dict with:
        messages (int): count of matching messages
        top_messages (list): up to 10 most recent messages
            each: {user, body, sentiment (bullish|bearish|neutral), created_at}
        sentiment_score (float): aggregate -1 to +1
    """
    import json
    import urllib.request
    import urllib.error

    ticker_upper = ticker.upper()
    url = f"{ST_BASE_URL}/{ticker_upper}.json"

    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "paper-trading-databus/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as e:
        log.debug("StockTwits API error for %s: %s", ticker, e)
        return {"messages": 0, "top_messages": [], "sentiment_score": 0.0}

    messages_raw = data.get("messages", [])
    if not messages_raw:
        return {"messages": 0, "top_messages": [], "sentiment_score": 0.0}

    top_messages: list[dict] = []
    total_score = 0.0
    total_bullish = 0
    total_bearish = 0
    total_neutral = 0

    for msg in messages_raw[:12]:
        user_data = msg.get("user", {})
        username = user_data.get("username", "?")
        body = (msg.get("body") or "")[:500]
        if not body:
            continue

        st_sentiment = (msg.get("entities", {}).get("sentiment") or {}).get("basic", "Neutral")
        if st_sentiment == "Bullish":
            total_bullish += 1
        elif st_sentiment == "Bearish":
            total_bearish += 1
        else:
            total_neutral += 1

        our_score = _simple_sentiment(body)
        total_score += our_score

        top_messages.append({
            "user": username,
            "body": body,
            "sentiment": st_sentiment.lower(),
            "created_at": msg.get("created_at", ""),
        })

    total_msgs = total_bullish + total_bearish + total_neutral
    if total_msgs > 0:
        classification_ratio = (total_bullish - total_bearish) / total_msgs
        keyword_avg = total_score / total_msgs
        aggregate = round((classification_ratio * 0.6 + keyword_avg * 0.4), 4)
    else:
        aggregate = 0.0

    aggregate = max(-1.0, min(1.0, aggregate))

    return {
        "messages": len(messages_raw),
        "top_messages": top_messages[:10],
        "sentiment_score": aggregate,
    }


# ── DuckDuckGo -> Reddit search proxy ────────────────────────────────────────

def fetch_reddit_via_search(ticker: str) -> dict[str, Any]:
    """Search Reddit via DuckDuckGo HTML proxy (no API key needed).

    Returns dict with:
        posts_data (list): {title, subreddit, snippet, sentiment}
        bullish (int): count of bullish posts
        bearish (int): count of bearish posts
    """
    import html.parser
    import urllib.request
    import urllib.error

    ticker_upper = ticker.upper()
    query = f"site:reddit.com ${ticker_upper} stock"
    encoded = urllib.request.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html_data = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.debug("DDG search error for %s: %s", ticker, e)
        return {"posts_data": [], "bullish": 0, "bearish": 0}

    results: list[dict[str, str]] = []
    snippets = re.findall(
        r'<a[^>]*class="result__a"[^>]*>(.*?)</a>.*?<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        html_data,
        re.DOTALL,
    )

    for title_html, snippet_html in snippets[:8]:
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        snippet = re.sub(r"<[^>]+>", "", snippet_html).strip()
        title = html.unescape(title) if hasattr(html, 'unescape') else title
        snippet = html.unescape(snippet) if hasattr(html, 'unescape') else snippet

        if not title:
            continue

        subreddit = ""
        sr_match = re.search(r"r/([a-zA-Z0-9_]+)", title + " " + snippet)
        if sr_match:
            subreddit = f"r/{sr_match.group(1)}"

        sentiment = "neutral"
        text_lower = (title + " " + snippet).lower()
        bullish_hits = sum(
            1 for kw in ["moon", "rocket", "bull", "bullish", "calls", "yolo",
                          "pump", "squeeze", "breakout", "long", "buy", "gain"]
            if kw in text_lower
        )
        bearish_hits = sum(
            1 for kw in ["dump", "bear", "bearish", "puts", "short", "crash",
                          "bag", "red", "sell", "rekt", "dead"]
            if kw in text_lower
        )
        if bullish_hits > bearish_hits:
            sentiment = "bullish"
        elif bearish_hits > bullish_hits:
            sentiment = "bearish"

        results.append({
            "title": title[:200],
            "subreddit": subreddit,
            "snippet": snippet[:300],
            "sentiment": sentiment,
        })

    bullish_count = sum(1 for r in results if r["sentiment"] == "bullish")
    bearish_count = sum(1 for r in results if r["sentiment"] == "bearish")

    return {
        "posts_data": results,
        "bullish": bullish_count,
        "bearish": bearish_count,
    }


# ── Chrome/Playwright scraper placeholder ────────────────────────────────────

def fetch_reddit_via_chrome(sub: str, limit: int = 15) -> list[dict[str, Any]]:
    """Fetch Reddit posts via Chrome/Playwright scraper (Level 3 fallback).

    Returns list of dicts with {title, subreddit, score, num_comments, url}.
    Returns empty list if Playwright is unavailable, which triggers
    data_bus's Level-3-inline fallback using old.reddit.com JSON.
    """
    log.debug("Chrome scraper stub — Playwright not installed, returning empty")
    return []