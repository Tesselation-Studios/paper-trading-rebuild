#!/usr/bin/env python3
"""
News Collector — RSS feed aggregation with caching, ticker extraction,
and keyword-based sentiment scoring.

Designed as a standalone module for the Data Bus. All operations are
synchronous; integration uses a daemon thread spawned by the main process.

Fetches from free RSS feeds (no API keys), deduplicates by URL, stores
results in Postgres (news_cache table), and provides a keyword sentiment
score using the same VADER-compatible word lists as data_bus.py.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import requests

log = logging.getLogger("news_collector")

# ── RSS Feed Sources ──────────────────────────────────────────────────────────
# Free, no API key required. Sourced from major financial publishers.

RSS_FEEDS: Dict[str, str] = {
    "marketwatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "marketwatch_rss": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "yahoo": "https://finance.yahoo.com/news/rssindex",
    "bloomberg": "https://feeds.bloomberg.com/markets/news.rss",
    "cnbc": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "seekingalpha": "https://seekingalpha.com/feed.xml",
}

# ── VADER-compatible keyword sentiment (mirrors data_bus.py _simple_sentiment) ─

_SENTIMENT_POSITIVE: Set[str] = {
    "bullish", "surge", "surged", "soar", "soared", "rally", "rallied",
    "upgrade", "upgraded", "outperform", "beat", "beats", "exceed",
    "exceeded", "strong", "growth", "profit", "profits", "record",
    "breakout", "boom", "innovation", "leader", "leading", "optimistic",
    "positive", "momentum", "gains", "gain", "rising", "rise",
    "rebound", "recovery", "opportunity", "opportunities", "dividend",
    "dividends", "buyback", "expansion", "expand", "approved",
    "breakthrough", "partnership", "launch", "success", "successful",
    "confidence", "confident", "outlook", "upside", "potential",
    "bargain", "undervalued", "overweight", "overweighted", "accumulate",
    "adding", "boost", "boosts", "skyrocket", "skyrocketed", "jump",
    "jumped", "pop", "spike", "spiked", "green", "profitability",
    "efficient", "efficiency", "raised", "raising", "target", "increase",
    "increased", "increasing",
}

_SENTIMENT_NEGATIVE: Set[str] = {
    "bearish", "plunge", "plunged", "crash", "crashed", "slump", "slumped",
    "downgrade", "downgraded", "underperform", "miss", "misses", "missed",
    "decline", "declined", "weak", "weakness", "loss", "losses", "debt",
    "liability", "risk", "risky", "volatile", "volatility", "uncertainty",
    "negative", "downturn", "recession", "inflation", "layoff", "layoffs",
    "cut", "cuts", "cutting", "sell", "selling", "sold", "dump",
    "dumped", "short", "shorted", "bear", "collapse", "collapsed",
    "bankrupt", "bankruptcy", "fraud", "investigation", "fine", "fined",
    "lawsuit", "penalty", "sanction", "deficit", "declining", "slowdown",
    "struggle", "struggling", "red", "warning", "warn", "warned",
    "underweight", "reduce", "pressure", "concern", "concerning",
    "worst", "fail", "failed", "failure", "drop", "dropped", "fall",
    "fallen", "fell", "lower", "lowered", "decrease", "decreased",
    "tightening",
}

_SENTIMENT_INTENSIFIERS: Set[str] = {
    "very", "extremely", "highly", "strongly", "significantly",
    "substantially", "massively", "dramatically", "sharply", "deeply",
}


# ── Known Tickers ─────────────────────────────────────────────────────────────
# Curated set of ~1000 heavily traded symbols (SP500 + NASDAQ100 + common ETFs).

KNOWN_TICKERS: Set[str] = {
    # SP500
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "BRK.B", "BRK.A",
    "TSLA", "UNH", "LLY", "JPM", "V", "XOM", "AVGO", "PG", "MA", "HD", "CVX",
    "MRK", "ABBV", "PEP", "KO", "COST", "ADBE", "WMT", "CRM", "BAC", "NFLX",
    "DIS", "AMD", "PYPL", "CMCSA", "TMO", "INTC", "VZ", "QCOM", "TXN", "NKE",
    "BA", "ABT", "NEE", "MS", "HON", "PM", "IBM", "DHR", "T", "RTX",
    "SPGI", "LOW", "CAT", "UNP", "AMGN", "GS", "COP", "AXP", "INTU", "BKNG",
    "TJT", "BLK", "CB", "SYK", "PLD", "SCHW", "SHEL", "C", "TMUS", "FI",
    "UPS", "DE", "ADP", "GILD", "PFE", "MMC", "BMY", "LMT", "TTE", "CMG",
    "SO", "DUK", "CI", "MDT", "ETN", "UBER", "MU", "MO", "NOC", "PNC",
    "EOG", "USO", "SLB", "FCX", "AON", "APD", "ITW", "MPC", "EMR", "ICE",
    "ZTS", "BDX", "CL", "MDLZ", "GD", "NSC", "TGT", "EQIX", "WELL", "GM",
    "OXY", "KMI", "PSX", "WMB", "OKE", "MMM", "HCA", "FDX", "SHW", "SPG",
    "DLR", "CSCO", "HUM", "CCI", "VRTX", "PLTR", "MCO", "TRV", "FISV", "AIG",
    "ALL", "MET", "PRU", "AFL", "HIG", "BRO", "AIZ", "LNC", "GL", "TW",
    "ERIE", "MKL", "CNA", "ACGL", "WRB", "CB", "WFC", "USB", "TFC", "PNFP",
    "FITB", "HBAN", "CFG", "RF", "KEY", "MTB", "STT", "NTRS", "NTRS",
    "WBD", "PARA", "FOXA", "FOX", "OMC", "IPG", "EA", "TTWO", "RBLX",
    "MANH", "ANSS", "CDNS", "SNPS", "PANW", "FTNT", "CHTR", "CMCSA",
    "DASH", "ZM", "WDAY", "TEAM", "CRWD", "DDOG", "MDB", "MRNA",
    "BIIB", "SAGE", "SRPT", "ALKS", "ILMN", "DXCM", "PODD", "ISRG",
    "MSCI", "KKR", "COIN", "HOOD", "SQ", "SHOP", "AFRM", "MELI",
    "ENPH", "SEDG", "FSLR", "GE", "HON", "EMR", "ROK", "AME",
    "PH", "JCI", "CARR", "TT", "OTIS", "IR", "TRMB", "GNRC",
    "ABNB", "EXPE", "BKNG", "HLT", "MAR", "CCL", "RCL", "NCLH",
    "LVS", "MGM", "WYNN", "DAL", "UAL", "AAL", "LUV", "JBLU",
    "SAVE", "XPEV", "NIO", "LI", "LCID", "RIVN", "F", "TSLA",
    "STLA", "VOW3.DE", "BMW.DE", "MBG.DE", "RACE",
    # Top ETFs
    "SPY", "IVV", "VOO", "QQQ", "VTI", "IWM", "DIA", "TLT", "IEF",
    "AGG", "BND", "GLD", "SLV", "USO", "XLF", "XLE", "XLK", "XLV",
    "XLI", "XLP", "XLU", "XLY", "XLRE", "XLC", "XLB", "VIG", "VYM",
    "SCHD", "SCHX", "VT", "VXUS", "BNDX", "EMB", "HYG", "LQD",
    "ARKK", "ARKG", "ARKF", "ARKQ", "ARKW", "ICLN", "TAN",
    "SOXX", "SMH", "XSD", "IBB", "XBI", "LABU", "KRE", "KBE",
    "EWJ", "EWZ", "EEM", "VWO", "FXI", "KWEB", "INDA", "EPI",
    "URA", "UUP", "FXE", "FXB", "FXY",
    # NASDAQ100
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "ALGN",
    "AMAT", "AMD", "AMGN", "AMZN", "ANSS", "ARM", "ASML", "AVGO",
    "AZN", "BIIB", "BKNG", "BKR", "CCEP", "CDNS", "CHTR", "CMCSA",
    "COST", "CPRT", "CRWD", "CSCO", "CSGP", "CSX", "CTAS", "DDOG",
    "DLTR", "DXCM", "EA", "EBAY", "ENPH", "EXC", "FAST", "FISV",
    "FTNT", "GEHC", "GFS", "GILD", "GOOG", "GOOGL", "HON", "IDXX",
    "ILMN", "INTC", "INTU", "ISRG", "JD", "KDP", "KHC", "KLAC",
    "LCID", "LRCX", "LULU", "MAR", "MCHP", "MDLZ", "MELI", "META",
    "MNST", "MRNA", "MRVL", "MSFT", "MU", "NFLX", "NTES", "NVDA",
    "NXPI", "ODFL", "ORLY", "PANW", "PAYX", "PCAR", "PEP", "PYPL",
    "QCOM", "REGN", "RIVN", "ROST", "SBUX", "SGEN", "SIRI", "SNPS",
    "SPLK", "SWKS", "TCOM", "TEAM", "TMUS", "TSLA", "TXN", "VRSK",
    "VRTX", "WBA", "WDAY", "XEL", "ZM", "ZS",
    # Additional commonly traded
    "SOFI", "PLTR", "RKLB", "ASTS", "IONQ", "RDDT", "GME", "AMC",
    "CLSK", "MARA", "RIOT", "COIN", "MSTR", "HIMS", "CROX", "DKNG",
    "PENN", "WBD", "PARA", "SNAP", "PINS", "MTCH", "BMBL", "FVRR",
    "UPST", "LCID", "CHWY", "WOLF", "ON", "STM", "UMC", "TSM",
}

# ── Postgres DSN ─────────────────────────────────────────────────────────────

_DSN: Optional[str] = None


def _get_dsn() -> str:
    """Build Postgres DSN from environment (same pattern as dual_writer)."""
    global _DSN
    if _DSN is None:
        host = os.getenv("PGHOST", "trading-db")
        port = os.getenv("PGPORT", "5433")
        dbname = os.getenv("PGDATABASE", "trading")
        user = os.getenv("PGUSER", "trader")
        pw = os.getenv("PGPASSWORD", "")
        _DSN = f"host={host} port={port} dbname={dbname} user={user}"
        if pw:
            _DSN += f" password={pw}"
    return _DSN


# ── Core Functions ────────────────────────────────────────────────────────────


def _compute_sentiment(text: str) -> float:
    """VADER-style keyword sentiment score, normalized to [-1.0, 1.0].

    Mirrors the _simple_sentiment logic from data_bus.py.
    """
    if not text:
        return 0.0
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if not words:
        return 0.0
    score = 0.0
    n_matched = 0
    for i, w in enumerate(words):
        multiplier = 1.0
        if i > 0 and words[i - 1] in _SENTIMENT_INTENSIFIERS:
            multiplier = 1.5
        if i > 0 and words[i - 1] in {"not", "no", "never", "neither", "nor"}:
            multiplier = -1.0
        if w in _SENTIMENT_POSITIVE:
            score += 0.3 * multiplier
            n_matched += 1
        elif w in _SENTIMENT_NEGATIVE:
            score -= 0.3 * multiplier
            n_matched += 1
    if n_matched == 0:
        return 0.0
    avg = score / n_matched
    return max(-1.0, min(1.0, avg))


def fetch_rss_feed(url: str, timeout: int = 15) -> List[Dict[str, Any]]:
    """Fetch and parse an RSS feed URL.

    Returns a list of article dicts with:
        title, url, summary, published (ISO-8601 string), source.

    Args:
        url: The RSS feed URL.
        timeout: Request timeout in seconds.

    Returns:
        List of parsed article dicts. Empty on any failure.
    """
    try:
        resp = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) PaperTrading/1.0",
        })
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning("RSS fetch failed for %s: %s", url[:60], e)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        log.warning("RSS parse failed for %s: %s", url[:60], e)
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    articles: List[Dict[str, Any]] = []

    # Handle standard RSS 2.0
    for item in root.iter("item"):
        title = _get_element_text(item, "title") or ""
        link = _get_element_text(item, "link") or ""
        summary = _get_element_text(item, "description") or ""
        pub_date_str = _get_element_text(item, "pubDate") or ""

        if not title and not link:
            continue

        published = _parse_rss_date(pub_date_str)

        articles.append({
            "title": title.strip(),
            "url": link.strip(),
            "summary": re.sub(r"<[^>]+>", "", summary).strip() if summary else "",
            "published": published,
        })

    # Handle Atom 1.0
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title_elem = entry.find("{http://www.w3.org/2005/Atom}title")
        link_elem = entry.find("{http://www.w3.org/2005/Atom}link")
        summary_elem = entry.find("{http://www.w3.org/2005/Atom}summary")
        published_elem = entry.find("{http://www.w3.org/2005/Atom}published")
        updated_elem = entry.find("{http://www.w3.org/2005/Atom}updated")

        title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
        link = link_elem.get("href", "") if link_elem is not None else ""
        summary = summary_elem.text.strip() if summary_elem is not None and summary_elem.text else ""
        pub_str = ""
        if published_elem is not None and published_elem.text:
            pub_str = published_elem.text
        elif updated_elem is not None and updated_elem.text:
            pub_str = updated_elem.text

        if not title and not link:
            continue

        published = _parse_atom_date(pub_str)

        articles.append({
            "title": title.strip(),
            "url": link.strip(),
            "summary": re.sub(r"<[^>]+>", "", summary).strip() if summary else "",
            "published": published,
        })

    return articles


def _get_element_text(parent: ET.Element, tag: str) -> Optional[str]:
    """Get text content of the first child element with the given tag."""
    elem = parent.find(tag)
    if elem is not None and elem.text:
        return elem.text.strip()
    return None


_RSS_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_rss_date(date_str: str) -> str:
    """Parse RFC 2822 / RSS pubDate into ISO-8601 string.

    Handles formats like:
        "Mon, 01 Jan 2024 12:00:00 GMT"
        "Mon, 01 Jan 2024 12:00:00 +0000"
    Falls back to current UTC if parsing fails.
    """
    if not date_str:
        return datetime.now(timezone.utc).isoformat()

    # Strip day-of-week prefix
    cleaned = date_str.strip()
    if "," in cleaned:
        cleaned = cleaned.split(",", 1)[1].strip()

    parts = cleaned.split()
    # Expected: [day, month, year, time, tz]
    if len(parts) < 4:
        return datetime.now(timezone.utc).isoformat()

    try:
        day = int(parts[0])
        month = _RSS_MONTHS.get(parts[1].lower()[:3], 1)
        year = int(parts[2])
        time_parts = parts[3].split(":")
        hour = int(time_parts[0]) if len(time_parts) > 0 else 0
        minute = int(time_parts[1]) if len(time_parts) > 1 else 0
        second = int(time_parts[2]) if len(time_parts) > 2 else 0

        dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, IndexError):
        return datetime.now(timezone.utc).isoformat()


def _parse_atom_date(date_str: str) -> str:
    """Parse Atom date (ISO-8601) into ISO-8601 string.

    Returns current UTC on failure.
    """
    if not date_str:
        return datetime.now(timezone.utc).isoformat()
    # Try standard ISO parsing
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        pass

    # Try with Z suffix normalization
    try:
        normalized = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.isoformat()
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).isoformat()


def extract_tickers(text: str, known_tickers: Set[str]) -> List[str]:
    """Extract ticker symbols from article text using regex + known ticker set.

    Matches 1-5 uppercase alpha (possibly with '.' for share classes like
    BRK.B) that appear in the known_tickers set. De-duplicates results
    while preserving order of first appearance.

    Args:
        text: Title + summary text to scan.
        known_tickers: Set of valid ticker symbols.

    Returns:
        Sorted list of matched tickers.
    """
    if not text:
        return []

    # Match: 1-5 uppercase letters, optionally followed by . and 1-3 letters
    candidates = re.findall(r"\b[A-Z]{1,5}(?:\.[A-Z]{1,3})?\b", text.upper())

    # De-duplicate while preserving order
    seen: Set[str] = set()
    result: List[str] = []
    for c in candidates:
        if c in known_tickers and c not in seen:
            seen.add(c)
            result.append(c)

    return result


def _deduplicate(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove articles with duplicate URLs, keeping the first occurrence."""
    seen_urls: Set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for a in articles:
        u = a.get("url", "")
        if u and u not in seen_urls:
            seen_urls.add(u)
            deduped.append(a)
    return deduped


def fetch_all_feeds(timeout: int = 15) -> List[Dict[str, Any]]:
    """Sequentially fetch all configured RSS feeds.

    Returns deduplicated articles from all sources, with the source key
    and computed sentiment_score and tickers added to each article dict.

    Args:
        timeout: Request timeout per feed in seconds.

    Returns:
        List of article dicts enriched with:
            source, sentiment_score, tickers
    """
    all_articles: List[Dict[str, Any]] = []

    for source_name, url in RSS_FEEDS.items():
        log.debug("Fetching %s from %s", source_name, url[:60])
        articles = fetch_rss_feed(url, timeout=timeout)
        log.info("Fetched %d articles from %s", len(articles), source_name)

        for article in articles:
            article["source"] = source_name
            # Compute sentiment on title + summary
            combined = f"{article.get('title', '')} {article.get('summary', '')}"
            article["sentiment_score"] = _compute_sentiment(combined)
            # Extract tickers
            article["tickers"] = extract_tickers(combined, KNOWN_TICKERS)

        all_articles.extend(articles)

    deduped = _deduplicate(all_articles)
    log.info("Total articles: %d (deduplicated from %d)", len(deduped), len(all_articles))
    return deduped


# ── Database Helpers ──────────────────────────────────────────────────────────


def ensure_news_cache_table() -> None:
    """Create the news_cache table if it does not exist (idempotent)."""
    import psycopg2

    sql = """
    CREATE TABLE IF NOT EXISTS public.news_cache (
        id SERIAL PRIMARY KEY,
        url TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        summary TEXT,
        source TEXT NOT NULL,
        published_at TIMESTAMPTZ NOT NULL,
        collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        tickers TEXT[],
        sentiment_score FLOAT DEFAULT 0.0,
        full_text TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_news_cache_published ON public.news_cache(published_at DESC);
    CREATE INDEX IF NOT EXISTS idx_news_cache_tickers ON public.news_cache USING GIN(tickers);
    CREATE INDEX IF NOT EXISTS idx_news_cache_source ON public.news_cache(source);
    """

    conn = psycopg2.connect(_get_dsn())
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        log.info("news_cache table ensured")
    except Exception as e:
        log.warning("Failed to ensure news_cache table: %s", e)
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_articles(articles: List[Dict[str, Any]]) -> int:
    """Upsert articles into Postgres news_cache table.

    Uses ON CONFLICT (url) DO NOTHING to skip duplicates (insert-only,
    never overwrites existing rows).

    Args:
        articles: List of article dicts with keys matching news_cache columns.

    Returns:
        Count of newly inserted rows.
    """
    import psycopg2
    import psycopg2.extras

    if not articles:
        return 0

    conn = psycopg2.connect(_get_dsn())
    try:
        with conn.cursor() as cur:
            rows = []
            for a in articles:
                published = a.get("published", "")
                if published:
                    try:
                        # Normalize ISO-8601 to timestamp
                        dt = datetime.fromisoformat(published)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        published = dt.isoformat()
                    except (ValueError, TypeError):
                        published = datetime.now(timezone.utc).isoformat()
                else:
                    published = datetime.now(timezone.utc).isoformat()

                tickers = a.get("tickers", [])
                rows.append((
                    a.get("url", ""),
                    a.get("title", ""),
                    a.get("summary", None),
                    a.get("source", ""),
                    published,
                    tickers if isinstance(tickers, list) else [],
                    float(a.get("sentiment_score", 0.0)),
                    a.get("full_text", None),
                ))

            psycopg2.extras.execute_values(
                cur,
                """INSERT INTO public.news_cache
                   (url, title, summary, source, published_at, tickers, sentiment_score, full_text)
                   VALUES %s
                   ON CONFLICT (url) DO NOTHING""",
                rows,
                template="""(%s, %s, %s, %s, %s::timestamptz, %s::text[], %s, %s)""",
            )
            n = cur.rowcount
        conn.commit()
        if n > 0:
            log.info("Upserted %d new articles into news_cache", n)
        return n
    except Exception as e:
        log.warning("Failed to upsert articles: %s", e)
        conn.rollback()
        return 0
    finally:
        conn.close()


# ── Scheduler Thread ──────────────────────────────────────────────────────────


_COLLECTOR_INTERVAL_SECONDS = 900  # 15 minutes


def _news_loop() -> None:
    """Background loop: fetch all feeds, upsert into Postgres, sleep."""
    log.info("News collector loop started (interval=%ds)", _COLLECTOR_INTERVAL_SECONDS)

    # Run once immediately on startup
    try:
        articles = fetch_all_feeds()
        if articles:
            new_count = upsert_articles(articles)
            log.info("Initial collect: %d articles fetched, %d new", len(articles), new_count)
        else:
            log.info("Initial collect: no articles fetched")
    except Exception as e:
        log.warning("Initial news collect failed: %s", e)
    # then enter periodic cycle
    while True:
        time.sleep(_COLLECTOR_INTERVAL_SECONDS)
        try:
            articles = fetch_all_feeds()
            if articles:
                new_count = upsert_articles(articles)
                log.info("Periodic collect: %d articles fetched, %d new", len(articles), new_count)
            else:
                log.debug("Periodic collect: no articles fetched")
        except Exception as e:
            log.warning("Periodic news collect failed: %s", e)


_COLLECTOR_THREAD: Optional[threading.Thread] = None


def start_news_collector() -> None:
    """Spawn a daemon thread to run the news collector every 15 minutes.

    Safe to call multiple times — only starts one thread.
    """
    global _COLLECTOR_THREAD
    if _COLLECTOR_THREAD is not None and _COLLECTOR_THREAD.is_alive():
        log.debug("News collector already running")
        return

    _COLLECTOR_THREAD = threading.Thread(
        target=_news_loop,
        daemon=True,
        name="news-collector",
    )
    _COLLECTOR_THREAD.start()
    log.info("News collector daemon thread started")
