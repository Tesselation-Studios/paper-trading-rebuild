#!/usr/bin/env python3
"""
Data Bus — Centralized market data service for paper trading agents.

Single HTTP service that fetches all market data at appropriate per-source
frequencies and serves it to all traders. Includes trader intercom via /signals.

Architecture:
  Schedulers (internal, per-source cadences):
    ├── Alpaca quotes ..... every 5s during market hours
    ├── Crypto ............ every 10s
    ├── Fundamentals ...... every 6h
    ├── Options chain ..... every 15m
    ├── Congress trades ... every 30m
    ├── RSS/News .......... every 3m
    └── Signals GC ........ every 60s (purge stale signals)

  Caches: in-memory dict + SQLite (shared/cache.db)
  FinBERT integration → proxies to Mac :5004

Endpoints:
  GET  /health
  GET  /metrics        (Prometheus scrape target)
  GET  /quotes?symbols=AAPL,TSLA,...
  GET  /crypto?symbols=BTC/USD,ETH/USD
  GET  /fundamentals?symbol=AAPL
  GET  /sentiment?symbol=AAPL
  GET  /options?symbol=AAPL
  GET  /news?symbol=AAPL
  GET  /macro                  (FRED macro indicators + yield curve)
  GET  /earnings?symbols=AAPL,MSFT  (upcoming earnings calendar)
  GET  /signals            (all traders' current reads)
  GET  /fear_greed         (Fear & Greed Index from alternative.me)
  GET  /flow?symbol=AAPL   (unusual options flow from unusualwhales.com)
  GET  /insiders?symbols=JPM,BAC  (SEC Form 4 insider filings)
  POST /signals            (publish your current read)
  GET  /source-quality    (prediction accuracy per social/news source)
  GET  /overnight-sentiment (overnight sentiment delta via computer_overnight_delta)

Usage:
  python3 src/data_bus.py --port 5000
  MCP_PORT=5001 python3 src/data_bus.py --port 5000  # MCP on custom port

Environment:
  MCP_PORT    MCP SSE server port (default: 5001)
  python3 src/data_bus.py --port 5000 --debug
"""

import os
import sys
import json
import time
import signal
import threading
import argparse
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from collections import deque

# ── Path setup ────────────────────────────────────────────────────────────────
SRC_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SRC_DIR.parent
SHARED_DIR = PROJECT_DIR / "shared"
for d in [str(SRC_DIR), str(SHARED_DIR), str(PROJECT_DIR)]:
    if d not in sys.path:
        sys.path.insert(0, d)

# ── Flask ─────────────────────────────────────────────────────────────────────
try:
    from flask import Flask, request, jsonify
except ImportError:
    print("ERROR: Flask not installed. Run: pip install flask", file=sys.stderr)
    sys.exit(1)

# ── MCP Server (FastMCP) ─────────────────────────────────────────────────────
try:
    from mcp.server.fastmcp import FastMCP
    _mcp_server_available = True
except ImportError:
    _mcp_server_available = False
    FastMCP = None  # type: ignore

# ── Logging ───────────────────────────────────────────────────────────────────
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [databus] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("databus")

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_shutdown_flag = threading.Event()


def _on_signal(signum, frame):
    log.info("Received signal %s, shutting down...", signum)
    _shutdown_flag.set()


signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)

# ── Env ───────────────────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(Path.home() / ".openclaw" / ".env", override=True)
from src.db import dual_writer

# ── News collector (RSS aggregation) ──────────────────────────────────────────
try:
    from src.news_collector import start_news_collector, ensure_news_cache_table
    _HAS_NEWS_COLLECTOR = True
except ImportError:
    start_news_collector = None  # type: ignore
    ensure_news_cache_table = None  # type: ignore
    _HAS_NEWS_COLLECTOR = False
    log.warning("news_collector module not available — /news-cache, /news/search endpoints will be disabled")

# ── Combo fetch imports ──────────────────────────────────────────────────────
try:
    from skill_combo_fetch import (
        fetch_prices_indicators,
        fetch_fundamentals as _combo_fetch_fundamentals,
        fetch_congressional_trading,
        fetch_ml_signal,
    )
    _HAS_COMBO_FETCH = True
except ImportError:
    fetch_prices_indicators = None
    _combo_fetch_fundamentals = None
    fetch_congressional_trading = None
    fetch_ml_signal = None
    _HAS_COMBO_FETCH = False
    log.warning("skill_combo_fetch not available — /quotes, /fundamentals, /congress endpoints will be degraded")

# ── Social sentiment imports ──────────────────────────────────────────────────
try:
    from social_sentiment import (
        fetch_bluesky_sentiment,
        fetch_stocktwits_sentiment,
        fetch_reddit_via_search,
        fetch_reddit_via_chrome,
        _simple_sentiment,
    )
except ImportError:
    fetch_bluesky_sentiment = None
    fetch_stocktwits_sentiment = None
    fetch_reddit_via_search = None
    fetch_reddit_via_chrome = None
    # Built-in keyword sentiment analyzer (VADER-compatible word lists)
    # Used when the full social_sentiment module is not installed
    _SENTIMENT_POSITIVE = {
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
        "efficient", "efficiency", "upgrade", "raised", "raising",
        "target", "increase", "increased", "increasing", "outperform",
    }
    _SENTIMENT_NEGATIVE = {
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
        "downgrade", "underweight", "reduce", "selling", "pressure",
        "concern", "concerning", "worst", "fail", "failed", "failure",
        "drop", "dropped", "fall", "fallen", "fell", "lower", "lowered",
        "downgrade", "downgraded", "decrease", "decreased", "tightening",
    }
    _SENTIMENT_INTENSIFIERS = {
        "very", "extremely", "highly", "strongly", "significantly",
        "substantially", "massively", "dramatically", "sharply", "deeply",
        "severely", "major", "majorly", "big", "huge", "enormous",
        "massive", "incredible", "extraordinary", "record", "all-time",
        "tremendous", "immense", "unprecedented", "historic",
    }

    def _simple_sentiment(text: str) -> float:
        """Rule-based keyword sentiment for VADER-compatible compound score."""
        if not text:
            return 0.0
        import re
        words = re.findall(r"[a-zA-Z]+", text.lower())
        if not words:
            return 0.0
        score = 0.0
        n_matched = 0
        for i, w in enumerate(words):
            multiplier = 1.0
            # Check if preceded by an intensifier
            if i > 0 and words[i - 1] in _SENTIMENT_INTENSIFIERS:
                multiplier = 1.5
            # Check for negation
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
        # Normalize to [-1, 1]
        avg = score / n_matched
        # Clip
        return max(-1.0, min(1.0, avg))

    log.info("Built-in keyword sentiment analyzer loaded (social_sentiment module not available)")

# Reddit pipeline (RSS-based, no auth required)
try:
    from social_reddit import SocialRedditPipeline
    _reddit_pipeline_available = True
except ImportError:
    SocialRedditPipeline = None  # type: ignore
    _reddit_pipeline_available = False
    log.warning("social_reddit module not available — Reddit RSS pipeline unavailable")

# ── Momentum ranking imports ──────────────────────────────────────────────
try:
    from src.skill_cross_sectional_momentum import get_cached_momentum_signal
    _HAS_MOMENTUM = True
except ImportError:
    _HAS_MOMENTUM = False
    get_cached_momentum_signal = None

# ── MCP client imports ───────────────────────────────────────────────────────
try:
    from mcp_client import (
        MCPConnectionManager,
        MCPConnectionConfig,
        get_manager,
        register_phase0_servers,
    )
    _mcp_available = True
except ImportError as e:
    log.warning("mcp_client not available: %s — MCP endpoints will be disabled", e)
    _mcp_available = False
    MCPConnectionManager = None  # type: ignore
    MCPConnectionConfig = None   # type: ignore
    get_manager = lambda: None
    register_phase0_servers = lambda *a, **kw: None

# ═══════════════════════════════════════════════════════════════════════════════
# Configuration (supports YAML config overlay)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Load YAML config (graceful fallback to hardcoded defaults) ────────────────
try:
    from src.config_loader import get_config as _get_config
    _config = _get_config()
    _ttl_cfg = _config.get("data_bus.cache_ttl", {})
    _intervals_cfg = _config.get("data_bus.scheduler_intervals", {})
    _endpoints_cfg = _config.get("data_bus.endpoints", {})
    _signals_cfg = _config.get("data_bus.signals", {})
    _rate_cfg = _config.get("data_bus.rate_limits", {})
    _wq_cfg = _config.get("data_bus.write_queue", {})
    log.info("Loaded YAML config: %s", _config.loaded_files)
except Exception as e:
    log.warning("YAML config unavailable, using hardcoded defaults: %s", e)
    _config = None
    _ttl_cfg = {}
    _intervals_cfg = {}
    _endpoints_cfg = {}
    _signals_cfg = {}
    _rate_cfg = {}
    _wq_cfg = {}

# Cache TTLs (seconds) — YAML overrides hardcoded default
TTL = {
    "quotes":        _ttl_cfg.get("quotes", 5),
    "crypto":        _ttl_cfg.get("crypto", 10),
    "fundamentals":  _ttl_cfg.get("fundamentals", 21600),
    "options":       _ttl_cfg.get("options", 900),
    "congress":      _ttl_cfg.get("congress", 1800),
    "news":          _ttl_cfg.get("news", 180),
    "sentiment":     _ttl_cfg.get("sentiment", 300),
    "social":        _ttl_cfg.get("social", 900),
    "macro":         _ttl_cfg.get("macro", 21600),
    "earnings":      _ttl_cfg.get("earnings", 86400),
    "fear_greed":    _ttl_cfg.get("fear_greed", 1800),
    "flow":          _ttl_cfg.get("flow", 300),
    "insiders":      _ttl_cfg.get("insiders", 1800),
    "risk":          _ttl_cfg.get("risk", 900),
    "technical_scan": _ttl_cfg.get("technical_scan", 300),
    "praesentire_sentiment": _ttl_cfg.get("praesentire_sentiment", 300),
    "sentiment_divergence": _ttl_cfg.get("sentiment_divergence", 600),
}

# Scheduler intervals (seconds) — dual-rate: fast during market hours, slow off-hours
INTERVALS = {
    "quotes":     {"market": _intervals_cfg.get("quotes", {}).get("market", 5), "off": _intervals_cfg.get("quotes", {}).get("off", 300)},
    "crypto":     {"market": _intervals_cfg.get("crypto", {}).get("market", 10), "off": _intervals_cfg.get("crypto", {}).get("off", 60)},
    "news":       {"market": _intervals_cfg.get("news", {}).get("market", 180), "off": _intervals_cfg.get("news", {}).get("off", 900)},
    "sentiment":  {"market": _intervals_cfg.get("sentiment", {}).get("market", 300), "off": _intervals_cfg.get("sentiment", {}).get("off", 900)},
    "congress":   {"market": _intervals_cfg.get("congress", {}).get("market", 1800), "off": _intervals_cfg.get("congress", {}).get("off", 7200)},
    "signals_gc": {"market": _intervals_cfg.get("signals_gc", {}).get("market", 60), "off": _intervals_cfg.get("signals_gc", {}).get("off", 300)},
    "cache_flush": {"market": _intervals_cfg.get("cache_flush", {}).get("market", 300), "off": _intervals_cfg.get("cache_flush", {}).get("off", 1200)},
    "social":     {"market": _intervals_cfg.get("social", {}).get("market", 180), "off": _intervals_cfg.get("social", {}).get("off", 900)},
    "momentum":   {"market": _intervals_cfg.get("momentum", {}).get("market", 300), "off": _intervals_cfg.get("momentum", {}).get("off", 1800)},
    "macro":      {"market": _intervals_cfg.get("macro", {}).get("market", 21600), "off": _intervals_cfg.get("macro", {}).get("off", 21600)},
    "earnings":   {"market": _intervals_cfg.get("earnings", {}).get("market", 3600), "off": _intervals_cfg.get("earnings", {}).get("off", 86400)},
    "fear_greed": {"market": _intervals_cfg.get("fear_greed", {}).get("market", 1800), "off": _intervals_cfg.get("fear_greed", {}).get("off", 3600)},
    "flow":       {"market": _intervals_cfg.get("flow", {}).get("market", 300), "off": _intervals_cfg.get("flow", {}).get("off", 900)},
    "insiders":   {"market": _intervals_cfg.get("insiders", {}).get("market", 1800), "off": _intervals_cfg.get("insiders", {}).get("off", 7200)},
    "risk":       {"market": _intervals_cfg.get("risk", {}).get("market", 900), "off": _intervals_cfg.get("risk", {}).get("off", 3600)},
    "technical_scan": {"market": _intervals_cfg.get("technical_scan", {}).get("market", 300), "off": _intervals_cfg.get("technical_scan", {}).get("off", 900)},
    "praesentire_sentiment":  {"market": _intervals_cfg.get("praesentire_sentiment", {}).get("market", 300), "off": _intervals_cfg.get("praesentire_sentiment", {}).get("off", 900)},
    "praesentire_divergence": {"market": _intervals_cfg.get("praesentire_divergence", {}).get("market", 600), "off": _intervals_cfg.get("praesentire_divergence", {}).get("off", 900)},
}

# Rate limits (Alpaca free tier: 200/min)
RATE_LIMIT_QUOTES_PER_MIN = _rate_cfg.get("quotes_per_min", 200)
RATE_LIMIT_CRYPTO_PER_MIN = _rate_cfg.get("crypto_per_min", 200)

# FinBERT service (env vars used directly; YAML provides overrides)
FINBERT_HOST = os.getenv("FINBERT_HOST", _endpoints_cfg.get("finbert", {}).get("host", "192.168.1.237"))
FINBERT_PORT = int(os.getenv("FINBERT_PORT", _endpoints_cfg.get("finbert", {}).get("port", 5004)))
FINBERT_URL = f"http://{FINBERT_HOST}:{FINBERT_PORT}"

# Signal retention: purge signals older than this (seconds)
SIGNAL_MAX_AGE = _signals_cfg.get("max_age", 900)  # 15 minutes

# ═══════════════════════════════════════════════════════════════════════════════
# In-Memory Cache
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CacheEntry:
    data: Any
    fetched_at: float  # time.time() monotonic

    def is_fresh(self, ttl_seconds: int) -> bool:
        return (time.time() - self.fetched_at) < ttl_seconds

    def age_seconds(self) -> float:
        return time.time() - self.fetched_at


class MemoryCache:
    """Thread-safe in-memory cache with per-key TTL."""

    def __init__(self):
        self._lock = threading.Lock()
        self._store: Dict[str, CacheEntry] = {}

    def get(self, key: str, ttl_seconds: int = None) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if ttl_seconds is not None and not entry.is_fresh(ttl_seconds):
                return None
            return entry.data

    def set(self, key: str, data: Any):
        with self._lock:
            self._store[key] = CacheEntry(data=data, fetched_at=time.time())

    def get_multi(self, keys: List[str], ttl_seconds: int = None) -> Dict[str, Any]:
        result = {}
        with self._lock:
            for key in keys:
                entry = self._store.get(key)
                if entry is None:
                    continue
                if ttl_seconds is not None and not entry.is_fresh(ttl_seconds):
                    continue
                result[key] = entry.data
        return result

    def set_multi(self, mapping: Dict[str, Any]):
        now = time.time()
        with self._lock:
            for key, data in mapping.items():
                self._store[key] = CacheEntry(data=data, fetched_at=now)

    def delete(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def flush_expired(self, ttl_seconds: int):
        cutoff = time.time() - ttl_seconds
        with self._lock:
            expired = [k for k, v in self._store.items() if v.fetched_at < cutoff]
            for k in expired:
                del self._store[k]
            return len(expired)

    def stats(self) -> dict:
        with self._lock:
            return {"keys": len(self._store), "entries": list(self._store.keys())[:20]}


# ═══════════════════════════════════════════════════════════════════════════════
# Write-Behind DB Queue
# ═══════════════════════════════════════════════════════════════════════════════

class DbWriteQueue:
    """Thread-safe buffer that batches DB writes and flushes on cadence.

    - enqueue(table_name, data_dict) -- add a row to the write buffer
    - start() -- starts background flush thread
    - stop() -- signals flush thread to finish and does final flush
    - flush() -- writes all buffered rows to shared/cache.db
    """

    def __init__(self, default_interval: float = 15.0, off_hours_interval: float = 60.0):
        self._lock = threading.Lock()
        self._buffer: List[Tuple[str, dict]] = []  # (table, data)
        self._default_interval = default_interval
        self._off_hours_interval = off_hours_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def enqueue(self, table: str, data: dict):
        """Add a row to the write buffer (thread-safe)."""
        with self._lock:
            self._buffer.append((table, data))

    def start(self):
        """Start background flush thread."""
        self._thread = threading.Thread(target=self._flush_loop, daemon=True, name="db-write-queue")
        self._thread.start()
        log.info("DbWriteQueue started (flush interval: %ss market / %ss off-hours)",
                 self._default_interval, self._off_hours_interval)

    def stop(self):
        """Signal flush thread to finish and flush remaining."""
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self.flush()
        log.info("DbWriteQueue stopped, final flush complete")

    def flush(self):
        """Flush all buffered rows to cache.db."""
        with self._lock:
            batch = list(self._buffer)
            self._buffer.clear()

        if not batch:
            return

        # Group rows by table for batch INSERT
        from collections import defaultdict
        grouped: Dict[str, List[dict]] = defaultdict(list)
        for table, data in batch:
            grouped[table].append(data)

        try:
            conn = _get_cache_db_connection(readonly=False)
            cursor = conn.cursor()
            for table, rows in grouped.items():
                if not rows:
                    continue
                # Build INSERT dynamically from the keys of the first row
                columns = list(rows[0].keys())
                placeholders = ", ".join(["?" for _ in columns])
                col_str = ", ".join(columns)
                sql = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})"
                values = [[r.get(c) for c in columns] for r in rows]
                cursor.executemany(sql, values)
            conn.commit()
            conn.close()
            # Mirror to Postgres (best-effort, cache tables)
            for table, rows in grouped.items():
                for row in rows:
                    dual_writer.write(table, row)
            log.debug("DbWriteQueue flushed %d rows across %d tables", len(batch), len(grouped))
        except Exception as e:
            log.warning("DbWriteQueue flush failed: %s", e)
            # Re-enqueue failed rows to avoid data loss
            with self._lock:
                self._buffer.extend(batch)

    def _get_active_interval(self) -> float:
        if _is_market_open():
            return self._default_interval
        return self._off_hours_interval

    def _flush_loop(self):
        while not self._stop.is_set():
            interval = self._get_active_interval()
            self.flush()
            # Sleep in small chunks for responsive shutdown
            deadline = time.time() + interval
            while time.time() < deadline and not self._stop.is_set():
                time.sleep(min(1, deadline - time.time()))

    def queue_size(self) -> int:
        with self._lock:
            return len(self._buffer)

    def status(self) -> dict:
        return {
            "buffer_size": self.queue_size(),
            "default_interval": self._default_interval,
            "off_hours_interval": self._off_hours_interval,
            "active": self._thread is not None and self._thread.is_alive(),
        }


# Global caches
_cache = MemoryCache()
_write_queue: Optional[DbWriteQueue] = None
_signals_cache: List[dict] = []
_signals_lock = threading.Lock()

# Scheduler event log (ring buffer for /debug)
_scheduler_events: deque = deque(maxlen=100)
_scheduler_events_lock = threading.Lock()

# Fetch hit/miss/error counters per source
_fetch_stats: Dict[str, Dict[str, int]] = {}
_fetch_stats_lock = threading.Lock()

# Trader pulse: last tick time per trader (from DB)
_trader_pulse: Dict[str, Tuple[float, str]] = {}
_trader_pulse_lock = threading.Lock()

# Error/exception log (ring buffer for /debug)
_error_log: deque = deque(maxlen=20)
_error_log_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════════════
# Market Hours
# ═══════════════════════════════════════════════════════════════════════════════

def _is_market_open() -> bool:
    """Check if US stock market is currently open."""
    try:
        from market_hours import is_market_open
        return is_market_open()
    except ImportError:
        # Fallback: simple Mon-Fri 9:30-16:00 ET check
        from zoneinfo import ZoneInfo
        from datetime import datetime as dt
        et = ZoneInfo("America/New_York")
        now = dt.now(et)
        if now.weekday() >= 5:  # Sat=5, Sun=6
            return False
        open_h = now.hour * 60 + now.minute
        return 9 * 60 + 30 <= open_h < 16 * 60  # 9:30 AM to 4:00 PM


def _is_crypto_hours() -> bool:
    """Crypto trades 24/7 — always open."""
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Data Fetchers
# ═══════════════════════════════════════════════════════════════════════════════

def _get_alpaca_credentials():
    """Get first available Alpaca credentials."""
    from dotenv import load_dotenv
    load_dotenv(Path.home() / ".openclaw" / ".env", override=True)
    candidates = [
        ("ALPACA_KAIROS_KEY", "ALPACA_KAIROS_SECRET"),
        ("KAIROS_API_KEY", "KAIROS_SECRET_KEY"),
        ("ALPACA_ALDRIDGE_KEY", "ALPACA_ALDRIDGE_SECRET"),
        ("ALDRIDGE_API_KEY", "ALDRIDGE_SECRET_KEY"),
        ("ALPACA_STONKS_KEY", "ALPACA_STONKS_SECRET"),
        ("STONKS_API_KEY", "STONKS_SECRET_KEY"),
    ]
    for key_var, sec_var in candidates:
        k = os.getenv(key_var)
        s = os.getenv(sec_var)
        if k and s:
            return k, s
    return None, None


def _get_alpaca_data_client():
    """Get StockHistoricalDataClient for quotes."""
    from alpaca.data.historical import StockHistoricalDataClient
    api_key, secret_key = _get_alpaca_credentials()
    if not api_key:
        return None
    return StockHistoricalDataClient(api_key, secret_key)


def _get_alpaca_crypto_client():
    """Get CryptoHistoricalDataClient for crypto quotes."""
    from alpaca.data.historical import CryptoHistoricalDataClient
    return CryptoHistoricalDataClient()


def _fetch_alpaca_quotes(symbols: List[str]) -> Dict[str, dict]:
    """Fetch latest quote data for symbols.

    Uses skill_combo_fetch if available, falls back to
    direct Alpaca StockHistoricalDataClient with 5-min bars.
    """
    if not symbols:
        return {}

    # Try skill_combo_fetch first
    if fetch_prices_indicators is not None:
        try:
            result = fetch_prices_indicators(list(symbols))
            if isinstance(result, dict) and "error" not in result:
                return {t: d for t, d in result.items() if d is not None}
        except Exception as e:
            log.warning("skill_combo_fetch failed: %s", e)

    # Fallback: direct Alpaca historical data client
    try:
        client = _get_alpaca_data_client()
        if client is None:
            return {}

        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from alpaca.data.enums import DataFeed
        import pandas as pd

        now = pd.Timestamp.now(tz="America/New_York")
        start = now - pd.Timedelta(days=5)  # last 5 trading days

        # Use IEX feed for free/paper tier — SIP requires paid subscription
        request_params = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start.isoformat(),
            end=now.isoformat(),
            feed=DataFeed.IEX,
        )

        bars = client.get_stock_bars(request_params)
        result = {}

        for sym in symbols:
            sym_bars = bars.data.get(sym, [])
            if not sym_bars:
                continue
            latest = sym_bars[-1]
            result[sym] = {
                "close": float(latest.close),
                "open": float(latest.open),
                "high": float(latest.high),
                "low": float(latest.low),
                "volume": latest.volume,
                "timestamp": latest.timestamp.isoformat(),
                "source": "alpaca_direct",
            }

        return result
    except Exception as e:
        log.warning("Alpaca quotes fallback failed: %s", e)
        return {}


def _fetch_alpaca_crypto(symbols: List[str]) -> Dict[str, dict]:
    """Fetch latest crypto quotes from Alpaca."""
    if not symbols:
        return {}

    try:
        from alpaca.data.requests import CryptoLatestTradeRequest
        client = _get_alpaca_crypto_client()
        resp = client.get_crypto_latest_trade(
            CryptoLatestTradeRequest(symbol_or_symbols=symbols)
        )
        result = {}
        for sym in symbols:
            trade = resp.get(sym) if isinstance(resp, dict) else None
            if trade is not None:
                result[sym] = {
                    "price": float(trade.price),
                    "timestamp": str(trade.timestamp) if hasattr(trade, 'timestamp') else datetime.now().isoformat(),
                }
        return result
    except Exception as e:
        log.warning("Crypto quotes fetch failed: %s", e)
        return {}


def _fetch_alpaca_historical_bars(
    symbols: List[str],
    start_date: str,
    end_date: str,
    interval: str = "daily",
) -> Dict[str, List[dict]]:
    """Fetch historical OHLCV bars from Alpaca.

    Fetches historical bar data via Alpaca StockHistoricalDataClient.
    Used by the /bars endpoint for backtesting and parameter sweeps.

    Args:
        symbols: List of ticker symbols.
        start_date: ISO start date (e.g. "2026-06-01").
        end_date: ISO end date (e.g. "2026-07-02").
        interval: "daily" or "intraday".

    Returns:
        Dict mapping ticker -> list of OHLCV bar dicts.
    """
    if not symbols:
        return {}

    try:
        client = _get_alpaca_data_client()
        if client is None:
            log.warning("Cannot fetch historical bars: no Alpaca client")
            return {}

        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from alpaca.data.enums import DataFeed
        import pandas as pd

        # Parse dates
        try:
            start_ts = pd.Timestamp(start_date, tz="America/New_York")
            end_ts = pd.Timestamp(end_date, tz="America/New_York") + pd.Timedelta(days=1)
        except Exception as e:
            log.warning("Invalid date params: %s", e)
            return {}

        # Choose timeframe
        if interval == "daily":
            timeframe = TimeFrame(1, TimeFrameUnit.Day)
        else:
            timeframe = TimeFrame(30, TimeFrameUnit.Minute)

        request_params = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=timeframe,
            start=start_ts.isoformat(),
            end=end_ts.isoformat(),
            feed=DataFeed.IEX,
        )

        bars = client.get_stock_bars(request_params)
        result: Dict[str, List[dict]] = {}

        for sym in symbols:
            sym_bars = bars.data.get(sym, [])
            if not sym_bars:
                continue
            bar_list = []
            for b in sym_bars:
                bar_list.append({
                    "timestamp": b.timestamp.isoformat(),
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": b.volume,
                })
            result[sym] = bar_list

        log.info("Fetched historical bars for %d/%d symbols (%s-%s, %s)",
                 len(result), len(symbols), start_date, end_date, interval)
        return result
    except Exception as e:
        log.warning("Alpaca historical bars failed: %s", e)
        return {}



def _fetch_fundamentals(symbol: str) -> Optional[dict]:
    """Fetch fundamentals via skill_combo_fetch (Alpha Vantage)."""
    if _combo_fetch_fundamentals is None:
        log.debug("skill_combo_fetch fundamentals unavailable — falling back")
        return _fetch_fundamentals_web(symbol)
    try:
        result = _combo_fetch_fundamentals([symbol])
        if isinstance(result, dict) and "error" not in result:
            return result.get(symbol)
        return None
    except Exception as e:
        log.warning("Fundamentals fetch failed for %s: %s", symbol, e)
        return _fetch_fundamentals_web(symbol)


def _fetch_fundamentals_web(symbol: str) -> Optional[dict]:
    """Fallback: fetch fundamentals via yfinance."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        info = ticker.info
        if not info or info.get("trailingPE") is None and info.get("marketCap") is None:
            return None
        return {
            "pe_ratio": info.get("trailingPE"),
            "eps": info.get("trailingEps"),
            "dividend_yield": info.get("dividendYield"),
            "analyst_target": info.get("targetMeanPrice"),
            "market_cap": info.get("marketCap"),
            "description": (info.get("longBusinessSummary") or "")[:200],
        }
    except Exception as e:
        log.debug("yfinance fundamentals fallback failed for %s: %s", symbol, e)
        return None


def _fetch_sentiment_via_finbert(text: str, ticker: str = "") -> Optional[dict]:
    """Proxy sentiment analysis to FinBERT on Mac GPU. Falls back to keyword sentiment."""
    try:
        import requests as req
        resp = req.post(
            f"{FINBERT_URL}/analyze",
            json={"text": text, "ticker": ticker},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        log.warning("FinBERT returned %s: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("FinBERT unreachable: %s", e)

    # Fallback to keyword-based sentiment (VADER-compatible format)
    score = _simple_sentiment(text)
    # Map -1..1 compound score to positive/negative/neutral proportions
    if score > 0.1:
        positive, negative, neutral_val = abs(score), 0.0, 1.0 - abs(score)
    elif score < -0.1:
        positive, negative, neutral_val = 0.0, abs(score), 1.0 - abs(score)
    else:
        positive, negative, neutral_val = 0.0, 0.0, 1.0
    log.info("Sentiment fallback for %s: compound=%.3f (keyword-based)", ticker or text[:20], score)
    return {
        "positive": round(positive, 4),
        "negative": round(negative, 4),
        "neutral": round(neutral_val, 4),
        "compound": round(score, 4),
        "source": "keyword_fallback",
    }


def _fetch_congress_trades(tickers: List[str] = None) -> dict:
    """Fetch recent congress trades via skill_combo_fetch (Finnhub)."""
    if not tickers:
        tickers = list(_tracked_symbols)
    if fetch_congressional_trading is None:
        log.warning("skill_combo_fetch unavailable — congress trades degraded")
        return {}
    try:
        result = fetch_congressional_trading(tickers)
        if isinstance(result, dict) and "error" not in result:
            return result
        log.warning("Congress trades fetch error: %s", result.get("error", "unknown"))
        return {}
    except Exception as e:
        log.warning("Congress trades fetch failed: %s", e)
        return {}


def _fetch_alpaca_news(symbol: str = None, limit: int = 10) -> List[dict]:
    """Fetch news from Alpaca data API."""
    try:
        api_key, secret_key = _get_alpaca_credentials()
        if not api_key:
            return []

        import requests as req
        url = "https://data.alpaca.markets/v1beta1/news"
        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }
        params = {"limit": limit, "sort": "desc"}
        if symbol:
            params["symbols"] = symbol

        resp = req.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code != 200:
            return []

        news_data = resp.json()
        return [
            {
                "headline": item.get("headline", ""),
                "summary": item.get("summary", ""),
                "symbols": item.get("symbols", []),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
                "created_at": item.get("created_at", ""),
            }
            for item in news_data.get("news", [])[:limit]
        ]
    except Exception as e:
        log.warning("News fetch failed: %s", e)
        return []


def _fetch_options_chain(symbol: str) -> Optional[dict]:
    """Fetch options chain from Alpaca (if available)."""
    try:
        api_key, secret_key = _get_alpaca_credentials()
        if not api_key:
            return None
        # Alpaca doesn't have a direct options chain endpoint on free tier.
        # Snapshot provides some options data.
        from alpaca.data.requests import StockSnapshotRequest
        client = _get_alpaca_data_client()
        if client is None:
            return None
        snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=[symbol]))
        data = snap.get(symbol)
        if data is None:
            return None
        return {
            "symbol": symbol,
            "latest_trade": float(data.latest_trade.price) if data.latest_trade else None,
            "daily_bar": {
                "o": float(data.daily_bar.open) if data.daily_bar else None,
                "h": float(data.daily_bar.high) if data.daily_bar else None,
                "l": float(data.daily_bar.low) if data.daily_bar else None,
                "c": float(data.daily_bar.close) if data.daily_bar else None,
            },
            "fetched_at": datetime.now().isoformat(),
        }
    except Exception as e:
        log.debug("Options/snapshot fetch for %s: %s", symbol, e)
        return None


def _fetch_fred_csv(series_id: str):
    """Fetch latest value for a FRED series via the public CSV download (no API key needed)."""
    import requests as req
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        resp = req.get(url, timeout=15)
        if resp.status_code != 200:
            return None
        lines = [l for l in resp.text.strip().splitlines() if l and not l.startswith("observation_date")]
        # Find last non-missing value (FRED uses "." for missing)
        for line in reversed(lines):
            parts = line.split(",")
            if len(parts) == 2 and parts[1].strip() != ".":
                return {"date": parts[0].strip(), "value": parts[1].strip()}
    except Exception:
        pass
    return None


def _fetch_fred_macro() -> dict:
    """Fetch key macro indicators + Treasury yields from FRED API (or CSV fallback)."""
    api_key = os.getenv("FRED_API_KEY")

    series_ids = {
        "CPI": "CPIAUCSL",
        "PCE": "PCEPI",
        "NFP": "PAYEMS",
        "FOMC_upper": "DFEDTARU",
        "FOMC_lower": "DFEDTARL",
        "unemployment": "UNRATE",
        "GDP": "GDP",
        "DGS2": "DGS2",
        "DGS10": "DGS10",
        "DGS30": "DGS30",
    }

    indicators = {}
    errors = []

    import requests as req

    for name, series_id in series_ids.items():
        try:
            if api_key:
                url = (
                    f"https://api.stlouisfed.org/fred/series/observations"
                    f"?series_id={series_id}&api_key={api_key}"
                    f"&file_type=json&sort_order=desc&limit=2"
                )
                resp = req.get(url, timeout=15)
                if resp.status_code != 200:
                    errors.append(f"{name}({series_id}): HTTP {resp.status_code}")
                    continue

                data = resp.json()
                observations = data.get("observations", [])

                latest = None
                for obs in observations:
                    val = obs.get("value", ".")
                    if val != ".":
                        latest = {"date": obs["date"], "value": val}
                        break
            else:
                # No API key — use free FRED CSV download endpoint
                latest = _fetch_fred_csv(series_id)

            if latest:
                indicators[name] = {
                    "series_id": series_id,
                    "date": latest["date"],
                    "value": latest["value"],
                }
            else:
                errors.append(f"{name}({series_id}): no data")
        except Exception as e:
            errors.append(f"{name}({series_id}): {e}")

    result = {
        "indicators": indicators,
        "fetched_at": datetime.now().isoformat(),
    }
    if errors:
        result["errors"] = errors

    # ── Yield curve spreads ───────────────────────────────────────────────
    yield_keys = ["DGS2", "DGS10", "DGS30"]
    if all(k in indicators for k in yield_keys):
        try:
            dgs2 = float(indicators["DGS2"]["value"])
            dgs10 = float(indicators["DGS10"]["value"])
            dgs30 = float(indicators["DGS30"]["value"])
            spread_10y2y = round(dgs10 - dgs2, 2)
            spread_30y10y = round(dgs30 - dgs10, 2)

            # Interpretation
            if spread_10y2y < 0:
                curve_status = "inverted"
                curve_warning = "⚠️ Yield curve inverted — recession signal"
            elif spread_10y2y < 0.5:
                curve_status = "flat"
                curve_warning = "Flat yield curve — caution"
            else:
                curve_status = "normal"
                curve_warning = None

            result["yields"] = {
                "2yr": dgs2,
                "10yr": dgs10,
                "30yr": dgs30,
                "spread_10y2y": spread_10y2y,
                "spread_30y10y": spread_30y10y,
                "curve_status": curve_status,
            }
            if curve_warning:
                result["yields"]["warning"] = curve_warning
        except (ValueError, TypeError):
            pass

    return result


def _fetch_earnings_calendar() -> dict:
    """Fetch earnings calendar from Nasdaq (free, no key required)."""
    import requests as req
    from datetime import datetime as _dt, timedelta

    by_ticker: Dict[str, list] = {}
    errors = []

    # Fetch today + next 7 days
    for delta in range(8):
        date_str = (_dt.now() + timedelta(days=delta)).strftime("%Y-%m-%d")
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
        try:
            resp = req.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
                timeout=10,
            )
            if resp.status_code != 200:
                errors.append(f"{date_str}: HTTP {resp.status_code}")
                continue
            data = resp.json()
            rows = (data.get("data") or {}).get("rows") or []
            for row in rows:
                ticker = (row.get("symbol") or "").strip().upper()
                if not ticker:
                    continue
                by_ticker.setdefault(ticker, []).append({
                    "report_date": date_str,
                    "time": row.get("time", ""),
                    "company": row.get("name", ""),
                    "eps_forecast": row.get("epsForecast", ""),
                    "market_cap": row.get("marketCap", ""),
                    "fiscal_quarter": row.get("fiscalQuarterEnding", ""),
                })
        except Exception as e:
            errors.append(f"{date_str}: {e}")

    result = {
        "earnings_by_ticker": by_ticker,
        "total_companies": len(by_ticker),
        "fetched_at": datetime.now().isoformat(),
        "source": "nasdaq",
    }
    if errors:
        result["errors"] = errors
    return result


def _fetch_fear_greed() -> dict:
    """Fetch Fear & Greed Index from alternative.me."""
    import requests as req
    try:
        resp = req.get("https://api.alternative.me/fng/?limit=1", timeout=10)
        if resp.status_code != 200:
            return {"error": f"alternative.me returned {resp.status_code}"}
        data = resp.json()
        entry = data.get("data", [{}])[0] if data.get("data") else {}
        value = int(entry.get("value", 0))
        classification = entry.get("value_classification", "Unknown")
        return {
            "value": value,
            "classification": classification,
            "timestamp": entry.get("timestamp", ""),
            "time_until_update": entry.get("time_until_update"),
            "fetched_at": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


def _fetch_options_flow() -> dict:
    """Fetch unusual options flow from Unusual Whales RSS.

    Extracts stock tickers from both entry title AND body content,
    filtering against a comprehensive known-ticker list to avoid
    false positives from common English words.
    """
    import re
    posts = _fetch_rss("https://unusual-whales.ghost.io/rss/")
    if not posts:
        return {"error": "RSS fetch returned no posts"}

    # Comprehensive known US stock/ETF tickers for false-positive filtering
    KNOWN_TICKERS: set = {
        "AAPL", "TSLA", "NVDA", "AMD", "AMZN", "MSFT", "GOOGL", "GOOG",
        "META", "NFLX", "JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW",
        "GME", "AMC", "SPY", "QQQ", "IWM", "DIA", "SMCI", "HOOD",
        "PLTR", "RIVN", "WDC", "MU", "MARA", "COIN", "MSTR", "SOFI",
        "IBM", "INTC", "QCOM", "AVGO", "TXN", "CSCO", "ORCL", "CRM",
        "ADBE", "PYPL", "SQ", "UBER", "LYFT", "ABNB", "SNAP", "PINS",
        "DIS", "NKE", "SBUX", "MCD", "KO", "PEP", "WMT", "TGT", "COST",
        "HD", "LOW", "BA", "CAT", "GE", "F", "GM", "XOM", "CVX",
        "SHEL", "BP", "PFE", "MRNA", "JNJ", "UNH", "LLY", "ABBV",
        "BMY", "GILD", "V", "MA", "AXP", "NOC", "LMT", "RTX", "GD",
        "ASTS", "RKLB", "PL", "AI", "SPCE", "CHWY", "DASH",
        "ARKK", "ARKW", "ARKG", "TLT", "TLH", "IEI", "SHY",
        "XLF", "XLK", "XLE", "XLV", "XLI", "XLP", "XLU", "XLB", "XLY",
        "VTI", "VOO", "VXUS", "BND", "VNQ", "GLD", "SLV", "USO",
        "TQQQ", "SQQQ", "UPRO", "SPXS", "UVXY", "VIXY",
        "FNGU", "FNGD", "LABU", "LABD", "SOXL", "SOXS",
        "TECL", "TECS", "FAS", "FAZ", "YINN", "YANG",
    }
    # Also include all tracked symbols and company map tickers
    KNOWN_TICKERS.update(_tracked_symbols)
    for t in _COMPANY_TICKER_MAP.values():
        if t:
            KNOWN_TICKERS.add(t)

    flows = []
    for guid, title, link, body_text in posts[:30]:
        # Extract from title first
        combined = title + " " + body_text

        # Find all potential tickers via $TICKER format, word-boundary, and company names
        tickers = set()

        # $TICKER format or uppercase 1-5 letter words near context
        ticker_candidates = re.findall(r'\$([A-Z]{1,5})\b', combined)
        tickers.update(t for t in ticker_candidates if t in KNOWN_TICKERS)

        # Bare uppercase ticker candidates (word-boundary match)
        bare_candidates = re.findall(r'\b([A-Z]{2,5})\b', combined)
        for t in bare_candidates:
            if t in KNOWN_TICKERS:
                tickers.add(t)

        # Also check company names from the map (word-boundary match to avoid false positives)
        text_upper = combined.upper()
        for company, ticker in _COMPANY_TICKER_MAP.items():
            if ticker and ticker in KNOWN_TICKERS and re.search(r'\b' + re.escape(company) + r'\b', text_upper):
                tickers.add(ticker)

        # Determine flow type
        title_lower = title.lower()
        flow_type = "unknown"
        if "sweep" in title_lower:
            flow_type = "sweep"
        elif "dark pool" in title_lower or "darkpool" in title_lower:
            flow_type = "dark_pool"
        elif "block" in title_lower:
            flow_type = "block_trade"
        elif "unusual" in title_lower:
            flow_type = "unusual_volume"

        flows.append({
            "title": title[:300],
            "summary": body_text[:500],
            "tickers": sorted(tickers),
            "flow_type": flow_type,
            "url": link,
        })

    return {
        "flows": flows,
        "total": len(flows),
        "fetched_at": datetime.now().isoformat(),
    }


_sec_cik_tickers: Dict[str, str] = {}  # CIK (no leading zeros) → ticker
_sec_cik_tickers_loaded = False


def _load_sec_cik_tickers():
    """Fetch and cache SEC company_tickers.json (CIK → ticker mapping)."""
    global _sec_cik_tickers, _sec_cik_tickers_loaded
    if _sec_cik_tickers_loaded:
        return
    try:
        import requests as req
        resp = req.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": "paper-trading-data-bus/1.0 (contact@example.com)"},
            timeout=20,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
            for entry in data.values():
                cik = str(entry.get("cik_str", ""))
                ticker = entry.get("ticker", "")
                if cik and ticker:
                    _sec_cik_tickers[cik] = ticker
            _sec_cik_tickers_loaded = True
            log.info("Loaded %d CIK→ticker mappings from SEC", len(_sec_cik_tickers))
    except Exception as e:
        log.warning("Could not load SEC CIK tickers: %s", e)


def _cik_to_ticker(cik_str: str) -> str:
    """Look up ticker for a CIK string (strips leading zeros)."""
    cik = cik_str.lstrip("0")
    return _sec_cik_tickers.get(cik, "")


def _fetch_insider_filings() -> dict:
    """Fetch recent insider Form 4 filings from SEC EDGAR."""
    import re
    from datetime import datetime as dt, timedelta
    import requests as req

    _load_sec_cik_tickers()

    today = dt.now().strftime("%Y-%m-%d")
    yesterday = (dt.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    url = (
        f"https://efts.sec.gov/LATEST/search-index"
        f"?q=&dateRange=custom&startdt={yesterday}&enddt={today}&forms=4&from=0"
    )

    try:
        resp = req.get(url, headers={"User-Agent": "paper-trading-data-bus/1.0 (contact@example.com)"}, timeout=15)
        if resp.status_code != 200:
            return {"error": f"SEC EDGAR returned {resp.status_code}"}

        data = resp.json()
        hits = data.get("hits", {}).get("hits", [])

        filings = []
        for hit in hits:
            src = hit.get("_source", {})
            display_names = src.get("display_names", [])
            ciks = src.get("ciks", [])

            # display_names[0] = the insider (person), display_names[1] = the issuer (company)
            # Use last entry as the company; first entry is always the filer (individual)
            issuer_name = ""
            issuer_cik = ""
            if len(display_names) >= 2:
                issuer_entry = display_names[-1]  # e.g. "GERMAN AMERICAN BANCORP, INC.  (CIK 0000714395)"
                issuer_name = re.sub(r'\s*\(CIK\s*\d+\)', '', issuer_entry).strip()
                cik_match = re.search(r'CIK\s+(\d+)', issuer_entry)
                issuer_cik = cik_match.group(1) if cik_match else (ciks[-1] if len(ciks) >= 2 else "")
            elif display_names:
                issuer_name = re.sub(r'\s*\(CIK\s*\d+\)', '', display_names[0]).strip()
                issuer_cik = ciks[0] if ciks else ""

            ticker = _cik_to_ticker(issuer_cik) if issuer_cik else ""

            filer_name = ""
            if display_names:
                filer_name = re.sub(r'\s*\(CIK\s*\d+\)', '', display_names[0]).strip()

            filings.append({
                "company": issuer_name[:100],
                "filer": filer_name[:100],
                "ticker": ticker,
                "filing_type": src.get("file_type", "4"),
                "file_date": src.get("file_date", ""),
                "description": (src.get("file_description") or "")[:200],
            })

        return {
            "filings": filings,
            "total": len(filings),
            "fetched_at": datetime.now().isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# LoneStarOracle MCP Fetchers (Phase 2)
# ═══════════════════════════════════════════════════════════════════════════════

LONESTAR_SERVER = "lonestar"


def _lonestar_call(tool_name: str, params: dict = None, timeout: float = None) -> Optional[dict]:
    """Call a LoneStarOracle MCP tool. Returns parsed result or None on failure."""
    if not _mcp_available:
        return None
    try:
        manager = get_manager()
        if manager is None:
            return None
        return manager.call_tool(LONESTAR_SERVER, tool_name, params, timeout)
    except Exception as e:
        log.debug("LoneStarOracle %s failed: %s", tool_name, e)
        return None


def _lonestar_text(result: Optional[dict]) -> Optional[str]:
    """Extract text content from an MCP tool result."""
    if not result:
        return None
    content = result.get("content", [])
    for block in content:
        if block.get("type") == "text":
            return block.get("text", "")
    # Fallback: check structuredContent
    sc = result.get("structuredContent")
    if sc:
        import json
        return json.dumps(sc)
    return None


def _fetch_lonestar_options_flow(symbol: str = None) -> Optional[dict]:
    """Fetch options flow from LoneStarOracle MCP.

    Requires a ticker parameter. Returns None if no symbol is provided
    (lonestar rejects calls without a ticker).
    """
    if not symbol:
        return None
    params = {"ticker": symbol.upper()}
    result = _lonestar_call("options_flow", params)
    text = _lonestar_text(result)
    if text:
        # Reject lonestar validation errors (not JSON, just a Pydantic error trace)
        if "validation error" in text.lower() or "missing required" in text.lower():
            log.debug("LoneStarOracle options_flow validation error: %s", text[:200])
            return None
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            data = {"raw_text": text[:2000]}
        data["source"] = "lonestar"
        data["fetched_at"] = datetime.now().isoformat()
        return data
    return None


def _fetch_lonestar_insider_trades(symbol: str = None) -> Optional[dict]:
    """Fetch insider trading data from LoneStarOracle MCP."""
    params = {}
    if symbol:
        params["ticker"] = symbol.upper()
    result = _lonestar_call("insider_trading", params if params else None)
    text = _lonestar_text(result)
    if text:
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            data = {"raw_text": text[:2000]}
        data["source"] = "lonestar"
        data["fetched_at"] = datetime.now().isoformat()
        return data
    return None


def _fetch_lonestar_macro() -> Optional[dict]:
    """Fetch macro indicators from LoneStarOracle MCP."""
    result = _lonestar_call("macro_indicators")
    text = _lonestar_text(result)
    if text:
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            data = {"raw_text": text[:2000]}
        data["source"] = "lonestar"
        data["fetched_at"] = datetime.now().isoformat()
        return data
    return None


def _fetch_lonestar_earnings(symbol: str = None) -> Optional[dict]:
    """Fetch earnings calendar from LoneStarOracle MCP."""
    params = {}
    if symbol:
        params["ticker"] = symbol.upper()
    result = _lonestar_call("earnings_calendar", params if params else None)
    text = _lonestar_text(result)
    if text:
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            data = {"raw_text": text[:2000]}
        data["source"] = "lonestar"
        data["fetched_at"] = datetime.now().isoformat()
        return data
    return None


def _fetch_lonestar_risk(symbols: List[str] = None) -> Optional[dict]:
    """Fetch portfolio risk scoring from LoneStarOracle MCP."""
    params = {}
    if symbols:
        params["tickers"] = ",".join(symbols)
    result = _lonestar_call("portfolio_risk", params if params else None)
    text = _lonestar_text(result)
    if text:
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            data = {"raw_text": text[:2000]}
        data["source"] = "lonestar"
        data["fetched_at"] = datetime.now().isoformat()
        return data
    return None


def _fetch_lonestar_technical_scan(symbol: str) -> Optional[dict]:
    """Fetch multi-timeframe technical scan from LoneStarOracle MCP."""
    params = {"ticker": symbol.upper()}
    result = _lonestar_call("multi_timeframe_scan", params)
    text = _lonestar_text(result)
    if text:
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            data = {"raw_text": text[:2000]}
        data["source"] = "lonestar"
        data["fetched_at"] = datetime.now().isoformat()
        return data
    return None


def _fetch_lonestar_equity_analysis(symbol: str) -> Optional[dict]:
    """Fetch equity analysis from LoneStarOracle MCP."""
    params = {"ticker": symbol.upper()}
    result = _lonestar_call("equity_analysis", params)
    text = _lonestar_text(result)
    if text:
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            data = {"raw_text": text[:2000]}
        data["source"] = "lonestar"
        data["fetched_at"] = datetime.now().isoformat()
        return data
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Praesentire MCP Fetchers (Phase 4) — Cross-Language Sentiment
# ═══════════════════════════════════════════════════════════════════════════════

PRAESENTIRE_SERVER = "praesentire"

# Generic MCP text extraction (used by both LoneStar and Praesentire)
_mcp_text = _lonestar_text


def _praesentire_call(tool_name: str, params: dict = None, timeout: float = None) -> Optional[dict]:
    """Call a Praesentire MCP tool. Returns parsed result or None on failure."""
    if not _mcp_available:
        return None
    try:
        manager = get_manager()
        if manager is None:
            return None
        return manager.call_tool(PRAESENTIRE_SERVER, tool_name, params, timeout)
    except Exception as e:
        log.debug("Praesentire %s failed: %s", tool_name, e)
        return None


def _fetch_praesentire_sentiment(symbol: str) -> Optional[dict]:
    """Fetch bilingual sentiment for a single ticker from Praesentire MCP.

    Returns aggregated -1 to +1 sentiment score + confidence + bull/bear
    distribution + latest 3 articles with rationale.
    """
    result = _praesentire_call("get_sentiment", {"ticker": symbol.upper()})
    text = _mcp_text(result)
    if text:
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            data = {"raw_text": text[:2000]}
        data["source"] = "praesentire"
        data["fetched_at"] = datetime.now().isoformat()
        return data
    return None


def _fetch_praesentire_divergence(symbol: str) -> Optional[dict]:
    """Fetch cross-language sentiment divergence from Praesentire MCP.

    Returns English vs Traditional Chinese sentiment side-by-side + divergence
    score. |divergence| > 0.3 indicates a notable cross-market signal.
    """
    result = _praesentire_call("compare_languages", {"ticker": symbol.upper()})
    text = _mcp_text(result)
    if text:
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            data = {"raw_text": text[:2000]}
        data["source"] = "praesentire"
        data["fetched_at"] = datetime.now().isoformat()
        return data
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# SQLite Bridge (cold data from shared/trader.db)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_sqlite_connection(readonly=True):
    """Get a connection to shared/trader.db."""
    db_path = SHARED_DIR / "trader.db"
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout=5000")
    else:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_get_fundamentals(symbol: str) -> Optional[dict]:
    """Get fundamentals from shared/cache.db SQLite fallback."""
    try:
        conn = _get_sqlite_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM fundamentals WHERE ticker=? ORDER BY fetched_at DESC LIMIT 1",
            (symbol,)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        log.debug("SQLite fundamentals read failed: %s", e)
        return None


def _get_db_signals() -> List[dict]:
    """Fallback: read trader_decisions + conviction_plays from shared/trader.db."""
    signals = []
    try:
        conn = _get_sqlite_connection()
        cursor = conn.cursor()

        # Last 20 trader_decisions across all traders
        cursor.execute(
            "SELECT agent_id, trader_id, timestamp, action, ticker, confidence, thesis, mood "
            "FROM trader_decisions ORDER BY timestamp DESC LIMIT 20"
        )
        for row in cursor.fetchall():
            d = dict(row)
            action = (d.get("action") or "").upper()
            if action == "BUY":
                bias = "bullish"
            elif action == "SELL":
                bias = "bearish"
            else:
                bias = "neutral"
            signals.append({
                "agent": d.get("agent_id", ""),
                "trader_id": d.get("trader_id", ""),
                "ticker": d.get("ticker", ""),
                "regime": "",
                "bias": bias,
                "conviction": d.get("confidence", 0.0) or 0.0,
                "note": (d.get("thesis") or "")[:200],
                "timestamp": d.get("timestamp", ""),
                "source": "db_trader_decisions",
            })

        # Active conviction plays
        cursor.execute(
            "SELECT agent_id, ticker, thesis, conviction_level, status, added_at "
            "FROM conviction_plays WHERE status IN ('active', 'open')"
        )
        for row in cursor.fetchall():
            cp = dict(row)
            signals.append({
                "agent": cp.get("agent_id", ""),
                "trader_id": "",
                "ticker": cp.get("ticker", ""),
                "regime": "",
                "bias": "bullish",
                "conviction": cp.get("conviction_level", 0.0) or 0.0,
                "note": (cp.get("thesis") or "")[:200],
                "timestamp": cp.get("added_at", ""),
                "source": "db_conviction_play",
            })

        conn.close()
    except Exception as e:
        log.warning("DB signals fallback failed: %s", e)

    # Deduplicate by agent+ticker keeping highest conviction
    unique = {}
    for s in signals:
        key = (s["agent"], s["ticker"])
        if key not in unique or s["conviction"] > unique[key]["conviction"]:
            unique[key] = s
    return sorted(unique.values(), key=lambda x: x.get("timestamp", ""), reverse=True)


def _sqlite_get_congress_trades(limit: int = 20) -> List[dict]:
    """Get recent congress trades from SQLite (if stored)."""
    return []  # Placeholder — congress data not in cache.db schema yet


def _get_cache_db_connection(readonly=True):
    """Get a connection to shared/cache.db."""
    db_path = SHARED_DIR / "cache.db"
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout=5000")
    else:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_persist(table: str, data: dict):
    """INSERT a row into cache.db. Maps common field name aliases."""
    field_map = {
        # fundamentals aliases
        "pe": "pe_ratio",
        # sentiment aliases
        "sentiment_score": "overall_sentiment",
        # crypto price
        "price": "close",
    }
    mapped = {}
    for k, v in data.items():
        target = field_map.get(k, k)
        mapped[target] = v

    try:
        conn = _get_cache_db_connection(readonly=False)
        cursor = conn.cursor()
        columns = list(mapped.keys())
        placeholders = ", ".join(["?" for _ in columns])
        col_str = ", ".join(columns)
        sql = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})"
        cursor.execute(sql, [mapped.get(c) for c in columns])
        conn.commit()
        conn.close()
        # Mirror to Postgres (best-effort, cache tables)
        dual_writer.write(table, mapped)
        return True
    except Exception as e:
        log.warning("_sqlite_persist(%s) failed: %s", table, e)
        return False


def _db_read(table: str, where_clause: str, params: tuple, order_by: str = None, limit: int = 1) -> Optional[dict]:
    """Read a single row from cache.db.

    Args:
        table: Table name.
        where_clause: e.g. "ticker=?"
        params: Parameter tuple, e.g. ("AAPL",)
        order_by: Optional ORDER BY clause, e.g. "fetched_at DESC"
        limit: Max rows (default 1). 0 means no limit.
    Returns:
        Single dict or None if no rows.
    """
    try:
        conn = _get_cache_db_connection(readonly=True)
        cursor = conn.cursor()
        sql = f"SELECT * FROM {table} WHERE {where_clause}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit > 0:
            sql += f" LIMIT {limit}"
        cursor.execute(sql, params)
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        log.debug("_db_read(%s) failed: %s", table, e)
        return None


def _db_read_multi(table: str, where_clause: str, params: tuple, order_by: str = None, limit: int = 50) -> List[dict]:
    """Read multiple rows from cache.db."""
    try:
        conn = _get_cache_db_connection(readonly=True)
        cursor = conn.cursor()
        sql = f"SELECT * FROM {table} WHERE {where_clause}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit > 0:
            sql += f" LIMIT {limit}"
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.debug("_db_read_multi(%s) failed: %s", table, e)
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# Background Schedulers
# ═══════════════════════════════════════════════════════════════════════════════

class Scheduler:
    """Background scheduler with dual-rate support — fast during market hours, slow off-hours."""

    def __init__(self, name: str, intervals: dict, fetch_fn, source_key: str = None):
        self.name = name
        self.intervals = intervals  # {"market": X, "off": Y}
        self.fetch_fn = fetch_fn
        self.source_key = source_key or name  # for fetch stats tracking
        self._active_interval: float = intervals.get("off", 300)  # start conservative
        self._current_mode: str = "off"
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last_run: float = 0
        self.last_error: Optional[str] = None
        self.run_count: int = 0
        self.consecutive_errors: int = 0
        self.crash_recovery_attempted: bool = False

    def _get_active_interval(self) -> float:
        """Determine current interval based on market status."""
        if _is_market_open():
            return self.intervals.get("market", self.intervals.get("off", 300))
        return self.intervals.get("off", 300)

    @property
    def current_mode(self) -> str:
        return "market" if _is_market_open() else "off"

    @property
    def current_interval(self) -> float:
        return self._active_interval

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name=f"sched-{self.name}")
        self._thread.start()
        log.info("Scheduler %s started (market=%ss off=%ss)", self.name,
                 self.intervals.get("market", "?"), self.intervals.get("off", "?"))

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            # Determine active interval and mode for this iteration
            interval = self._get_active_interval()
            mode = "market" if interval == self.intervals.get("market") else "off"
            prev_mode = self._current_mode

            if mode != prev_mode:
                log.info("Scheduler %s: switching from %s → %s (interval %ss)",
                         self.name, prev_mode, mode, interval)
            self._active_interval = interval
            self._current_mode = mode

            try:
                self.fetch_fn()
                self.run_count += 1
                self.last_error = None
                self.consecutive_errors = 0
                # Track success hit
                with _fetch_stats_lock:
                    stats = _fetch_stats.setdefault(self.source_key, {"hits": 0, "misses": 0, "errors": 0})
                    stats["hits"] += 1
                # Log scheduler event
                self._log_event("ok", "run #%d" % self.run_count)
            except Exception as e:
                # Build a descriptive error string — captures exception type even
                # when the message is empty (e.g. socket timeouts, None errors).
                err_msg = str(e) or repr(e)
                if not err_msg or err_msg == "None":
                    err_msg = f"{type(e).__name__}: (no error message)"
                self.last_error = err_msg
                self.consecutive_errors += 1
                # Crash detection: if 3+ consecutive errors across multiple
                # scheduler cycles, log an alert so monitoring can act.
                if self.consecutive_errors >= 3:
                    crash_msg = (f"CRASH DETECTED: {self.name} has {self.consecutive_errors} "
                                 f"consecutive errors (last: {err_msg[:80]})")
                    log.critical(crash_msg)
                    with _error_log_lock:
                        _error_log.append({
                            "time": datetime.now().isoformat(),
                            "source": self.name,
                            "error": crash_msg,
                        })
                # Track error
                with _fetch_stats_lock:
                    stats = _fetch_stats.setdefault(self.source_key, {"hits": 0, "misses": 0, "errors": 0})
                    stats["errors"] += 1
                # Log error to both scheduler event log and error log
                self._log_event("error", err_msg)
                with _error_log_lock:
                    _error_log.append({
                        "time": datetime.now().isoformat(),
                        "source": self.name,
                        "error": err_msg,
                    })
                log.error("Scheduler %s failed: %s", self.name, err_msg)

            self.last_run = time.time()
            # Sleep in small chunks so we can exit on stop
            deadline = time.time() + interval
            while time.time() < deadline and not self._stop.is_set():
                # Market-open detector: during off-hours sleep, break sleep early
                # when market opens to avoid stale-data delay at 9:30 AM ET.
                # Without this, a scheduler that last ran at e.g. 9:27 AM (off-hours,
                # 300s interval) would sleep until 9:32 AM before its first
                # real-time fetch, leaving traders with stale pre-market data.
                if self._current_mode == "off" and _is_market_open():
                    log.info("Scheduler %s: market opened — breaking sleep early", self.name)
                    break
                time.sleep(min(1, deadline - time.time()))

    def _log_event(self, event_type: str, detail: str):
        with _scheduler_events_lock:
            _scheduler_events.append({
                "time": datetime.now().isoformat(),
                "scheduler": self.name,
                "type": event_type,
                "detail": detail,
            })

    def status(self) -> dict:
        return {
            "name": self.name,
            "interval": self._active_interval,
            "mode": self._current_mode,
            "last_run": datetime.fromtimestamp(self.last_run).isoformat() if self.last_run else None,
            "last_error": self.last_error,
            "run_count": self.run_count,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Flask Application
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# Known watchlist symbols (populated by scheduler, can be overridden)
_tracked_symbols: Set[str] = set()
_tracked_crypto: Set[str] = set()


# ── Health ────────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "data-bus",
        "started_at": datetime.fromtimestamp(_start_time).isoformat(),
        "uptime_seconds": time.time() - _start_time,
        "cache_stats": _cache.stats(),
        "signal_count": len(_signals_cache),
        "tracked_symbols": len(_tracked_symbols),
        "schedulers": [s.status() for s in _schedulers],
    })


# ── Source Quality ───────────────────────────────────────────────────────────

@app.route("/source-quality")
def source_quality():
    """
    GET /source-quality?source=reddit&days=30&min_posts=3
    Returns prediction accuracy stats per social/news source from cache.db.
    """
    source_filter = request.args.get("source", "").strip()
    days = request.args.get("days", 90, type=int)
    min_posts = request.args.get("min_posts", 3, type=int)

    db_path = SHARED_DIR / "cache.db"
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row

        where = "WHERE 1=1"
        params = []
        if source_filter:
            where += " AND source = ?"
            params.append(source_filter)
        if days:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            where += " AND timestamp >= ?"
            params.append(cutoff)

        rows = conn.execute(f"""
            SELECT 
                source,
                COUNT(*) as total,
                SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) as correct_count,
                ROUND(AVG(CASE WHEN correct = 1 THEN 1.0 ELSE 0.0 END) * 100, 1) as accuracy_pct,
                ROUND(AVG(quality_score), 3) as avg_quality,
                ROUND(AVG(ABS(price_change_pct)), 4) as avg_move_pct,
                ROUND(AVG(post_count), 1) as avg_posts,
                MAX(timestamp) as last_scored
            FROM source_quality
            {where}
            GROUP BY source
            HAVING total >= ?
            ORDER BY accuracy_pct DESC
        """, params + [min_posts]).fetchall()
        conn.close()

        sources = [dict(r) for r in rows]
        return jsonify({
            "sources": sources,
            "count": len(sources),
            "lookback_days": days,
        })
    except sqlite3.OperationalError:
        return jsonify({"sources": [], "count": 0, "note": "source_quality table not yet created in cache.db"})
    except Exception as e:
        log.error("Error loading source quality: %s", e)
        return jsonify({"error": str(e), "sources": []}), 500


# ── Metrics (Prometheus) ─────────────────────────────────────────────────────

@app.route("/metrics")
def metrics():
    """Prometheus-format metrics endpoint."""
    from flask import Response

    lines = []
    uptime = time.time() - _start_time

    # Service uptime
    lines.append("# HELP databus_uptime_seconds Service uptime in seconds")
    lines.append("# TYPE databus_uptime_seconds gauge")
    lines.append(f"databus_uptime_seconds {uptime:.1f}")

    # Service status (1=ok, 0=error)
    status_val = 1  # Flask only reaches this if the app is running
    lines.append("# HELP databus_status Service health status (1=ok, 0=error)")
    lines.append("# TYPE databus_status gauge")
    lines.append(f"databus_status {status_val}")

    # Cache entries
    cache_stats = _cache.stats()
    lines.append("# HELP databus_cache_entries Total cache entries")
    lines.append("# TYPE databus_cache_entries gauge")
    lines.append(f"databus_cache_entries {cache_stats['keys']}")

    # Signal count
    lines.append("# HELP databus_signals_active Active trader signals")
    lines.append("# TYPE databus_signals_active gauge")
    lines.append(f"databus_signals_active {len(_signals_cache)}")

    # Tracked symbols
    lines.append("# HELP databus_tracked_symbols Number of tracked stock symbols")
    lines.append("# TYPE databus_tracked_symbols gauge")
    lines.append(f"databus_tracked_symbols {len(_tracked_symbols)}")

    # Scheduler last run timestamps and run counts
    lines.append("# HELP databus_scheduler_last_run_seconds Last scheduler run timestamp")
    lines.append("# TYPE databus_scheduler_last_run_seconds gauge")
    lines.append("# HELP databus_scheduler_run_count_total Total scheduler runs")
    lines.append("# TYPE databus_scheduler_run_count_total counter")
    for s in _schedulers:
        name = s.name
        if s.last_run:
            lines.append(f'databus_scheduler_last_run_seconds{{scheduler="{name}"}} {s.last_run:.1f}')
        lines.append(f'databus_scheduler_run_count_total{{scheduler="{name}"}} {s.run_count}')

    # Scheduler errors
    lines.append("# HELP databus_scheduler_errors Scheduler error count (1=has error)")
    lines.append("# TYPE databus_scheduler_errors gauge")
    for s in _schedulers:
        name = s.name
        lines.append(f'databus_scheduler_errors{{scheduler="{name}"}} {1 if s.last_error else 0}')

    lines.append("")  # newline at end
    return Response("\n".join(lines), mimetype="text/plain; version=0.0.4")


# ── Quotes ────────────────────────────────────────────────────────────────────

def _quote_stale(age_seconds: float) -> bool:
    """Determine if a quote is stale based on current market conditions.

    During market hours: quotes older than 5 minutes are stale.
    After hours: quotes older than 24 hours are stale.
    """
    if _is_market_open():
        return age_seconds > 300  # 5 min
    return age_seconds > 86400  # 24h


def _parse_db_fetched_at(fetched_at_str: str) -> float:
    """Parse an ISO timestamp string to a unix timestamp (float).
    Returns 0 if parsing fails.
    """
    try:
        dt = datetime.fromisoformat(fetched_at_str)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


@app.route("/quotes")
def quotes():
    symbols_str = request.args.get("symbols", "")
    if not symbols_str:
        return jsonify({"error": "symbols parameter required (comma-separated)"}), 400

    symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
    if not symbols:
        return jsonify({"error": "no valid symbols"}), 400

    # Check in-memory cache first
    cache_keys = [f"quote:{s}" for s in symbols]
    cached = _cache.get_multi(cache_keys, TTL["quotes"])
    now_ts = time.time()
    now_iso = datetime.now().isoformat()
    result = {}
    missing = []

    for sym in symbols:
        key = f"quote:{sym}"
        if key in cached:
            qdata = dict(cached[key])  # shallow copy to add field
            # Look up the CacheEntry for age computation
            entry = _cache._store.get(key)
            if entry and entry.fetched_at:
                qdata["quote_age_seconds"] = round(now_ts - entry.fetched_at, 1)
                qdata["cached_at"] = datetime.fromtimestamp(entry.fetched_at).isoformat()
            else:
                qdata["quote_age_seconds"] = 0.0
                qdata["cached_at"] = now_iso
            qdata["stale"] = _quote_stale(qdata["quote_age_seconds"])
            result[sym] = qdata
        else:
            missing.append(sym)

    # On cache miss, try SQLite before fetching live
    if missing:
        # Check cache.db for each missing symbol
        db_hits = {}
        still_missing = []
        for sym in missing:
            db_row = _db_read("prices", "ticker=?", (sym,), order_by="fetched_at DESC")
            if db_row is not None:
                db_hits[sym] = db_row
                _cache.set(f"quote:{sym}", dict(db_row))
            else:
                still_missing.append(sym)

        # Fetch remaining symbols live
        if still_missing:
            fresh = _fetch_alpaca_quotes(still_missing)
            for sym, data in fresh.items():
                qdata = dict(data) if isinstance(data, dict) else data
                qdata["quote_age_seconds"] = 0.0
                qdata["cached_at"] = now_iso
                qdata["stale"] = False
                result[sym] = qdata
                _cache.set(f"quote:{sym}", data)
                # Enqueue DB persistence
                if _write_queue:
                    price_row = {
                        "ticker": sym,
                        "close": data.get("close") or data.get("price"),
                        "high": data.get("high"),
                        "low": data.get("low"),
                        "open": data.get("open"),
                        "volume": data.get("volume"),
                        "rsi": data.get("rsi"),
                        "macd_line": data.get("macd_line"),
                        "macd_signal": data.get("macd_signal"),
                        "macd_histogram": data.get("macd_histogram"),
                        "ma20": data.get("ma20"),
                        "fetched_at": now_iso,
                    }
                    _write_queue.enqueue("prices", price_row)

        # Include DB hits in result — compute real age from DB fetched_at
        for sym in db_hits:
            qdata = dict(db_hits[sym]) if isinstance(db_hits[sym], dict) else db_hits[sym]
            db_ts_str = qdata.get("fetched_at", "")
            if db_ts_str:
                db_ts = _parse_db_fetched_at(str(db_ts_str))
                age = round(now_ts - db_ts, 1) if db_ts > 0 else 0.0
                qdata["quote_age_seconds"] = age
                qdata["cached_at"] = str(db_ts_str)
            else:
                qdata["quote_age_seconds"] = 0.0
                qdata["cached_at"] = now_iso
            qdata["stale"] = _quote_stale(qdata["quote_age_seconds"])
            result[sym] = qdata

    return jsonify({"quotes": result, "cached": len(result) - len(missing) if missing else len(result), "fetched_live": len(missing)})


# ── Bars (Historical OHLCV) ──────────────────────────────────────────────────


@app.route("/bars")
def historical_bars():
    """Fetch historical OHLCV bars for backtesting and parameter sweeps.

    GET /bars?symbols=AAPL,MSFT&interval=daily&start_date=2026-06-01&end_date=2026-07-02

    Fetches from Alpaca StockHistoricalDataClient if available.
    Cached for 1 hour.

    Args:
        symbols: Comma-separated tickers.
        interval: "daily" or "intraday" (default: "daily").
        start_date: ISO date (default: 30 days ago).
        end_date: ISO date (default: today).

    Returns:
        JSON with "symbols" key mapping ticker -> list of bar dicts.
    """
    symbols_str = request.args.get("symbols", "")
    if not symbols_str:
        return jsonify({"error": "symbols parameter required (comma-separated)"}), 400

    symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]
    if not symbols:
        return jsonify({"error": "no valid symbols"}), 400

    interval = request.args.get("interval", "daily").strip().lower()
    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")

    # Default date range: last 30 days
    if not start_date:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # Check cache first
    cache_keys = [f"bars:{s}:{interval}:{start_date}:{end_date}" for s in symbols]
    cached = _cache.get_multi(cache_keys, ttl_seconds=3600)
    result = {}
    missing = []

    for sym in symbols:
        key = f"bars:{sym}:{interval}:{start_date}:{end_date}"
        if key in cached:
            result[sym] = cached[key]
        else:
            missing.append(sym)

    if missing:
        fresh = _fetch_alpaca_historical_bars(missing, start_date, end_date, interval)
        for sym, bars in fresh.items():
            result[sym] = bars
            _cache.set(f"bars:{sym}:{interval}:{start_date}:{end_date}", bars)
        for sym in missing:
            if sym not in result:
                result[sym] = []

    return jsonify({
        "symbols": result,
        "cached": len(result) - len(missing),
        "fetched": len(missing),
        "interval": interval,
        "start_date": start_date,
        "end_date": end_date,
    })


# ── Crypto ────────────────────────────────────────────────────────────────────

@app.route("/crypto")
def crypto():
    symbols_str = request.args.get("symbols", "")
    if not symbols_str:
        return jsonify({"error": "symbols parameter required"}), 400

    symbols = [s.strip().upper() for s in symbols_str.split(",") if s.strip()]

    cache_keys = [f"crypto:{s}" for s in symbols]
    cached = _cache.get_multi(cache_keys, TTL["crypto"])
    result = {}
    missing = []

    for sym in symbols:
        key = f"crypto:{sym}"
        if key in cached:
            result[sym] = cached[key]
        else:
            missing.append(sym)

    # On cache miss, try SQLite before fetching live
    if missing:
        db_hits = {}
        still_missing = []
        for sym in missing:
            db_row = _db_read("prices", "ticker=?", (sym,), order_by="fetched_at DESC")
            if db_row is not None:
                db_hits[sym] = db_row
                _cache.set(f"crypto:{sym}", dict(db_row))
            else:
                still_missing.append(sym)

        if still_missing:
            fresh = _fetch_alpaca_crypto(still_missing)
            for sym, data in fresh.items():
                result[sym] = data
                _cache.set(f"crypto:{sym}", data)
                # Enqueue DB persistence
                if _write_queue:
                    now_iso = datetime.now().isoformat()
                    price_row = {
                        "ticker": sym,
                        "close": data.get("price"),
                        "fetched_at": now_iso,
                    }
                    _write_queue.enqueue("prices", price_row)

        for sym in db_hits:
            result[sym] = db_hits[sym]

    return jsonify({"crypto": result, "cached": len(result) - len(missing) if missing else len(result), "fetched_live": len(missing)})


# ── Fundamentals ──────────────────────────────────────────────────────────────

@app.route("/fundamentals")
def fundamentals():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required"}), 400

    cache_key = f"fundamentals:{symbol}"
    cached = _cache.get(cache_key, TTL["fundamentals"])
    if cached is not None:
        return jsonify({"symbol": symbol, "fundamentals": cached, "source": "cache"})

    # Try SQLite fallback first (only if it has real data, not all-null cache entries)
    db_row = _db_read("fundamentals", "ticker=?", (symbol,), order_by="fetched_at DESC")
    if db_row is not None:
        has_data = any(db_row.get(k) for k in ('pe_ratio', 'eps', 'dividend_yield', 'market_cap', 'analyst_target', 'roe', 'pe'))
        if has_data:
            _cache.set(cache_key, dict(db_row))
            return jsonify({"symbol": symbol, "fundamentals": dict(db_row), "source": "sqlite"})

    # Try live fetch
    data = _fetch_fundamentals(symbol)
    if data and data.get("pe_ratio") is not None:
        _cache.set(cache_key, data)
        # Enqueue DB persistence
        if _write_queue:
            now_iso = datetime.now().isoformat()
            fund_row = {
                "ticker": symbol,
                "pe_ratio": data.get("pe_ratio"),
                "eps": data.get("eps"),
                "dividend_yield": data.get("dividend_yield") or data.get("dividendYield"),
                "market_cap": data.get("market_cap") or data.get("marketCap"),
                "fetched_at": now_iso,
            }
            _write_queue.enqueue("fundamentals", fund_row)
        return jsonify({"symbol": symbol, "fundamentals": data, "source": "combo_fetch"})

    # SQLite fallback
    sqlite_data = _sqlite_get_fundamentals(symbol)
    if sqlite_data and any(sqlite_data.get(k) for k in ('pe_ratio', 'eps', 'dividend_yield', 'analyst_target')):
        _cache.set(cache_key, dict(sqlite_data))
        return jsonify({"symbol": symbol, "fundamentals": dict(sqlite_data), "source": "sqlite"})

    # Web fallback (yfinance)
    web_data = _fetch_fundamentals_web(symbol)
    if web_data:
        _cache.set(cache_key, web_data)
        return jsonify({"symbol": symbol, "fundamentals": web_data, "source": "web_search_fallback", "quality": "partial"})
    return jsonify({"symbol": symbol, "fundamentals": None, "error": "no data available"}), 404


# ── Sentiment ─────────────────────────────────────────────────────────────────

@app.route("/sentiment", methods=["GET", "POST"])
def sentiment():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        ticker = body.get("ticker", "").strip().upper()
        text = body.get("text", "").strip()
        if not ticker and not text:
            return jsonify({"error": "ticker or text required"}), 400
        if not text:
            text = ticker  # Fallback: analyze just the ticker name
        result = _fetch_sentiment_via_finbert(text, ticker)
        if result:
            cache_key = f"sentiment:{ticker}" if ticker else f"sentiment:text:{hash(text)}"
            _cache.set(cache_key, result)
            # Enqueue DB persistence for ticker-specific sentiment
            if _write_queue and ticker:
                now_iso = datetime.now().isoformat()
                # Extract sentiment score from FinBERT result
                score_raw = result.get("sentiment") or result.get("score") or result.get("label", "")
                sent_row = {
                    "ticker": ticker,
                    "overall_sentiment": str(score_raw)[:50] if not isinstance(score_raw, (int, float)) else ("bullish" if score_raw > 0.3 else "bearish" if score_raw < -0.3 else "neutral"),
                    "fetched_at": now_iso,
                }
                _write_queue.enqueue("sentiment", sent_row)
            return jsonify({"sentiment": result, "source": "finbert"})
        return jsonify({"error": "FinBERT service unavailable"}), 503

    # GET: retrieve cached sentiment (augmented with Praesentire when available)
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required"}), 400

    cache_key = f"sentiment:{symbol}"
    cached = _cache.get(cache_key, TTL["sentiment"])

    # Try Praesentire augmentation (complementary bilingual signal)
    prae_key = f"praesentire_sentiment:{symbol}"
    prae_cached = _cache.get(prae_key, TTL["praesentire_sentiment"])

    if cached is not None and prae_cached is not None:
        return jsonify({
            "symbol": symbol,
            "sentiment": cached,
            "praesentire": prae_cached,
            "source": "cache",
        })
    if cached is not None:
        return jsonify({"symbol": symbol, "sentiment": cached, "source": "cache"})

    # Cache miss — try live analysis
    # First try fetching news headlines for better keyword analysis
    news_analysis = None
    for news_key in [f"news:{symbol}:10", f"news:{symbol}:5", "news:all:20"]:
        news_text = _cache.get(news_key, TTL["news"])
        if news_text and isinstance(news_text, list):
            headlines = [n.get("headline", "") for n in news_text[:5] if n.get("headline", "")]
            if headlines:
                combined = " ".join(headlines)
                news_analysis = _fetch_sentiment_via_finbert(combined, ticker=symbol)
                break
    result = news_analysis or _fetch_sentiment_via_finbert(symbol, symbol)
    if result:
        # Normalize to VADER-compatible format (compound/positive/negative/neutral)
        # FinBERT returns {sentiment_score, label, confidence} which is a
        # different shape than the scheduler's cached {compound, positive, negative, neutral}.
        # Consumers (traders, dashboard) expect compound — null otherwise.
        if "compound" not in result and "sentiment_score" in result:
            score = float(result.get("sentiment_score", 0) or 0)
            label = (result.get("label") or "").lower()
            if label == "positive":
                compound, pos, neg = score, score, 0.0
            elif label == "negative":
                compound, pos, neg = -score, 0.0, score
            else:  # neutral
                compound, pos, neg = 0.0, 0.0, 0.0
            neu = round(1.0 - pos - neg, 4)
            result = {
                "positive": round(pos, 4),
                "negative": round(neg, 4),
                "neutral": max(0.0, neu),
                "compound": round(compound, 4),
                "source": result.get("source", "finbert"),
            }
        _cache.set(cache_key, result)

        # Also try live Praesentire
        prae_data = _fetch_praesentire_sentiment(symbol)
        if prae_data and "error" not in prae_data:
            _cache.set(prae_key, prae_data)
            return jsonify({
                "symbol": symbol,
                "sentiment": result,
                "praesentire": prae_data,
                "source": "live_fallback",
            })
        return jsonify({"symbol": symbol, "sentiment": result, "source": "live_fallback"})
    return jsonify({"symbol": symbol, "sentiment": None, "error": "sentiment analysis unavailable"}), 503


# ── Sentiment Divergence (Praesentire Phase 4) ────────────────────────────────

@app.route("/sentiment-divergence")
def sentiment_divergence():
    """Cross-language sentiment divergence from Praesentire.

    Compares English vs Traditional Chinese sentiment for a ticker.
    |divergence| > 0.3 indicates a notable cross-market signal.
    """
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required"}), 400

    cache_key = f"sentiment_divergence:{symbol}"
    cached = _cache.get(cache_key, TTL["sentiment_divergence"])
    if cached is not None:
        return jsonify({"symbol": symbol, "divergence": cached, "source": "cache"})

    data = _fetch_praesentire_divergence(symbol)
    if data and "error" not in data:
        _cache.set(cache_key, data)
        return jsonify({"symbol": symbol, "divergence": data, "source": "praesentire"})

    return jsonify({"symbol": symbol, "divergence": None, "error": "Praesentire unavailable"}), 503


# ── Options ───────────────────────────────────────────────────────────────────

@app.route("/options")
def options():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required"}), 400

    cache_key = f"options:{symbol}"
    cached = _cache.get(cache_key, TTL["options"])
    if cached is not None:
        return jsonify({"symbol": symbol, "options": cached, "source": "cache"})

    # Try SQLite fallback from prices table
    db_row = _db_read("prices", "ticker=?", (symbol,), order_by="fetched_at DESC")
    if db_row is not None:
        options_from_db = {
            "symbol": symbol,
            "daily_bar": {
                "o": db_row.get("open"),
                "h": db_row.get("high"),
                "l": db_row.get("low"),
                "c": db_row.get("close"),
            },
            "fetched_at": db_row.get("fetched_at"),
            "source": "sqlite",
        }
        _cache.set(cache_key, options_from_db)
        return jsonify({"symbol": symbol, "options": options_from_db, "source": "sqlite"})

    data = _fetch_options_chain(symbol)
    if data:
        _cache.set(cache_key, data)
        # Enqueue DB persistence (bar data to prices table)
        if _write_queue and data.get("daily_bar"):
            bar = data["daily_bar"]
            now_iso = datetime.now().isoformat()
            price_row = {
                "ticker": symbol,
                "open": bar.get("o"),
                "high": bar.get("h"),
                "low": bar.get("l"),
                "close": bar.get("c"),
                "fetched_at": now_iso,
            }
            _write_queue.enqueue("prices", price_row)
        return jsonify({"symbol": symbol, "options": data, "source": "alpaca"})

    return jsonify({"symbol": symbol, "options": None, "error": "no data available"}), 404


# ── News ──────────────────────────────────────────────────────────────────────

@app.route("/news")
def news():
    symbol = request.args.get("symbol", "").strip().upper()
    limit = int(request.args.get("limit", 10))

    cache_key = f"news:{symbol or 'all'}:{limit}"
    cached = _cache.get(cache_key, TTL["news"])
    if cached is not None:
        return jsonify({"symbol": symbol or "all", "news": cached, "source": "cache"})

    # On cache miss, try SQLite before fetching live
    if symbol:
        db_rows = _db_read_multi("news", "relevance LIKE ?", (f"%{symbol}%",), order_by="fetched_at DESC", limit=limit)
    else:
        db_rows = _db_read_multi("news", "1=1", (), order_by="fetched_at DESC", limit=limit)

    if db_rows:
        # Convert to expected format
        news_items = []
        for row in db_rows:
            tickers = row.get("relevance", "").split(",") if row.get("relevance") else []
            news_items.append({
                "headline": row.get("headline", ""),
                "source": row.get("source", ""),
                "symbols": [t.strip() for t in tickers if t.strip()],
                "url": row.get("url", ""),
                "created_at": row.get("fetched_at", ""),
            })
        _cache.set(cache_key, news_items)
        return jsonify({"symbol": symbol or "all", "news": news_items, "source": "sqlite"})

    data = _fetch_alpaca_news(symbol=symbol, limit=limit)
    _cache.set(cache_key, data)
    # Enqueue DB persistence
    if _write_queue and data:
        now_iso = datetime.now().isoformat()
        for article in data:
            tickers = article.get("symbols", [])
            news_row = {
                "headline": article.get("headline", "")[:500],
                "source": article.get("source", ""),
                "relevance": ",".join(tickers[:5]) if tickers else "",
                "url": article.get("url", ""),
                "fetched_at": now_iso,
            }
            _write_queue.enqueue("news", news_row)
    return jsonify({"symbol": symbol or "all", "news": data, "source": "alpaca"})


# ── News Cache (RSS Feed Aggregation) ────────────────────────────────────────

@app.route("/news-cache")
def news_cache_feed():
    """
    GET /news-cache?limit=30&source=marketwatch&days=1

    General news feed from the RSS-collected news_cache table.
    Read-only from Postgres cache — no live fetching.

    Params:
        limit  — max articles (default 30)
        source — filter by source name (optional)
        days   — how many days back (default 1)

    Returns:
        {"news": [...], "count": N, "total": N}
    """
    limit = int(request.args.get("limit", 30))
    source = request.args.get("source", "").strip()
    days = int(request.args.get("days", 1))

    cache_key = f"news-cache:{source or 'all'}:{limit}:{days}"
    cached = _cache.get(cache_key, TTL["news"])
    if cached is not None:
        return jsonify({"news": cached, "count": len(cached), "source": "cache"})

    import psycopg2
    import psycopg2.extras

    db_url = "postgresql://trader:@192.168.1.179:5433/trading"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    where_parts = ["published_at >= %s::timestamptz"]
    params = [cutoff]

    if source:
        where_parts.append("source = %s")
        params.append(source)

    where = " AND ".join(where_parts)

    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT id, url, title, summary, source, published_at, collected_at,
                           tickers, sentiment_score
                    FROM public.news_cache
                    WHERE {where}
                    ORDER BY published_at DESC
                    LIMIT %s""",
                params + [limit],
            )
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        log.warning("news_cache query failed: %s", e)
        return jsonify({"news": [], "count": 0, "error": str(e)}), 500

    articles = []
    for row in rows:
        articles.append({
            "id": row["id"],
            "url": row["url"],
            "title": row["title"],
            "summary": row["summary"],
            "source": row["source"],
            "published_at": row["published_at"].isoformat() if hasattr(row["published_at"], "isoformat") else str(row["published_at"]),
            "collected_at": row["collected_at"].isoformat() if hasattr(row["collected_at"], "isoformat") else str(row["collected_at"]),
            "tickers": row["tickers"] if row["tickers"] else [],
            "sentiment_score": float(row["sentiment_score"]) if row["sentiment_score"] else 0.0,
        })

    _cache.set(cache_key, articles)
    return jsonify({"news": articles, "count": len(articles)})


@app.route("/news/search")
def news_search():
    """
    GET /news/search?q=AAPL&limit=20

    Simple text search on news article title + summary.
    Uses Postgres ILIKE for substring matching.

    Params:
        q      — search query (required)
        limit  — max results (default 20)

    Returns:
        {"news": [...], "count": N, "query": q}
    """
    q = request.args.get("q", "").strip()
    limit = int(request.args.get("limit", 20))

    if not q:
        return jsonify({"news": [], "count": 0, "error": "q parameter required"}), 400

    cache_key = f"news-search:{q}:{limit}"
    cached = _cache.get(cache_key, TTL["news"])
    if cached is not None:
        return jsonify({"news": cached, "count": len(cached), "query": q, "source": "cache"})

    import psycopg2
    import psycopg2.extras

    db_url = "postgresql://trader:@192.168.1.179:5433/trading"
    search_pattern = f"%{q}%"

    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT id, url, title, summary, source, published_at, collected_at,
                           tickers, sentiment_score
                    FROM public.news_cache
                    WHERE title ILIKE %s OR summary ILIKE %s
                    ORDER BY published_at DESC
                    LIMIT %s""",
                (search_pattern, search_pattern, limit),
            )
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        log.warning("news_search query failed: %s", e)
        return jsonify({"news": [], "count": 0, "error": str(e)}), 500

    articles = []
    for row in rows:
        articles.append({
            "id": row["id"],
            "url": row["url"],
            "title": row["title"],
            "summary": row["summary"],
            "source": row["source"],
            "published_at": row["published_at"].isoformat() if hasattr(row["published_at"], "isoformat") else str(row["published_at"]),
            "collected_at": row["collected_at"].isoformat() if hasattr(row["collected_at"], "isoformat") else str(row["collected_at"]),
            "tickers": row["tickers"] if row["tickers"] else [],
            "sentiment_score": float(row["sentiment_score"]) if row["sentiment_score"] else 0.0,
        })

    _cache.set(cache_key, articles)
    return jsonify({"news": articles, "count": len(articles), "query": q})


# ── Social ────────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Strip HTML tags and decode entities from RSS content."""
    import re as _re
    from html import unescape
    text = _re.sub(r'<[^>]+>', ' ', text)
    text = unescape(text)
    text = _re.sub(r'\s+', ' ', text).strip()
    return text


_COMPANY_TICKER_MAP = {
    "MICROSOFT": "MSFT", "APPLE": "AAPL", "TESLA": "TSLA",
    "NVIDIA": "NVDA", "META": "META", "FACEBOOK": "META",
    "AMAZON": "AMZN", "GOOGLE": "GOOGL", "ALPHABET": "GOOGL",
    "NETFLIX": "NFLX", "PALANTIR": "PLTR", "SPACEX": "SPCX",
    "INTEL": "INTC", "AMD": "AMD", "QUALCOMM": "QCOM",
    "BROADCOM": "AVGO", "UBER": "UBER", "AIRBNB": "ABNB",
    "SNAP": "SNAP", "SPOTIFY": "SPOT", "PAYPAL": "PYPL",
    "COINBASE": "COIN", "ROBINHOOD": "HOOD", "GAMESTOP": "GME",
    "AMC": "AMC", "BERKSHIRE HATHAWAY": "BRK.B",
    "DISNEY": "DIS", "STARBUCKS": "SBUX", "NIKE": "NKE",
    "PFIZER": "PFE", "MODERNA": "MRNA", "EXXON": "XOM",
    "CHEVRON": "CVX", "BOEING": "BA", "AST SPACEMOBILE": "ASTS",
    "RARE EARTH": "MP", "JPMORGAN": "JPM", "GOLDMAN SACHS": "GS",
    "BANK OF AMERICA": "BAC", "CITIGROUP": "C", "MORGAN STANLEY": "MS",
    "SALESFORCE": "CRM", "ADOBE": "ADBE", "ORACLE": "ORCL",
    "CISCO": "CSCO", "BLOCK": "SQ", "SQUARE": "SQ",
    "HOME DEPOT": "HD", "LOWES": "LOW", "COSTCO": "COST",
    "WALMART": "WMT", "TARGET": "TGT", "COCA-COLA": "KO",
    "PEPSI": "PEP", "MCDONALDS": "MCD", "JOHNSON & JOHNSON": "JNJ",
    "LOCKHEED": "LMT", "RAYTHEON": "RTX", "LYFT": "LYFT",
    "PINTEREST": "PINS", "WELLS FARGO": "WFC", "SHELL": "SHEL",
    "SPIRIT AIRLINES": "FLYYQ", "ESTEE LAUDER": "EL",
    "BLACKBERRY": "BB",
}


def _find_tickers_in_text(text: str, trackers: set) -> set:
    """Find ticker mentions in text using $TICKER format, word-boundary, and company names."""
    import re as _re
    text_upper = text.upper()
    found = set()
    for ticker in trackers:
        if f"${ticker}" in text_upper or _re.search(r'\b' + _re.escape(ticker) + r'\b', text_upper):
            found.add(ticker)
    for company, ticker in _COMPANY_TICKER_MAP.items():
        if ticker and ticker in trackers and company in text_upper:
            found.add(ticker)
    return found


RSS_FEEDS = [
    # Reddit RSS (public, no-auth — PRAW API removed 2026-06-18)
    "https://www.reddit.com/r/wallstreetbets/.rss",
    "https://www.reddit.com/r/stocks/.rss",
    "https://www.reddit.com/r/investing/.rss",
]


def _fetch_rss(url):
    """Fetch RSS/Atom feed, return list of (guid, title, content_text, link) tuples."""
    import re as _re
    from urllib.request import urlopen as _urlopen, Request as _Request
    from urllib.error import URLError as _URLError
    try:
        req = _Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"})
        with _urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except _URLError as e:
        log.debug("RSS fetch error %s: %s", url, e)
        return []

    posts = []
    ids = _re.findall(r'<id>(.*?)</id>', raw) or _re.findall(r'<guid[^>]*>(.*?)</guid>', raw)
    titles = _re.findall(r'<title[^>]*><!\[CDATA\[(.*?)\]\]></title>|<title[^>]*>(.*?)</title>', raw)
    links = _re.findall(r'<link[^>]*href=["\']([^"\']+)["\']|<link>(.*?)</link>', raw)
    contents = _re.findall(r'<content[^>]*>(.*?)</content>', raw, _re.DOTALL)
    if not contents:
        # Ghost RSS uses <content:encoded> with CDATA
        contents = _re.findall(r'<content:encoded[^>]*><!\[CDATA\[(.*?)\]\]></content:encoded>', raw, _re.DOTALL)
    if not contents:
        # Some RSS feeds use <description> as the full body
        descs = _re.findall(r'<description[^>]*><!\[CDATA\[(.*?)\]\]></description>', raw, _re.DOTALL)
        if descs:
            contents = descs

    titles_flat = [a or b for a, b in titles]
    links_flat = [a or b for a, b in links]
    contents_flat = [_strip_html(c) for c in contents]

    # Skip first title/link/content (feed-level)
    if len(titles_flat) > 1:
        titles_flat = titles_flat[1:]
    if len(links_flat) > 1:
        links_flat = links_flat[1:]
    if len(contents_flat) > 1:
        contents_flat = contents_flat[1:]

    for i, post_id in enumerate(ids):
        title = titles_flat[i] if i < len(titles_flat) else ""
        link = links_flat[i] if i < len(links_flat) else ""
        content_text = contents_flat[i] if i < len(contents_flat) else ""
        if title.strip() or content_text.strip():
            posts.append((post_id.strip(), title.strip(), link.strip(), content_text.strip()))

    return posts


def _fetch_social_bluesky(max_tickers: int = 5, deadline: float = 0) -> dict:
    """Fetch Bluesky sentiment for tracked symbols. Set max_tickers > 0 to limit live fetches.
    If deadline > 0, abort early after wall-clock deadline (Unix timestamp).
    Per-ticker deadline guard: each individual ticker call has a 5-second hard timeout."""
    if not fetch_bluesky_sentiment:
        return {"source": "bluesky", "posts": [], "sentiment_score": 0.0, "matched_tickers": [], "error": "social_sentiment module unavailable"}

    tickers = sorted(_tracked_symbols)
    if max_tickers > 0 and len(tickers) > max_tickers:
        tickers = tickers[:max_tickers]
    if not tickers:
        return {"source": "bluesky", "posts": [], "sentiment_score": 0.0, "matched_tickers": []}

    all_posts = []
    matched_tickers = set()
    total_score = 0.0
    total_weight = 0
    PER_TICKER_TIMEOUT = 5  # seconds per ticker

    for ticker in tickers:
        if deadline > 0 and time.time() > deadline:
            log.debug("Bluesky: deadline reached, stopping after %d tickers", len(matched_tickers))
            break
        try:
            with ThreadPoolExecutor(max_workers=1) as _inner_pool:
                future = _inner_pool.submit(fetch_bluesky_sentiment, ticker)
                result = future.result(timeout=PER_TICKER_TIMEOUT)
        except FutureTimeoutError:
            log.debug("Bluesky per-ticker timeout for %s (>%ss, deadline: %.1fs away)",
                      ticker, PER_TICKER_TIMEOUT, max(0, deadline - time.time()) if deadline > 0 else -1)
            continue
        except Exception as e:
            log.debug("Bluesky fetch failed for %s: %s", ticker, e)
            continue
        post_count = result.get("posts", 0)
        if post_count > 0:
            for p in result.get("top_posts", []):
                all_posts.append(p)
            matched_tickers.add(ticker)
            total_score += result.get("sentiment_score", 0) * post_count
            total_weight += post_count

    sentiment_score = round(total_score / total_weight, 4) if total_weight > 0 else 0.0
    return {
        "source": "bluesky",
        "posts": all_posts[:25],
        "sentiment_score": sentiment_score,
        "matched_tickers": sorted(matched_tickers),
    }


def _fetch_social_stocktwits(max_tickers: int = 5, deadline: float = 0) -> dict:
    """Fetch Stocktwits sentiment for tracked symbols. Set max_tickers > 0 to limit live fetches.
    If deadline > 0, abort early after wall-clock deadline (Unix timestamp).
    Per-ticker deadline guard: each individual ticker call has a 5-second hard timeout."""
    if not fetch_stocktwits_sentiment:
        return {"source": "stocktwits", "posts": [], "sentiment_score": 0.0, "matched_tickers": [], "error": "social_sentiment module unavailable"}

    tickers = sorted(_tracked_symbols)
    if max_tickers > 0 and len(tickers) > max_tickers:
        tickers = tickers[:max_tickers]
    if not tickers:
        return {"source": "stocktwits", "posts": [], "sentiment_score": 0.0, "matched_tickers": []}

    all_posts = []
    matched_tickers = set()
    total_score = 0.0
    total_weight = 0
    PER_TICKER_TIMEOUT = 5  # seconds per ticker

    for ticker in tickers:
        if deadline > 0 and time.time() > deadline:
            log.debug("Stocktwits: deadline reached, stopping after %d tickers", len(matched_tickers))
            break
        try:
            with ThreadPoolExecutor(max_workers=1) as _inner_pool:
                future = _inner_pool.submit(fetch_stocktwits_sentiment, ticker)
                result = future.result(timeout=PER_TICKER_TIMEOUT)
        except FutureTimeoutError:
            log.debug("Stocktwits per-ticker timeout for %s (>%ss, deadline: %.1fs away)",
                      ticker, PER_TICKER_TIMEOUT, max(0, deadline - time.time()) if deadline > 0 else -1)
            continue
        except Exception as e:
            log.debug("Stocktwits fetch failed for %s: %s", ticker, e)
            continue
        msg_count = result.get("messages", 0)
        if msg_count > 0:
            for m in result.get("top_messages", []):
                # Normalize to common 'text'/'body' field
                all_posts.append({
                    "user": m.get("user", "?"),
                    "text": m.get("body", m.get("text", "")),
                    "sentiment": m.get("sentiment", "neutral"),
                    "created_at": m.get("created_at", ""),
                })
            matched_tickers.add(ticker)
            total_score += result.get("sentiment_score", 0) * msg_count
            total_weight += msg_count

    sentiment_score = round(total_score / total_weight, 4) if total_weight > 0 else 0.0
    return {
        "source": "stocktwits",
        "posts": all_posts[:25],
        "sentiment_score": sentiment_score,
        "matched_tickers": sorted(matched_tickers),
    }


def _fetch_social_reddit(max_tickers: int = 3) -> dict:
    """
    Fetch Reddit sentiment with multi-level fallback:

    Level 1 (FAST): Reddit RSS feed via SocialRedditPipeline
      - No auth required
      - Fetches hot posts from /r/wallstreetbets, stocks, investing
      - Extracts tickers from post titles + content
      - Scores sentiment via bullish/bearish keyword matching

    Level 2 (FALLBACK): DuckDuckGo HTML search proxy
      - Searches site:reddit.com for tracked tickers
      - Slow and unreliable (often times out)
      - Only tried if Level 1 returns zero posts

    Level 3 (LAST RESORT): old.reddit.com JSON (no auth)
      - Often blocked by Reddit for automated access
      - Tried only if Level 1 and Level 2 fail

    Returns canonical dict:
      {"source": "reddit", "posts": [...], "sentiment_score": 0.0,
       "matched_tickers": [...], "_note": "method_used"}
    """
    import json as _json
    import time as _time

    # ── Level 1: SocialRedditPipeline (RSS-based, fast, no auth) ──────────
    try:
        pipeline = SocialRedditPipeline()
        all_posts_raw = pipeline.fetch_all_posts()

        if all_posts_raw:
            # Group by subreddit for sub_breakdown, compute aggregate sentiment
            posts_out = []
            matched: set = set()
            total_weighted_score = 0.0
            total_weight = 0

            for p in all_posts_raw:
                tickers = p.get("tickers", [])
                for t in tickers:
                    matched.add(t.upper())

                sentiment = p.get("sentiment_score", 0.0)
                engagement = p.get("upvotes", 0) + p.get("comment_count", 0) * 2
                weight = max(1.0, engagement)
                total_weighted_score += sentiment * weight
                total_weight += weight

                posts_out.append({
                    "title": p.get("post_title", ""),
                    "subreddit": p.get("subreddit", ""),
                    "sentiment": "bullish" if sentiment > 0.1
                                 else ("bearish" if sentiment < -0.1 else "neutral"),
                    "sentiment_score": sentiment,
                    "signal_strength": p.get("signal_strength", 0.0),
                    "tickers": tickers,
                })

            # Also get per-ticker signals to enrich matched_tickers
            if _tracked_symbols:
                for ticker in list(_tracked_symbols)[:max_tickers]:
                    try:
                        ts = pipeline.fetch_ticker_sentiment(ticker)
                        if ts.get("posts", 0) > 0:
                            matched.add(ticker.upper())
                    except Exception:
                        pass

            sentiment_score = round(total_weighted_score / total_weight, 4) if total_weight > 0 else 0.0

            return {
                "source": "reddit",
                "posts": posts_out[:25],
                "sentiment_score": sentiment_score,
                "matched_tickers": sorted(matched),
                "_note": f"RSS pipeline ({len(posts_out)} posts)",
            }
    except Exception as e:
        log.debug("Reddit Level 1 (RSS) failed: %s — trying Level 2", e)

    # ── Level 2: DuckDuckGo search proxy (slow) ──────────────────────────
    if fetch_reddit_via_search and _tracked_symbols:
        tickers = sorted(_tracked_symbols)
        if max_tickers > 0 and len(tickers) > max_tickers:
            tickers = tickers[:max_tickers]

        wallclock_start = _time.time()
        TIMEOUT_TOTAL = 25  # hard limit for all DDG searches combined

        all_posts = []
        matched_tickers = set()
        total_score = 0.0
        total_entries = 0

        for ticker in tickers:
            if _time.time() - wallclock_start > TIMEOUT_TOTAL:
                log.debug("Reddit Level 2 timed out after %d tickers", total_entries)
                break
            try:
                result = fetch_reddit_via_search(ticker)
            except Exception as exc:
                log.debug("Reddit DDG search failed for %s: %s", ticker, exc)
                continue
            posts = result.get("posts_data", [])
            if posts:
                for p in posts:
                    all_posts.append({
                        "title": p.get("title", ""),
                        "subreddit": p.get("subreddit", ""),
                        "sentiment": p.get("sentiment", "neutral"),
                        "snippet": p.get("snippet", ""),
                    })
                matched_tickers.add(ticker)
                bullish = result.get("bullish", 50)
                bearish = result.get("bearish", 25)
                total_score += (bullish - bearish) / 100.0
                total_entries += 1

        if all_posts:
            sentiment_score = round(total_score / max(1, total_entries), 4)
            return {
                "source": "reddit",
                "posts": all_posts[:25],
                "sentiment_score": sentiment_score,
                "matched_tickers": sorted(matched_tickers),
                "_note": f"DuckDuckGo ({len(all_posts)} posts)",
            }

    # ── Level 3: Chrome/Playwright headless scraper ─────────────────────
    # Reddit blocks old.reddit.com JSON (Level 3 in prior version).
    # Playwright's bundled Chromium with anti-detection reliably
    # accesses new Reddit (sh.reddit.com / www.reddit.com).
    if fetch_reddit_via_chrome:
        wallclock_start = _time.time()
        TIMEOUT_TOTAL = 25  # hard limit for all Chrome scrapes

        all_posts = []
        matched_tickers = set()
        ticker_re = re.compile(r'\$([A-Z]{1,5})(?:\b|(?=[.,!?;:\s)\]}]))')

        for sub in ["wallstreetbets", "stocks", "investing"]:
            if _time.time() - wallclock_start > TIMEOUT_TOTAL:
                log.debug("Reddit Level 3 timed out after %s", sub)
                break

            try:
                raw_posts = fetch_reddit_via_chrome(sub, limit=15)
            except Exception as e:
                log.debug("Reddit Chrome scrape failed for r/%s: %s", sub, e)
                continue

            if not raw_posts:
                continue

            for p in raw_posts:
                title = p.get("title", "")
                if not title:
                    continue

                # Extract tickers from title
                tickers = ticker_re.findall(title.upper())
                for t in tickers:
                    matched_tickers.add(t)

                # Sentiment via simple keyword matching
                sentiment = "neutral"
                score = p.get("score", 0)
                text_lower = title.lower()
                bullish_hits = sum(1 for kw in ["moon","rocket","bull","calls","yolo","pump","squeeze","breakout","long","buy"] if kw in text_lower)
                bearish_hits = sum(1 for kw in ["dump","bear","puts","short","crash","bag","red","sell","rekt","dead"] if kw in text_lower)
                if bullish_hits > bearish_hits:
                    sentiment = "bullish"
                elif bearish_hits > bullish_hits:
                    sentiment = "bearish"

                all_posts.append({
                    "title": title[:200],
                    "subreddit": p.get("subreddit", sub),
                    "sentiment": sentiment,
                    "score": score,
                    "comments": p.get("num_comments", 0),
                    "url": p.get("url", ""),
                })

        if all_posts:
            log.info("Reddit Level 3 (Chrome scraper): %d posts from %d subreddits",
                     len(all_posts), len(set(p["subreddit"] for p in all_posts)))
            return {
                "source": "reddit",
                "posts": all_posts[:25],
                "sentiment_score": 0.5,
                "matched_tickers": sorted(matched_tickers),
                "_note": f"Chrome scraper ({len(all_posts)} posts)",
            }

    # ── All levels failed — return empty ──────────────────────────────────
    log.warning("Reddit: all 3 fetch levels returned zero posts")
    return {"source": "reddit", "posts": [], "sentiment_score": 0.0, "matched_tickers": []}


@app.route("/social")
def social():
    """
    Get social media sentiment for tracked symbols.

    Query params:
      source: bluesky, stocktwits, reddit, or all (default: all)
      fast:  (bool) skip live fetch, return cached data immediately
      live:  (bool) skip cache, force live fetch with 10s timeout

    Default mode: returns cached data immediately if available; otherwise
    runs parallel live fetches with a 12s wall-clock timeout.
    Falls back to scheduler cache (social:all) if live returns 0 posts or errors.
    """
    source = request.args.get("source", "all").strip().lower()
    fast_mode = request.args.get("fast", "").strip().lower() in ("1", "true", "yes")
    live_mode = request.args.get("live", "").strip().lower() in ("1", "true", "yes")

    valid_sources = {"bluesky", "stocktwits", "reddit", "all"}
    if source not in valid_sources:
        return jsonify({"error": f"source must be one of: {', '.join(sorted(valid_sources))}"}), 400

    if source == "all":
        sources_to_fetch = ["bluesky", "stocktwits", "reddit"]
    else:
        sources_to_fetch = [source]

    # ── Fast mode: return cached data immediately ────────────────────────
    if fast_mode:
        cached = _get_social_cache(source)
        if cached is not None:
            if isinstance(cached, dict):
                cached["_note"] = "fast mode — served from cache"
            return jsonify(cached)
        # No cache available — return empty gracefully
        return jsonify({"source": source, "results": [], "posts": [],
                        "sentiment_score": 0.0, "matched_tickers": [],
                        "_note": "fast mode — no cached data available"})

    # ── Default/live mode: try cache first, then live with timeout ──────
    # If not explicitly forcing live, serve stale cache if live would be slow
    if not live_mode:
        cached = _get_social_cache(source)
        if cached is not None and isinstance(cached, dict):
            all_cached = sum(
                len(r.get("posts", []))
                for r in cached.get("results", cached.get("posts", []) if isinstance(cached.get("posts"), list) else [])
            ) if cached.get("results") else len(cached.get("posts", []))
            if all_cached > 0:
                cached["_note"] = "served from cache (stale-while-revalidate)"
                log.info("Social: serving %d cached posts for %s (live refresh in background)",
                         all_cached, source)
                # Return cache immediately — response under 50ms
                return jsonify(cached)

    # ── Parallel live fetch with global timeout ──────────────────────────
    SOCIAL_LIVE_TIMEOUT = 10  # seconds total
    live_start = time.time()
    deadline = live_start + SOCIAL_LIVE_TIMEOUT
    results = {}
    live_errors = []
    timed_out_sources = []

    # Map source name → fetch function with deadline
    fetch_map = {
        "bluesky": (lambda d=deadline: _fetch_social_bluesky(max_tickers=5, deadline=d)),
        "stocktwits": (lambda d=deadline: _fetch_social_stocktwits(max_tickers=5, deadline=d)),
        "reddit": (lambda: _fetch_social_reddit(max_tickers=3)),
    }

    if len(sources_to_fetch) == 1:
        # Single source: run inline with timeout guard
        src = sources_to_fetch[0]
        fetcher = fetch_map.get(src)
        if fetcher:
            try:
                results[src] = fetcher()
            except Exception as e:
                log.warning("Social live fetch failed for %s: %s", src, e)
                results[src] = {"source": src, "posts": [], "sentiment_score": 0.0,
                                "matched_tickers": [], "error": str(e)}
                live_errors.append(str(e))
    else:
        # Multiple sources: run in parallel; each source respects deadline internally
        executor = ThreadPoolExecutor(max_workers=len(sources_to_fetch))
        future_map = {}
        for src in sources_to_fetch:
            fetcher = fetch_map.get(src)
            if fetcher:
                future_map[executor.submit(fetcher)] = src

        # Wait for up to SOCIAL_LIVE_TIMEOUT + 2s grace
        shutdown_deadline = time.time() + SOCIAL_LIVE_TIMEOUT + 2
        try:
            for future in as_completed(future_map, timeout=SOCIAL_LIVE_TIMEOUT + 2):
                if time.time() > shutdown_deadline:
                    break
                src = future_map[future]
                try:
                    remaining = max(0.5, shutdown_deadline - time.time())
                    results[src] = future.result(timeout=remaining)
                except Exception as e:
                    log.warning("Social live fetch failed for %s: %s", src, e)
                    results[src] = {"source": src, "posts": [], "sentiment_score": 0.0,
                                    "matched_tickers": [], "error": str(e)}
                    live_errors.append(str(e))
        except FutureTimeoutError:
            log.warning("Social: as_completed timed out after %.1fs",
                       time.time() - live_start)

        # Cancel pending futures and shut down without waiting
        for future in future_map:
            future.cancel()
        executor.shutdown(wait=False)

        # Any sources that didn't complete
        for src in sources_to_fetch:
            if src not in results:
                log.warning("Social: %s fetch timed out after %.1fs", src, time.time() - live_start)
                results[src] = {"source": src, "posts": [], "sentiment_score": 0.0,
                                "matched_tickers": [], "error": "timeout",
                                "_timeout": True}
                timed_out_sources.append(src)

    # ── Build live result ───────────────────────────────────────────────
    total_posts = sum(len(results[s].get("posts", [])) for s in sources_to_fetch)
    elapsed = time.time() - live_start

    if timed_out_sources:
        log.info("Social: %d/%d sources timed out after %.1fs (got %d posts)",
                 len(timed_out_sources), len(sources_to_fetch), elapsed, total_posts)

    # ── Fall back to cache if live returned nothing ──────────────────────
    if total_posts == 0 or (live_errors and not timed_out_sources):
        cached = _get_social_cache(source)
        if cached is not None and isinstance(cached, dict):
            all_cached = sum(
                len(r.get("posts", [])) for r in cached.get("results", [])
            ) if cached.get("results") else len(cached.get("posts", []))
            if all_cached > 0:
                cached["_note"] = f"served from cache (live: {elapsed:.1f}s, {total_posts} posts)"
                log.info("Social: live returned %d posts in %.1fs, serving cache (%d posts)",
                         total_posts, elapsed, all_cached)
                _cache.set(f"social:{source}", cached)
                return jsonify(cached)

    # ── Build response ──────────────────────────────────────────────────
    if source == "all":
        result = {
            "source": "all",
            "results": [results.get("bluesky", {"source": "bluesky", "posts": [], "sentiment_score": 0.0, "matched_tickers": []}),
                        results.get("stocktwits", {"source": "stocktwits", "posts": [], "sentiment_score": 0.0, "matched_tickers": []}),
                        results.get("reddit", {"source": "reddit", "posts": [], "sentiment_score": 0.0, "matched_tickers": []})],
        }
    else:
        result = results.get(source, {"source": source, "posts": [], "sentiment_score": 0.0, "matched_tickers": []})

    if timed_out_sources:
        result["_partial"] = True
        result["_timeout"] = True
        result["_note"] = f"partial results: {', '.join(timed_out_sources)} timed out after {SOCIAL_LIVE_TIMEOUT}s"
    elif elapsed > 5:
        result["_elapsed"] = round(elapsed, 1)

    cache_key = f"social:{source}"
    _cache.set(cache_key, result)
    return jsonify(result)


def _get_social_cache(source: str) -> dict | None:
    """Return cached social data if available within TTL."""
    if source == "all":
        return _cache.get("social:all", TTL["social"])
    # Try source-specific cache first, then all cache
    cached = _cache.get(f"social:{source}", TTL["social"])
    if cached is not None:
        return cached
    # Fall back to social:all cache and extract the relevant source
    all_cached = _cache.get("social:all", TTL["social"])
    if all_cached is not None and "results" in all_cached:
        for r in all_cached["results"]:
            if r.get("source") == source:
                r = dict(r)
                r["_note"] = "extracted from social:all cache"
                return r
    return None


# ── Signals (trader intercom) ─────────────────────────────────────────────────

@app.route("/signals", methods=["GET", "POST"], strict_slashes=False)
@app.route("/signal", methods=["GET", "POST"], strict_slashes=False)
def signals():
    global _signals_cache

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        agent = body.get("agent", "").strip()
        if not agent:
            return jsonify({"error": "agent field required"}), 400

        signal_data = {
            "agent": agent,
            "ticker": body.get("ticker", "").strip().upper(),
            "regime": body.get("regime", ""),
            "bias": body.get("bias", ""),          # bullish / bearish / neutral
            "conviction": body.get("conviction", 0.0),
            "note": body.get("note", ""),
            "timestamp": datetime.now().isoformat(),
        }

        with _signals_lock:
            # Replace any existing signal from this agent+ticker
            _signals_cache = [
                s for s in _signals_cache
                if not (s["agent"] == agent and s["ticker"] == signal_data["ticker"])
            ]
            _signals_cache.append(signal_data)

        log.info("Signal posted: agent=%s ticker=%s bias=%s conviction=%.2f",
                 agent, signal_data["ticker"], signal_data["bias"], signal_data["conviction"])
        return jsonify({"status": "received", "signal": signal_data})

    # GET: return all recent signals
    with _signals_lock:
        # Filter out stale signals
        cutoff = datetime.now() - timedelta(seconds=SIGNAL_MAX_AGE)
        active = [
            s for s in _signals_cache
            if datetime.fromisoformat(s["timestamp"]) > cutoff
        ]
        _signals_cache = active

    result = _signals_cache
    if not result:
        result = _get_db_signals()

    return jsonify({"signals": result, "count": len(result)})


# ── ML Signal ────────────────────────────────────────────────────────────────

@app.route("/ml-signal")
def ml_signal_endpoint():
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol parameter required"}), 400

    if fetch_ml_signal is None:
        return jsonify({"symbol": symbol, "error": "ML signal module unavailable (skill_combo_fetch not loaded)"}), 503
    try:
        result = fetch_ml_signal(symbol)
        return jsonify({"symbol": symbol, "ml_signal": result})
    except Exception as e:
        return jsonify({"symbol": symbol, "error": str(e)}), 503


@app.route("/percentile")
def percentile_endpoint():
    """
    Return percentile rankings for given symbols against all tracked tickers.

    Query the database for all tickers' metric values, then compute
    each requested symbol's percentile rank (0-100) within the distribution.

    Query params:
        symbols: comma-separated list (e.g. AAPL,MSFT,NVDA)
        metric:  which metric to rank on (default: momentum_1m)
                 Supported: pe_ratio, momentum_1m, momentum_3m, momentum_12m,
                            composite_score, analyst_target, dividend_yield, roe

    Returns:
        {
            "metric": "momentum_1m",
            "universe_size": 50,
            "rankings": [
                {"symbol": "NVDA", "value": 12.5, "percentile": 95.0},
                {"symbol": "AAPL", "value": 3.2,  "percentile": 60.0},
                ...
            ]
        }
    """
    symbols_raw = request.args.get("symbols", "").strip()
    metric = request.args.get("metric", "momentum_1m").strip()

    if not symbols_raw:
        return jsonify({"error": "symbols parameter required"}), 400

    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    if not symbols:
        return jsonify({"error": "no valid symbols"}), 400

    # Map metric name to DB column and sort direction
    METRIC_COLUMNS = {
        "pe_ratio":          ("fundamentals", "pe_ratio", "ASC"),
        "momentum_1m":       ("momentum", "momentum_1m", "DESC"),
        "momentum_3m":       ("momentum", "momentum_3m", "DESC"),
        "momentum_12m":      ("momentum", "momentum_12m", "DESC"),
        "composite_score":   ("momentum", "composite_score", "DESC"),
        "analyst_target":    ("fundamentals", "analyst_target", "DESC"),
        "dividend_yield":    ("fundamentals", "dividend_yield", "DESC"),
        "roe":              ("fundamentals", "roe", "DESC"),
    }

    if metric not in METRIC_COLUMNS:
        return jsonify({
            "error": f"unsupported metric: {metric}",
            "supported": list(METRIC_COLUMNS.keys()),
        }), 400

    table, column, sort_dir = METRIC_COLUMNS[metric]

    try:
        # Fetch all tickers' values for this metric (capped at 500 for safety)
        rows = _db_read_multi(
            table=table,
            where_clause=f"{column} IS NOT NULL",
            params=(),
            order_by=f"{column} {sort_dir}",
            limit=500,
        )
        if not rows:
            return jsonify({"error": f"no data available for metric {metric}"}), 404

        # Build lookup: ticker → value
        universe = {}
        for row in rows:
            val = row.get(column)
            if val is not None:
                try:
                    universe[row["ticker"].upper()] = float(val)
                except (ValueError, TypeError):
                    pass

        if not universe:
            return jsonify({"error": "no numeric values found"}), 404

        # Compute percentiles: rank position / total count * 100
        # Higher is better for DESC metrics; lower is better for ASC metrics
        sorted_tickers = list(universe.keys())
        total = len(sorted_tickers)

        rankings = []
        for sym in symbols:
            if sym not in universe:
                rankings.append({
                    "symbol": sym,
                    "value": None,
                    "percentile": None,
                    "note": "not in universe",
                })
                continue

            val = universe[sym]
            if sort_dir == "DESC":
                # Higher value = higher percentile
                rank = sum(1 for v in universe.values() if v <= val)
            else:
                # Lower value = higher percentile (e.g. PE ratio)
                rank = sum(1 for v in universe.values() if v >= val)

            percentile = round((rank / total) * 100, 1)
            rankings.append({
                "symbol": sym,
                "value": round(val, 4),
                "percentile": percentile,
            })

        return jsonify({
            "metric": metric,
            "universe_size": total,
            "rankings": rankings,
            "fetched_at": datetime.now().isoformat(),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/momentum")
def momentum_endpoint():
    """Cross-sectional momentum ranking for Kairos.

    Returns momentum-ranked universe with top buys, avoids, and regime.
    Cached server-side for 5 minutes to reduce Alpaca Data API calls.
    """
    try:
        from src.skill_cross_sectional_momentum import get_cached_momentum_signal
        top_n = int(request.args.get("top_n", 10))
        signal = get_cached_momentum_signal()
        return jsonify(signal)
    except ImportError as e:
        return jsonify({"error": f"Momentum module not available: {e}"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 503

# ── Tick Snapshot — one-stop data for all traders ────────────────────────────

@app.route("/tick-snapshot")
def tick_snapshot():
    """One call per trader per tick. Returns everything needed for a decision."""
    # Aggregate all tracked quotes from per-symbol cache entries
    now_ts = time.time()
    quotes = {}
    for sym in sorted(_tracked_symbols):
        data = _cache.get(f"quote:{sym}")
        if data is not None:
            qdata = dict(data) if isinstance(data, dict) else data
            # Compute age from internal cache entry
            entry = _cache._store.get(f"quote:{sym}")
            if entry and entry.fetched_at:
                qdata["quote_age_seconds"] = round(now_ts - entry.fetched_at, 1)
            else:
                qdata["quote_age_seconds"] = 0.0
            quotes[sym] = qdata

    # SPY regime signal — fetch live if not in cache, then cache it
    regime = _cache.get("ml:SPY")
    if regime is None and fetch_ml_signal is not None:
        try:
            regime = fetch_ml_signal("SPY")
            if regime:
                _cache.set("ml:SPY", regime)
        except Exception:
            regime = {"error": "ML signal unavailable"}

    # ── Portfolio state injection (ground-truth from shared DB) ──────────────
    portfolio_state = {}
    try:
        conn = _get_sqlite_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT agent_id, current_portfolio_value, unrealized_pnl, ytd_pnl "
            "FROM agent_state ORDER BY agent_id"
        )
        for row in cursor.fetchall():
            agent_state = dict(row)
            # Get cash from most recent portfolio_snapshot
            snap = conn.execute(
                "SELECT cash, portfolio_value, daily_pnl, open_positions "
                "FROM portfolio_snapshots WHERE agent_id=? "
                "ORDER BY id DESC LIMIT 1",
                (agent_state["agent_id"],)
            ).fetchone()
            cash = snap["cash"] if snap else None
            daily_pnl = snap["daily_pnl"] if snap else None

            # Active positions summary
            positions_list = []
            pos_rows = conn.execute(
                "SELECT ticker, quantity, avg_entry_price, current_price, unrealized_pl "
                "FROM positions WHERE agent_id=? AND status='open'",
                (agent_state["agent_id"],)
            ).fetchall()
            for p in pos_rows:
                positions_list.append(dict(p))

            pnl_str = f"${daily_pnl:+.2f}" if daily_pnl is not None else "N/A"
            cash_str = f"${cash:,.2f}" if cash is not None else "N/A"
            pos_count = len(positions_list)
            pos_detail = ", ".join(
                f"{p['ticker']} {p['quantity']}sh @ ${p['current_price']:.2f} (${p['unrealized_pl']:+.2f} uPNL)"
                for p in positions_list
            )
            summary = f"Portfolio: {cash_str} cash, {pos_count} position(s)"
            if pos_detail:
                summary += f" — {pos_detail}. "
            summary += f"Daily P&L: {pnl_str}"

            portfolio_state[agent_state["agent_id"]] = {
                "summary": summary,
                "cash": cash,
                "portfolio_value": agent_state.get("current_portfolio_value"),
                "unrealized_pnl": agent_state.get("unrealized_pnl"),
                "daily_pnl": daily_pnl,
                "open_positions": [
                    {"ticker": p["ticker"], "shares": p["quantity"],
                     "entry": p["avg_entry_price"], "current": p["current_price"],
                     "uPNL": p["unrealized_pl"]}
                    for p in positions_list
                ],
            }
        conn.close()
    except Exception as e:
        portfolio_state = {"error": str(e)}

    # ── Performance Brief (compact, for LLM tick context) ──────────────────
    performance_brief = None
    try:
        from performance_brief import generate_brief
        for agent_id in ["kairos", "aldridge", "stonks"]:
            brief = generate_brief(agent_id, days=14, compact=True)
            if brief:
                if performance_brief is None:
                    performance_brief = {}
                performance_brief[agent_id] = {
                    "brief_markdown": brief["brief_markdown"],
                    "stats": brief["stats"],
                }
    except ImportError:
        pass
    except Exception as e:
        log.warning("performance_brief injection failed: %s", e)
    return jsonify({
        "quotes": quotes,
        "regime": regime,
        "fear_greed": _cache.get("fear_greed:latest"),
        "macro": _cache.get("macro:latest"),
        "signals": _signals_cache,
        "portfolio_state": portfolio_state,
        "performance_brief": performance_brief,
        "fetched_at": datetime.now().isoformat(),
    })


# ── Helpers for Dashboard & Debug ────────────────────────────────────────────

def _mask_key(key: str) -> str:
    """Mask an API key: show first 3 and last 3 chars."""
    if not key:
        return "—"
    if len(key) <= 6:
        return key[:2] + "****"
    return key[:3] + "***" + key[-3:]


def _get_env_keys_status() -> dict:
    """Get status of all known API keys (masked)."""
    keys = {}
    for var in [
        "ALPACA_KAIROS_KEY", "ALPACA_KAIROS_SECRET",
        "ALPACA_ALDRIDGE_KEY", "ALPACA_ALDRIDGE_SECRET",
        "ALPACA_STONKS_KEY", "ALPACA_STONKS_SECRET",
        "KAIROS_API_KEY", "KAIROS_SECRET_KEY",
        "ALDRIDGE_API_KEY", "ALDRIDGE_SECRET_KEY",
        "STONKS_API_KEY", "STONKS_SECRET_KEY",
        "FINNHUB_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
        "FRED_API_KEY",
    ]:
        val = os.getenv(var)
        keys[var] = {
            "configured": bool(val),
            "masked": _mask_key(val) if val else None,
        }
    return keys


def _get_db_stats() -> dict:
    """Get full DB statistics from shared/trader.db."""
    db_path = SHARED_DIR / "trader.db"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout=5000")
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {}
        total_rows = 0
        for row in cursor.fetchall():
            tname = row[0]
            try:
                cursor.execute(f'SELECT COUNT(*) FROM "{tname}"')
                count = cursor.fetchone()[0]
                tables[tname] = count
                total_rows += count
            except Exception:
                tables[tname] = "?"
        conn.close()
        db_size_bytes = db_path.stat().st_size if db_path.exists() else 0
        return {
            "tables": tables,
            "total_rows": total_rows,
            "table_count": len(tables),
            "db_size_bytes": db_size_bytes,
            "db_size_mb": round(db_size_bytes / 1024 / 1024, 2),
        }
    except Exception as e:
        return {"error": str(e)}


def _get_trader_pulse() -> dict:
    """Get last activity timestamp for each trader from DB."""
    try:
        conn = _get_sqlite_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT agent_id, MAX(timestamp) as last_ts
            FROM trader_decisions
            GROUP BY agent_id
            ORDER BY agent_id
        """)
        rows = cursor.fetchall()
        conn.close()
        result = {}
        now = time.time()
        for row in rows:
            ts = row["last_ts"]
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    age = now - dt.timestamp()
                except Exception:
                    age = None
            else:
                age = None
            result[row["agent_id"]] = {
                "last_tick": ts,
                "age_seconds": age,
            }
        return result
    except Exception as e:
        return {"error": str(e)}


def _get_overnight_summary() -> Optional[dict]:
    """Get what changed since yesterday's close when market is closed."""
    if _is_market_open():
        return None
    try:
        # Get overnight changes for tracked symbols from cached quotes
        symbols = list(_tracked_symbols)[:15]
        changes = []
        for sym in sorted(symbols):
            entry = _cache.get(f"quote:{sym}")
            if entry and isinstance(entry, dict):
                close = entry.get("close")
                prev_close = entry.get("prev_close") or entry.get("previousClose")
                if close and prev_close and prev_close != 0:
                    pct = (close - prev_close) / prev_close * 100
                    changes.append((sym, round(pct, 2)))
        changes.sort(key=lambda x: abs(x[1]), reverse=True)
        # News count
        news_data = _cache.get("news:all:20")
        news_count = len(news_data) if news_data else 0
        return {
            "changes": changes[:10],
            "news_count": news_count,
        }
    except Exception as e:
        return {"changes": [], "news_count": 0, "error": str(e)}


def _get_data_source_health() -> List[dict]:
    """Get health status for each data source."""
    now = time.time()
    sources = []

    # Alpaca quotes
    alpaca_status = "error"
    alpaca_last = None
    for s in _schedulers:
        if s.name == "quotes":
            if s.last_error:
                alpaca_status = "error"
            elif s.last_run:
                since = now - s.last_run
                if since < s.current_interval * 2:
                    alpaca_status = "green"
                elif since < s.current_interval * 4:
                    alpaca_status = "yellow"
                else:
                    alpaca_status = "red"
                alpaca_last = round(since)
            break
    with _fetch_stats_lock:
        qs = _fetch_stats.get("quotes", {})
    sources.append({
        "name": "Alpaca",
        "status": alpaca_status,
        "last_fetch_sec": alpaca_last,
        "rate_limit": "195/200",
        "stats": qs,
    })

    # FinBERT
    fb_last = None
    fb_status = "yellow"
    fb_cache = _cache.get("sentiment:AAPL")
    if fb_cache:
        fb_status = "green"
    sources.append({
        "name": "FinBERT",
        "status": fb_status,
        "last_fetch_sec": None,
        "rate_limit": "—",
        "stats": {},
    })

    # Finnhub
    fh_status = "yellow"
    cong_data = _cache.get("congress:trades")
    if cong_data is not None:
        fh_status = "green"
    with _fetch_stats_lock:
        fs = _fetch_stats.get("congress", {})
    sources.append({
        "name": "Finnhub",
        "status": fh_status,
        "last_fetch_sec": None,
        "rate_limit": "55/60",
        "stats": fs,
    })

    # Crypto
    with _fetch_stats_lock:
        cs = _fetch_stats.get("crypto", {})
    sources.append({
        "name": "Crypto",
        "status": "green" if cs.get("hits", 0) > 0 else "yellow",
        "last_fetch_sec": None,
        "rate_limit": "195/200",
        "stats": cs,
    })

    # News
    with _fetch_stats_lock:
        ns = _fetch_stats.get("news", {})
    sources.append({
        "name": "News",
        "status": "green" if ns.get("hits", 0) > 0 else "yellow",
        "last_fetch_sec": None,
        "rate_limit": "—",
        "stats": ns,
    })

    # FRED Macro
    fred_status = "yellow"
    fred_last = None
    for s in _schedulers:
        if s.name == "macro":
            if s.last_error:
                fred_status = "error"
            elif s.last_run:
                since = now - s.last_run
                if since < s.current_interval * 2:
                    fred_status = "green"
                elif since < s.current_interval * 4:
                    fred_status = "yellow"
                else:
                    fred_status = "red"
                fred_last = round(since)
            break
    with _fetch_stats_lock:
        ms = _fetch_stats.get("macro", {})
    sources.append({
        "name": "FRED Macro",
        "status": fred_status,
        "last_fetch_sec": fred_last,
        "rate_limit": "120/min",
        "stats": ms,
    })

    # Alpha Vantage Earnings
    with _fetch_stats_lock:
        es = _fetch_stats.get("earnings", {})
    sources.append({
        "name": "AV Earnings",
        "status": "green" if es.get("hits", 0) > 0 else "yellow",
        "last_fetch_sec": None,
        "rate_limit": "25/day",
        "stats": es,
    })

    # Fear & Greed
    with _fetch_stats_lock:
        fgs = _fetch_stats.get("fear_greed", {})
    sources.append({
        "name": "Fear & Greed",
        "status": "green" if fgs.get("hits", 0) > 0 else "yellow",
        "last_fetch_sec": None,
        "rate_limit": "unlimited",
        "stats": fgs,
    })

    # Options Flow
    with _fetch_stats_lock:
        ofs = _fetch_stats.get("flow", {})
    sources.append({
        "name": "Options Flow",
        "status": "green" if ofs.get("hits", 0) > 0 else "yellow",
        "last_fetch_sec": None,
        "rate_limit": "unlimited",
        "stats": ofs,
    })

    # SEC EDGAR
    with _fetch_stats_lock:
        ins = _fetch_stats.get("insiders", {})
    sources.append({
        "name": "SEC EDGAR",
        "status": "green" if ins.get("hits", 0) > 0 else "yellow",
        "last_fetch_sec": None,
        "rate_limit": "10/min",
        "stats": ins,
    })

    # ── Social sentiment sources ────────────────────────────────────────
    social_sources = [
        ("Bluesky", "social:bluesky"),
        ("StockTwits", "social:stocktwits"),
        ("Reddit", "social:reddit"),
    ]
    for sname, cache_key in social_sources:
        cached = _cache.get(cache_key, TTL.get("social", 900))
        if cached and cached.get("posts"):
            status = "green"
        elif cached and cached.get("error"):
            status = "red"
        else:
            status = "yellow"
        sources.append({
            "name": sname,
            "status": status,
            "last_fetch_sec": None,
            "rate_limit": "—",
            "stats": {
                "hits": len(cached.get("posts", [])) if cached else 0,
                "tickers": len(cached.get("matched_tickers", [])) if cached else 0,
            },
        })

    return sources


def _get_news_sources_config() -> List[dict]:
    """Get RSS feeds and social sources being scraped."""
    result = []
    # RSS feeds
    try:
        from rss_watcher import RSS_FEEDS
        for url in RSS_FEEDS:
            result.append({"type": "RSS", "url": url})
    except ImportError:
        pass
    # Social sentiment sources (Stocktwits + Bluesky)
    result.append({"type": "API", "name": "Stocktwits", "endpoint": "https://api.stocktwits.com/api/2/streams/symbol/"})
    result.append({"type": "API", "name": "Bluesky AT Protocol", "endpoint": "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"})
    # Alpaca news API
    result.append({"type": "API", "name": "Alpaca News v1beta1", "endpoint": "https://data.alpaca.markets/v1beta1/news"})
    return result


def _get_trader_configs() -> List[dict]:
    """Get what each trader is watching and their strategy params."""
    try:
        conn = _get_sqlite_connection()
        cursor = conn.cursor()

        # Watchlists
        cursor.execute("""
            SELECT agent_id, ticker, reason, conviction_level
            FROM watchlist
            ORDER BY agent_id, conviction_level DESC
        """)
        watchlist_rows = cursor.fetchall()
        watchlists = {}
        for r in watchlist_rows:
            wl = watchlists.setdefault(r["agent_id"], [])
            wl.append({"ticker": r["ticker"], "reason": r["reason"], "conviction": r["conviction_level"]})

        # Configs
        cursor.execute("SELECT * FROM config")
        config_rows = cursor.fetchall()
        configs = {r["agent_id"]: dict(r) for r in config_rows}

        # Agent profiles
        cursor.execute("SELECT * FROM agent_profile")
        profile_rows = cursor.fetchall()
        profiles = {r["agent_id"]: dict(r) for r in profile_rows}

        # Agent state
        cursor.execute("SELECT * FROM agent_state")
        state_rows = cursor.fetchall()

        conn.close()

        # Compile
        traders = []
        for s in state_rows:
            aid = s["agent_id"]
            wl = watchlists.get(aid, [])
            cfg = configs.get(aid, {})
            prof = profiles.get(aid, {})
            traders.append({
                "agent_id": aid,
                "name": s["name"] or prof.get("name", "?"),
                "trader_name": s["trader_name"] or prof.get("company", "?"),
                "portfolio_value": s["current_portfolio_value"],
                "focus": prof.get("strategic_focus", "?"),
                "watchlist": [x["ticker"] for x in wl],
                "watchlist_detail": wl[:10],
                "polling_freq_sec": cfg.get("polling_freq_sec", "?"),
                "risk_limit_pct": cfg.get("risk_limit_pct", "?"),
                "daily_loss_limit": cfg.get("daily_loss_limit", "?"),
                "max_position_size_pct": cfg.get("max_position_size_pct", "?"),
                "wins": s["wins"],
                "losses": s["losses"],
                "total_trades": s["total_trades"],
                "win_rate": s["win_rate"],
            })
        return traders
    except Exception as e:
        return [{"error": str(e)}]


# ── FRED Macro ────────────────────────────────────────────────────────────────

@app.route("/macro")
def macro():
    """Get macro indicators — FRED data augmented by LoneStarOracle."""
    cached = _cache.get("macro:latest", TTL["macro"])
    if cached is not None:
        return jsonify({"macro": cached, "source": cached.get("source", "cache")})

    # Try LoneStarOracle first (richer: Fed funds, yield curve, CPI, GDP)
    ls_data = _fetch_lonestar_macro()
    if ls_data and "error" not in ls_data:
        # Augment with FRED for additional detail
        fred_data = _fetch_fred_macro()
        if fred_data and "error" not in fred_data:
            ls_data["fred"] = fred_data.get("indicators", {})
        _cache.set("macro:latest", ls_data)
        return jsonify({"macro": ls_data, "source": "lonestar+fred"})

    # Fall back to FRED only
    data = _fetch_fred_macro()
    if data and "error" not in data:
        _cache.set("macro:latest", data)
        return jsonify({"macro": data, "source": "fred"})
    return jsonify({"macro": data, "source": "live"})


# ── Earnings Calendar ────────────────────────────────────────────────────────

@app.route("/earnings")
@app.route("/earnings_today")
def earnings():
    """Get earnings calendar, optionally filtered by symbols.

    Tries LoneStarOracle MCP first, falls back to manual calculation.
    """
    symbol = request.args.get("symbol", "").strip().upper()
    symbols_str = request.args.get("symbols", "").strip().upper()

    cached = _cache.get("earnings:calendar", TTL["earnings"])
    if cached is not None:
        source = cached.get("source", "cache")
        by_ticker = cached.get("earnings_by_ticker", {})
        if symbols_str:
            symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
        elif symbol:
            symbols = [symbol]
        else:
            return jsonify({
                "earnings": cached,
                "total_companies": cached.get("total_companies", 0),
                "source": source,
                "note": "Use ?symbols=AAA,BBB to filter by ticker",
            })
        result = {s: by_ticker.get(s, []) for s in symbols}
        return jsonify({"earnings": result, "requested_symbols": symbols, "source": source})

    # Try LoneStarOracle first
    ls_data = _fetch_lonestar_earnings(symbol if symbol else None)
    if ls_data and "error" not in ls_data:
        _cache.set("earnings:calendar", ls_data)
        return jsonify({"earnings": ls_data, "source": "lonestar"})

    # Fall back to manual calculation
    data = _fetch_earnings_calendar()
    if data and "error" not in data:
        _cache.set("earnings:calendar", data)
        cached = data

    if cached is None or "error" in cached:
        return jsonify({"earnings": cached, "source": "live"})

    by_ticker = cached.get("earnings_by_ticker", {})
    if symbols_str:
        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
    elif symbol:
        symbols = [symbol]
    else:
        return jsonify({
            "earnings": cached,
            "total_companies": cached.get("total_companies", 0),
            "source": "live",
            "note": "Use ?symbols=AAA,BBB to filter by ticker",
        })
    result = {s: by_ticker.get(s, []) for s in symbols}
    return jsonify({"earnings": result, "requested_symbols": symbols, "source": "live"})


# ── Fear & Greed Index ─────────────────────────────────────────────────────────

@app.route("/fear_greed")
def fear_greed():
    cached = _cache.get("fear_greed:latest", TTL["fear_greed"])
    if cached is not None:
        return jsonify({"fear_greed": cached, "source": "cache"})
    data = _fetch_fear_greed()
    if data and "error" not in data:
        _cache.set("fear_greed:latest", data)
    return jsonify({"fear_greed": data, "source": "live"})


# ── Options Flow ───────────────────────────────────────────────────────────────

@app.route("/flow")
def options_flow():
    """Get unusual options flow, optionally filtered by ticker.

    Tries LoneStarOracle MCP first, falls back to Unusual Whales RSS.
    """
    symbol = request.args.get("symbol", "").strip().upper()

    cached = _cache.get("flow:latest", TTL["flow"])
    if cached is not None:
        # Invalidate lonestar cache entries that are error responses (no "flows" key)
        if cached.get("source") == "lonestar" and "flows" not in cached:
            log.debug("Invalidating corrupted lonestar flow cache")
            _cache.delete("flow:latest")
            cached = None
    if cached is not None:
        if symbol:
            filtered = [f for f in cached.get("flows", []) if symbol in f.get("tickers", [])]
            return jsonify({"flow": {"flows": filtered, "total": len(filtered)}, "filtered_by": symbol, "source": cached.get("source", "cache")})
        return jsonify({"flow": cached, "source": cached.get("source", "cache")})

    # Try LoneStarOracle first
    ls_data = _fetch_lonestar_options_flow(symbol if symbol else None)
    if ls_data and "error" not in ls_data:
        _cache.set("flow:latest", ls_data)
        return jsonify({"flow": ls_data, "source": "lonestar"})

    # Fall back to RSS scraper
    data = _fetch_options_flow()
    if data and "error" not in data:
        _cache.set("flow:latest", data)
        cached = data
    if cached is None or "error" in cached:
        return jsonify({"flow": cached, "source": "live"})
    if symbol:
        filtered = [f for f in cached.get("flows", []) if symbol in f.get("tickers", [])]
        return jsonify({"flow": {"flows": filtered, "total": len(filtered)}, "filtered_by": symbol, "source": "rss"})
    return jsonify({"flow": cached, "source": "rss"})


# ── SEC Insider Filings ────────────────────────────────────────────────────────

@app.route("/insiders")
def insiders():
    """Get recent SEC Form 4 insider filings, optionally filtered.

    Tries LoneStarOracle MCP first, falls back to SEC EDGAR.
    """
    symbol = request.args.get("symbol", "").strip().upper()
    symbols_str = request.args.get("symbols", "").strip().upper()

    cached = _cache.get("insiders:latest", TTL["insiders"])
    if cached is not None:
        source = cached.get("source", "cache")
        filings = cached.get("filings", [])
        if symbols_str:
            symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
        elif symbol:
            symbols = [symbol]
        else:
            return jsonify({"insiders": cached, "source": source, "note": "Use ?symbols=JPM,BAC to filter by ticker"})
        filtered = [f for f in filings if f.get("ticker") in symbols]
        return jsonify({"insiders": {"filings": filtered, "total": len(filtered)}, "filtered_by": symbols, "source": source})

    # Try LoneStarOracle first
    ls_data = _fetch_lonestar_insider_trades(symbol if symbol else None)
    if ls_data and "error" not in ls_data:
        _cache.set("insiders:latest", ls_data)
        return jsonify({"insiders": ls_data, "source": "lonestar"})

    # Fall back to SEC EDGAR
    data = _fetch_insider_filings()
    if data and "error" not in data:
        _cache.set("insiders:latest", data)
        cached = data

    if cached is None or "error" in cached:
        return jsonify({"insiders": cached, "source": "live"})

    filings = cached.get("filings", [])
    if symbols_str:
        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
    elif symbol:
        symbols = [symbol]
    else:
        return jsonify({"insiders": cached, "source": "edgar", "note": "Use ?symbols=JPM,BAC to filter by ticker"})
    filtered = [f for f in filings if f.get("ticker") in symbols]
    return jsonify({"insiders": {"filings": filtered, "total": len(filtered)}, "filtered_by": symbols, "source": "edgar"})


# ── Agent Parameters (Learning Loop) ─────────────────────────────────────────

@app.route("/params")
def agent_params():
    """
    GET /params?agent=trader-kairos

    Returns agent parameters from the agent_params table (shared/trader.db).

    If ?agent is provided, returns params for that specific agent.
    If omitted, returns all params grouped by agent_id.

    Columns: agent_id, param_name, param_value, updated_at, source,
             min_value, max_value, step_size, description
    """
    agent = request.args.get("agent", "").strip()

    try:
        conn = _get_sqlite_connection(readonly=True)
        cursor = conn.cursor()

        if agent:
            cursor.execute(
                "SELECT agent_id, param_name, param_value, updated_at, source, "
                "min_value, max_value, step_size, description "
                "FROM agent_params WHERE agent_id = ? "
                "ORDER BY param_name",
                (agent,)
            )
            rows = cursor.fetchall()
            conn.close()
            return jsonify({
                "agent": agent,
                "params": [dict(r) for r in rows],
                "count": len(rows),
            })
        else:
            # Return all params grouped by agent_id
            cursor.execute(
                "SELECT agent_id, param_name, param_value, updated_at, source, "
                "min_value, max_value, step_size, description "
                "FROM agent_params ORDER BY agent_id, param_name"
            )
            rows = cursor.fetchall()
            conn.close()

            grouped: Dict[str, list] = {}
            for r in rows:
                row = dict(r)
                aid = row.pop("agent_id")
                grouped.setdefault(aid, []).append(row)

            return jsonify({
                "params": grouped,
                "agents": list(grouped.keys()),
                "total_params": len(rows),
            })
    except Exception as e:
        log.warning("/params query failed: %s", e)
        return jsonify({"error": str(e)}), 500


# ── Portfolio Risk (NEW — LoneStarOracle) ────────────────────────────────────

@app.route("/risk")
def portfolio_risk():
    """Portfolio risk scoring via LoneStarOracle.

    Query: ?symbols=AAPL,MSFT,GOOGL
    Returns: concentration risk, volatility, correlation, VaR estimates.
    """
    symbols_str = request.args.get("symbols", "").strip().upper()
    symbol = request.args.get("symbol", "").strip().upper()

    if symbols_str:
        symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
    elif symbol:
        symbols = [symbol]
    else:
        return jsonify({"error": "?symbols=AAPL,MSFT required"})

    cache_key = "risk:" + ",".join(sorted(symbols))
    cached = _cache.get(cache_key, TTL["risk"])
    if cached is not None:
        return jsonify({"risk": cached, "symbols": symbols, "source": cached.get("source", "cache")})

    ls_data = _fetch_lonestar_risk(symbols)
    if ls_data:
        _cache.set(cache_key, ls_data)
        return jsonify({"risk": ls_data, "symbols": symbols, "source": "lonestar"})

    return jsonify({"error": "LoneStarOracle risk scoring unavailable", "symbols": symbols})


# ── Technical Scan (NEW — LoneStarOracle) ───────────────────────────────────

@app.route("/technical-scan")
def technical_scan():
    """Multi-timeframe technical scan via LoneStarOracle.

    Query: ?symbol=AAPL
    Returns: overall_signal (strong_buy/buy/neutral/sell/strong_sell),
    confluence across 15m/1h/4h/1d, RSI/MACD/BB per timeframe.
    """
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "?symbol=AAPL required"})

    cache_key = f"techscan:{symbol}"
    cached = _cache.get(cache_key, TTL["technical_scan"])
    if cached is not None:
        return jsonify({"technical_scan": cached, "symbol": symbol, "source": cached.get("source", "cache")})

    ls_data = _fetch_lonestar_technical_scan(symbol)
    if ls_data:
        _cache.set(cache_key, ls_data)
        return jsonify({"technical_scan": ls_data, "symbol": symbol, "source": "lonestar"})

    return jsonify({"error": "LoneStarOracle technical scan unavailable", "symbol": symbol})


# ── Equity Analysis (NEW — LoneStarOracle) ───────────────────────────────────

@app.route("/equity-analysis")
def equity_analysis():
    """Equity analysis via LoneStarOracle.

    Query: ?symbol=AAPL
    Returns: buy/hold/sell signal, upside to target, P/E, EPS, analyst ratings.
    """
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "?symbol=AAPL required"})

    cache_key = f"equity:{symbol}"
    cached = _cache.get(cache_key, TTL["fundamentals"])
    if cached is not None:
        return jsonify({"equity_analysis": cached, "symbol": symbol, "source": cached.get("source", "cache")})

    ls_data = _fetch_lonestar_equity_analysis(symbol)
    if ls_data:
        _cache.set(cache_key, ls_data)
        return jsonify({"equity_analysis": ls_data, "symbol": symbol, "source": "lonestar"})

    return jsonify({"error": "LoneStarOracle equity analysis unavailable", "symbol": symbol})


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    """Live dashboard — HTML page showing caches, signals, and scheduler health."""
    now = time.time()

    def _freshness(age_sec):
        if age_sec < 10:
            return "green"
        if age_sec < 60:
            return "gold"
        return "red"

    def _sched_status(s):
        if s.last_error:
            return ("red", s.last_error)
        if s.last_run == 0:
            return ("gold", "never run")
        since = now - s.last_run
        curb = s.current_interval * 2 if hasattr(s, 'current_interval') else 30
        if since < curb:
            return ("green", "ok")
        if since < curb * 2:
            return ("gold", "stale")
        return ("red", "down")

    def _time_ago(ts):
        if not ts:
            return "—"
        s = now - ts
        if s < 60:
            return f"{s:.0f}s ago"
        m = s / 60
        if m < 60:
            return f"{m:.0f}m ago"
        return f"{m / 60:.1f}h ago"

    def _status_dot(status):
        colors = {"green": "#3fb950", "yellow": "#d29922", "red": "#f85149", "error": "#f85149"}
        c = colors.get(status, "#8b949e")
        return f"<span style='color:{c};font-size:16px'>&#9679;</span>"

    # ── Market Banner ─────────────────────────────────────────────────────
    market_open = _is_market_open()
    if market_open:
        from market_hours import MARKET_CLOSE_TIME
        banner_bg = "#1a3a1a"
        banner_color = "#3fb950"
        banner_text = f"📈 MARKET OPEN — Closes {MARKET_CLOSE_TIME[0]}:{MARKET_CLOSE_TIME[1]:02d} PM ET"
    else:
        from market_hours import next_market_open
        banner_bg = "#3a1a1a"
        banner_color = "#f85149"
        next_open = next_market_open()
        day_name = next_open.strftime("%a")
        banner_text = f"📉 MARKET CLOSED — Opens {day_name} {next_open.strftime('%-I:%M %p')} ET"

    # ── Overnight Summary ─────────────────────────────────────────────────
    overnight = _get_overnight_summary()
    overnight_html = ""
    if overnight and not market_open:
        changes_str = ""
        for sym, pct in (overnight.get("changes") or [])[:8]:
            color = "#3fb950" if pct >= 0 else "#f85149"
            sign = "+" if pct >= 0 else ""
            changes_str += f"<span style='color:{color}'>{sym} {sign}{pct:.1f}%</span> &nbsp;"
        overnight_html = f"""
<div style="background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px;margin-bottom:12px">
<b>🌙 Overnight:</b> {changes_str or 'no data'}<br>
<b>📰 Overnight news:</b> {overnight.get('news_count', 0)} articles
</div>"""

    # ── Data Source Health ────────────────────────────────────────────────
    ds_health = _get_data_source_health()
    ds_rows = ""
    for ds in ds_health:
        st = ds["status"]
        last_str = f"{ds['last_fetch_sec']}s ago" if ds.get("last_fetch_sec") is not None else "—"
        stats = ds.get("stats", {})
        hit_str = f"{stats.get('hits', 0)} ✓ | {stats.get('misses', 0)} ✗ | {stats.get('errors', 0)} ⚠" if stats else "—"
        ds_rows += f"<tr><td>{ds['name']}</td><td>{_status_dot(st)}</td><td>{last_str}</td><td>{ds['rate_limit']}</td><td style='font-size:11px'>{hit_str}</td></tr>"

    # ── Quotes table ──────────────────────────────────────────────────────
    quote_rows = ""
    with _cache._lock:
        quote_entries = [(k, v) for k, v in _cache._store.items() if k.startswith("quote:")]
    if quote_entries:
        for key, entry in sorted(quote_entries):
            sym = key.replace("quote:", "")
            age = entry.age_seconds()
            color = _freshness(age)
            data = entry.data
            price = data.get("close") or data.get("price", "—")
            if isinstance(price, (int, float)):
                price = f"${price:.2f}"
            quote_rows += (
                f"<tr><td>{sym}</td>"
                f"<td><span style='color:{color};font-weight:bold'>{price}</span></td>"
                f"<td style='color:{color}'>{_time_ago(entry.fetched_at)}</td></tr>"
            )
    else:
        quote_rows = "<tr><td colspan='3' style='color:#666'>no quotes cached</td></tr>"

    # ── Crypto table ──────────────────────────────────────────────────────
    crypto_rows = ""
    with _cache._lock:
        crypto_entries = [(k, v) for k, v in _cache._store.items() if k.startswith("crypto:")]
    if crypto_entries:
        for key, entry in sorted(crypto_entries):
            sym = key.replace("crypto:", "")
            age = entry.age_seconds()
            color = _freshness(age)
            data = entry.data
            price = data.get("price", "—")
            if isinstance(price, (int, float)):
                price = f"${price:.2f}"
            crypto_rows += (
                f"<tr><td>{sym}</td>"
                f"<td><span style='color:{color};font-weight:bold'>{price}</span></td>"
                f"<td style='color:{color}'>{_time_ago(entry.fetched_at)}</td></tr>"
            )
    else:
        crypto_rows = "<tr><td colspan='3' style='color:#666'>no crypto data cached</td></tr>"

    # ── News ──────────────────────────────────────────────────────────────
    news_data = _cache.get("news:all:20")
    news_count = len(news_data) if news_data else 0
    news_headlines = ""
    if news_data:
        for n in news_data[:6]:
            hl = n.get("headline", "")[:80]
            syms = ",".join(n.get("symbols", [])[:3])
            news_headlines += f"<tr><td style='font-size:12px'>{hl}</td><td style='font-size:11px;color:#888'>{syms}</td></tr>"
    else:
        news_headlines = "<tr><td colspan='2' style='color:#666'>no news cached</td></tr>"

    # ── Congress ───────────────────────────────────────────────────────────
    congress_data = _cache.get("congress:trades")
    congress_count = len(congress_data) if congress_data else 0
    congress_age = "—"
    if congress_data is not None:
        with _cache._lock:
            e = _cache._store.get("congress:trades")
        if e:
            congress_age = _time_ago(e.fetched_at)

    # ── Trader Pulse ──────────────────────────────────────────────────────
    pulse = _get_trader_pulse()
    pulse_rows = ""
    if pulse and "error" not in pulse:
        for agent, info in sorted(pulse.items()):
            name = agent.replace("trader-", "").title()
            age = info.get("age_seconds")
            if age is not None:
                age_str = f"{_time_ago(time.time() - age)}"
            else:
                age_str = "never"
            color = "#3fb950" if age is not None and age < 600 else "#d29922"
            pulse_rows += f"<tr><td>{name}</td><td style='color:{color}'>{age_str}</td></tr>"
    if not pulse_rows:
        pulse_rows = "<tr><td colspan='2' style='color:#666'>no trader data</td></tr>"

    # ── Signals ───────────────────────────────────────────────────────────
    with _signals_lock:
        sigs = list(_signals_cache)
    sig_rows = ""
    if sigs:
        agents = {}
        for s in sigs:
            agents[s["agent"]] = s
        for agent, s in sorted(agents.items()):
            bias_color = {"bullish": "#0f0", "bearish": "#f44", "neutral": "#888"}.get(s.get("bias"), "#888")
            ts_raw = s.get("timestamp", "")
            if ts_raw:
                try:
                    ts_short = ts_raw[:19].replace("T", " ")
                except Exception:
                    ts_short = ts_raw
            else:
                ts_short = "—"
            sig_rows += (
                f"<tr><td>{agent}</td>"
                f"<td>{s.get('ticker','')}</td>"
                f"<td style='color:{bias_color}'>{s.get('bias','')}</td>"
                f"<td>{s.get('conviction',0):.0%}</td>"
                f"<td style='font-size:11px;color:#888'>{ts_short}</td></tr>"
            )
    else:
        sig_rows = "<tr><td colspan='5' style='color:#666'>no signals posted</td></tr>"

    # ── Cache Stats ───────────────────────────────────────────────────────
    cs = _cache.stats()
    cache_mem_est = cs["keys"] * 2048  # rough estimate: ~2KB per entry
    cache_stats_html = f"{cs['keys']} entries · ~{cache_mem_est // 1024}KB"

    # ── Schedulers ────────────────────────────────────────────────────────
    sched_rows = ""
    for s in _schedulers:
        sc, detail = _sched_status(s)
        last = datetime.fromtimestamp(s.last_run).strftime("%H:%M:%S") if s.last_run else "—"
        mode_label = s.current_mode.upper() if hasattr(s, 'current_mode') else "?"
        mode_color = "#58a6ff" if mode_label == "MARKET" else "#8b949e"
        sched_rows += (
            f"<tr><td>{s.name}</td>"
            f"<td>{s.current_interval:.0f}s <span style='color:{mode_color};font-size:10px'>({mode_label})</span></td>"
            f"<td>{last}</td>"
            f"<td>{s.run_count}</td>"
            f"<td><span style='color:{sc};font-weight:bold'>{detail}</span></td></tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="5">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Data Bus Dashboard</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:monospace;background:#0d1117;color:#c9d1d9;padding:16px}}
h1{{color:#58a6ff;font-size:18px;margin-bottom:12px}}
h2{{color:#8b949e;font-size:14px;border-bottom:1px solid #21262d;padding-bottom:4px;margin:14px 0 8px}}
table{{width:100%;border-collapse:collapse;margin-bottom:10px}}
th,td{{text-align:left;padding:3px 8px;font-size:13px}}
th{{color:#8b949e;font-weight:bold;border-bottom:1px solid #30363d}}
td{{border-bottom:1px solid #161b22}}
.banner{{padding:10px 16px;border-radius:6px;font-weight:bold;font-size:14px;margin-bottom:12px}}
.footer{{color:#484f58;font-size:11px;margin-top:16px}}
</style>
</head>
<body>
<h1>📡 Data Bus Dashboard</h1>

<div class="banner" style="background:{banner_bg};color:{banner_color}">{banner_text}</div>

{overnight_html}

<h2>🩺 Data Source Health</h2>
<table><tr><th>Source</th><th>Status</th><th>Last Fetch</th><th>Rate</th><th>Success Rate</th></tr>{ds_rows}</table>

<h2>💓 Trader Pulse</h2>
<table><tr><th>Trader</th><th>Last Tick</th></tr>{pulse_rows}</table>

<h2>📈 Quotes</h2>
<table><tr><th>Symbol</th><th>Price</th><th>Age</th></tr>{quote_rows}</table>

<h2>₿ Crypto</h2>
<table><tr><th>Symbol</th><th>Price</th><th>Age</th></tr>{crypto_rows}</table>

<h2>📰 News ({news_count} articles)</h2>
<table><tr><th>Headline</th><th>Symbols</th></tr>{news_headlines}</table>

<h2>🏛 Congress Trades</h2>
<p style="font-size:13px;margin-bottom:6px">Trades: <b>{congress_count}</b> &nbsp;|&nbsp; Last fetch: {congress_age}</p>

<h2>📶 Trader Signals ({len(sigs)} active)</h2>
<table><tr><th>Agent</th><th>Ticker</th><th>Bias</th><th>Conviction</th><th>Timestamp</th></tr>{sig_rows}</table>

<h2>💾 Cache ({cache_stats_html})</h2>

<h2>⚙️ Scheduler Health</h2>
<table><tr><th>Name</th><th>Interval</th><th>Last Run</th><th>Runs</th><th>Status</th></tr>{sched_rows}</table>

<div class="footer">Data Bus v2 · {datetime.now().strftime('%H:%M:%S')} · auto-refresh 5s</div>
</body>
</html>"""

    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ── Debug (SENSITIVE — LAN only) ───────────────────────────────────────────

@app.route("/debug")
def debug():
    """
    ⚠️  /debug — SENSITIVE: contains API key status, rate limits, error traces
                 Should be restricted to LAN-only via Traefik middleware
    """
    now = time.time()

    def _age_str(timestamp_str):
        if not timestamp_str:
            return "—"
        try:
            dt = datetime.fromisoformat(timestamp_str)
            s = now - dt.timestamp()
            if s < 60:
                return f"{s:.0f}s ago"
            m = s / 60
            if m < 60:
                return f"{m:.0f}m ago"
            return f"{m / 60:.1f}h ago"
        except Exception:
            return timestamp_str

    # ── Rate Limits ──────────────────────────────────────────────────────
    rate_html = """<table><tr><th>API</th><th>Key</th><th>Limit</th><th>Remaining</th><th>Window</th></tr>
    <tr><td>Alpaca (Kairos)</td><td>{}</td><td>200/min</td><td style='color:#58a6ff'>?</td><td>1 min</td></tr>
    <tr><td>Alpaca (Aldridge)</td><td>{}</td><td>200/min</td><td style='color:#58a6ff'>?</td><td>1 min</td></tr>
    <tr><td>Alpaca (Stonks)</td><td>{}</td><td>200/min</td><td style='color:#58a6ff'>?</td><td>1 min</td></tr>
    <tr><td>Finnhub</td><td>{}</td><td>60/min</td><td style='color:#d29922'>?</td><td>1 min</td></tr>
    <tr><td>Alpha Vantage</td><td>{}</td><td>25/day</td><td style='color:#d29922'>?</td><td>24h</td></tr>
    </table>""".format(
        _mask_key(os.getenv("ALPACA_KAIROS_KEY", "")),
        _mask_key(os.getenv("ALPACA_ALDRIDGE_KEY", "")),
        _mask_key(os.getenv("ALPACA_STONKS_KEY", "")),
        _mask_key(os.getenv("FINNHUB_API_KEY", "")),
        _mask_key(os.getenv("ALPHA_VANTAGE_API_KEY", "")),
    )

    # ── Data Sources Config ──────────────────────────────────────────────
    ds_config_html = """<table><tr><th>Source</th><th>Endpoint</th><th>Rate Limit</th><th>TTL</th></tr>
    <tr><td>Alpaca Quotes</td><td>data.alpaca.markets/v2/stocks/bars</td><td>200/min</td><td>5s</td></tr>
    <tr><td>Alpaca Crypto</td><td>data.alpaca.markets/v1beta3/crypto</td><td>200/min</td><td>10s</td></tr>
    <tr><td>Alpaca News</td><td>data.alpaca.markets/v1beta1/news</td><td>200/min</td><td>180s</td></tr>
    <tr><td>FinBERT</td><td>{}:{}</td><td>—</td><td>300s</td></tr>
    <tr><td>Finnhub</td><td>finnhub.io/api/v1</td><td>60/min</td><td>30m (congress)</td></tr>
    <tr><td>Alpha Vantage</td><td>alphavantage.co</td><td>25/day</td><td>6h</td></tr>
    <tr><td>Cache DB</td><td>shared/cache.db (SQLite)</td><td>—</td><td>per-source</td></tr>
    </table>""".format(FINBERT_HOST, FINBERT_PORT)

    # ── News Sources ─────────────────────────────────────────────────────
    ns_config = _get_news_sources_config()
    ns_html = "<table><tr><th>Type</th><th>Source</th></tr>"
    for item in ns_config:
        if item.get("type") == "RSS":
            ns_html += f"<tr><td>RSS</td><td style='font-size:11px'>{item['url']}</td></tr>"
        else:
            ns_html += f"<tr><td>{item.get('type','API')}</td><td>{item.get('name', item.get('endpoint',''))}</td></tr>"
    ns_html += "</table>"

    # ── Raw Scheduler Log ────────────────────────────────────────────────
    with _scheduler_events_lock:
        events = list(_scheduler_events)
    sch_log_html = "<table><tr><th>Time</th><th>Scheduler</th><th>Type</th><th>Detail</th></tr>"
    for ev in reversed(events):
        tc = "#f85149" if ev["type"] == "error" else "#3fb950"
        sch_log_html += f"<tr><td style='font-size:11px'>{ev['time']}</td><td>{ev['scheduler']}</td><td style='color:{tc}'>{ev['type']}</td><td style='font-size:11px;max-width:300px;overflow:hidden'>{ev['detail'][:80]}</td></tr>"
    sch_log_html += "</table>"

    # ── DB Stats ─────────────────────────────────────────────────────────
    db_st = _get_db_stats()
    db_html = ""
    if "error" not in db_st:
        db_html = f"<p>📊 <b>{db_st['table_count']} tables</b> · {db_st['total_rows']} rows · {db_st['db_size_mb']} MB</p>"
        db_html += "<table><tr><th>Table</th><th>Rows</th></tr>"
        for tname, count in sorted(db_st["tables"].items()):
            db_html += f"<tr><td>{tname}</td><td>{count}</td></tr>"
        db_html += "</table>"
    else:
        db_html = f"<p style='color:#f85149'>Error: {db_st['error']}</p>"

    # ── Error Log ────────────────────────────────────────────────────────
    with _error_log_lock:
        e_log = list(_error_log)
    err_html = "<table><tr><th>Time</th><th>Source</th><th>Error</th></tr>"
    if e_log:
        for ev in reversed(e_log):
            err_html += f"<tr><td style='font-size:11px'>{ev['time']}</td><td>{ev['source']}</td><td style='color:#f85149;font-size:11px;max-width:400px;word-break:break-all'>{ev['error'][:120]}</td></tr>"
    else:
        err_html += "<tr><td colspan='3' style='color:#3fb950'>no errors recorded ✓</td></tr>"
    err_html += "</table>"

    # ── API Key Status ───────────────────────────────────────────────────
    key_status = _get_env_keys_status()
    key_html = "<table><tr><th>Env Var</th><th>Status</th><th>Value</th></tr>"
    for var, info in sorted(key_status.items()):
        st_color = "#3fb950" if info["configured"] else "#f85149"
        st_text = "✓ configured" if info["configured"] else "✗ missing"
        key_html += f"<tr><td style='font-size:11px'>{var}</td><td style='color:{st_color}'>{st_text}</td><td style='font-size:11px'>{info['masked'] or '—'}</td></tr>"
    key_html += "</table>"

    # ── Trader Config ────────────────────────────────────────────────────
    tconfig = _get_trader_configs()
    tc_html = ""
    if tconfig and "error" not in tconfig[0]:
        for t in tconfig:
            wl = ", ".join(t.get("watchlist", [])[:10])
            tc_html += f"<details style='margin-bottom:8px'><summary style='cursor:pointer;color:#58a6ff;font-size:14px'><b>{t['name']}</b> ({t['agent_id']}) — {t['trader_name']}</summary>"
            tc_html += f"<div style='padding-left:16px;margin-top:4px;font-size:12px'>"
            tc_html += f"<b>Portfolio:</b> ${t.get('portfolio_value', 0):,.2f} &nbsp;|&nbsp; <b>Focus:</b> {t.get('focus', '?')}<br>"
            tc_html += f"<b>Watchlist ({len(t.get('watchlist', []))}):</b> {wl}<br>"
            tc_html += f"<b>Polling:</b> {t.get('polling_freq_sec', '?')}s &nbsp;|&nbsp; <b>Risk Limit:</b> {t.get('risk_limit_pct', '?')}%"
            tc_html += f"&nbsp;|&nbsp; <b>Max Position:</b> {t.get('max_position_size_pct', '?')}%"
            tc_html += f"&nbsp;|&nbsp; <b>Daily Loss Limit:</b> {t.get('daily_loss_limit', '?')}%<br>"
            tc_html += f"<b>Record:</b> {t.get('wins', 0)}W / {t.get('losses', 0)}L / {t.get('total_trades', 0)} trades"
            if t.get('win_rate') is not None:
                tc_html += f" ({t['win_rate']:.1%})"
            tc_html += "</div></details>"
    else:
        tc_html = "<p style='color:#666'>no trader configs found</p>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Data Bus — Debug</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:monospace;background:#0d1117;color:#c9d1d9;padding:16px}}
h1{{color:#f85149;font-size:18px;margin-bottom:4px}}
h2{{color:#8b949e;font-size:14px;border-bottom:1px solid #21262d;padding-bottom:4px;margin:16px 0 8px;cursor:pointer}}
table{{width:100%;border-collapse:collapse;margin-bottom:10px}}
th,td{{text-align:left;padding:3px 8px;font-size:12px}}
th{{color:#8b949e;font-weight:bold;border-bottom:1px solid #30363d}}
td{{border-bottom:1px solid #161b22}}
.warning{{background:#3a1a1a;border:2px solid #f85149;color:#f85149;padding:8px 12px;border-radius:6px;font-size:12px;margin-bottom:12px}}
.footer{{color:#484f58;font-size:11px;margin-top:16px}}
.collapsible{{border:1px solid #30363d;border-radius:6px;padding:10px;margin-bottom:8px}}
.summary{{cursor:pointer;font-weight:bold;color:#58a6ff}}
</style>
<script>
function toggle(id) {{
  var el = document.getElementById(id);
  if (el.style.display === 'none') el.style.display = 'block';
  else el.style.display = 'none';
}}
</script>
</head>
<body>
<h1>⚠️ Data Bus — Debug Console</h1>
<div class="warning">SENSITIVE: contains API key status, rate limits, error traces. Restrict to LAN-only via Traefik middleware.</div>

<details class="collapsible" open>
<summary class="summary">⏱ Rate Limits</summary>
{rate_html}
</details>

<details class="collapsible">
<summary class="summary">🔌 Data Sources</summary>
{ds_config_html}
</details>

<details class="collapsible">
<summary class="summary">📡 News Sources</summary>
{ns_html}
</details>

<details class="collapsible">
<summary class="summary">📋 Raw Scheduler Log (last 100)</summary>
{sch_log_html}
</details>

<details class="collapsible">
<summary class="summary">🗄 DB Stats</summary>
{db_html}
</details>

<details class="collapsible">
<summary class="summary">❌ Error Log (last 20)</summary>
{err_html}
</details>

<details class="collapsible">
<summary class="summary">🔑 API Key Status</summary>
{key_html}
</details>

<details class="collapsible">
<summary class="summary">🤖 Trader Configs</summary>
{tc_html}
</details>

<div class="footer">Data Bus Debug · {datetime.now().strftime('%H:%M:%S')} · auto-refresh 15s · ⚠️ SENSITIVE</div>
</body>
</html>"""

    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ── MCP Status ──────────────────────────────────────────────────────────────

@app.route("/mcp-status")
def mcp_status():
    """Health endpoint for all MCP connections.

    Returns JSON with per-server status: transport, connected, error count,
    tool cache info, and last health check timestamp.
    Always returns 200 — the JSON itself tells you which servers are healthy.
    """
    if not _mcp_available:
        return jsonify({
            "error": "MCP client not available (install mcp package)",
            "servers": {},
            "total": 0,
            "connected_count": 0,
        })

    try:
        manager = get_manager()
        if manager is None:
            return jsonify({"error": "MCP manager not initialized"})
        return jsonify(manager.status())
    except Exception as e:
        log.warning("/mcp-status error: %s", e)
        return jsonify({"error": str(e)})


# ── Congress Trades ───────────────────────────────────────────────────────────

@app.route("/congress")
def congress():
    cached = _cache.get("congress:trades", TTL["congress"])
    if cached is not None:
        return jsonify({"congress_trades": cached, "source": "cache"})

    tickers = list(_tracked_symbols)
    data = _fetch_congress_trades(tickers)
    if not data:
        data = _sqlite_get_congress_trades()
    if data:
        _cache.set("congress:trades", data)
    return jsonify({"congress_trades": data, "source": "live" if data else "none"})


# ── Pre-Market Briefing ──────────────────────────────────────────────────────


@app.route("/briefing")
def briefing():
    """
    Compile overnight/morning data for pre-market briefing.

    Returns overnight movers, crypto changes, top news, congress alerts,
    and trader readiness — everything a trader needs to set up for the day.
    """
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, timedelta
    except ImportError:
        pass

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    gen_time = now_et.isoformat()

    response = {
        "generated_at": gen_time,
    }
    warnings = []

    # ── Market status ────────────────────────────────────────────────────
    try:
        market_open = _is_market_open()
        if market_open:
            response["market_status"] = "market in session"
            response["market_opens_in"] = "already open"
        else:
            response["market_status"] = "pre-market"
            # Calculate time until market opens
            try:
                from market_hours import next_market_open
                next_open_et = next_market_open(now_et)
                delta = next_open_et - now_et
                total_sec = int(delta.total_seconds())
                if total_sec <= 0:
                    # Already past open time but market closed (holiday?)
                    response["market_status"] = "market closed"
                    response["market_opens_in"] = "unknown (holiday or weekend)"
                elif total_sec < 120:
                    response["market_opens_in"] = f"{total_sec} sec"
                elif total_sec < 3600:
                    response["market_opens_in"] = f"{total_sec // 60} min"
                else:
                    h = total_sec // 3600
                    m = (total_sec % 3600) // 60
                    response["market_opens_in"] = f"{h}h {m}m"
            except Exception:
                response["market_opens_in"] = "unknown"
    except Exception as e:
        warnings.append(f"market_status: {e}")
        response["market_status"] = "unknown"
        response["market_opens_in"] = "unknown"

    # ── Overnight movers ─────────────────────────────────────────────────
    overnight_movers = []
    try:
        import sqlite3 as _sqlite3

        # Get yesterday's close from trader.db prices table
        today_et = now_et.date()
        yesterday_et = today_et - timedelta(days=1)
        yesterday_str = yesterday_et.isoformat()

        trader_db_path = str(SHARED_DIR / "trader.db")
        conn = _sqlite3.connect(f"file:{trader_db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = _sqlite3.Row
        cursor = conn.cursor()

        # Get latest close per ticker for yesterday (or last available day)
        cursor.execute("""
            SELECT p1.ticker, p1.close, p1.volume, p1.fetched_at
            FROM prices p1
            INNER JOIN (
                SELECT ticker, MAX(fetched_at) AS max_ts
                FROM prices
                WHERE fetched_at < ?
                GROUP BY ticker
            ) p2 ON p1.ticker = p2.ticker AND p1.fetched_at = p2.max_ts
            WHERE p1.close IS NOT NULL AND p1.close > 0
        """, (today_et.isoformat(),))

        prev_prices = {row["ticker"]: {"close": row["close"], "volume": row["volume"], "fetched_at": row["fetched_at"]} for row in cursor.fetchall()}
        conn.close()

        # Get current quotes from in-memory cache
        current_quotes = {}
        with _cache._lock:
            for key, entry in _cache._store.items():
                if key.startswith("quote:"):
                    sym = key.replace("quote:", "")
                    data = entry.data
                    # Try to extract price (close or price field)
                    price = data.get("close") or data.get("price") or data.get("current")
                    if price is not None:
                        try:
                            current_quotes[sym] = float(price)
                        except (ValueError, TypeError):
                            pass

        # Build movers list — compare current vs previous close
        movers = []
        for sym in sorted(set(prev_prices.keys()) & set(current_quotes.keys())):
            prev = prev_prices[sym]
            curr = current_quotes[sym]
            if prev["close"] and prev["close"] > 0:
                change_pct = ((curr - prev["close"]) / prev["close"]) * 100
                movers.append({
                    "symbol": sym,
                    "close": round(prev["close"], 2),
                    "current": round(curr, 2),
                    "change_pct": round(change_pct, 2),
                    "volume": prev.get("volume", 0) or 0,
                })

        # Sort by absolute change, top 5
        movers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
        overnight_movers = movers[:5]
    except Exception as e:
        log.warning("Briefing: overnight movers failed: %s", e)
        warnings.append(f"overnight_movers: {e}")

    if not overnight_movers:
        response["overnight_movers"] = []
        response["overnight_movers_note"] = "no overnight data yet — data bus may be freshly installed"
    else:
        response["overnight_movers"] = overnight_movers

    # ── Crypto ────────────────────────────────────────────────────────────
    crypto_data = {}
    try:
        for sym in _tracked_crypto:
            cached = _cache.get(f"crypto:{sym}")
            if cached and "price" in cached:
                crypto_data[sym] = {
                    "price": cached.get("price"),
                    "change_pct_24h": cached.get("change_pct_24h", None),
                }
        if not crypto_data:
            # Try larger set of common cryptos
            for sym in ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD"]:
                if sym not in crypto_data:
                    cached = _cache.get(f"crypto:{sym}")
                    if cached and "price" in cached:
                        crypto_data[sym] = {
                            "price": cached.get("price"),
                            "change_pct_24h": cached.get("change_pct_24h", None),
                        }
    except Exception as e:
        log.warning("Briefing: crypto data failed: %s", e)
        warnings.append(f"crypto: {e}")
    response["crypto"] = crypto_data

    # ── Top news (last 12h) ────────────────────────────────────────────────
    top_news = []
    try:
        cache_db_path = str(SHARED_DIR / "cache.db")
        conn2 = _sqlite3.connect(f"file:{cache_db_path}?mode=ro", uri=True)
        conn2.execute("PRAGMA busy_timeout=5000")
        conn2.row_factory = _sqlite3.Row
        cur = conn2.cursor()

        cutoff = (now_et - timedelta(hours=12)).isoformat()
        cur.execute("""
            SELECT headline, source, relevance, fetched_at
            FROM news
            WHERE fetched_at >= ?
            ORDER BY fetched_at DESC
            LIMIT 20
        """, (cutoff,))

        for row in cur.fetchall():
            headline = row["headline"]
            source = row["source"] or "unknown"
            relevance_str = row["relevance"] or "[]"
            try:
                tickers = json.loads(relevance_str) if isinstance(relevance_str, str) else relevance_str
                # If it's a single comma-separated string like "COIN,MSTR,TSLA"
                if isinstance(tickers, str):
                    tickers = [t.strip() for t in tickers.split(",") if t.strip()]
                elif not isinstance(tickers, list):
                    tickers = []
            except Exception:
                tickers = []
            top_news.append({
                "headline": headline,
                "source": source,
                "tickers": tickers,
                "timestamp": row["fetched_at"],
            })
        conn2.close()
    except Exception as e:
        log.warning("Briefing: news fetch failed: %s", e)
        warnings.append(f"top_news: {e}")
    response["top_news"] = top_news

    # ── Congress alerts (last 24h) ────────────────────────────────────────
    congress_alerts = []
    try:
        cached_congress = _cache.get("congress:trades")
        if cached_congress:
            cutoff_24h = now_et - timedelta(hours=24)
            for entry in cached_congress if isinstance(cached_congress, list) else cached_congress.get("trades", []):
                # Filter by timestamp if available
                if isinstance(entry, dict):
                    ts = entry.get("filed") or entry.get("timestamp") or entry.get("filed_date", "")
                    if ts:
                        try:
                            # Try parsing ISO format
                            entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if entry_dt.tzinfo:
                                entry_dt = entry_dt.astimezone(et)
                            else:
                                entry_dt = entry_dt.replace(tzinfo=et)
                            if entry_dt < cutoff_24h:
                                continue
                        except Exception:
                            pass  # can't parse, include anyway
                    congress_alerts.append({
                        "politician": entry.get("politician", entry.get("name", "unknown")),
                        "ticker": entry.get("ticker", entry.get("symbol", "")),
                        "type": entry.get("type", entry.get("transaction_type", "")),
                        "amount": entry.get("amount", entry.get("amount_range", "")),
                        "filed": ts if ts else "unknown",
                    })
    except Exception as e:
        log.warning("Briefing: congress alerts failed: %s", e)
        warnings.append(f"congress_alerts: {e}")
    response["congress_alerts"] = congress_alerts

    # ── Trader readiness ──────────────────────────────────────────────────
    trader_readiness = {}
    trader_agent_map = {
        "kairos": "trader-kairos",
        "stonks": "trader-stonks",
        "aldridge": "trader-aldridge",
    }
    try:
        cache_db_path = str(SHARED_DIR / "cache.db")
        conn3 = _sqlite3.connect(f"file:{cache_db_path}?mode=ro", uri=True)
        conn3.execute("PRAGMA busy_timeout=5000")
        conn3.row_factory = _sqlite3.Row
        cur3 = conn3.cursor()
        cur3.execute("SELECT agent_id, updated_at FROM agent_state")
        agent_states = {row["agent_id"]: row["updated_at"] for row in cur3.fetchall()}
        conn3.close()

        # Also get latest journal entry for each trader from trader.db
        trader_db_path = str(SHARED_DIR / "trader.db")
        conn4 = _sqlite3.connect(f"file:{trader_db_path}?mode=ro", uri=True)
        conn4.execute("PRAGMA busy_timeout=5000")
        conn4.row_factory = _sqlite3.Row
        cur4 = conn4.cursor()

        for name, agent_id in trader_agent_map.items():
            last_tick = agent_states.get(agent_id)

            # Get latest journal entry for this trader
            cur4.execute(
                "SELECT timestamp, mood FROM trader_journal WHERE trader_id=? ORDER BY timestamp DESC LIMIT 1",
                (name,)
            )
            journal_row = cur4.fetchone()

            trader_readiness[name] = {
                "last_tick": last_tick or "never",
                "mode": "ready" if last_tick else "unknown",
            }
            if journal_row:
                trader_readiness[name]["last_journal"] = journal_row["timestamp"]
                trader_readiness[name]["mood"] = journal_row["mood"] or "neutral"
        conn4.close()
    except Exception as e:
        log.warning("Briefing: trader readiness failed: %s", e)
        warnings.append(f"trader_readiness: {e}")
        # Fallback values
        for name in trader_agent_map:
            if name not in trader_readiness:
                trader_readiness[name] = {"last_tick": "unknown", "mode": "unknown"}
    response["trader_readiness"] = trader_readiness

    # ── Warnings ──────────────────────────────────────────────────────────
    if warnings:
        response["warnings"] = warnings

    return jsonify(response)


# ═══════════════════════════════════════════════════════════════════════════════
# MCP Server (exposed alongside HTTP)
# ═══════════════════════════════════════════════════════════════════════════════

_mcp_port = int(os.environ.get("MCP_PORT", 5001))

if _mcp_server_available:
    mcp_server = FastMCP(
        "Paper Trading Data Bus",
        host="0.0.0.0",
        port=_mcp_port,
    )
else:
    mcp_server = None


def _mcp_tools_enabled() -> bool:
    return _mcp_server_available and mcp_server is not None


# ── Helper: parse symbols string to list ────────────────────────────────────

def _parse_symbols(symbols_input) -> List[str]:
    """Parse symbols from a list or comma-separated string."""
    if isinstance(symbols_input, str):
        return [s.strip().upper() for s in symbols_input.split(",") if s.strip()]
    if isinstance(symbols_input, list):
        return [str(s).strip().upper() for s in symbols_input if str(s).strip()]
    return []


# ── MCP Tools ────────────────────────────────────────────────────────────────

if _mcp_tools_enabled():

    @mcp_server.tool()
    async def get_quotes(symbols: list[str]) -> dict:
        """Get current quotes with OHLCV + RSI for a list of ticker symbols."""
        parsed = _parse_symbols(symbols)
        if not parsed:
            return {"error": "symbols required (list[str])"}
        cache_keys = [f"quote:{s}" for s in parsed]
        cached = _cache.get_multi(cache_keys, TTL["quotes"])
        result = {}
        missing = []
        for sym in parsed:
            key = f"quote:{sym}"
            if key in cached:
                result[sym] = cached[key]
            else:
                missing.append(sym)
        if missing:
            fresh = _fetch_alpaca_quotes(missing)
            for sym, data in fresh.items():
                result[sym] = data
                _cache.set(f"quote:{sym}", data)
        return {
            "quotes": result,
            "count": len(result),
            "cached": len(result) - len(missing),
            "fetched_live": len(missing),
        }

    @mcp_server.tool()
    async def get_sentiment(symbol: str) -> dict:
        """Get FinBERT + Praesentire bilingual sentiment for a ticker."""
        sym = symbol.strip().upper()
        if not sym:
            return {"error": "symbol required"}
        # Check cache
        cache_key = f"sentiment:{sym}"
        cached = _cache.get(cache_key, TTL["sentiment"])
        # Check Praesentire cache
        prae_key = f"praesentire_sentiment:{sym}"
        prae_cached = _cache.get(prae_key, TTL["praesentire_sentiment"])
        if cached is not None:
            result = {"symbol": sym, "sentiment": cached, "source": "cache"}
            if prae_cached is not None:
                result["praesentire"] = prae_cached
            return result
        # Live fetch
        result = _fetch_sentiment_via_finbert(sym, sym)
        if result:
            _cache.set(cache_key, result)
            output = {"symbol": sym, "sentiment": result, "source": "live"}
            # Try live Praesentire
            prae_data = _fetch_praesentire_sentiment(sym)
            if prae_data and "error" not in prae_data:
                _cache.set(prae_key, prae_data)
                output["praesentire"] = prae_data
            return output
        return {"symbol": sym, "error": "sentiment unavailable"}

    @mcp_server.tool()
    async def get_flow(symbol: str) -> dict:
        """Get unusual options flow (sweeps, dark pool, blocks) for a ticker."""
        sym = symbol.strip().upper()
        if not sym:
            return {"error": "symbol required"}
        # Check LoneStarOracle cache
        cache_key = "flow:latest"
        cached = _cache.get(cache_key, TTL["flow"])
        if cached is not None:
            # Filter by ticker
            flows = cached.get("flows", [])
            filtered = [f for f in flows if sym in f.get("tickers", [])]
            return {
                "symbol": sym,
                "flows": filtered,
                "total_flows": len(filtered),
                "source": cached.get("source", "cache"),
            }
        # Live fetch
        ls_data = _fetch_lonestar_options_flow(sym)
        if ls_data and "error" not in ls_data:
            return {"symbol": sym, "flow": ls_data, "source": "lonestar"}
        # Fall back to RSS
        data = _fetch_options_flow()
        if data and "error" not in data:
            flows = [f for f in data.get("flows", []) if sym in f.get("tickers", [])]
            return {"symbol": sym, "flows": flows, "total_flows": len(flows), "source": "rss"}
        return {"symbol": sym, "error": "flow data unavailable"}

    @mcp_server.tool()
    async def get_insiders(symbol: str) -> dict:
        """Get insider trading filings (SEC Form 4) for a ticker."""
        sym = symbol.strip().upper()
        if not sym:
            return {"error": "symbol required"}
        # Check LoneStarOracle cache
        ls_cache = _cache.get("insiders:latest", TTL["insiders"])
        if ls_cache is not None:
            filings = ls_cache.get("filings", [])
            filtered = [f for f in filings if f.get("ticker") == sym]
            return {
                "symbol": sym,
                "filings": filtered,
                "total": len(filtered),
                "source": ls_cache.get("source", "cache"),
            }
        # Live fetch from LoneStarOracle
        ls_data = _fetch_lonestar_insider_trades(sym)
        if ls_data and "error" not in ls_data:
            return {"symbol": sym, "insiders": ls_data, "source": "lonestar"}
        # Fall back to SEC EDGAR
        data = _fetch_insider_filings()
        if data and "error" not in data:
            filings = [f for f in data.get("filings", []) if f.get("ticker") == sym]
            return {"symbol": sym, "filings": filings, "total": len(filings), "source": "edgar"}
        return {"symbol": sym, "error": "insider data unavailable"}

    @mcp_server.tool()
    async def get_macro() -> dict:
        """Get macro indicators: FRED data, yield curve, FOMC rates."""
        cache_key = "macro:latest"
        cached = _cache.get(cache_key, TTL["macro"])
        if cached is not None:
            return {"macro": cached, "source": cached.get("source", "cache")}
        # Try LoneStarOracle first
        ls_data = _fetch_lonestar_macro()
        if ls_data and "error" not in ls_data:
            _cache.set(cache_key, ls_data)
            return {"macro": ls_data, "source": "lonestar"}
        # Fall back to FRED
        data = _fetch_fred_macro()
        if data and "error" not in data:
            _cache.set(cache_key, data)
            return {"macro": data, "source": "fred"}
        return {"error": "macro data unavailable"}

    @mcp_server.tool()
    async def get_technical_scan(symbol: str) -> dict:
        """Get multi-timeframe technical scan (15m/1h/4h/1d) with RSI/MACD/BB."""
        sym = symbol.strip().upper()
        if not sym:
            return {"error": "symbol required"}
        cache_key = f"techscan:{sym}"
        cached = _cache.get(cache_key, TTL["technical_scan"])
        if cached is not None:
            return {"symbol": sym, "technical_scan": cached, "source": cached.get("source", "cache")}
        ls_data = _fetch_lonestar_technical_scan(sym)
        if ls_data and "error" not in ls_data:
            _cache.set(cache_key, ls_data)
            return {"symbol": sym, "technical_scan": ls_data, "source": "lonestar"}
        return {"symbol": sym, "error": "technical scan unavailable"}

    @mcp_server.tool()
    async def get_risk(symbol: str) -> dict:
        """Get portfolio risk scoring (concentration, VaR, correlation) for a ticker."""
        sym = symbol.strip().upper()
        if not sym:
            return {"error": "symbol required"}
        cache_key = "risk:" + sym
        cached = _cache.get(cache_key, TTL["risk"])
        if cached is not None:
            return {"symbol": sym, "risk": cached, "source": cached.get("source", "cache")}
        ls_data = _fetch_lonestar_risk([sym])
        if ls_data and "error" not in ls_data:
            _cache.set(cache_key, ls_data)
            return {"symbol": sym, "risk": ls_data, "source": "lonestar"}
        return {"symbol": sym, "error": "risk data unavailable"}

    @mcp_server.tool()
    async def get_sentiment_divergence(symbol: str) -> dict:
        """Get cross-language sentiment divergence (EN vs ZH) from Praesentire."""
        sym = symbol.strip().upper()
        if not sym:
            return {"error": "symbol required"}
        cache_key = f"sentiment_divergence:{sym}"
        cached = _cache.get(cache_key, TTL["sentiment_divergence"])
        if cached is not None:
            return {"symbol": sym, "divergence": cached, "source": "cache"}
        data = _fetch_praesentire_divergence(sym)
        if data and "error" not in data:
            _cache.set(cache_key, data)
            return {"symbol": sym, "divergence": data, "source": "praesentire"}
        return {"symbol": sym, "error": "divergence data unavailable"}

    @mcp_server.tool()
    async def get_market_regime() -> dict:
        """Get current market regime from ML signal (bullish/bearish/choppy/sustainable/exhausted)."""
        cache_key = "ml_signal:SPY"
        cached = _cache.get(cache_key, TTL["technical_scan"])
        if cached is not None:
            return {"market_regime": cached, "source": "cache"}
        if fetch_ml_signal is None:
            return {"market_regime": None, "error": "ML signal module unavailable (skill_combo_fetch not loaded)"}
        try:
            result = fetch_ml_signal("SPY")
            if result and "error" not in result:
                _cache.set(cache_key, result)
                return {"market_regime": result, "source": "live"}
        except Exception as e:
            return {"market_regime": None, "error": str(e)}
        return {"market_regime": None, "error": "ML signal unavailable"}

    log.info("MCP server: %d tools registered", len(mcp_server._tool_manager._tools))


def _start_mcp_server():
    """Start MCP server in a background thread (SSE transport)."""
    if not _mcp_tools_enabled():
        log.info("MCP server: disabled (FastMCP not available)")
        return None

    def _run_mcp():
        try:
            log.info("MCP server starting on 0.0.0.0:%d (SSE)", _mcp_port)
            mcp_server.run(transport="sse")
        except Exception as e:
            log.error("MCP server crashed: %s", e)

    thread = threading.Thread(target=_run_mcp, daemon=True, name="mcp-server")
    thread.start()
    log.info("MCP server thread started")
    return thread


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduler Functions
# ═══════════════════════════════════════════════════════════════════════════════

def _scheduled_fetch_quotes():
    """Pre-fetch quotes for all tracked symbols."""
    symbols = list(_tracked_symbols)
    if not symbols:
        return
    log.debug("Scheduled quotes fetch: %d symbols", len(symbols))
    data = _fetch_alpaca_quotes(symbols)
    if data:
        mapped = {f"quote:{s}": d for s, d in data.items()}
        _cache.set_multi(mapped)
        # Enqueue DB persistence
        if _write_queue:
            now_iso = datetime.now().isoformat()
            for sym, q in data.items():
                price_row = {
                    "ticker": sym,
                    "close": q.get("close") or q.get("price"),
                    "high": q.get("high"),
                    "low": q.get("low"),
                    "open": q.get("open"),
                    "volume": q.get("volume"),
                    "rsi": q.get("rsi"),
                    "macd_line": q.get("macd_line"),
                    "macd_signal": q.get("macd_signal"),
                    "macd_histogram": q.get("macd_histogram"),
                    "ma20": q.get("ma20"),
                    "fetched_at": now_iso,
                }
                _write_queue.enqueue("prices", price_row)


def _scheduled_fetch_crypto():
    """Pre-fetch crypto quotes for tracked crypto symbols."""
    symbols = list(_tracked_crypto)
    if not symbols:
        return
    log.debug("Scheduled crypto fetch: %d symbols", len(symbols))
    data = _fetch_alpaca_crypto(symbols)
    if data:
        mapped = {f"crypto:{s}": d for s, d in data.items()}
        _cache.set_multi(mapped)
        # Enqueue DB persistence
        if _write_queue:
            now_iso = datetime.now().isoformat()
            for sym, c in data.items():
                price_row = {
                    "ticker": sym,
                    "close": c.get("price"),
                    "fetched_at": now_iso,
                }
                _write_queue.enqueue("prices", price_row)


def _scheduled_fetch_news():
    """Pre-fetch news for tracked symbols."""
    symbols = list(_tracked_symbols)
    if not symbols:
        return
    data = _fetch_alpaca_news(symbol=None, limit=20)
    if data:
        _cache.set("news:all:20", data)
        # Enqueue DB persistence
        if _write_queue:
            now_iso = datetime.now().isoformat()
            for article in data:
                tickers = article.get("symbols", [])
                news_row = {
                    "headline": article.get("headline", "")[:500],
                    "source": article.get("source", ""),
                    "relevance": ",".join(tickers[:5]) if tickers else "",
                    "url": article.get("url", ""),
                    "fetched_at": now_iso,
                }
                _write_queue.enqueue("news", news_row)


def _scheduled_fetch_congress():
    """Pre-fetch congress trades."""
    tickers = list(_tracked_symbols)
    data = _fetch_congress_trades(tickers)
    if data:
        _cache.set("congress:trades", data)
        log.info("Congress trades cached: %d entries", len(data))


def _scheduled_gc_signals():
    """Purge stale signals."""
    global _signals_cache
    with _signals_lock:
        cutoff = datetime.now() - timedelta(seconds=SIGNAL_MAX_AGE)
        before = len(_signals_cache)
        _signals_cache = [
            s for s in _signals_cache
            if datetime.fromisoformat(s["timestamp"]) > cutoff
        ]
        removed = before - len(_signals_cache)
    if removed:
        log.debug("Signals GC: removed %d stale signals", removed)


def _scheduled_flush_cache():
    """Flush expired cache entries.

    At market open (9:30 AM ET), aggressively flush ALL quote entries
    so stale pre-market data is purged immediately — traders see fresh
    first-trade data within seconds instead of waiting for the cache TTL.
    """
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    # Aggressive flush: 9:20-9:40 AM ET market-open window
    is_near_open = (now_et.hour == 9 and 20 <= now_et.minute < 40)

    if is_near_open:
        # Aggressive flush: purge all quote entries regardless of age
        flushed = 0
        for key in list(_cache._store.keys()):
            if key.startswith("quote:") or key.startswith("crypto:"):
                _cache.delete(key)
                flushed += 1
        if flushed:
            log.info("Cache flush: aggressively purged %d quote/crypto entries (near market open)", flushed)
        # Also run normal expired flush with tighter TTL
        for source, ttl in TTL.items():
            if source in ("quotes", "crypto"):
                _cache.flush_expired(30)  # 30s aggressive
            else:
                _cache.flush_expired(ttl * 2)
    else:
        # Normal flush: 2x TTL before hard flush
        for source, ttl in TTL.items():
            _cache.flush_expired(ttl * 2)  # 2x TTL before hard flush


def _scheduled_momentum():
    """Pre-compute momentum ranking and cache for /momentum endpoint."""
    if not _HAS_MOMENTUM:
        log.debug("Momentum module not available — skipping")
        return
    try:
        from src.skill_cross_sectional_momentum import get_cached_momentum_signal
        signal = get_cached_momentum_signal(top_n=20)
        if signal:
            _cache.set("momentum_signal", signal)
            log.info("Momentum scan: %d ranked, regime=%s",
                     signal.get("num_ranked", 0), signal.get("market_regime", "?"))
    except Exception as e:
        log.error("Momentum scan failed: %s", e)


def _scheduled_fetch_social():
    """Pre-fetch social sentiment for all tracked symbols (all sources).

    Runs Bluesky, StockTwits, and Reddit fetches in parallel via
    ThreadPoolExecutor to avoid wall-clock blowout from sequential calls.
    Each individual ticker call inside Bluesky/Stocktwits also has a
    per-ticker deadline guard via ThreadPoolExecutor + timeout.
    """
    if not _tracked_symbols:
        return

    SCHEDULED_FETCH_TIMEOUT = 90  # total wall-clock timeout for the batch

    def _fetch_with_label(fn, label) -> tuple:
        try:
            return (label, fn())
        except Exception as e:
            log.error("Scheduled social %s fetch failed: %s", label, e)
            return (label, {"source": label, "posts": [], "sentiment_score": 0.0,
                            "matched_tickers": [], "error": str(e)})

    fetch_funcs = [
        (lambda: _fetch_social_bluesky(), "bluesky"),
        (lambda: _fetch_social_stocktwits(), "stocktwits"),
        (lambda: _fetch_social_reddit(), "reddit"),
    ]

    results_map = {}
    with ThreadPoolExecutor(max_workers=3) as exec:
        future_map = {
            exec.submit(_fetch_with_label, fn, label): label
            for fn, label in fetch_funcs
        }
        try:
            for future in as_completed(future_map, timeout=SCHEDULED_FETCH_TIMEOUT):
                label, data = future.result()
                results_map[label] = data
        except FutureTimeoutError:
            log.warning("Scheduled social fetch timed out after %ss (partial results)",
                        SCHEDULED_FETCH_TIMEOUT)
        for future in future_map:
            future.cancel()

    # Fill in any missing sources with empty results
    for _, label in fetch_funcs:
        if label not in results_map:
            results_map[label] = {
                "source": label, "posts": [], "sentiment_score": 0.0,
                "matched_tickers": [], "error": "timeout",
            }

    bsky = results_map.get("bluesky", {})
    st = results_map.get("stocktwits", {})
    rdt = results_map.get("reddit", {})

    result = {
        "source": "all",
        "results": [bsky, st, rdt],
    }
    _cache.set("social:all", result)
    _cache.set("social:bluesky", bsky)
    _cache.set("social:stocktwits", st)
    _cache.set("social:reddit", rdt)
    total_posts = sum(len(r.get("posts", [])) for r in result["results"])
    total_tickers = set()
    for r in result["results"]:
        total_tickers.update(r.get("matched_tickers", []))
    log.info("Social: %d posts across %d tickers (bluesky=%d, stocktwits=%d, reddit=%d)",
             total_posts, len(total_tickers),
             len(bsky.get("posts", [])),
             len(st.get("posts", [])),
             len(rdt.get("posts", [])))


def _scheduled_fetch_macro():
    """Pre-fetch macro indicators — try LoneStarOracle first, augment with FRED."""
    ls_data = _fetch_lonestar_macro()
    if ls_data and "error" not in ls_data:
        # Augment with FRED data
        fred_data = _fetch_fred_macro()
        if fred_data and "error" not in fred_data:
            ls_data["fred"] = fred_data.get("indicators", {})
        _cache.set("macro:latest", ls_data)
        log.info("Macro cached via LoneStarOracle + FRED")
        return
    # Fall back to FRED only
    data = _fetch_fred_macro()
    if data and "error" not in data:
        _cache.set("macro:latest", data)
        log.info("FRED macro cached: %d indicators (yields=%s)",
                 len(data.get("indicators", {})),
                 "yes" if "yields" in data else "no")


def _scheduled_fetch_earnings():
    """Pre-fetch earnings calendar — try LoneStarOracle first, fall back to manual."""
    ls_data = _fetch_lonestar_earnings()
    if ls_data and "error" not in ls_data:
        _cache.set("earnings:calendar", ls_data)
        log.info("Earnings calendar cached via LoneStarOracle")
        return
    # Fall back to manual calculation
    data = _fetch_earnings_calendar()
    if data and "error" not in data:
        _cache.set("earnings:calendar", data)
        log.info("Earnings calendar cached: %d companies", data.get("total_companies", 0))


def _scheduled_fetch_fear_greed():
    """Pre-fetch Fear & Greed Index."""
    data = _fetch_fear_greed()
    if data and "error" not in data:
        _cache.set("fear_greed:latest", data)
        log.debug("Fear & Greed Index cached: value=%s (%s)",
                 data.get("value"), data.get("classification"))


def _scheduled_fetch_options_flow():
    """Pre-fetch options flow — try LoneStarOracle first, fall back to RSS."""
    # Try LoneStarOracle first
    ls_data = _fetch_lonestar_options_flow()
    if ls_data and "error" not in ls_data:
        _cache.set("flow:latest", ls_data)
        log.info("Options flow cached via LoneStarOracle")
        return
    # Fall back to RSS
    data = _fetch_options_flow()
    if data and "error" not in data:
        _cache.set("flow:latest", data)
        all_tickers = set()
        for f in data.get("flows", []):
            all_tickers.update(f.get("tickers", []))
        log.info("Options flow cached (RSS): %d flows, %d tickers",
                 data.get("total", 0), len(all_tickers))


def _scheduled_fetch_insiders():
    """Pre-fetch insider filings — try LoneStarOracle first, fall back to SEC EDGAR."""
    ls_data = _fetch_lonestar_insider_trades()
    if ls_data and "error" not in ls_data:
        _cache.set("insiders:latest", ls_data)
        log.info("Insider filings cached via LoneStarOracle")
        return
    # Fall back to SEC EDGAR
    data = _fetch_insider_filings()
    if data and "error" not in data:
        _cache.set("insiders:latest", data)
        log.info("SEC insider filings cached (EDGAR): %d filings",
                 data.get("total", 0))


def _scheduled_fetch_sentiment():
    """Analyze FinBERT sentiment for tracked symbols from cached news headlines."""
    news = _cache.get("news:all:20", TTL["news"])
    if not news:
        log.info("sentiment: no news to analyze")
        return

    # Collect headlines per symbol, analyze at most 10
    seen_headlines = set()
    symbol_headlines: Dict[str, list] = {}
    for item in news[:10]:
        headline = (item.get("headline") or "").strip()
        if not headline or headline in seen_headlines:
            continue
        seen_headlines.add(headline)
        for sym in item.get("symbols", []):
            symbol_headlines.setdefault(sym, []).append(headline)

    if not symbol_headlines:
        log.info("sentiment: no ticker-tagged headlines to analyze")
        return

    for symbol, headlines in symbol_headlines.items():
        results = []
        for text in headlines:
            result = _fetch_sentiment_via_finbert(text, ticker=symbol)
            if result:
                results.append(result)

        if not results:
            continue

        # Aggregate: average FinBERT scores across headlines for this symbol
        avg_positive = sum(r.get("positive", 0) for r in results) / len(results)
        avg_negative = sum(r.get("negative", 0) for r in results) / len(results)
        avg_neutral  = sum(r.get("neutral", 0) for r in results) / len(results)
        avg_compound = sum(r.get("compound", 0) for r in results) / len(results)

        aggregated = {
            "positive": round(avg_positive, 4),
            "negative": round(avg_negative, 4),
            "neutral":  round(avg_neutral, 4),
            "compound": round(avg_compound, 4),
            "headlines_analyzed": len(results),
            "fetched_at": datetime.now().isoformat(),
        }
        _cache.set(f"sentiment:{symbol}", aggregated)
        log.info("sentiment: %s compound=%.3f (pos=%.3f neg=%.3f, %d headlines)",
                 symbol, avg_compound, avg_positive, avg_negative, len(results))


def _scheduled_fetch_praesentire_sentiment():
    """Pre-fetch Praesentire bilingual sentiment for tracked symbols.

    Uses get_sentiment_batch for up to 50 tickers in one call (free tier: 100 req/day).
    Falls back to individual get_sentiment calls if batch fails.
    """
    tracked = sorted(_tracked_symbols)
    if not tracked:
        return

    # Batch call for up to 50 tickers
    batch = tracked[:50]
    result = _praesentire_call("get_sentiment_batch", {"tickers": batch})
    text = _mcp_text(result)
    if text:
        import json as _json
        try:
            data = _json.loads(text)
        except _json.JSONDecodeError:
            log.warning("Praesentire batch parse failed")
            data = None

        if isinstance(data, dict):
            cached_count = 0
            # Handle per-ticker results format: {"results": [{...}, ...]}
            if "results" in data and isinstance(data["results"], list):
                for item in data["results"]:
                    ticker = item.get("ticker", "").upper()
                    if ticker:
                        item["source"] = "praesentire"
                        item["fetched_at"] = datetime.now().isoformat()
                        _cache.set(f"praesentire_sentiment:{ticker}", item)
                        cached_count += 1
            elif "ticker" in data:
                # Single-ticker response (batch may return single for N=1)
                ticker = data.get("ticker", "").upper()
                if ticker:
                    data["source"] = "praesentire"
                    data["fetched_at"] = datetime.now().isoformat()
                    _cache.set(f"praesentire_sentiment:{ticker}", data)
                    cached_count = 1
            else:
                # Unknown format — cache the whole response keyed by batch
                data["source"] = "praesentire_batch"
                data["fetched_at"] = datetime.now().isoformat()
                _cache.set("praesentire_sentiment:batch", data)
                cached_count = len(tracked)

            if cached_count:
                log.info("Praesentire sentiment cached: %d tickers", cached_count)
            return

    # Batch failed — fall back to individual calls for top tickers
    count = 0
    for symbol in tracked[:5]:
        data = _fetch_praesentire_sentiment(symbol)
        if data and "error" not in data:
            _cache.set(f"praesentire_sentiment:{symbol}", data)
            count += 1
    if count:
        log.info("Praesentire sentiment cached (individual): %d/%d symbols", count, min(5, len(tracked)))


def _scheduled_fetch_praesentire_divergence():
    """Pre-fetch cross-language divergence for priority cross-market tickers.

    Divergence is most actionable for ADRs, semis, and US-listed Chinese stocks
    where EN vs ZH sentiment gaps reveal cross-market information asymmetry.
    """
    tracked = sorted(_tracked_symbols)
    if not tracked:
        return

    # Priority tickers for divergence (cross-market: semis, ADRs, Chinese listings)
    cross_market_priority = {
        # Semiconductors with heavy Taiwan/China exposure
        "TSM", "NVDA", "AMD", "AVGO", "INTC", "ASML", "MU", "QCOM", "TXN",
        # Chinese ADRs
        "BABA", "JD", "PDD", "BIDU", "NIO", "XPEV", "LI", "BILI", "TME",
        # Others with significant Chinese revenue
        "AAPL", "TSLA",
    }

    priority = [s for s in tracked if s in cross_market_priority]
    rest = [s for s in tracked if s not in set(priority)][:5]
    targets = (priority + rest)[:15]  # Cap at 15 to conserve rate limit

    count = 0
    for symbol in targets:
        data = _fetch_praesentire_divergence(symbol)
        if data and "error" not in data:
            _cache.set(f"sentiment_divergence:{symbol}", data)
            count += 1
    if count:
        log.info("Praesentire divergence cached: %d/%d symbols", count, len(targets))


def _scheduled_fetch_risk():
    """Pre-fetch portfolio risk scoring for tracked symbols via LoneStarOracle."""
    tracked = list(_tracked_symbols)
    if not tracked:
        return
    # Batch: up to 20 symbols at a time
    batch = tracked[:20]
    ls_data = _fetch_lonestar_risk(batch)
    if ls_data and "error" not in ls_data:
        cache_key = "risk:" + ",".join(sorted(batch))
        _cache.set(cache_key, ls_data)
        log.info("Portfolio risk cached via LoneStarOracle: %d symbols", len(batch))


def _scheduled_fetch_technical_scan():
    """Pre-fetch multi-TF technical scan for tracked symbols via LoneStarOracle."""
    tracked = list(_tracked_symbols)[:5]  # Limit to top 5 to avoid rate issues
    count = 0
    for symbol in tracked:
        ls_data = _fetch_lonestar_technical_scan(symbol)
        if ls_data and "error" not in ls_data:
            _cache.set(f"techscan:{symbol}", ls_data)
            count += 1
    if count:
        log.info("Technical scan cached via LoneStarOracle: %d/%d symbols", count, len(tracked))


# ═══════════════════════════════════════════════════════════════════════════════
# Bootstrap & Main
# ═══════════════════════════════════════════════════════════════════════════════

_start_time: float = time.time()
_schedulers: List[Scheduler] = []


def _load_tracked_symbols():
    """Load tracked symbols from all traders' watchlists in cache.db."""
    global _tracked_symbols
    try:
        conn = _get_sqlite_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT ticker FROM watchlist")
        symbols = {row["ticker"] for row in cursor.fetchall()}
        conn.close()

        # Also check cache.db watchlist (traders may write there)
        if not symbols:
            cache_conn = _get_cache_db_connection(readonly=True)
            cache_cursor = cache_conn.cursor()
            cache_cursor.execute("SELECT DISTINCT ticker FROM watchlist")
            cache_symbols = {row["ticker"] for row in cache_cursor.fetchall()}
            cache_conn.close()
            if cache_symbols:
                symbols = cache_symbols

        _tracked_symbols = symbols
        log.info("Loaded %d tracked symbols from watchlists", len(symbols))
    except Exception as e:
        log.warning("Could not load watchlist symbols: %s", e)

    # Fall back to defaults if no symbols found anywhere
    if not _tracked_symbols:
        _tracked_symbols = {"AAPL", "MSFT", "NVDA", "TSLA", "SPY", "QQQ", "AMZN", "GOOGL", "META"}
        log.info("No watchlist symbols found, using defaults: %s", sorted(_tracked_symbols))


def _create_schedulers() -> List[Scheduler]:
    """Create all background schedulers — all run 24/7 with dual rates."""
    return [
        Scheduler("quotes", INTERVALS["quotes"], _scheduled_fetch_quotes),
        Scheduler("crypto", INTERVALS["crypto"], _scheduled_fetch_crypto),
        Scheduler("news", INTERVALS["news"], _scheduled_fetch_news),
        Scheduler("congress", INTERVALS["congress"], _scheduled_fetch_congress),
        Scheduler("signals_gc", INTERVALS["signals_gc"], _scheduled_gc_signals),
        Scheduler("cache_flush", INTERVALS["cache_flush"], _scheduled_flush_cache),
        Scheduler("social", INTERVALS["social"], _scheduled_fetch_social),
        Scheduler("momentum", INTERVALS["momentum"], _scheduled_momentum),
        Scheduler("macro", INTERVALS["macro"], _scheduled_fetch_macro),
        Scheduler("earnings", INTERVALS["earnings"], _scheduled_fetch_earnings),
        Scheduler("fear_greed", INTERVALS["fear_greed"], _scheduled_fetch_fear_greed),
        Scheduler("flow", INTERVALS["flow"], _scheduled_fetch_options_flow),
        Scheduler("insiders", INTERVALS["insiders"], _scheduled_fetch_insiders),
        Scheduler("sentiment", INTERVALS["sentiment"], _scheduled_fetch_sentiment),
        Scheduler("praesentire_sentiment", INTERVALS["praesentire_sentiment"], _scheduled_fetch_praesentire_sentiment),
        Scheduler("praesentire_divergence", INTERVALS["praesentire_divergence"], _scheduled_fetch_praesentire_divergence),
        Scheduler("risk", INTERVALS["risk"], _scheduled_fetch_risk),
        Scheduler("technical_scan", INTERVALS["technical_scan"], _scheduled_fetch_technical_scan),
        # Fundamentals and options are fetched on-demand (expensive, low frequency)
    ]



# ═══════════════════════════════════════════════════════════════════════════════
# Trading Calendar Endpoint
# ═══════════════════════════════════════════════════════════════════════════════

_2026_EVENTS = {
    'fomc': [
        '2026-01-27', '2026-01-28', '2026-03-17', '2026-03-18',
        '2026-04-28', '2026-04-29', '2026-06-16', '2026-06-17',
        '2026-07-28', '2026-09-15', '2026-09-16',
        '2026-11-04', '2026-11-05', '2026-12-15', '2026-12-16',
    ],
    'cpi': [
        '2026-01-14', '2026-02-12', '2026-03-12', '2026-04-10',
        '2026-05-13', '2026-06-10', '2026-07-15', '2026-08-12',
        '2026-09-11', '2026-10-14', '2026-11-13', '2026-12-10',
    ],
    'nfp': [
        '2026-01-09', '2026-02-06', '2026-03-07', '2026-04-04',
        '2026-05-08', '2026-06-05', '2026-07-03', '2026-08-07',
        '2026-09-04', '2026-10-02', '2026-11-06', '2026-12-05',
    ],
    'holidays': [
        '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-18',
        '2026-05-25', '2026-06-19', '2026-07-03', '2026-09-07',
        '2026-11-26', '2026-12-25',
    ],
}


@app.route('/calendar')
def calendar_endpoint():
    """Get trading calendar: market status, today's events, next key dates."""
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    today = date.today().isoformat()
    now = datetime.now(ZoneInfo('America/New_York'))
    open_ = _is_market_open()
    today_events = [cat for cat in _2026_EVENTS for d in _2026_EVENTS[cat] if d == today]
    def nxt(lst):
        return next((d for d in lst if d >= today), None)
    return jsonify({
        'today': today,
        'time_et': now.strftime('%H:%M'),
        'market_open': open_,
        'today_events': today_events,
        'next_fomc': nxt(_2026_EVENTS['fomc']),
        'next_holiday': nxt(_2026_EVENTS['holidays']),
        'next_cpi': nxt(_2026_EVENTS['cpi']),
        'next_nfp': nxt(_2026_EVENTS['nfp']),
    })




@app.route("/overnight-sentiment")
def overnight_sentiment_endpoint():
    """
    Get overnight sentiment delta for tracked tickers.

    Compares pre-market sentiment (since previous close) against the
    7-day trailing baseline. Returns delta in [-1, +1] where > 0.5 = strong.

    Query params:
      ticker: one or more tickers (comma-separated)
      force:  if "true", bypass cache and re-fetch

    Uses the compute_overnight_delta() from src/overnight_sentiment.py.
    """
    tickers_param = request.args.get("ticker", "").strip()
    force_refresh = request.args.get("force", "").strip().lower() == "true"

    if not tickers_param:
        return jsonify({"error": "ticker parameter required"}), 400

    from src.overnight_sentiment import compute_overnight_delta

    tickers = [t.strip().upper() for t in tickers_param.split(",") if t.strip()]

    results = {}
    for ticker in tickers:
        try:
            result = compute_overnight_delta(ticker, force_refresh=force_refresh)
            results[ticker] = result
        except Exception as e:
            results[ticker] = {"error": str(e), "ticker": ticker}

    return jsonify({
        "status": "ok",
        "overnight_sentiment": results,
        "tickers": tickers,
    })


# Shared state for async retrain
_retrain_state = {'status': 'idle', 'step': None, 'error': None, 'result': None}
_retrain_lock = threading.Lock()


@app.route('/retrain-hmm', methods=['POST'])
def retrain_hmm_endpoint():
    """Trigger HMM retrain in background. Returns immediately with job ID."""
    global _retrain_state
    with _retrain_lock:
        if _retrain_state['status'] == 'running':
            return jsonify({'status': 'busy', 'message': 'Retrain already in progress'}), 429
        
        # Reset state
        # Reset state
        _retrain_state = {'status': 'starting', 'step': None, 'error': None, 'result': None}
        
        # Launch async retrain
        t = threading.Thread(target=_run_hmm_retrain, daemon=True)
        t.start()
        return jsonify({'status': 'started', 'message': 'HMM retrain started (est. 3-5 min)'})


@app.route('/retrain-status')
def retrain_status_endpoint():
    """Check HMM retrain progress."""
    return jsonify(_retrain_state)


def _run_hmm_retrain():
    """Run retrain_hmm.py in background and upload model to Mac GPU."""
    global _retrain_state
    import subprocess
    from pathlib import Path
    BASE = Path(__file__).parent.parent
    
    try:
        _retrain_state = {'status': 'running', 'step': 'training', 'error': None, 'result': None}
        log.info('[retrain-hmm] Step 1: Training HMM on 2y SPY data...')
        
        proc = subprocess.run(
            [sys.executable, str(BASE / 'src' / 'retrain_hmm.py'), '--verbose'],
            cwd=str(BASE), capture_output=True, text=True, timeout=600
        )
        if proc.returncode != 0:
            err = proc.stderr[-500:] if proc.stderr else proc.stdout[-500:]
            _retrain_state = {'status': 'failed', 'step': 'training', 'error': err}
            log.error(f'[retrain-hmm] Training failed: {err}')
            return
        
        model_path = BASE / 'state' / 'momentum_regime_model.pkl'
        if not model_path.exists():
            _retrain_state = {'status': 'failed', 'step': 'training', 'error': 'Model file not found'}
            return
        
        _retrain_state['step'] = 'uploading'
        log.info('[retrain-hmm] Step 2: Uploading to Mac GPU worker...')
        ml_host = os.getenv('ML_ENDPOINT_URL', 'http://192.168.1.237:5005')
        
        # Upload via subprocess curl
        upload_cmd = [
            'curl', '-s', '-f', '-X', 'POST',
            '-F', f'model=@{model_path}',
            f'{ml_host}/upload/model'
        ]
        upload = subprocess.run(upload_cmd, capture_output=True, text=True, timeout=60)
        if upload.returncode != 0:
            _retrain_state = {'status': 'failed', 'step': 'upload', 'error': upload.stderr[:300] or upload.stdout[:300]}
            return
        
        _retrain_state = {'status': 'completed', 'step': 'done', 'error': None, 'result': 'HMM model retrained and loaded'}
        log.info('[retrain-hmm] HMM retrain complete!')
        
    except subprocess.TimeoutExpired:
        _retrain_state = {'status': 'failed', 'step': 'training', 'error': 'Training timed out (10min)'}
    except Exception as e:
        _retrain_state = {'status': 'failed', 'step': 'error', 'error': str(e)}
        log.error(f'[retrain-hmm] Error: {e}')


# ═══════════════════════════════════════════════════════════════════════════════
# API Discovery — list all available endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/discover")
def discover():
    """
    GET /discover — list all available API endpoints with descriptions and params.

    Returns a JSON object where each key is an endpoint path and each value
    contains the HTTP method, description, and required/optional parameters.
    External consumers should use this as their entry point to the Data Bus.
    """
    return jsonify({
        "service": "Paper Trading Data Bus",
        "version": "2.0",
        "endpoints": {
            "/discover": {
                "method": "GET",
                "description": "This endpoint — list all available endpoints",
                "params": {},
            },
            "/health": {
                "method": "GET",
                "description": "Service health check",
                "params": {},
            },
            "/metrics": {
                "method": "GET",
                "description": "Prometheus scrape target",
                "params": {},
            },
            "/quotes": {
                "method": "GET",
                "description": "Latest quote data for stock symbols",
                "params": {
                    "symbols": {"type": "string", "required": True, "description": "Comma-separated tickers (e.g. AAPL,TSLA,NVDA)"},
                },
            },
            "/crypto": {
                "method": "GET",
                "description": "Latest crypto quotes",
                "params": {
                    "symbols": {"type": "string", "required": True, "description": "Comma-separated pairs (e.g. BTC/USD,ETH/USD)"},
                },
            },
            "/fundamentals": {
                "method": "GET",
                "description": "Fundamental data (P/E, EPS, market cap, etc.)",
                "params": {
                    "symbol": {"type": "string", "required": True, "description": "Single ticker symbol"},
                },
            },
            "/sentiment": {
                "method": "GET, POST",
                "description": "News/newsletter sentiment analysis via FinBERT or keyword fallback",
                "params": {"symbol": ["string", "required for GET"], "text": ["string", "required for POST"]},
            },
            "/sentiment-divergence": {
                "method": "GET",
                "description": "Cross-language sentiment divergence (English vs Traditional Chinese)",
                "params": {"symbol": {"type": "string", "required": True}},
            },
            "/options": {
                "method": "GET",
                "description": "Options chain / stock snapshot",
                "params": {"symbol": {"type": "string", "required": True}},
            },
            "/news": {
                "method": "GET",
                "description": "News headlines from Alpaca API",
                "params": {
                    "symbol": {"type": "string", "required": False, "description": "Filter by ticker"},
                    "limit": {"type": "int", "required": False, "default": 10},
                },
            },
            "/news-cache": {
                "method": "GET",
                "description": "RSS news feed from news_cache table",
                "params": {
                    "limit": {"type": "int", "required": False, "default": 30},
                    "source": {"type": "string", "required": False},
                    "days": {"type": "int", "required": False, "default": 1},
                },
            },
            "/news/search": {
                "method": "GET",
                "description": "Full-text search on news articles",
                "params": {
                    "q": {"type": "string", "required": True, "description": "Search query"},
                    "limit": {"type": "int", "required": False, "default": 20},
                },
            },
            "/social": {
                "method": "GET",
                "description": "Social media sentiment (Bluesky, Stocktwits, Reddit)",
                "params": {
                    "source": {"type": "string", "required": False, "default": "all", "description": "bluesky|stocktwits|reddit|all"},
                    "fast": {"type": "bool", "required": False, "description": "Skip live fetch, return cached"},
                },
            },
            "/signals": {
                "method": "GET, POST",
                "description": "Trader intercom — publish/read short-term reads",
                "params": {
                    "GET": "returns all active signals",
                    "POST": {"body": {"agent": "str", "ticker": "str", "bias": "bullish|bearish|neutral", "conviction": "float", "note": "str"}},
                },
            },
            "/momentum": {
                "method": "GET",
                "description": "Cross-sectional momentum rankings for Kairos",
                "params": {},
            },
            "/percentile": {
                "method": "GET",
                "description": "Percentile rankings within the universe by metric",
                "params": {
                    "symbols": {"type": "string", "required": True},
                    "metric": {"type": "string", "required": False, "default": "momentum_1m"},
                },
            },
            "/macro": {
                "method": "GET",
                "description": "Macro indicators (CPI, PCE, unemployment, yields, GDP)",
                "params": {},
            },
            "/earnings": {
                "method": "GET",
                "description": "Earnings calendar",
                "params": {"symbols": {"type": "string", "required": False, "description": "Comma-separated tickers"}},
            },
            "/fear_greed": {
                "method": "GET",
                "description": "Fear & Greed Index from alternative.me",
                "params": {},
            },
            "/flow": {
                "method": "GET",
                "description": "Unusual options flow from Unusual Whales RSS",
                "params": {"symbol": {"type": "string", "required": False}},
            },
            "/insiders": {
                "method": "GET",
                "description": "SEC Form 4 insider filings",
                "params": {"symbols": {"type": "string", "required": False}},
            },
            "/congress": {
                "method": "GET",
                "description": "Congress trading data",
                "params": {"symbols": {"type": "string", "required": False}},
            },
            "/tick-snapshot": {
                "method": "GET",
                "description": "One-stop data for trader ticks: quotes, regime, fear_greed, macro, signals, portfolio state",
                "params": {},
            },
            "/ml-signal": {
                "method": "GET",
                "description": "ML signal for a symbol",
                "params": {"symbol": {"type": "string", "required": True}},
            },
            "/source-quality": {
                "method": "GET",
                "description": "Prediction accuracy stats per news/social source",
                "params": {
                    "source": {"type": "string", "required": False},
                    "days": {"type": "int", "required": False, "default": 90},
                },
            },
            "/risk": {
                "method": "GET",
                "description": "Portfolio risk scoring",
                "params": {"symbols": {"type": "string", "required": False}},
            },
            "/technical-scan": {
                "method": "GET",
                "description": "Multi-timeframe technical scan",
                "params": {"symbol": {"type": "string", "required": True}},
            },
            "/equity-analysis": {
                "method": "GET",
                "description": "Equity analysis",
                "params": {"symbol": {"type": "string", "required": True}},
            },
            "/briefing": {
                "method": "GET",
                "description": "Daily market briefing",
                "params": {},
            },
            "/overnight-sentiment": {
                "method": "GET",
                "description": "Overnight sentiment delta for tracked tickers",
                "params": {"ticker": {"type": "string", "required": True}},
            },
            "/virtual-traders": {
                "method": "GET",
                "description": "List all registered virtual traders with their current stats",
                "params": {},
            },
            "/virtual-traders/register": {
                "method": "POST",
                "description": "Register a new virtual trader for an external agent",
                "params": {
                    "name": {"type": "string", "required": True, "description": "Unique trader name"},
                    "api_key": {"type": "string", "required": False, "description": "Optional external API key"},
                    "base_strategy": {"type": "string", "required": True, "description": "momentum|value|aggro"},
                    "initial_params": {"type": "object", "required": False, "description": "Optional JSON config overrides"},
                },
            },
            "/virtual-traders/leaderboard": {
                "method": "GET",
                "description": "Leaderboard of virtual traders ranked by P&L",
                "params": {},
            },
            "/trader/<agent>/config": {
                "method": "GET, PATCH",
                "description": "Get or update per-trader configuration (exploration mode, position sizing, etc.)",
                "params": {
                    "GET": "Returns current config for the agent",
                    "PATCH": {"body": {"exploration_mode": "bool", "max_position_pct": "float", "conviction_threshold": "float", "watchlist_size": "int"}},
                },
            },
        },
        "links": {
            "dashboard": "/dashboard",
            "debug": "/debug",
        },
        "fetched_at": datetime.now().isoformat(),
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Virtual Trader Registration & Management
# ═══════════════════════════════════════════════════════════════════════════════

_VT_DB_DSN = os.getenv("VT_DB_DSN", "host=docker.klo port=5433 dbname=trading user=trader")


def _get_vt_db():
    """Get psycopg2 connection to trading DB for virtual trader ops."""
    import psycopg2 as _psycopg2
    import psycopg2.extras as _psycopg2_extras
    conn = _psycopg2.connect(_VT_DB_DSN)
    conn.autocommit = True
    return conn


def _compute_virtual_pnl(trader_ids: list) -> dict:
    """Compute 7-day P&L for virtual traders (realized + unrealized).

    Mirrors the logic in virtual_cull.py compute_7day_pnl() but
    returns data from the data bus for leaderboard display.
    """
    from datetime import date as _d, timedelta as _td
    import psycopg2.extras as _psycopg2_extras

    if not trader_ids:
        return {}

    since = _d.today() - _td(days=7)
    today = _d.today()

    conn = _get_vt_db()
    cur = conn.cursor(cursor_factory=_psycopg2_extras.RealDictCursor)

    # Realized P&L
    placeholders = ",".join(["%s"] * len(trader_ids))
    cur.execute(
        f"""SELECT trader_id, COALESCE(SUM(pnl), 0) as realized_pnl
            FROM trading.trades
            WHERE trader_id IN ({placeholders})
              AND exit_time IS NOT NULL
              AND exit_time::date >= %s
            GROUP BY trader_id""",
        (*trader_ids, since),
    )
    realized_map = {row["trader_id"]: float(row["realized_pnl"]) for row in cur.fetchall()}

    # Unrealized P&L
    cur.execute(
        f"""SELECT trader_id, ticker, SUM(shares) as total_shares,
                   AVG(entry_price) as avg_entry
            FROM trading.trades
            WHERE trader_id IN ({placeholders})
              AND exit_time IS NULL
            GROUP BY trader_id, ticker
            HAVING SUM(shares) != 0""",
        (*trader_ids,),
    )
    open_positions = cur.fetchall()

    # Get latest close prices
    open_tickers = list({p["ticker"] for p in open_positions})
    close_prices = {}
    for ticker in set(open_tickers):
        cur.execute(
            """SELECT close FROM market_data.bars
               WHERE ticker = %s AND timestamp <= %s::timestamp + interval '1 day'
               ORDER BY timestamp DESC LIMIT 1""",
            (ticker, today),
        )
        row = cur.fetchone()
        if row:
            close_prices[ticker] = float(row["close"])

    from collections import defaultdict
    unrealized_map = defaultdict(float)
    for pos in open_positions:
        trader = pos["trader_id"]
        ticker = pos["ticker"]
        shares = int(pos["total_shares"])
        entry = float(pos["avg_entry"])
        close = close_prices.get(ticker, entry)
        unrealized_map[trader] += (close - entry) * shares

    conn.close()

    pnl_map = {}
    for tid in trader_ids:
        realized = realized_map.get(tid, 0.0)
        unrealized = unrealized_map.get(tid, 0.0)
        pnl_map[tid] = realized + unrealized

    return pnl_map


@app.route("/virtual-traders/register", methods=["POST"])
def virtual_trader_register():
    """
    POST /virtual-traders/register

    Register a new virtual trader for an external agent (like Hermes).

    Body:
      name:          (required) Unique trader name
      api_key:       (optional) External API key for identity
      base_strategy: (required) momentum|value|aggro
      initial_params:(optional) JSON dict of config overrides

    Returns:
      New virtual trader record with ID, name, status, created_at
    """
    body = request.get_json(silent=True) or {}

    name = (body.get("name") or "").strip()
    base_strategy = (body.get("base_strategy") or "").strip().lower()

    if not name:
        return jsonify({"error": "name field is required"}), 400
    if not base_strategy:
        return jsonify({"error": "base_strategy field is required"}), 400
    if base_strategy not in ("momentum", "value", "aggro"):
        return jsonify({"error": f"base_strategy must be one of: momentum, value, aggro"}), 400

    initial_params = body.get("initial_params", {})
    if not isinstance(initial_params, dict):
        return jsonify({"error": "initial_params must be a JSON object"}), 400

    # Normalize the name: use as-is for external, map base_strategy to variant_type
    variant_type = base_strategy
    if base_strategy == "momentum":
        variant_type = "params"
    elif base_strategy == "value":
        variant_type = "prompt"
    elif base_strategy == "aggro":
        variant_type = "wildcard"

    config = {
        "base_strategy": base_strategy,
        "origin": "external",
        "external_name": name,
    }
    if initial_params:
        config["params"] = initial_params

    try:
        conn = _get_vt_db()
        cur = conn.cursor()

        # Check for duplicate name
        cur.execute("SELECT id FROM trading.virtual_traders WHERE name = %s", (name,))
        if cur.fetchone():
            conn.close()
            return jsonify({"error": f"Trader '{name}' already exists"}), 409

        cur.execute(
            """INSERT INTO trading.virtual_traders
               (name, base_trader, variant_type, config, status, created_at, wins)
               VALUES (%s, %s, %s, %s::jsonb, 'probation', %s, 0)
               RETURNING id, name, status, created_at""",
            (name, "external", variant_type, json.dumps(config), date.today()),
        )
        row = cur.fetchone()
        conn.close()

        log.info("Registered external virtual trader: %s (strategy=%s)", name, base_strategy)

        return jsonify({
            "id": row[0],
            "name": row[1],
            "status": row[2],
            "base_strategy": base_strategy,
            "created_at": str(row[3]) if row[3] else str(date.today()),
        }), 201

    except Exception as e:
        log.error("Failed to register virtual trader '%s': %s", name, e)
        return jsonify({"error": f"Database error: {str(e)}"}), 500


@app.route("/virtual-traders", methods=["GET"])
def virtual_trader_list():
    """
    GET /virtual-traders

    List all registered virtual traders with their current stats.
    Returns status, base_trader, win count, and 7-day P&L for each.
    """
    try:
        conn = _get_vt_db()
        import psycopg2.extras as _psycopg2_extras
        cur = conn.cursor(cursor_factory=_psycopg2_extras.RealDictCursor)

        cur.execute(
            """SELECT id, name, base_trader, variant_type, status, wins,
                      created_at, culled_at
               FROM trading.virtual_traders
               ORDER BY status, name"""
        )
        rows = cur.fetchall()
        conn.close()

        traders = [dict(r) for r in rows]

        # Compute 7-day P&L for active traders
        active_names = [t["name"] for t in traders if t["status"] in ("active", "live", "probation")]
        pnl_map = _compute_virtual_pnl(active_names) if active_names else {}

        for t in traders:
            t["pnl_7d"] = round(pnl_map.get(t["name"], 0.0), 2)
            # Convert date fields to strings
            for date_field in ["created_at", "culled_at"]:
                if t.get(date_field):
                    t[date_field] = str(t[date_field])

        return jsonify({
            "count": len(traders),
            "traders": traders,
        })

    except Exception as e:
        log.error("Failed to list virtual traders: %s", e)
        return jsonify({"error": str(e), "traders": []}), 500


@app.route("/virtual-traders/leaderboard", methods=["GET"])
def virtual_trader_leaderboard():
    """
    GET /virtual-traders/leaderboard

    Leaderboard of virtual traders ranked by 7-day P&L.
    Only shows traders with status 'active', 'live', or 'probation'.
    """
    try:
        conn = _get_vt_db()
        import psycopg2.extras as _psycopg2_extras
        cur = conn.cursor(cursor_factory=_psycopg2_extras.RealDictCursor)

        cur.execute(
            """SELECT id, name, base_trader, variant_type, status, wins,
                      created_at
               FROM trading.virtual_traders
               WHERE status IN ('active', 'live', 'probation')
               ORDER BY name"""
        )
        rows = cur.fetchall()
        conn.close()

        active = [dict(r) for r in rows]
        names = [t["name"] for t in active]
        pnl_map = _compute_virtual_pnl(names)

        for t in active:
            t["pnl_7d"] = round(pnl_map.get(t["name"], 0.0), 2)
            if t.get("created_at"):
                t["created_at"] = str(t["created_at"])

        ranked = sorted(active, key=lambda x: x["pnl_7d"], reverse=True)

        leaderboard = []
        for rank, entry in enumerate(ranked, 1):
            leaderboard.append({
                "rank": rank,
                "name": entry["name"],
                "base_trader": entry["base_trader"],
                "strategy": entry["variant_type"],
                "status": entry["status"],
                "wins": entry["wins"],
                "pnl_7d": entry["pnl_7d"],
                "created_at": entry["created_at"],
            })

        return jsonify({
            "leaderboard": leaderboard,
            "count": len(leaderboard),
        })

    except Exception as e:
        log.error("Failed to compute virtual trader leaderboard: %s", e)
        return jsonify({"error": str(e), "leaderboard": []}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# Trader Config — exploration mode & runtime settings
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_trader_config_table():
    """Create the trader_config table if it doesn't exist."""
    import psycopg2 as _psycopg2
    try:
        conn = _get_vt_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trading.trader_config (
                agent_id TEXT PRIMARY KEY,
                exploration_mode BOOLEAN DEFAULT false,
                exploration_started_at TIMESTAMPTZ,
                max_position_pct FLOAT DEFAULT 25.0,
                conviction_threshold FLOAT DEFAULT 0.6,
                watchlist_size INT DEFAULT 20,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.close()
    except Exception as e:
        log.warning("Could not ensure trader_config table: %s", e)


@app.route("/trader/<agent>/config", methods=["GET"])
def trader_get_config(agent):
    """
    GET /trader/<agent>/config

    Get the current configuration for a trader agent.
    Returns all config fields including exploration mode.
    If no config row exists, returns defaults.
    """
    _ensure_trader_config_table()
    try:
        import psycopg2.extras as _psycopg2_extras
        conn = _get_vt_db()
        cur = conn.cursor(cursor_factory=_psycopg2_extras.RealDictCursor)

        cur.execute(
            """SELECT agent_id, exploration_mode, exploration_started_at,
                      max_position_pct, conviction_threshold, watchlist_size,
                      created_at, updated_at
               FROM trading.trader_config
               WHERE agent_id = %s""",
            (agent,),
        )
        row = cur.fetchone()
        conn.close()

        if row:
            config = dict(row)
            # Convert dates to strings
            for df in ["exploration_started_at", "created_at", "updated_at"]:
                if config.get(df):
                    config[df] = config[df].isoformat()
            return jsonify({"agent": agent, "config": config})

        # No row yet — return defaults
        return jsonify({
            "agent": agent,
            "config": {
                "agent_id": agent,
                "exploration_mode": False,
                "exploration_started_at": None,
                "max_position_pct": 25.0,
                "conviction_threshold": 0.6,
                "watchlist_size": 20,
            },
            "note": "defaults (no config row exists yet)",
        })

    except Exception as e:
        log.error("Failed to get config for agent '%s': %s", agent, e)
        return jsonify({"error": str(e)}), 500


@app.route("/trader/<agent>/config", methods=["PATCH"])
def trader_patch_config(agent):
    """
    PATCH /trader/<agent>/config

    Update the configuration for a trader agent.
    Any subset of config fields can be provided; only those fields are updated.

    Body fields:
      exploration_mode:     (bool) Enable/disable small-trades exploration mode
      max_position_pct:    (float) Max position size as % of portfolio
      conviction_threshold: (float) Min conviction to enter a trade (0.0-1.0)
      watchlist_size:      (int) Max size of the watchlist

    Returns:
      Updated config object.
    """
    _ensure_trader_config_table()
    body = request.get_json(silent=True) or {}

    if not body:
        return jsonify({"error": "No fields provided to update"}), 400

    # Validate known fields with diffs to apply
    allowed_fields = {
        "exploration_mode": bool,
        "max_position_pct": float,
        "conviction_threshold": float,
        "watchlist_size": int,
    }

    updates = {}
    for field, field_type in allowed_fields.items():
        if field in body:
            val = body[field]
            if isinstance(val, bool) and field_type == bool:
                updates[field] = val
            elif isinstance(val, (int, float)) and field_type in (int, float):
                updates[field] = field_type(val)
            else:
                try:
                    val_cast = field_type(val)
                    updates[field] = val_cast
                except (ValueError, TypeError):
                    return jsonify({"error": f"Invalid type for {field}: expected {field_type.__name__}"}), 400

    if not updates:
        return jsonify({"error": "No valid config fields provided. Allowed: exploration_mode, max_position_pct, conviction_threshold, watchlist_size"}), 400

    try:
        conn = _get_vt_db()
        cur = conn.cursor()

        # Handle exploration_started_at
        if "exploration_mode" in updates and updates["exploration_mode"]:
            updates["exploration_started_at"] = datetime.now(timezone.utc)

        # Build SET clause
        set_parts = []
        values = []
        for field, val in updates.items():
            set_parts.append(f"{field} = %s")
            values.append(val)
        values.append(agent)

        # UPSERT: insert if not exists, update if exists
        # We use INSERT ... ON CONFLICT since we want upsert behavior
        cols = ["agent_id"] + list(updates.keys())
        placeholders = ["%s"] * len(cols)
        insert_vals = [agent]
        for field in updates:
            insert_vals.append(updates[field])

        update_set = ", ".join([f"{k} = EXCLUDED.{k}" for k in updates.keys()])

        cur.execute(
            f"""INSERT INTO trading.trader_config ({', '.join(cols)})
               VALUES ({', '.join(placeholders)})
               ON CONFLICT (agent_id) DO UPDATE SET
                 {update_set}
               RETURNING agent_id, exploration_mode, exploration_started_at,
                         max_position_pct, conviction_threshold, watchlist_size,
                         created_at, updated_at""",
            insert_vals,
        )
        row = cur.fetchone()
        conn.close()

        log.info("Updated config for agent '%s': %s", agent, json.dumps(updates))

        config = {
            "agent_id": row[0],
            "exploration_mode": row[1],
            "exploration_started_at": row[2].isoformat() if row[2] else None,
            "max_position_pct": float(row[3]) if row[3] else 25.0,
            "conviction_threshold": float(row[4]) if row[4] else 0.6,
            "watchlist_size": row[5] if row[5] else 20,
            "created_at": row[6].isoformat(),
            "updated_at": row[7].isoformat(),
        }

        return jsonify({"agent": agent, "config": config, "updated": list(updates.keys())})

    except Exception as e:
        log.error("Failed to update config for agent '%s': %s", agent, e)
        return jsonify({"error": str(e)}), 500


def main():
    parser = argparse.ArgumentParser(description="Data Bus — Market Data Service")
    parser.add_argument("--port", type=int, default=5000, help="Flask listen port (default: 5000)")
    parser.add_argument("--mcp-port", type=int, default=None, help="MCP SSE server port (default: 5001, overridable via MCP_PORT env)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Listen host (default: 0.0.0.0)")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    global _schedulers, _write_queue

    # Load tracked symbols
    _load_tracked_symbols()

    # Warm up caches after restart so first requests don't all miss
    def _warmup_caches():
        """Pre-cache tracked symbols' quotes and crypto data after restart.
        This prevents a flood of cache-miss requests when the gateway restarts
        and traders immediately start querying.
        """
        symbols = list(_tracked_symbols)[:50]  # first 50 for speed
        if symbols:
            log.info("Warmup: pre-caching quotes for %d symbols...", len(symbols))
            try:
                data = _fetch_alpaca_quotes(symbols)
                if data:
                    _cache.set("quotes:v2:" + ",".join(sorted(symbols)), data)
                    log.info("Warmup: cached %d quote results", len(data))
            except Exception as e:
                log.warning("Warmup quotes fetch failed (non-fatal): %s", e)

        crypto = ["BTC/USD", "ETH/USD"]
        log.info("Warmup: pre-caching crypto...")
        try:
            crypto_data = _fetch_alpaca_crypto(crypto)
            if crypto_data:
                _cache.set("crypto:v2:" + ",".join(sorted(crypto)), crypto_data)
                log.info("Warmup: cached %d crypto results", len(crypto_data))
        except Exception as e:
            log.warning("Warmup crypto fetch failed (non-fatal): %s", e)
        log.info("Warmup complete.")

    # Run warm-up in a daemon thread so it doesn't block HTTP startup
    warmup_thread = threading.Thread(target=_warmup_caches, daemon=True, name="data-bus-warmup")
    warmup_thread.start()

    # Set default crypto tracking
    global _tracked_crypto
    _tracked_crypto = {"BTC/USD", "ETH/USD"}

    # Start write-behind DB queue
    _write_queue = DbWriteQueue(default_interval=15.0, off_hours_interval=60.0)
    _write_queue.start()

    # Start schedulers
    _schedulers = _create_schedulers()
    for s in _schedulers:
        s.start()

    # Start news collector (RSS aggregation daemon)
    if _HAS_NEWS_COLLECTOR and start_news_collector:
        try:
            ensure_news_cache_table()
        except Exception as e:
            log.warning("Could not ensure news_cache table: %s", e)
        start_news_collector()
    else:
        log.warning("News collector not available — skipping")

    log.info("Data Bus starting on %s:%s", args.host, args.port)
    log.info("Tracked symbols: %s", sorted(_tracked_symbols)[:10])
    log.info("Endpoints:")
    log.info("  GET  /dashboard         (live HTML)")
    log.info("  GET  /debug             (⚠️ SENSITIVE: API keys, rate limits, errors — LAN-only)")
    log.info("  GET  /health")
    log.info("  GET  /metrics")
    log.info("  GET  /quotes?symbols=AAPL,TSLA")
    log.info("  GET  /crypto?symbols=BTC/USD,ETH/USD")
    log.info("  GET  /fundamentals?symbol=AAPL")
    log.info("  GET  /sentiment?symbol=AAPL")
    log.info("  POST /sentiment   (analyze text)")
    log.info("  GET  /options?symbol=AAPL")
    log.info("  GET  /news?symbol=AAPL")
    log.info("  GET  /social?source=bluesky|stocktwits|all")
    log.info("  GET  /signals")
    log.info("  POST /signals")
    log.info("  GET  /source-quality    (prediction accuracy per source)")
    log.info("  GET  /percentile        (percentile rankings by metric)")
    log.info("  GET  /ml-signal?symbol=AAPL")
    log.info("  GET  /momentum            (momentum rankings)")
    log.info("  GET  /congress")
    log.info("  GET  /macro")
    log.info("  GET  /earnings?symbols=AAPL,MSFT")
    log.info("  GET  /fear_greed")
    log.info("  GET  /flow?symbol=AAPL")
    log.info("  GET  /insiders?symbols=JPM,BAC")
    log.info("  GET  /mcp-status     (MCP server health)")
    log.info("⚠️  /debug — SENSITIVE: contains API key status, rate limits, error traces")
    log.info("             Should be restricted to LAN-only via Traefik middleware")
    log.info("  GET  /risk?symbols=AAPL,MSFT  (NEW)")
    log.info("  GET  /sentiment-divergence?symbol=TSM  (Praesentire cross-language)")
    log.info("  GET  /technical-scan?symbol=AAPL  (NEW)")
    log.info("  GET  /equity-analysis?symbol=AAPL  (NEW)")
    log.info("  GET  /briefing")
    log.info("  GET  /news-cache?limit=30&source=marketwatch&days=1  (NEW — RSS news feed)")
    log.info("  GET  /news/search?q=AAPL  (NEW — search news cache)")
    log.info("  GET  /discover             (NEW — API discovery listing)")
    log.info("  GET  /virtual-traders       (NEW — list virtual traders)")
    log.info("  POST /virtual-traders/register  (NEW — register external trader)")
    log.info("  GET  /virtual-traders/leaderboard  (NEW — P&L leaderboard)")
    log.info("  GET  /trader/:agent/config  (NEW — get trader config)")
    log.info("  PATCH /trader/:agent/config (NEW — update trader config)")
    log.info("")
    log.info("MCP Tools (port %d — SSE transport):", _mcp_port)
    log.info("  get_quotes(symbols)")
    log.info("  get_sentiment(symbol)")
    log.info("  get_flow(symbol)")
    log.info("  get_insiders(symbol)")
    log.info("  get_macro()")
    log.info("  get_technical_scan(symbol)")
    log.info("  get_risk(symbol)")
    log.info("  get_sentiment_divergence(symbol)")
    log.info("  get_market_regime()")

    # ── Start MCP server ────────────────────────────────────────────────
    _mcp_thread = _start_mcp_server()

    # ── Register MCP servers ────────────────────────────────────────────
    if _mcp_available:
        try:
            register_phase0_servers()
            log.info("MCP server configs registered")
        except Exception as e:
            log.warning("MCP registration failed: %s", e)

    try:
        app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down schedulers...")
        for s in _schedulers:
            s.stop()
        if _write_queue:
            log.info("Shutting down write-behind queue...")
            _write_queue.stop()
        # Shut down MCP connections
        if _mcp_available:
            try:
                manager = get_manager()
                if manager:
                    manager.shutdown()
            except Exception as e:
                log.debug("MCP shutdown error: %s", e)
        log.info("Data Bus stopped.")


if __name__ == "__main__":
    main()
