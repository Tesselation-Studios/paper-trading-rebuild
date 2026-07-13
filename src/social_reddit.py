#!/usr/bin/env python3
"""
Reddit sentiment pipeline — RSS-based, no auth required.

Provides SocialRedditPipeline which data_bus uses for Level 1 Reddit
sentiment fetching.  Uses public RSS feeds from popular stock subreddits.

Exported:
    SocialRedditPipeline — class with fetch_all_posts() and fetch_ticker_sentiment()
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

log = logging.getLogger("social_reddit")

# ── Keyword sentiment (same word lists as social_sentiment) ──────────────────

_BULLISH_WORDS: set[str] = {
    "moon", "rocket", "squeeze", "breakout", "bullish", "bull",
    "calls", "yolo", "long", "buy", "buying", "bought", "loaded",
    "mooning", "gain", "gains", "profit", "profits",
    "surge", "soaring", "soar", "skyrocket", "explosive", "explode",
    "pump", "pumping", "rally", "rallied", "rallying",
    "recovery", "recovering", "rebound", "rebounding",
    "strong", "strength", "positive", "growth", "growing",
    "upside", "potential", "promising", "opportunity",
    "undervalued", "oversold", "support", "solid", "improving",
}

_BEARISH_WORDS: set[str] = {
    "dump", "dumping", "bear", "bearish", "puts", "short",
    "crash", "crashing", "crashed", "bag", "bags", "bagholder",
    "red", "sell", "selling", "sold", "rekt", "wrecked",
    "rug", "rugpull", "collapse", "plunge", "tank", "tanking",
    "liquidate", "decline", "declining", "drop", "dropping", "dropped",
    "fall", "falling", "fell", "down", "downturn", "weak", "weakness",
    "negative", "loss", "losses", "losing", "underwater",
    "overvalued", "overbought", "expensive", "bubble",
    "worst", "terrible", "poor", "disappointing",
}

_TICKER_RE = re.compile(r"\$([A-Z]{1,5})(?:\b|(?=[.,!?;:\s)\]}]))")
_BARE_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")

# Known tickers for filtering
_KNOWN_TICKERS: set[str] = {
    "AAPL", "TSLA", "NVDA", "AMD", "AMZN", "MSFT", "GOOGL", "GOOG",
    "META", "NFLX", "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW",
    "GME", "AMC", "SPY", "QQQ", "IWM", "DIA", "SMCI", "HOOD",
    "PLTR", "RIVN", "MU", "MARA", "COIN", "MSTR", "SOFI",
    "IBM", "INTC", "QCOM", "AVGO", "TXN", "CSCO",
    "ADBE", "PYPL", "SQ", "UBER", "LYFT", "ABNB", "SNAP", "PINS",
    "DIS", "NKE", "SBUX", "MCD", "KO", "PEP", "WMT",
    "BA", "CAT", "GE", "F", "GM", "XOM", "CVX",
    "PFE", "MRNA", "JNJ", "UNH", "LLY", "ABBV",
    "V", "MA", "AXP",
    "ASTS", "RKLB",
    "SPCE", "CHWY", "DASH",
}


def _keyword_sentiment(text: str) -> float:
    """Simple keyword sentiment score -1..+1."""
    if not text:
        return 0.0
    text_lower = text.lower()
    bullish_count = sum(1 for w in _BULLISH_WORDS if w in text_lower)
    bearish_count = sum(1 for w in _BEARISH_WORDS if w in text_lower)
    total = bullish_count + bearish_count
    if total == 0:
        return 0.0
    return round((bullish_count - bearish_count) / (total + 2), 4)  # +2 dampening


def _extract_tickers(text: str) -> list[str]:
    """Extract likely stock tickers from text."""
    tickers: set[str] = set()
    upper_text = text.upper()

    # Match $TICKER format
    for match in _TICKER_RE.finditer(upper_text):
        ticker = match.group(1)
        if ticker in _KNOWN_TICKERS:
            tickers.add(ticker)

    # Match bare 2-5 character uppercase words in the presence of financial context
    financial_keywords = {"stock", "buy", "sell", "call", "put", "wsb",
                          "moon", "rocket", "pump", "dump", "yolo"}
    text_lower = text.lower()
    has_financial_context = any(kw in text_lower for kw in financial_keywords)

    if has_financial_context:
        for match in _BARE_TICKER_RE.finditer(upper_text):
            ticker = match.group(1)
            if ticker in _KNOWN_TICKERS and ticker not in {"I", "A", "FOR", "NOT", "THE", "AND", "ARE", "WAS", "ITS"}:
                tickers.add(ticker)

    return sorted(tickers)


# ── RSS Feed Parser ──────────────────────────────────────────────────────────

# Public RSS feed URLs for stock-related subreddits
_RSS_FEEDS: list[tuple[str, str]] = [
    ("wallstreetbets", "https://www.reddit.com/r/wallstreetbets/hot/.rss"),
    ("stocks",         "https://www.reddit.com/r/stocks/hot/.rss"),
    ("investing",      "https://www.reddit.com/r/investing/hot/.rss"),
    ("options",        "https://www.reddit.com/r/options/hot/.rss"),
    ("smallstreetbets","https://www.reddit.com/r/smallstreetbets/hot/.rss"),
]


class SocialRedditPipeline:
    """RSS-based Reddit sentiment pipeline — no auth required.

    Fetches hot posts from stock-related subreddits, extracts tickers,
    and scores sentiment via keyword matching.
    """

    def __init__(self):
        self._sentiment_fn = _keyword_sentiment

    def fetch_all_posts(self, max_per_feed: int = 10) -> list[dict[str, Any]]:
        """Fetch and parse hot posts from all configured subreddits.

        Returns list of dicts:
            {post_title, subreddit, tickers, sentiment_score,
             signal_strength, upvotes, comment_count}
        """
        import json
        import urllib.request
        import urllib.error
        import xml.etree.ElementTree as ET

        all_posts: list[dict[str, Any]] = []

        for sub_name, feed_url in _RSS_FEEDS:
            try:
                req = urllib.request.Request(
                    feed_url,
                    headers={
                        "User-Agent": "paper-trading-databus/1.0 (RSS reader)",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read()

                root = ET.fromstring(raw)
                # RSS namespace
                ns = {"default": "http://www.w3.org/2005/Atom"}
                entries = root.findall(".//default:entry", ns) or root.findall(".//entry")

                if not entries:
                    # Try RSS 2.0 format
                    entries = root.findall(".//item") or []

                for entry in entries[:max_per_feed]:
                    title_el = entry.find("default:title", ns)
                    if title_el is None:
                        title_el = entry.find("title")
                    title = (title_el.text or "").strip() if title_el is not None else ""

                    # Try various content paths
                    content = ""
                    for tag in ["default:content", "content", "default:summary",
                                "summary", "description"]:
                        el = None
                        try:
                            el = entry.find(tag, ns) if ":" in tag else entry.find(tag)
                        except Exception:
                            pass
                        if el is not None and el.text:
                            content = el.text
                            break

                    # Extract tickers from title
                    combined = title + " " + content[:500]
                    tickers = _extract_tickers(combined)

                    sentiment_score = self._sentiment_fn(title)
                    signal_strength = 0.0

                    if tickers:
                        # Compute signal strength: more tickers + stronger sentiment = stronger signal
                        strength = abs(sentiment_score) * (1 + len(tickers) * 0.2)
                        # Bonus for WSB (known high-beta crowd)
                        if sub_name == "wallstreetbets":
                            strength *= 1.3
                        signal_strength = round(min(strength, 1.0), 4)

                    # Estimate engagement from content length / structural clues
                    # (RSS doesn't provide upvote/comment counts directly)
                    content_length = len(content) if content else 0
                    upvotes = max(1, content_length // 100)
                    comment_count = max(0, upvotes // 3)

                    all_posts.append({
                        "post_title": title[:300],
                        "subreddit": sub_name,
                        "tickers": tickers,
                        "sentiment_score": sentiment_score,
                        "signal_strength": signal_strength,
                        "upvotes": upvotes,
                        "comment_count": comment_count,
                    })

            except Exception as e:
                log.debug("RSS parse failed for r/%s: %s", sub_name, e)
                continue

        if not all_posts:
            # Fallback: try old.reddit.com JSON for WSB
            try:
                all_posts = self._fetch_old_reddit_json(max_per_feed)
            except Exception as e:
                log.debug("old.reddit.com JSON fallback failed: %s", e)

        return all_posts

    def fetch_ticker_sentiment(self, ticker: str) -> dict[str, Any]:
        """Fetch sentiment for a specific ticker from subreddit search.

        Uses old.reddit.com search (no auth, JSON format) for a specific
        ticker.  Returns {posts: int, top_posts: list, sentiment_score}.
        """
        import json
        import urllib.request
        import urllib.error

        query = f"${ticker}"
        url = f"https://old.reddit.com/search.json?q={urllib.request.quote(query)}&restrict_sr=on&sort=new&limit=10&t=day"

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "paper-trading-databus/1.0 (JSON reader)",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError, OSError) as e:
            log.debug("Reddit JSON search error for %s: %s", ticker, e)
            return {"posts": 0, "top_posts": [], "sentiment_score": 0.0}

        children = data.get("data", {}).get("children", [])
        if not children:
            return {"posts": 0, "top_posts": [], "sentiment_score": 0.0}

        top_posts: list[dict] = []
        total_score = 0.0
        post_count = 0

        for child in children[:10]:
            post = child.get("data", {})
            title = (post.get("title") or "")[:300]
            subreddit = post.get("subreddit", "unknown")
            ups = post.get("ups", 0)
            num_comments = post.get("num_comments", 0)

            if not title:
                continue

            score = self._sentiment_fn(title)
            total_score += score
            post_count += 1

            top_posts.append({
                "title": title,
                "subreddit": f"r/{subreddit}",
                "sentiment_score": score,
                "upvotes": ups,
                "num_comments": num_comments,
            })

        aggregate = round(total_score / post_count, 4) if post_count > 0 else 0.0
        return {
            "posts": len(children),
            "top_posts": top_posts,
            "sentiment_score": aggregate,
        }

    @staticmethod
    def _fetch_old_reddit_json(max_per_feed: int) -> list[dict[str, Any]]:
        """Fallback: fetch from old.reddit.com JSON for WSB."""
        import json
        import urllib.request
        import urllib.error

        all_posts: list[dict[str, Any]] = []
        subs_to_try = ["wallstreetbets", "stocks"]

        for sub in subs_to_try:
            url = f"https://old.reddit.com/r/{sub}/hot.json?limit={max_per_feed}"
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "paper-trading-databus/1.0 (JSON reader)",
                    },
                )
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())

                children = data.get("data", {}).get("children", [])
                for child in children[:max_per_feed]:
                    post = child.get("data", {})
                    title = (post.get("title") or "").strip()
                    if not title:
                        continue

                    tickers = _extract_tickers(title)
                    sentiment_score = _keyword_sentiment(title)
                    signal_strength = 0.0
                    if tickers:
                        strength = abs(sentiment_score) * (1 + len(tickers) * 0.2)
                        if sub == "wallstreetbets":
                            strength *= 1.3
                        signal_strength = round(min(strength, 1.0), 4)

                    all_posts.append({
                        "post_title": title[:300],
                        "subreddit": sub,
                        "tickers": tickers,
                        "sentiment_score": sentiment_score,
                        "signal_strength": signal_strength,
                        "upvotes": post.get("ups", 0),
                        "comment_count": post.get("num_comments", 0),
                    })
            except Exception as e:
                log.debug("old.reddit.com JSON failed for r/%s: %s", sub, e)
                continue

        return all_posts
