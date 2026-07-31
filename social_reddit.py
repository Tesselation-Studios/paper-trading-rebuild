#!/usr/bin/env python3
"""
Social Reddit — Reddit sentiment pipeline for paper trading.

Fetches hot/new/rising posts from finance subreddits, extracts ticker mentions,
scores sentiment via keyword analysis, computes mention velocity vs 7-day
rolling baseline, and caches results in shared SQLite with configurable TTL.

Architecture:
  ┌──────────────┐    ┌──────────────┐    ┌────────────┐    ┌───────────────┐
  │ PRAW Stream  │───▶│ Ticker       │───▶│ Sentiment  │───▶│ SQLite Cache  │
  │ (hot/new)    │    │ Extraction   │    │ Scoring    │    │ (5-min TTL)   │
  ├──────────────┤    │ (regex)      │    │ (keyword)  │    └───────────────┘
  │ RSS Feed   │    └──────────────┘    └────────────┘
  │ (public)   │───▶┌──────────────┐
  └──────────────┘    │ Velocity     │
                      │ Tracker      │
                      │ (7d baseline)│
                      └──────────────┘

Usage:
    from social_reddit import SocialRedditPipeline

    pipeline = SocialRedditPipeline()
    result = pipeline.fetch_ticker_sentiment("GME")
    signal = pipeline.fetch_aggregate_signal()  # d["signal_score"] (0-1)

CLI:
    python3 src/social_reddit.py --ticker GME
    python3 src/social_reddit.py --aggregate
"""

import re
import json
import time
import sys
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from typing import Optional, Dict, List, Set, Tuple, Any

# Shared sentiment keywords — single source of truth
from shared.sentiment_keywords import BULLISH_KEYWORDS, BEARISH_KEYWORDS

log = logging.getLogger("social_reddit")

# ── Paths ─────────────────────────────────────────────────────────────────────
SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR
SHARED_DIR = PROJECT_DIR / "shared"
CACHE_DB = SHARED_DIR / "cache.db"

# Ensure shared directory exists
SHARED_DIR.mkdir(parents=True, exist_ok=True)

# ── Configuration ─────────────────────────────────────────────────────────────
TARGET_SUBREDDITS = ["wallstreetbets", "stocks", "investing", "pennystocks", "options"]
FETCH_MODE = "hot"  # hot, new, rising
POST_LIMIT_PER_SUB = 50
COMMENT_LIMIT_PER_POST = 0  # 0 = titles only; >0 to include top comments
CACHE_TTL_SECONDS = 300  # 5 minutes
SIGNAL_TTL = 60  # 60 seconds for aggregate signal cache (used by /social)

# Sentiment keyword dictionaries — imported from shared.sentiment_keywords
# (BULLISH_KEYWORDS and BEARISH_KEYWORDS are sets of sentiment-bearing terms)

# Ticker regex pattern
TICKER_RE = re.compile(r'\$([A-Z]{1,5})(?:\b|(?=[.,!?;:\s)\]}]))')
# Also catch bare uppercase words that look like tickers, but only in finance context
TICKER_CANDIDATE_RE = re.compile(r'(?<!\$)(?<!\w)([A-Z]{2,5})(?:\b|(?=[.,!?;:\s)\]}]))')

# Known ticker-like words to exclude (common English, not stock tickers)
EXCLUDED_WORDS: Set[str] = {
    "I", "A", "IT", "AT", "BE", "BY", "DO", "GO", "HE", "IF", "IN", "IS",
    "MY", "NO", "ON", "OR", "SO", "TO", "UP", "US", "WE",
    "ALL", "AND", "ARE", "ASK", "BIG", "BUT", "CAN", "CEO", "CPI", "DID", "DNA",
    "DOW", "END", "ETF", "FAR", "FED", "FOR", "GET", "GDP", "GEO", "GPT", "GST",
    "HAS", "HER", "HIS", "HOW", "IPO", "IRS", "ITS", "JOB", "LED", "LET",
    "LOW", "MAN", "MAY", "NET", "NEW", "NOW", "NYSE", "OCT", "OFF", "OLD",
    "ONE", "OUT", "OWN", "PER", "PST", "PUT", "SAY", "SEC", "SEE", "SET",
    "SHE", "SPY", "TAX", "THE", "TOO", "TWO", "USA", "USE", "WAS", "WAY",
    "WHO", "WILL", "YET", "YOU",
    "BUY", "SELL", "LONG", "SHORT", "CALL", "PUT", "MOON", "ROCKET",
    "YOLO", "FOMO", "ATH", "OTC", "DD", "IMO", "TLDR", "ELI5", "PSA",
    "EDIT", "HODL", "THIS", "THAT", "WITH", "FROM", "HAVE", "BEEN",
    "INTO", "ONTO", "OVER", "UNDER", "THAN", "THEN", "MORE", "MOST",
    "SOME", "SUCH", "WHAT", "WHEN", "WHERE", "WHICH", "WHILE", "WHY",
    "ALSO", "ONLY", "JUST", "LIKE", "VERY", "WELL", "EVEN", "STILL",
    "ALREADY", "ALWAYS", "ABOUT", "ABOVE", "AFTER", "AGAIN", "BELOW",
    "BETWEEN", "BOTH", "EACH", "FEW", "FIRST", "LAST", "NEXT", "OTHER",
    "SAME", "MUCH",
    # Some edge cases that look like tickers
    "REAL", "TRUE", "BEST", "LIFE", "GOLD", "OIL", "GAS", "FIRE",
    "RISK", "FREE", "OPEN", "CLOSE", "HIGH", "LOSE", "LOST", "SAFE",
    "DARK", "FAIR", "FINE", "GLAD", "HALF", "HUGE", "KEEN", "KIND",
    "LATE", "LEAN", "LOUD", "MAIN", "NEAR", "NEAT", "OKAY", "PURE",
    "QUIT", "RARE", "RAWS", "SAID", "SAME", "SICK", "SLIM", "SLOW",
    "SNAP", "SOFT", "SURE", "TALL", "THIN", "TILL", "VAST", "WARM",
    "WEAK", "WIDE", "WILD", "WISE", "ZERO",
    # False positives from subreddit context
    "CFO", "CEO", "CTO", "COO", "CIO", "CFA", "CPA", "MBA",
    "WIN", "WON", "LOST", "LETS", "LET", "DR", "MR", "MRS", "MS",
    "CALLS", "PUTS", "BULLS", "BEARS", "GAIN", "LOSS",
    "TL", "DR", "IMO", "TLDR", "EDIT", "PSA",
    # Geography / common nouns in finance headlines
    "JAPAN", "CHINA", "EUROPE", "WORLD", "GLOBAL", "MARKET", "TRADE",
    "YEARS", "MONTHS", "WEEKS", "DAYS", "TODAY", "YESTERDAY",
    "PLAN", "PLANS", "DEAL", "DEALS", "BILL", "BILLS", "TAX", "TAXES",
    "HOME", "HOMES", "HOUSE", "CASE", "RATE", "RATES",
}


class SocialRedditPipeline:
    """
    Reddit social sentiment pipeline with PRAW integration.
    Falls back to Reddit public RSS/Atom feed if PRAW is unavailable.
    No API keys needed for the RSS fallback.
    """

    def __init__(self, subreddits: Optional[List[str]] = None,
                 cache_ttl: int = CACHE_TTL_SECONDS,
                 db_path: Optional[Path] = None,
                 use_praw: bool = False):
        self.subreddits = subreddits or TARGET_SUBREDDITS
        self.cache_ttl = cache_ttl
        self.db_path = db_path or CACHE_DB

        # Reddit public data source (no API keys needed)
        # Default: use RSS feed. PRAW requires OAuth credentials.
        self._praw = None
        self._has_praw = False
        if use_praw:
            self._init_praw()

    def _init_praw(self) -> bool:
        """Initialize PRAW client. Returns True if successful."""
        if self._has_praw:
            return True
        try:
            import praw
            self._praw = praw.Reddit(
                client_id="social_reddit_pipeline",
                client_secret="",
                user_agent="stonks-sentiment/1.0 (by /u/stonks_capital)"
            )
            self._has_praw = True
            log.info("PRAW initialized successfully")
            return True
        except Exception as e:
            log.warning("PRAW not available: %s — using Reddit RSS feed instead", e)
            self._praw = None
            self._has_praw = False
            return False

    # ── Database helpers ───────────────────────────────────────────────────

    def _get_db(self) -> sqlite3.Connection:
        """Get a connection to the shared cache DB, creating tables as needed."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_tables(conn)
        return conn

    def _ensure_tables(self, conn: sqlite3.Connection) -> None:
        """Create/verify required tables."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS reddit_signals_v2 (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT NOT NULL,
                subreddit       TEXT NOT NULL,
                post_title      TEXT,
                post_text       TEXT,
                upvotes         INTEGER DEFAULT 0,
                comment_count   INTEGER DEFAULT 0,
                score_ratio     REAL DEFAULT 0.5,
                flair           TEXT,
                mention_count   INTEGER DEFAULT 1,
                sentiment_score REAL DEFAULT 0.0,
                signal_strength REAL DEFAULT 0.0,
                post_url        TEXT,
                post_id         TEXT,
                created_utc     REAL,
                scraped_at      TEXT DEFAULT (datetime('now')),
                source          TEXT DEFAULT 'praw'
            );

            CREATE INDEX IF NOT EXISTS idx_reddit_v2_ticker
                ON reddit_signals_v2(ticker, scraped_at);
            CREATE INDEX IF NOT EXISTS idx_reddit_v2_ts
                ON reddit_signals_v2(scraped_at);

            -- Mention velocity baseline (rolling 7-day stats)
            CREATE TABLE IF NOT EXISTS reddit_velocity (
                ticker          TEXT NOT NULL,
                window_start    TEXT NOT NULL,
                window_end      TEXT NOT NULL,
                mention_count   INTEGER DEFAULT 0,
                avg_sentiment   REAL DEFAULT 0.0,
                daily_breakdown TEXT DEFAULT '{}',
                PRIMARY KEY (ticker, window_start)
            );

            -- Cache table for processed results
            CREATE TABLE IF NOT EXISTS reddit_cache (
                cache_key       TEXT PRIMARY KEY,
                data            TEXT NOT NULL,
                expires_at      TEXT NOT NULL
            );
        """)
        conn.commit()

    def _cache_get(self, key: str) -> Optional[Dict]:
        """Get cached data if not expired."""
        conn = self._get_db()
        try:
            row = conn.execute(
                "SELECT data, expires_at FROM reddit_cache WHERE cache_key = ?",
                (key,)
            ).fetchone()
            if row:
                expires = datetime.fromisoformat(row["expires_at"])
                # Make both naive for comparison
                now = datetime.now()
                if expires.replace(tzinfo=None) > now:
                    return json.loads(row["data"])
                # Expired — delete
                conn.execute("DELETE FROM reddit_cache WHERE cache_key = ?", (key,))
                conn.commit()
        finally:
            conn.close()
        return None

    def _cache_set(self, key: str, data: Dict, ttl: Optional[int] = None) -> None:
        """Cache data with TTL."""
        if ttl is None:
            ttl = self.cache_ttl
        expires = (datetime.now() + timedelta(seconds=ttl)).isoformat()
        conn = self._get_db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO reddit_cache (cache_key, data, expires_at) VALUES (?, ?, ?)",
                (key, json.dumps(data, default=str), expires)
            )
            conn.commit()
        finally:
            conn.close()

    # ── Sentiment scoring ──────────────────────────────────────────────────

    def _score_text(self, text: str) -> Tuple[float, int, int]:
        """
        Score text on -1 to +1 scale using keyword matching.
        Returns (sentiment_score, bullish_count, bearish_count).
        """
        if not text:
            return (0.0, 0, 0)
        text_lower = text.lower()
        bullish = sum(1 for kw in BULLISH_KEYWORDS if kw in text_lower)
        bearish = sum(1 for kw in BEARISH_KEYWORDS if kw in text_lower)
        total = bullish + bearish
        if total == 0:
            return (0.0, 0, 0)
        score = (bullish - bearish) / total
        # Clamp to [-1, 1]
        score = max(-1.0, min(1.0, score))
        return (score, bullish, bearish)

    def _text_has_ticker_buzz(self, text: str) -> bool:
        """Quick check if text contains any bullish/bearish keyword."""
        if not text:
            return False
        text_lower = text.lower()
        return any(kw in text_lower for kw in BULLISH_KEYWORDS | BEARISH_KEYWORDS)

    # ── Ticker extraction ──────────────────────────────────────────────────

    def _extract_tickers(self, text: str) -> Set[str]:
        """
        Extract ticker symbols from text.
        Primary: $TICKER (e.g., $GME, $AAPL)
        Secondary: Contextual uppercase words in subreddits known for finance chatter.
        """
        if not text:
            return set()

        tickers: Set[str] = set()

        # $TICKER pattern — high precision
        for m in TICKER_RE.finditer(text):
            t = m.group(1).upper()
            if t not in EXCLUDED_WORDS:
                tickers.add(t)

        # Bare uppercase ticker candidates — only in finance subreddits,
        # only when there's adjacent bullish/bearish context
        if self._text_has_ticker_buzz(text):
            for m in TICKER_CANDIDATE_RE.finditer(text):
                t = m.group(1).upper()
                if t not in EXCLUDED_WORDS:
                    tickers.add(t)

        return tickers

    @staticmethod
    def is_valid_ticker(s: str) -> bool:
        """Check if a string is a valid US equity ticker."""
        return bool(re.match(r'^[A-Z]{1,5}$', s))

    # ── PRAW fetching ──────────────────────────────────────────────────────

    def _fetch_subreddit_posts_praw(self, subreddit_name: str) -> List[Dict]:
        """Fetch posts from a single subreddit via PRAW.
        Only succeeds if PRAW has valid OAuth credentials."""
        if not self._has_praw or not self._praw:
            return []

        # Quick connectivity test — PRAW without client_secret often fails
        try:
            test = self._praw.subreddit(subreddit_name).title
        except Exception:
            log.debug("PRAW not authenticated for r/%s, falling back to RSS", subreddit_name)
            return []

        posts = []
        try:
            sub = self._praw.subreddit(subreddit_name)
            if FETCH_MODE == "hot":
                submissions = sub.hot(limit=POST_LIMIT_PER_SUB)
            elif FETCH_MODE == "new":
                submissions = sub.new(limit=POST_LIMIT_PER_SUB)
            else:
                submissions = sub.rising(limit=POST_LIMIT_PER_SUB)

            for submission in submissions:
                try:
                    text = (submission.title or "") + " " + (submission.selftext or "")
                    tickers = self._extract_tickers(text)

                    if not tickers:
                        continue

                    sentiment, bull, bear = self._score_text(text)
                    engagement = (submission.score or 0) + (submission.num_comments or 0) * 2
                    total_mentions = len(tickers)

                    post = {
                        "ticker": "",  # Determined per-ticker in DB
                        "tickers": list(tickers),
                        "subreddit": subreddit_name,
                        "post_title": (submission.title or "")[:200],
                        "post_text": (submission.selftext or "")[:500],
                        "upvotes": submission.score or 0,
                        "comment_count": submission.num_comments or 0,
                        "score_ratio": submission.upvote_ratio or 0.5,
                        "flair": submission.link_flair_text or "",
                        "sentiment_score": sentiment,
                        "signal_strength": self._compute_post_signal(sentiment, engagement),
                        "post_url": f"https://reddit.com{submission.permalink}" if hasattr(submission, 'permalink') else "",
                        "post_id": submission.id,
                        "created_utc": submission.created_utc,
                        "mention_count": total_mentions,
                        "source": "praw",
                    }
                    posts.append(post)
                except Exception as e:
                    log.debug("PRAW post processing error: %s", e)
                    continue
        except Exception as e:
            log.warning("PRAW fetch error for r/%s: %s", subreddit_name, e)

        return posts

    def _compute_post_signal(self, sentiment: float, engagement: int) -> float:
        """
        Compute signal strength [0, 1] for a post based on sentiment and engagement.

        High engagement + strong sentiment = strong signal.
        Neutral or low engagement = weak signal.
        """
        abs_sent = abs(sentiment)
        # Engagement scaling: log-based, capped at 0.5
        if engagement <= 0:
            eng_score = 0.0
        else:
            eng_score = min(0.5, 0.1 * (engagement ** 0.5))

        return min(1.0, abs_sent * 0.5 + eng_score)

    # ── Reddit RSS fallback (no PRAW) ───────────────────────────────────────

    def _fetch_subreddit_posts_rss(self, subreddit_name: str) -> List[Dict]:
        """
        Fallback: fetch Reddit posts via public RSS/Atom feed.
        No API keys needed — just a User-Agent header.
        Reddit blocks .json for automated access but .rss still works.
        """
        import html
        from urllib.request import urlopen, Request as URLRequest
        from urllib.error import URLError

        posts = []
        url = f"https://www.reddit.com/r/{subreddit_name}/hot/.rss"

        try:
            req = URLRequest(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                },
            )
            with urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8", errors="replace")

            # Parse Atom feed entries
            entries = re.findall(r'<entry>(.*?)</entry>', raw, re.DOTALL)
            for entry in entries:
                entry_title = re.findall(r'<title>(.*?)</title>', entry)
                entry_id = re.findall(r'<id>(.*?)</id>', entry)
                entry_links = re.findall(r'<link[^>]*href="([^"]+)"', entry)
                entry_content = re.findall(r'<content[^>]*>(.*?)</content>', entry, re.DOTALL)
                entry_published = re.findall(r'<published>(.*?)</published>', entry)
                entry_updated = re.findall(r'<updated>(.*?)</updated>', entry)

                if not entry_title:
                    continue

                title = html.unescape(entry_title[0].strip())

                # Parse content (HTML) into plain text
                content_html = entry_content[0] if entry_content else ""
                content_text = re.sub(r'<[^>]+>', ' ', content_html)
                content_text = html.unescape(content_text)
                content_text = re.sub(r'\s+', ' ', content_text).strip()

                # Reddit RSS content usually contains a "submitted by u/..." prefix
                full_text = title + " " + content_text
                tickers = self._extract_tickers(full_text)
                if not tickers:
                    continue

                sentiment, bull, bear = self._score_text(full_text)
                link = entry_links[0] if entry_links else ""

                # Parse timestamp (Reddit RSS uses ISO 8601 timestamps)
                created_utc = time.time()
                pub_str = entry_published[0] if entry_published else (entry_updated[0] if entry_updated else "")
                if pub_str:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(pub_str.replace('Z', '+00:00'))
                        created_utc = dt.timestamp()
                    except (ValueError, AttributeError):
                        pass

                post = {
                    "ticker": "",
                    "tickers": list(tickers),
                    "subreddit": subreddit_name,
                    "post_title": title[:200],
                    "post_text": content_text[:500],
                    "upvotes": 0,  # RSS doesn't include scores
                    "comment_count": 0,  # RSS doesn't include comment counts
                    "score_ratio": 0.5,
                    "flair": "",
                    "sentiment_score": sentiment,
                    "signal_strength": self._compute_post_signal(sentiment, len(tickers) * 2),
                    "post_url": link[:500],
                    "post_id": entry_id[0].strip() if entry_id else "",
                    "created_utc": created_utc,
                    "mention_count": len(tickers),
                    "source": "reddit_rss",
                }
                posts.append(post)

        except URLError as e:
            log.debug("Reddit RSS fetch error for r/%s: %s", subreddit_name, e)
        except Exception as e:
            log.debug("Reddit RSS unexpected error for r/%s: %s", subreddit_name, e)

        return posts

    # ── Main fetch ─────────────────────────────────────────────────────────

    def fetch_all_posts(self) -> List[Dict]:
        """
        Fetch recent posts from all target subreddits.
        Returns a list of post dicts with tickers, sentiment, and signal.

        Results are cached in-memory for CACHE_TTL_SECONDS to avoid
        rate limiting from Reddit's aggressive 429 enforcement.
        """
        now = time.time()
        if hasattr(self, '_all_posts_cache') and self._all_posts_cache:
            cached_data, cached_at = self._all_posts_cache
            if (now - cached_at) < (self.cache_ttl * 0.6):  # 60% of TTL
                log.debug("Returning cached fetch_all_posts result (%d posts)", len(cached_data))
                return cached_data

        all_posts: List[Dict] = []
        for sub in self.subreddits:
            # Primary: Reddit RSS feed (no auth needed)
            try:
                posts = self._fetch_subreddit_posts_rss(sub)
            except Exception as e:
                log.debug("RSS fetch error for r/%s: %s", sub, e)
                posts = []
            if not posts and self._has_praw:
                # Fallback: PRAW (requires properly configured credentials)
                try:
                    posts = self._fetch_subreddit_posts_praw(sub)
                except Exception:
                    posts = []
            all_posts.extend(posts)

        self._all_posts_cache = (all_posts, now)
        return all_posts

    def fetch_ticker_sentiment(self, ticker: str) -> Dict:
        """
        Fetch and compute sentiment for a single ticker across all subreddits.

        Returns:
        {
            "ticker": "GME",
            "posts": 5,
            "total_upvotes": 234,
            "total_comments": 45,
            "sentiment_score": 0.65,        # -1 to +1
            "normalized_score": 0.825,       # 0 to 1 (0 = most bearish, 1 = most bullish)
            "signal_score": 0.72,           # 0 to 1 (sentiment * engagement)
            "mention_count": 12,
            "subreddit_breakdown": {...},
            "top_posts": [...],
            "source": "praw",
            "cached": bool,
        }
        """
        ticker_upper = ticker.upper()

        # Check cache
        cache_key = f"reddit:ticker:{ticker_upper}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            cached["cached"] = True
            return cached

        all_posts = self.fetch_all_posts()
        ticker_posts = [p for p in all_posts if ticker_upper in p.get("tickers", [])]

        if not ticker_posts:
            result = self._empty_ticker_result(ticker_upper)
            self._cache_set(cache_key, result)
            return result

        # Process
        total_upvotes = 0
        total_comments = 0
        weighted_sentiment = 0.0
        total_weight = 0.0
        sub_breakdown: Dict[str, Dict] = {}
        mention_count = 0
        top_posts: List[Dict] = []

        for post in ticker_posts:
            upvotes = post.get("upvotes", 0)
            comments = post.get("comment_count", 0)
            weight = max(1.0, upvotes + comments * 2)
            sentiment = post.get("sentiment_score", 0.0)
            mentions = post.get("mention_count", 1)
            sub = post.get("subreddit", "unknown")

            total_upvotes += upvotes
            total_comments += comments
            weighted_sentiment += sentiment * weight
            total_weight += weight
            mention_count += mentions

            # Subreddit breakdown
            if sub not in sub_breakdown:
                sub_breakdown[sub] = {"posts": 0, "upvotes": 0, "sentiment_sum": 0.0}
            sub_breakdown[sub]["posts"] += 1
            sub_breakdown[sub]["upvotes"] += upvotes
            sub_breakdown[sub]["sentiment_sum"] += sentiment

            top_posts.append({
                "title": post.get("post_title", ""),
                "subreddit": sub,
                "upvotes": upvotes,
                "sentiment_score": sentiment,
                "signal_strength": post.get("signal_strength", 0.0),
                "post_url": post.get("post_url", ""),
            })

        sentiment_score = round(weighted_sentiment / total_weight, 4) if total_weight > 0 else 0.0
        normalized_score = round((sentiment_score + 1.0) / 2.0, 4)  # Map -1..1 to 0..1
        signal_score = self._compute_aggregate_signal(ticker_posts)

        # Compute momentum based on upvote acceleration
        if len(ticker_posts) >= 3:
            sorted_posts = sorted(ticker_posts, key=lambda p: p.get("created_utc", 0))
            recent = sorted_posts[-3:]
            older = sorted_posts[:3]
            recent_avg = sum(p.get("upvotes", 0) for p in recent) / max(1, len(recent))
            older_avg = sum(p.get("upvotes", 0) for p in older) / max(1, len(older))
            momentum = (recent_avg - older_avg) / max(1, older_avg) if older_avg > 0 else 0.0
            momentum = max(-1.0, min(1.0, momentum))
            momentum_label = "accelerating" if momentum > 0.1 else ("decelerating" if momentum < -0.1 else "stable")
        else:
            momentum = 0.0
            momentum_label = "insufficient_data"

        # Compute subreddit breakdown averages
        for sub, data in sub_breakdown.items():
            data["avg_sentiment"] = round(data["sentiment_sum"] / data["posts"], 4)
            del data["sentiment_sum"]

        # Sort top posts by signal_strength
        top_posts.sort(key=lambda p: p["signal_strength"], reverse=True)

        result = {
            "ticker": ticker_upper,
            "posts": len(ticker_posts),
            "total_upvotes": total_upvotes,
            "total_comments": total_comments,
            "sentiment_score": sentiment_score,
            "normalized_score": normalized_score,
            "signal_score": signal_score,
            "mention_count": mention_count,
            "momentum": round(momentum, 4),
            "momentum_label": momentum_label,
            "subreddit_breakdown": sub_breakdown,
            "top_posts": top_posts[:10],
            "source": "praw" if self._has_praw else "reddit_rss",
            "cached": False,
        }

        self._cache_set(cache_key, result)
        self._store_velocity(ticker_upper, ticker_posts)
        return result

    def _empty_ticker_result(self, ticker: str) -> Dict:
        return {
            "ticker": ticker,
            "posts": 0,
            "total_upvotes": 0,
            "total_comments": 0,
            "sentiment_score": 0.0,
            "normalized_score": 0.5,
            "signal_score": 0.0,
            "mention_count": 0,
            "momentum": 0.0,
            "momentum_label": "no_data",
            "subreddit_breakdown": {},
            "top_posts": [],
            "source": "praw" if self._has_praw else "reddit_rss",
            "cached": False,
        }

    # ── Velocity tracking ──────────────────────────────────────────────────

    def _store_velocity(self, ticker: str, posts: List[Dict]) -> None:
        """Store a velocity datapoint for mention count tracking."""
        if not posts:
            return

        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(hours=1)).isoformat()
        window_end = now.isoformat()

        mention_count = sum(p.get("mention_count", 1) for p in posts)
        sentiments = [p.get("sentiment_score", 0.0) for p in posts]
        avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0

        # Daily breakdown
        daily: Dict[str, int] = {}
        for p in posts:
            day_key = datetime.fromtimestamp(
                p.get("created_utc", time.time()), tz=timezone.utc
            ).strftime("%Y-%m-%d")
            daily[day_key] = daily.get(day_key, 0) + 1

        conn = self._get_db()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO reddit_velocity
                   (ticker, window_start, window_end, mention_count, avg_sentiment, daily_breakdown)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (ticker, window_start, window_end, mention_count, avg_sentiment,
                 json.dumps(daily))
            )
            conn.commit()
        finally:
            conn.close()

    def get_velocity_vs_baseline(self, ticker: str) -> Dict:
        """
        Compute mention velocity vs 7-day rolling baseline.

        Returns:
        {
            "ticker": "GME",
            "current_velocity": 42.5,        # mentions/day last 24h
            "baseline_velocity": 15.3,        # avg mentions/day over 7 days
            "velocity_ratio": 2.78,           # current / baseline
            "velocity_trend": "rising",       # rising, falling, stable
            "raw_counts": {...},
        }
        """
        ticker_upper = ticker.upper()
        now = datetime.now(timezone.utc)

        # Recent 24h
        recent_start = (now - timedelta(hours=24)).isoformat()

        # Baseline 7-day (excluding last 24h)
        baseline_start = (now - timedelta(days=7)).isoformat()
        baseline_end = (now - timedelta(hours=24)).isoformat()

        conn = self._get_db()
        try:
            # Current velocity (last 24h)
            current = conn.execute(
                """SELECT COALESCE(SUM(mention_count), 0) as total,
                          COUNT(*) as windows
                   FROM reddit_velocity
                   WHERE ticker = ? AND window_start >= ?""",
                (ticker_upper, recent_start)
            ).fetchone()

            # Baseline (7 days - 24h)
            baseline = conn.execute(
                """SELECT COALESCE(SUM(mention_count), 0) as total,
                          COUNT(*) as windows
                   FROM reddit_velocity
                   WHERE ticker = ? AND window_start >= ? AND window_start < ?""",
                (ticker_upper, baseline_start, baseline_end)
            ).fetchone()

            # Daily breakdown
            daily_rows = conn.execute(
                """SELECT daily_breakdown FROM reddit_velocity
                   WHERE ticker = ? AND window_start >= ?
                   ORDER BY window_start DESC LIMIT 50""",
                (ticker_upper, baseline_start)
            ).fetchall()
        finally:
            conn.close()

        # Parse daily breakdown
        daily_counts: Dict[str, int] = {}
        for row in daily_rows:
            try:
                day_data = json.loads(row["daily_breakdown"])
                for day, count in day_data.items():
                    daily_counts[day] = daily_counts.get(day, 0) + count
            except (json.JSONDecodeError, TypeError):
                pass

        # Calculate velocities with proper time-normalization
        current_total = current["total"] if current else 0
        baseline_total = baseline["total"] if baseline else 0
        # Try without column aliases
        if isinstance(current, sqlite3.Row):
            current_total = current["total"] if current["total"] else 0

        # Actually fix: let me recompute
        current_total = 0
        current_windows = 0
        baseline_total = 0
        baseline_windows = 0

        conn = self._get_db()
        try:
            cur = conn.execute(
                "SELECT COALESCE(SUM(mention_count), 0), COUNT(*) FROM reddit_velocity WHERE ticker = ? AND window_start >= ?",
                (ticker_upper, recent_start)
            ).fetchone()
            if cur:
                current_total = cur[0] or 0
                current_windows = cur[1] or 0

            bl = conn.execute(
                "SELECT COALESCE(SUM(mention_count), 0), COUNT(*) FROM reddit_velocity WHERE ticker = ? AND window_start >= ? AND window_start < ?",
                (ticker_upper, baseline_start, baseline_end)
            ).fetchone()
            if bl:
                baseline_total = bl[0] or 0
                baseline_windows = bl[1] or 0
        finally:
            conn.close()

        # Normalize to mentions per hour
        current_hours = max(1, current_windows)  # each window ~1 hour
        baseline_hours = max(1, baseline_windows)

        # Compute daily equivalents
        current_per_day = (current_total / current_hours) * 24
        baseline_per_day = (baseline_total / baseline_hours) * 24

        velocity_ratio = round(current_per_day / baseline_per_day, 2) if baseline_per_day > 0 else 1.0

        if velocity_ratio > 1.5:
            trend = "rising"
        elif velocity_ratio < 0.67:
            trend = "falling"
        else:
            trend = "stable"

        return {
            "ticker": ticker_upper,
            "current_velocity": round(current_per_day, 1),
            "baseline_velocity": round(baseline_per_day, 1),
            "velocity_ratio": velocity_ratio,
            "velocity_trend": trend,
            "raw_counts": {
                "current_total": current_total,
                "current_hours": current_hours,
                "baseline_total": baseline_total,
                "baseline_hours": baseline_hours,
            },
            "daily_breakdown": dict(sorted(daily_counts.items(), reverse=True)),
        }

    # ── Aggregate signal computation ───────────────────────────────────────

    def _compute_aggregate_signal(self, posts: List[Dict]) -> float:
        """
        Compute aggregate signal score [0, 1] for a set of posts.

        Formula:
        - Avg sentiment × 0.4 (sentiment strength)
        - Engagement × 0.3 (upvotes + comments normalized)
        - Post count × 0.2 (breadth)
        - Sentiment disagreement × 0.1 (penalty for mixed signals)
        """
        if not posts:
            return 0.0

        n = len(posts)
        sentiments = [p.get("sentiment_score", 0.0) for p in posts]
        upvotes = [p.get("upvotes", 0) for p in posts]

        avg_sentiment = sum(sentiments) / n
        abs_sentiment = abs(avg_sentiment)

        # Engagement: log-scaled sum
        total_engagement = sum(min(500, u) for u in upvotes)
        eng_score = min(1.0, total_engagement / 1000.0)

        # Breadth: number of posts (diminishing returns)
        breadth_score = min(1.0, n / 30.0)

        # Disagreement penalty: high variance in sentiment = mixed signal
        variance = sum((s - avg_sentiment) ** 2 for s in sentiments) / n if n > 1 else 0
        agreement_score = max(0.0, 1.0 - (variance * 2.0))

        signal = (
            abs_sentiment * 0.40 +
            eng_score * 0.30 +
            breadth_score * 0.20 +
            agreement_score * 0.10
        )

        return round(min(1.0, signal), 4)

    def fetch_aggregate_signal(self, tickers: Optional[List[str]] = None,
                               min_posts: int = 2) -> Dict:
        """
        Fetch aggregate social signal for all or specified tickers.

        Returns:
        {
            "status": "ok",
            "tickers_scanned": 5,
            "signal": {"avg_normalized_score": 0.65, "avg_signal_score": 0.42, ...},
            "scores": {
                "GME": {"signal_score": 0.72, "normalized_score": 0.825, ...},
                ...
            },
            "top_tickers": [...sorted by signal],
        }
        """
        all_posts = self.fetch_all_posts()

        # Group posts by ticker
        ticker_posts: Dict[str, List[Dict]] = defaultdict(list)
        for post in all_posts:
            for t in post.get("tickers", []):
                t_upper = t.upper()
                if self.is_valid_ticker(t_upper):
                    ticker_posts[t_upper].append(post)

        if tickers:
            # Filter to requested tickers
            requested = set(t.upper() for t in tickers)
            ticker_posts = {t: posts for t, posts in ticker_posts.items() if t in requested}

        scores: Dict[str, Dict] = {}
        for t, posts in ticker_posts.items():
            if len(posts) < min_posts:
                continue
            sentiment = self._compute_aggregate_signal(posts)
            # Also get per-ticker detail
            scores[t] = {
                "signal_score": sentiment,
                "post_count": len(posts),
                "total_upvotes": sum(p.get("upvotes", 0) for p in posts),
            }

        if not scores:
            return {"status": "ok", "tickers_scanned": 0, "signal": {}, "scores": {}, "top_tickers": []}

        avg_signal = sum(s["signal_score"] for s in scores.values()) / len(scores)
        avg_posts = sum(s["post_count"] for s in scores.values()) / len(scores)
        top_tickers = sorted(scores.items(), key=lambda x: x[1]["signal_score"], reverse=True)

        return {
            "status": "ok",
            "tickers_scanned": len(scores),
            "signal": {
                "avg_signal_score": round(avg_signal, 4),
                "avg_post_count": round(avg_posts, 1),
            },
            "scores": scores,
            "top_tickers": [{"ticker": t, "signal": s["signal_score"]} for t, s in top_tickers[:20]],
        }

    def fetch_normalized_signal(self, ticker: str) -> Dict:
        """
        Fetch a normalized 0-1 signal score for a ticker, suitable for
        plugging directly into Stonks's conviction formula.

        Combines:
        - Sentiment score (normalized 0-1) × 0.40
        - Velocity ratio × 0.25 (mention surge)
        - Signal strength × 0.25 (engagement-weighted)
        - Momentum × 0.10 (upvote acceleration)
        """
        ticker_upper = ticker.upper()

        # Check signal cache (short TTL for aggregate signals)
        cache_key = f"reddit:signal:{ticker_upper}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        sentiment_data = self.fetch_ticker_sentiment(ticker_upper)
        velocity = self.get_velocity_vs_baseline(ticker_upper)

        normalized = sentiment_data.get("normalized_score", 0.5)
        velocity_ratio = min(3.0, velocity.get("velocity_ratio", 1.0))
        velocity_score = min(1.0, velocity_ratio / 3.0)
        signal_strength = sentiment_data.get("signal_score", 0.0)
        momentum = max(0.0, min(1.0, (sentiment_data.get("momentum", 0.0) + 1.0) / 2.0))

        combined = (
            normalized * 0.40 +
            velocity_score * 0.25 +
            signal_strength * 0.25 +
            momentum * 0.10
        )

        result = {
            "ticker": ticker_upper,
            "signal_score": round(combined, 4),
            "components": {
                "sentiment_normalized": normalized,
                "velocity_score": round(velocity_score, 4),
                "signal_strength": signal_strength,
                "momentum": round(momentum, 4),
            },
            "velocity": velocity,
            "post_count": sentiment_data.get("posts", 0),
            "source": sentiment_data.get("source", "unknown"),
            "cached": False,
        }

        self._cache_set(cache_key, result, ttl=SIGNAL_TTL)
        return result


# ── Module-level singleton ────────────────────────────────────────────────────
_default_pipeline: Optional[SocialRedditPipeline] = None


def get_pipeline() -> SocialRedditPipeline:
    """Get or create the default pipeline instance."""
    global _default_pipeline
    if _default_pipeline is None:
        _default_pipeline = SocialRedditPipeline()
    return _default_pipeline


def fetch_ticker_sentiment(ticker: str) -> Dict:
    """Convenience: fetch sentiment for a single ticker."""
    return get_pipeline().fetch_ticker_sentiment(ticker)


def fetch_normalized_signal(ticker: str) -> Dict:
    """Convenience: fetch normalized 0-1 signal for a ticker."""
    return get_pipeline().fetch_normalized_signal(ticker)


def fetch_aggregate_signal(tickers: Optional[List[str]] = None) -> Dict:
    """Convenience: fetch aggregate signal for all tracked tickers."""
    return get_pipeline().fetch_aggregate_signal(tickers)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [social_reddit] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    import argparse
    parser = argparse.ArgumentParser(description="Reddit social sentiment pipeline")
    parser.add_argument("--ticker", type=str, help="Fetch sentiment for a single ticker")
    parser.add_argument("--aggregate", action="store_true", help="Fetch aggregate signal")
    parser.add_argument("--velocity", type=str, help="Get velocity vs baseline for a ticker")
    parser.add_argument("--signal", type=str, help="Get normalized 0-1 signal for a ticker")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")

    args = parser.parse_args()
    pipeline = get_pipeline()

    if args.ticker:
        result = pipeline.fetch_ticker_sentiment(args.ticker)
        print(json.dumps(result, indent=2, default=str))
    elif args.signal:
        result = pipeline.fetch_normalized_signal(args.signal)
        print(json.dumps(result, indent=2, default=str))
    elif args.velocity:
        result = pipeline.get_velocity_vs_baseline(args.velocity)
        print(json.dumps(result, indent=2, default=str))
    elif args.aggregate:
        result = pipeline.fetch_aggregate_signal()
        print(json.dumps(result, indent=2, default=str))
    else:
        # Default: show summary of all tickers
        result = pipeline.fetch_aggregate_signal(min_posts=1)
        print("Reddit Social Sentiment — Aggregate Report")
        print("=" * 60)
        if result["tickers_scanned"] == 0:
            print("No tickers detected. Try --ticker GME or --ticker AAPL")
        else:
            print(f"Tickers scanned: {result['tickers_scanned']}")
            if result.get("signal"):
                s = result["signal"]
                print(f"Avg signal score: {s.get('avg_signal_score', 'N/A')}")
                print(f"Avg posts/ticker: {s.get('avg_post_count', 'N/A')}")
            print("\nTop tickers by signal:")
            for t in result.get("top_tickers", [])[:10]:
                print(f"  {t['ticker']:>6}: {t['signal']:.4f}")
        print("\nTip: Use --ticker TICKER for detail, --signal TICKER for 0-1 score")
