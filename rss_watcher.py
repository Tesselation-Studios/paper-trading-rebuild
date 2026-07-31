#!/usr/bin/env python3
"""
Social Watcher for Stan (trader-stonks).

Polls Stocktwits and Bluesky for sentiment signals during market hours.
When new posts appear mentioning Stan's held tickers or high-energy keywords,
wakes Stan up via: openclaw agent --agent trader-stonks --message "..."

Run via cron: */3 9-16 * * 1-5 (market hours only)

Usage:
    python3 src/rss_watcher.py
    python3 src/rss_watcher.py --dry-run     # show what would fire, don't wake agent
    python3 src/rss_watcher.py --reset       # clear seen post IDs and exit
"""

import sys
import os
import json
import re
import sqlite3
import subprocess
import argparse
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).parent))

# Import social sentiment augmentation
from social_sentiment import (
    fetch_bluesky_sentiment,
    fetch_stocktwits_sentiment,
    match_sentiment_tickers,
    get_watchlist_tickers,
)

# ── config ────────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    # Reddit RSS (public, no-auth — PRAW API removed per Raf directive 2026-06-18)
    "https://www.reddit.com/r/wallstreetbets/.rss",
    "https://www.reddit.com/r/stocks/.rss",
    "https://www.reddit.com/r/investing/.rss",
]

KEYWORDS = [
    "squeeze", "moon", "yolo", "calls", "puts", "gamma", "rally",
    "bull", "pump", "earnings play", "short", "DD", "due diligence",
    "options", "breakout", "momentum", "catalyst",
]

# SQLite to track seen post IDs (prevent re-firing on the same post)
STATE_DB = Path.home() / ".openclaw" / "workspace-trader-stonks" / "rss_watcher.db"
TRADER_DB = Path(__file__).resolve().parent / "shared" / "trader.db"

AGENT_ID = "trader-stonks"

# Sources to check (overridden by --source flag)
SOURCES = ["bluesky", "stocktwits"]

# Sentiment thresholds: only wake Stan if signal crosses these
SENTIMENT_POST_THRESHOLD = 3     # min posts/messages to consider
SENTIMENT_BULLISH_THRESHOLD = 0.60  # min bullish_pct to treat as signal
SENTIMENT_ENGAGEMENT_THRESHOLD = 5   # min likes+reposts to mention

# ── state DB ─────────────────────────────────────────────────────────────────

def _open_state_db():
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STATE_DB))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_posts (
            post_id TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fired_at TEXT NOT NULL,
            trigger_ticker TEXT,
            trigger_keyword TEXT,
            post_title TEXT,
            post_url TEXT,
            feed TEXT
        )
    """)
    # Prune old entries (keep last 7 days)
    conn.execute(
        "DELETE FROM seen_posts WHERE seen_at < datetime('now', '-7 days')"
    )
    conn.execute(
        "DELETE FROM signal_log WHERE fired_at < datetime('now', '-7 days')"
    )
    conn.commit()
    return conn


def _is_seen(conn, post_id):
    row = conn.execute("SELECT 1 FROM seen_posts WHERE post_id=?", (post_id,)).fetchone()
    return row is not None


def _mark_seen(conn, post_id):
    conn.execute(
        "INSERT OR IGNORE INTO seen_posts (post_id, seen_at) VALUES (?, datetime('now'))",
        (post_id,)
    )
    conn.commit()


def _log_signal(conn, ticker, keyword, title, url, feed):
    conn.execute(
        "INSERT INTO signal_log (fired_at, trigger_ticker, trigger_keyword, post_title, post_url, feed) "
        "VALUES (datetime('now'), ?, ?, ?, ?, ?)",
        (ticker, keyword, title[:500], url[:500], feed)
    )
    conn.commit()


# ── ticker loading ────────────────────────────────────────────────────────────

def _get_stan_tickers():
    """Load Stan's current positions + watchlist from trader.db."""
    tickers = set()
    if not TRADER_DB.exists():
        return tickers
    try:
        conn = sqlite3.connect(str(TRADER_DB))
        conn.execute("PRAGMA busy_timeout=5000")
        # Positions
        for row in conn.execute("SELECT ticker FROM positions WHERE quantity > 0 AND status='open'"):
            tickers.add(row[0].upper())
        # Watchlist
        for row in conn.execute("SELECT ticker FROM watchlist WHERE agent_id=?", (AGENT_ID,)):
            tickers.add(row[0].upper())
        # Recent decisions (last 7 days)
        for row in conn.execute(
            "SELECT DISTINCT ticker FROM decisions WHERE agent_id=? AND timestamp > datetime('now', '-7 days')",
            (AGENT_ID,)
        ):
            if row[0]:
                tickers.add(row[0].upper())
        conn.close()
    except Exception as e:
        print(f"[rss_watcher] Warning: could not load Stan tickers: {e}", file=sys.stderr)
    # Filter: only valid US equity symbols
    return {t for t in tickers if re.match(r'^[A-Z]{1,5}$', t)}


# ── RSS parsing ───────────────────────────────────────────────────────────────

def _fetch_rss(url):
    """Fetch RSS/Atom feed, return list of (id, title, link) tuples."""
    try:
        req = Request(url, headers={"User-Agent": "paper-trading-rss-watcher/1.0"})
        with urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8", errors="replace")
    except URLError as e:
        print(f"[rss_watcher] Feed fetch error {url}: {e}", file=sys.stderr)
        return []

    posts = []
    # Parse entry/item blocks (works for both RSS 2.0 and Atom)
    # Extract IDs
    ids = re.findall(r'<id>(.*?)</id>', content) or re.findall(r'<guid[^>]*>(.*?)</guid>', content)
    titles = re.findall(r'<title[^>]*><!\[CDATA\[(.*?)\]\]></title>|<title[^>]*>(.*?)</title>', content)
    links = re.findall(r'<link[^>]*href=["\']([^"\']+)["\']|<link>(.*?)</link>', content)

    # Flatten tuple groups
    titles_flat = [a or b for a, b in titles]
    links_flat = [a or b for a, b in links]

    # Skip the first title/link (feed-level, not entry-level)
    if len(titles_flat) > 1:
        titles_flat = titles_flat[1:]
    if len(links_flat) > 1:
        links_flat = links_flat[1:]

    for i, post_id in enumerate(ids):
        title = titles_flat[i] if i < len(titles_flat) else ""
        link = links_flat[i] if i < len(links_flat) else ""
        posts.append((post_id.strip(), title.strip(), link.strip()))

    return posts


# ── matching ──────────────────────────────────────────────────────────────────

def _match_post(title, tickers):
    """
    Check if a post title mentions a tracked ticker or high-energy keywords.
    Returns (matched_ticker_or_None, matched_keyword_or_None).
    """
    title_upper = title.upper()
    title_lower = title.lower()

    # Ticker match: look for $TICKER or standalone word matching ticker
    for ticker in tickers:
        if f"${ticker}" in title_upper:
            return ticker, None
        # Word boundary match (avoid false positives like "AMD" in "AMENDED")
        if re.search(r'\b' + ticker + r'\b', title_upper):
            return ticker, None

    # Keyword match
    for kw in KEYWORDS:
        if kw.lower() in title_lower:
            return None, kw

    return None, None


# ── agent wake-up ─────────────────────────────────────────────────────────────

def _wake_stan(signals, dry_run=False, social_signals=None):
    """
    Send a single consolidated message to trader-stonks with all matched signals.
    signals: list of (ticker, keyword, title, url, feed) — now only social_signals used
    social_signals: optional dict of social sentiment results per source
    """
    lines = []

    # Append social sentiment signals
    if social_signals:
        for ss in social_signals:
            source = ss.get("source", "unknown")
            ticker = ss.get("ticker", "?")
            posts = ss.get("posts", 0) or ss.get("messages", 0)
            bullish = ss.get("bullish_pct", 0)
            score = ss.get("sentiment_score", 0)
            top = ss.get("top_posts", []) or ss.get("top_messages", [])
            trend = "🟢 BULLISH" if bullish >= 0.60 else "🔴 BEARISH" if bullish <= 0.40 else "🟡 NEUTRAL"
            lines.append(
                f'• {source.title()} — ${ticker}: {posts} posts, '
                f'{bullish:.0%} bullish, score {score:+.2f} [{trend}]'
            )
            # Add a sample top post
            if top:
                sample = top[0]
                sample_text = sample.get("text", "") or sample.get("body", "")
                sample_eng = sample.get("likes", 0) + sample.get("reposts", 0)
                lines.append(f'  ↳ "{sample_text[:100]}" (❤️{sample_eng})')

    total_signal_count = len(signals) + (len(social_signals) if social_signals else 0)
    source_labels = []
    if social_signals:
        seen_sources = {ss.get("source", "?") for ss in social_signals}
        source_labels.extend(s.title() for s in seen_sources)

    sources_str = " + ".join(source_labels)
    msg = (
        f"SOCIAL SIGNAL ({total_signal_count} new signals from {sources_str}): "
        f"Community is buzzing. Scan these now and decide if any confirm a trade:\n"
        + "\n".join(lines)
    )

    print(f"[rss_watcher] Firing signal to {AGENT_ID}:\n{msg}\n")

    if dry_run:
        print("[rss_watcher] DRY RUN — not sending.")
        return

    try:
        result = subprocess.run(
            ["openclaw", "agent", "--agent", AGENT_ID, "--message", msg],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"[rss_watcher] Warning: openclaw returned {result.returncode}: {result.stderr}", file=sys.stderr)
        else:
            print(f"[rss_watcher] Signal sent OK.")
    except subprocess.TimeoutExpired:
        print("[rss_watcher] Warning: openclaw agent call timed out.", file=sys.stderr)
    except Exception as e:
        print(f"[rss_watcher] Error sending signal: {e}", file=sys.stderr)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="RSS/Social watcher for Stan (trader-stonks)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would fire without waking agent")
    parser.add_argument("--reset", action="store_true", help="Clear seen post IDs and exit")
    parser.add_argument(
        "--source", choices=["bluesky", "stocktwits", "all"],
        default="bluesky",
        help="Social source to poll (default: bluesky)",
    )
    args = parser.parse_args()

    conn = _open_state_db()

    if args.reset:
        conn.execute("DELETE FROM seen_posts")
        conn.commit()
        print("[rss_watcher] Cleared all seen post IDs.")
        conn.close()
        return

    tickers = _get_stan_tickers()
    print(f"[rss_watcher] Tracking {len(tickers)} tickers: {sorted(tickers)}")

    new_signals = []
    social_signals = []

    # ── Bluesky ──

    # ── Bluesky ──
    if args.source in ("bluesky", "all"):
        print("[rss_watcher] Checking Bluesky AT Protocol...")
        for ticker in sorted(tickers):
            result = fetch_bluesky_sentiment(ticker)
            posts = result.get("posts", 0)
            bullish = result.get("bullish_pct", 0)
            if posts >= SENTIMENT_POST_THRESHOLD and bullish >= SENTIMENT_BULLISH_THRESHOLD:
                social_signals.append(result)
                print(f"[rss_watcher] Bluesky ${ticker}: {posts} posts, {bullish:.0%} bullish ⚡")
            elif posts > 0:
                print(f"[rss_watcher] Bluesky ${ticker}: {posts} posts, {bullish:.0%} bullish (below threshold)")

    # ── Stocktwits ──
    if args.source in ("stocktwits", "all"):
        print("[rss_watcher] Checking Stocktwits...")
        for ticker in sorted(tickers):
            result = fetch_stocktwits_sentiment(ticker)
            msgs = result.get("messages", 0)
            bullish = result.get("bullish_pct", 0)
            if msgs >= SENTIMENT_POST_THRESHOLD and bullish >= SENTIMENT_BULLISH_THRESHOLD:
                social_signals.append(result)
                print(f"[rss_watcher] Stocktwits ${ticker}: {msgs} msgs, {bullish:.0%} bullish 🔥")
            elif msgs > 0:
                print(f"[rss_watcher] Stocktwits ${ticker}: {msgs} msgs, {bullish:.0%} bullish (below threshold)")

    conn.close()

    if new_signals or social_signals:
        _wake_stan(new_signals, dry_run=args.dry_run, social_signals=social_signals)
    else:
        print("[rss_watcher] No new matching signals.")


if __name__ == "__main__":
    main()
